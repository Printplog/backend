import hmac
import logging
import random
import uuid
from decimal import Decimal
from datetime import timedelta
from django.db import transaction as db_transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, permissions
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework.response import Response
import os
from urllib.parse import urlparse
from api.models import SiteSettings, Tool, TrackingSupportMessage, TrackingSupportReply
from api.permissions import IsAdminOnly
from api.serializers.support import (
    PublicTrackingSupportThreadSerializer,
    TrackingSupportReplyCreateSerializer,
    TrackingSupportReplySerializer,
)
from api.utils.integration_secrets import get_integration_secret
from api.utils.support_email import (
    SupportEmailError,
    notify_owner_of_customer_reply,
    notify_owner_of_new_ticket,
)
from api.utils.support_realtime import (
    authorize_private_channel,
    create_customer_access_token,
    public_realtime_config,
    publish_owner_banking_event,
    publish_support_update,
    ticket_channel,
)
from wallet.models import Wallet
from .models import FernwayProfile, FernwayAccount, FernwayCard, FernwayTransaction
from .notifications import (
    CHALLENGE_TTL_SECONDS,
    BankingEmailError,
    BankingVerificationError,
    confirm_fernway_email_verification,
    confirm_owner_email_verification,
    mask_email,
    notify_account_updated,
    notify_card_added,
    notify_card_updated,
    notify_login,
    notify_transaction_posted,
    notify_transfer_requested,
    notify_transfer_reviewed,
    read_fernway_email_verification_grant,
    request_fernway_email_verification,
    request_owner_email_verification,
    send_fernway_credentials_email,
)

logger = logging.getLogger(__name__)
from .serializers import (
    FernwayProfileSerializer,
    FernwayAdminProfileSerializer,
    FernwayAccountSerializer,
    FernwayCardSerializer,
    FernwayTransactionSerializer,
)

SUBSCRIPTION_CYCLE_DAYS = 30


def _dollars_to_cents(val, default_cents=0):
    try:
        return max(0, int(round(Decimal(str(val).replace(',', '').strip()) * 100)))
    except Exception:
        return default_cents


def _get_monthly_price() -> Decimal:
    tool = Tool.objects.filter(slug='fernway').first()
    return tool.price if tool and tool.price else Decimal('15.00')


def _get_online_banking_url() -> str:
    configured = (SiteSettings.get_settings().online_banking_url or '').strip().rstrip('/')
    if configured:
        return configured
    return (os.getenv('FERNWAY_APP_URL') or '').strip().rstrip('/')


def _get_wallet_balance(user) -> float:
    wallet = Wallet.objects.filter(user=user).first()
    return float(wallet.spendable_balance) if wallet else 0.0


def _random_account_number() -> str:
    return ''.join(str(random.randint(0, 9)) for _ in range(10))


def _normalize_pin(raw, default='1234') -> str:
    if raw is None or str(raw).strip() == '':
        return default
    digits = ''.join(ch for ch in str(raw) if ch.isdigit())
    if len(digits) != 4:
        raise ValueError('Transaction PIN must be exactly 4 digits.')
    return digits


def _format_card_number(raw: str | None, fallback_last4: str = '8892') -> tuple[str, str]:
    digits = ''.join(ch for ch in str(raw or '') if ch.isdigit())
    if len(digits) < 12:
        last4 = (digits[-4:] if len(digits) >= 4 else str(fallback_last4 or '8892')[:4]).zfill(4)
        digits = f'453291827364{last4}'
    digits = digits[:16]
    formatted = ' '.join(digits[i:i + 4] for i in range(0, len(digits), 4))
    return formatted, digits[-4:]


def _seed_profile_assets(
    profile: FernwayProfile,
    *,
    account_balance_cents=25000000,
    card_holder_name=None,
    card_number=None,
    card_last4='8892',
    card_expiry='12/28',
    card_cvv='721',
    card_pin='1234',
    card_limit_cents=5000000,
    card_is_frozen=False,
):
    """Create one unified Account Balance, a physical Debit Card, and starter transactions."""
    primary_account = FernwayAccount.objects.create(
        profile=profile,
        account_type='checking',
        name='Account Balance',
        account_number=_random_account_number(),
        routing_number='021000021',
        balance=account_balance_cents,
        apy_bps=0,
    )

    formatted_number, last4 = _format_card_number(card_number, card_last4)
    expiry = str(card_expiry or '12/28').strip()[:7] or '12/28'
    cvv = ''.join(ch for ch in str(card_cvv or '721') if ch.isdigit())[:4] or '721'
    pin_digits = ''.join(ch for ch in str(card_pin or '1234') if ch.isdigit())[:4]
    pin = pin_digits if len(pin_digits) == 4 else '1234'
    holder = (card_holder_name or f"{profile.first_name} {profile.last_name}").strip()

    FernwayCard.objects.create(
        profile=profile,
        account=primary_account,
        label='Lindenway Physical Debit Card',
        kind='physical',
        brand='visa',
        card_number=formatted_number,
        last4=last4,
        expiry=expiry,
        cvv=cvv,
        pin=pin,
        holder_name=holder,
        monthly_limit=card_limit_cents,
        is_frozen=bool(card_is_frozen),
        online_payments=True,
    )

    now = timezone.now()
    starter_txs = [
        {
            'account': primary_account,
            'direction': 'credit',
            'amount': 15000000,
            'counterparty': 'Direct Deposit Payroll',
            'narration': 'Monthly salary deposit',
            'category': 'income',
            'channel': 'wire',
            'reference': f'TX-{uuid.uuid4().hex[:8].upper()}',
            'occurred_at': now - timedelta(days=1),
        },
        {
            'account': primary_account,
            'direction': 'debit',
            'amount': 245000,
            'counterparty': 'Apple Store Fifth Ave',
            'narration': 'Hardware purchase',
            'category': 'shopping',
            'channel': 'card',
            'reference': f'TX-{uuid.uuid4().hex[:8].upper()}',
            'occurred_at': now - timedelta(days=2),
        },
        {
            'account': primary_account,
            'direction': 'credit',
            'amount': 4500000,
            'counterparty': 'Client Wire Transfer',
            'narration': 'Consulting payout',
            'category': 'income',
            'channel': 'wire',
            'reference': f'TX-{uuid.uuid4().hex[:8].upper()}',
            'occurred_at': now - timedelta(days=4),
        },
        {
            'account': primary_account,
            'direction': 'debit',
            'amount': 32000,
            'counterparty': 'Le Bernardin NYC',
            'narration': 'Dinner reservation',
            'category': 'dining',
            'channel': 'card',
            'reference': f'TX-{uuid.uuid4().hex[:8].upper()}',
            'occurred_at': now - timedelta(days=6),
        },
    ]
    for tx in starter_txs:
        FernwayTransaction.objects.create(**tx)


def _apply_profile_updates(profile: FernwayProfile, data: dict, *, acting_user=None, is_admin: bool = False) -> list[str]:
    """Apply editable profile, single balance, and physical card updates to an existing FernwayProfile."""
    changes: list[str] = []

    if 'first_name' in data and str(data['first_name']).strip():
        new_first = str(data['first_name']).strip()
        if new_first != profile.first_name:
            profile.first_name = new_first
            changes.append('Profile name')
    if 'last_name' in data and str(data['last_name']).strip():
        new_last = str(data['last_name']).strip()
        if new_last != profile.last_name:
            profile.last_name = new_last
            if 'Profile name' not in changes:
                changes.append('Profile name')

    verification_token = str(data.get('email_verification_token') or '').strip()
    if 'email' in data and str(data['email']).strip():
        new_email = str(data['email']).strip().lower()
        if '@' not in new_email:
            raise ValueError('Please enter a valid sign-in email address.')
        if FernwayProfile.objects.filter(email__iexact=new_email).exclude(pk=profile.pk).exists():
            raise ValueError('Another Lindenway online banking account already uses that sign-in email.')

        email_changed = new_email != (profile.email or '').strip().lower()
        if email_changed:
            if verification_token:
                expected_uid = acting_user.id if acting_user else profile.user_id
                try:
                    read_fernway_email_verification_grant(
                        token=verification_token,
                        expected_user_id=expected_uid,
                        expected_email=new_email,
                    )
                except BankingVerificationError as exc:
                    raise ValueError(str(exc)) from exc
                profile.email_verified = True
                profile.email_verified_at = timezone.now()
            else:
                profile.email_verified = False
                profile.email_verified_at = None
            profile.email = new_email
            changes.append('Sign-in email')
        elif verification_token and not profile.email_verified:
            expected_uid = acting_user.id if acting_user else profile.user_id
            try:
                read_fernway_email_verification_grant(
                    token=verification_token,
                    expected_user_id=expected_uid,
                    expected_email=new_email,
                )
            except BankingVerificationError as exc:
                raise ValueError(str(exc)) from exc
            profile.email_verified = True
            profile.email_verified_at = timezone.now()
            changes.append('Email verified')
    elif verification_token and not profile.email_verified:
        expected_uid = acting_user.id if acting_user else profile.user_id
        try:
            read_fernway_email_verification_grant(
                token=verification_token,
                expected_user_id=expected_uid,
                expected_email=profile.email,
            )
        except BankingVerificationError as exc:
            raise ValueError(str(exc)) from exc
        profile.email_verified = True
        profile.email_verified_at = timezone.now()
        changes.append('Email verified')

    if 'phone' in data and data['phone'] != profile.phone:
        profile.phone = data['phone']
        changes.append('Phone number')
    if 'address' in data and data['address'] != profile.address:
        profile.address = data['address']
        changes.append('Address')
    if 'password' in data and data['password'] and data['password'] != profile.password:
        profile.password = data['password']
        changes.append('Sign-in password')
    if 'transaction_pin' in data and str(data['transaction_pin']).strip():
        next_pin = _normalize_pin(data['transaction_pin'], profile.transaction_pin or '1234')
        if next_pin != profile.transaction_pin:
            profile.transaction_pin = next_pin
            changes.append('Transaction PIN')
    if 'theme' in data and data['theme']:
        profile.theme = data['theme']

    profile.save()

    # Single unified Account Balance
    primary = profile.accounts.filter(account_type='checking').first() or profile.accounts.first()
    if primary:
        prev_balance = primary.balance
        if 'account_balance' in data:
            primary.balance = _dollars_to_cents(data['account_balance'], primary.balance)
        elif 'checking_balance' in data:
            primary.balance = _dollars_to_cents(data['checking_balance'], primary.balance)
        if primary.balance != prev_balance:
            changes.append(f"Account balance (${primary.balance / 100:,.2f})")
        primary.name = 'Account Balance'
        if 'checking_account_number' in data and data['checking_account_number']:
            primary.account_number = data['checking_account_number']
        if 'routing_number' in data and data['routing_number']:
            primary.routing_number = data['routing_number']
        primary.save()
        # Consolidate away any legacy secondary split account so only one balance exists
        profile.accounts.exclude(pk=primary.pk).delete()

    card_changed = False
    cards = profile.cards.all()
    for card in cards:
        before = (
            card.holder_name,
            card.monthly_limit,
            card.card_number,
            card.expiry,
            card.cvv,
            card.pin,
            card.is_frozen,
        )
        card.kind = 'physical'
        if 'card_holder_name' in data and str(data['card_holder_name']).strip():
            card.holder_name = str(data['card_holder_name']).strip()
        else:
            card.holder_name = f"{profile.first_name} {profile.last_name}".strip()
        if 'card_limit' in data:
            card.monthly_limit = _dollars_to_cents(data['card_limit'], card.monthly_limit)
        if 'card_number' in data and str(data['card_number']).strip():
            formatted_number, last4 = _format_card_number(data['card_number'], card.last4)
            card.card_number = formatted_number
            card.last4 = last4
        elif 'card_last4' in data and data['card_last4']:
            formatted_number, last4 = _format_card_number(None, str(data['card_last4']))
            card.card_number = formatted_number
            card.last4 = last4
        if 'card_expiry' in data and str(data['card_expiry']).strip():
            card.expiry = str(data['card_expiry']).strip()[:7]
        if 'card_cvv' in data and str(data['card_cvv']).strip():
            cvv_digits = ''.join(ch for ch in str(data['card_cvv']) if ch.isdigit())[:4]
            if cvv_digits:
                card.cvv = cvv_digits
        if 'card_pin' in data and str(data['card_pin']).strip():
            card.pin = _normalize_pin(data['card_pin'], card.pin or '1234')
        if 'card_is_frozen' in data:
            card.is_frozen = bool(data['card_is_frozen'])
        after = (
            card.holder_name,
            card.monthly_limit,
            card.card_number,
            card.expiry,
            card.cvv,
            card.pin,
            card.is_frozen,
        )
        if after != before:
            card_changed = True
        card.save()

    if card_changed:
        changes.append('Debit card details')

    return changes


def _create_profile_for_user(user, data: dict) -> FernwayProfile:
    """Create a brand-new Fernway online banking account (free Test or paid monthly Live)."""
    if not getattr(user, 'is_superuser', False) and not getattr(user, 'email_verified', False):
        raise ValueError(
            'Verify your SharpToolz account email first so you can receive transfer requests, login alerts, and support updates.'
        )

    is_test = bool(data.get('test', True))
    first_name = str(data.get('first_name') or getattr(user, 'first_name', '') or 'Alexander').strip()
    last_name = str(data.get('last_name') or getattr(user, 'last_name', '') or 'Hamilton').strip()
    raw_email = str(data.get('email') or '').strip().lower()
    if not raw_email or '@' not in raw_email:
        raise ValueError('Please enter a valid sign-in email address.')
    if FernwayProfile.objects.filter(email__iexact=raw_email).exists():
        raise ValueError('That sign-in email is already in use by another Lindenway account. Choose a unique email.')

    verification_token = str(data.get('email_verification_token') or '').strip()
    email_verified = False
    if verification_token:
        try:
            read_fernway_email_verification_grant(
                token=verification_token,
                expected_user_id=user.id,
                expected_email=raw_email,
            )
        except BankingVerificationError as exc:
            raise ValueError(str(exc)) from exc
        email_verified = True

    password = str(data.get('password') or '').strip()
    if len(password) < 4:
        raise ValueError('Please set a sign-in password of at least 4 characters.')

    transaction_pin = _normalize_pin(data.get('transaction_pin'), '1234')
    theme = str(data.get('theme') or 'emerald-forest').strip()
    raw_balance = data.get('account_balance', data.get('checking_balance', '250000.00'))
    balance_cents = _dollars_to_cents(raw_balance, 25000000)
    card_limit_cents = _dollars_to_cents(data.get('card_limit', '50000.00'), 5000000)
    card_number = data.get('card_number')
    card_last4 = str(data.get('card_last4') or '8892')[:4]
    card_expiry = str(data.get('card_expiry') or '12/28').strip()[:7]
    card_cvv = str(data.get('card_cvv') or '721').strip()[:4]
    card_pin = _normalize_pin(data.get('card_pin'), '1234')
    card_holder = str(data.get('card_holder_name') or f"{first_name} {last_name}").strip()
    card_is_frozen = bool(data.get('card_is_frozen', False))

    now = timezone.now()

    with db_transaction.atomic():
        if not is_test:
            price = _get_monthly_price()
            wallet = Wallet.objects.select_for_update().filter(user=user).first()
            if not wallet or wallet.spendable_balance < price:
                raise PermissionError(
                    f'Insufficient wallet balance. Monthly Live subscription is ${price:.2f}/month. Fund your wallet with crypto to continue.'
                )
            wallet.debit(
                price,
                description=f'Lindenway Online Banking ({raw_email}) - Monthly Live Subscription (30 days)',
            )
            sub_status = 'active'
            sub_start = now
            sub_expires = now + timedelta(days=SUBSCRIPTION_CYCLE_DAYS)
        else:
            sub_status = 'test'
            sub_start = None
            sub_expires = None

        profile = FernwayProfile.objects.create(
            user=user,
            first_name=first_name,
            last_name=last_name,
            email=raw_email,
            email_verified=email_verified,
            email_verified_at=now if email_verified else None,
            phone=str(data.get('phone') or '+1 (555) 019-2834'),
            address=str(data.get('address') or '742 Evergreen Terrace, New York, NY 10001'),
            password=password,
            transaction_pin=transaction_pin,
            theme=theme,
            is_created=True,
            is_test=is_test,
            subscription_status=sub_status,
            subscription_started_at=sub_start,
            subscription_expires_at=sub_expires,
        )

        _seed_profile_assets(
            profile,
            account_balance_cents=balance_cents,
            card_holder_name=card_holder,
            card_number=card_number,
            card_last4=card_last4,
            card_expiry=card_expiry,
            card_cvv=card_cvv,
            card_pin=card_pin,
            card_limit_cents=card_limit_cents,
            card_is_frozen=card_is_frozen,
        )

    return profile


def _get_user_profile(user, profile_id=None):
    qs = FernwayProfile.objects.filter(user=user)
    if profile_id:
        return qs.filter(id=profile_id).first()
    return qs.filter(is_created=True).first() or qs.first()


class FernwayProfileListCreateView(APIView):
    """
    List all Fernway online banking accounts created by the current SharpToolz user,
    or create a brand-new banking account (Test Free or Live Monthly Subscription).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        profiles = list(
            FernwayProfile.objects.filter(user=request.user, is_created=True)
            .prefetch_related('accounts', 'cards')
            .order_by('-created_at')
        )

        # Keep persisted status synced if an active subscription passed its expiry timestamp
        now = timezone.now()
        for p in profiles:
            if not p.is_test and p.subscription_status == 'active' and p.subscription_expires_at and p.subscription_expires_at <= now:
                p.subscription_status = 'expired'
                p.save(update_fields=['subscription_status', 'updated_at'])

        monthly_price = float(_get_monthly_price())
        wallet_balance = _get_wallet_balance(request.user)
        active_subs = sum(1 for p in profiles if p.effective_subscription_status == 'active')
        expiring_soon = sum(1 for p in profiles if p.effective_subscription_status == 'active' and p.days_remaining <= 5)
        expired_count = sum(1 for p in profiles if p.effective_subscription_status == 'expired')
        pending_transfers_total = FernwayTransaction.objects.filter(
            account__profile__user=request.user,
            account__profile__is_created=True,
            direction='debit',
            status='pending',
        ).count()

        return Response({
            'results': FernwayProfileSerializer(profiles, many=True).data,
            'count': len(profiles),
            'active_subscriptions_count': active_subs,
            'monthly_total_due': round(active_subs * monthly_price, 2),
            'expiring_soon_count': expiring_soon,
            'expired_count': expired_count,
            'pending_transfers_total': pending_transfers_total,
            'monthly_price': monthly_price,
            'wallet_balance': wallet_balance,
            'online_banking_url': _get_online_banking_url(),
            'owner_email': getattr(request.user, 'email', '') or '',
            'owner_email_verified': bool(
                getattr(request.user, 'is_superuser', False) or getattr(request.user, 'email_verified', False)
            ),
        })

    def post(self, request):
        try:
            profile = _create_profile_for_user(request.user, request.data)
        except PermissionError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        email_sent = False
        email_error = None
        if bool(request.data.get('send_credentials_email', False)):
            try:
                send_fernway_credentials_email(profile)
                email_sent = True
            except BankingEmailError as exc:
                email_error = str(exc)

        payload = dict(FernwayProfileSerializer(profile).data)
        payload['credentials_email_sent'] = email_sent
        if email_error:
            payload['credentials_email_error'] = email_error
        return Response(payload, status=status.HTTP_201_CREATED)


def _client_ip(request) -> str:
    forwarded = request.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '') or ''


class FernwayOwnerEmailVerificationRequestView(APIView):
    """Send a 4-digit verification code via Resend to verify the SharpToolz user's own account email."""
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "fernway_email_verification"

    def post(self, request):
        if getattr(request.user, 'email_verified', False):
            return Response({
                'already_verified': True,
                'owner_email': request.user.email,
                'owner_email_verified': True,
            })

        try:
            challenge_id = request_owner_email_verification(user=request.user)
        except BankingVerificationError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except BankingEmailError:
            return Response(
                {'error': 'The verification email could not be sent right now. Check Resend configuration and try again.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response({
            'challenge_id': challenge_id,
            'email_hint': mask_email(request.user.email),
            'expires_in': CHALLENGE_TTL_SECONDS,
        })


class FernwayOwnerEmailVerificationConfirmView(APIView):
    """Confirm the 4-digit code and mark the SharpToolz user's own account email as verified."""
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "fernway_email_verification"

    def post(self, request):
        challenge_id = str(request.data.get('challenge_id') or '').strip()
        code = str(request.data.get('code') or '').strip()
        if not challenge_id or not code:
            return Response({'error': 'Enter the 4-digit verification code.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            verified_email = confirm_owner_email_verification(
                user=request.user,
                challenge_id=challenge_id,
                code=code,
            )
        except BankingVerificationError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            'owner_email': verified_email,
            'owner_email_verified': True,
        })


class FernwayEmailVerificationRequestView(APIView):
    """Send a 4-digit verification code via Resend to verify a Fernway account email."""
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "fernway_email_verification"

    def post(self, request):
        email = str(request.data.get('email') or '').strip().lower()
        profile_id = str(request.data.get('profile_id') or '').strip()
        if not email or '@' not in email:
            return Response({'error': 'Enter a valid email address.'}, status=status.HTTP_400_BAD_REQUEST)

        conflict_qs = FernwayProfile.objects.filter(email__iexact=email)
        if profile_id:
            conflict_qs = conflict_qs.exclude(pk=profile_id)
        if conflict_qs.exists():
            return Response(
                {'error': 'That email is already used by another Lindenway account. Choose a different email.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            challenge_id = request_fernway_email_verification(user_id=request.user.id, email=email)
        except BankingVerificationError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except BankingEmailError:
            return Response(
                {'error': 'The verification email could not be sent right now. Check Resend configuration and try again.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response({
            'challenge_id': challenge_id,
            'email_hint': mask_email(email),
            'expires_in': CHALLENGE_TTL_SECONDS,
        })


class FernwayEmailVerificationConfirmView(APIView):
    """Confirm a 4-digit verification code and return a signed token for creating/updating a Fernway account."""
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "fernway_email_verification"

    def post(self, request):
        challenge_id = str(request.data.get('challenge_id') or '').strip()
        code = str(request.data.get('code') or '').strip()
        if not challenge_id or not code:
            return Response({'error': 'Enter the 4-digit verification code.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            token, payload = confirm_fernway_email_verification(
                user_id=request.user.id,
                challenge_id=challenge_id,
                code=code,
            )
        except BankingVerificationError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            'verification_token': token,
            'email': payload['email'],
        })


class FernwayProfileDetailView(APIView):
    """Retrieve, update, or delete one specific Fernway online banking account owned by the user."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, profile_id):
        profile = _get_user_profile(request.user, profile_id)
        if not profile:
            return Response({'error': 'Online banking account not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(FernwayProfileSerializer(profile).data)

    def put(self, request, profile_id):
        profile = _get_user_profile(request.user, profile_id)
        if not profile:
            return Response({'error': 'Online banking account not found.'}, status=status.HTTP_404_NOT_FOUND)
        try:
            changes = _apply_profile_updates(profile, request.data, acting_user=request.user)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if changes:
            notify_account_updated(profile, changes=changes)
        return Response(FernwayProfileSerializer(profile).data)

    def delete(self, request, profile_id):
        profile = _get_user_profile(request.user, profile_id)
        if not profile:
            return Response({'error': 'Online banking account not found.'}, status=status.HTTP_404_NOT_FOUND)
        profile.delete()
        return Response({'status': 'deleted'})


class FernwayProfileUpgradeView(APIView):
    """Upgrade a Test Mode banking account to a Live Monthly Subscription (30 days)."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, profile_id=None):
        profile = _get_user_profile(request.user, profile_id or request.data.get('profile_id'))
        if not profile:
            return Response({'error': 'Online banking account not found.'}, status=status.HTTP_404_NOT_FOUND)

        if profile.is_live:
            return Response(FernwayProfileSerializer(profile).data, status=status.HTTP_200_OK)

        price = _get_monthly_price()
        now = timezone.now()
        with db_transaction.atomic():
            wallet = Wallet.objects.select_for_update().filter(user=request.user).first()
            if not wallet or wallet.spendable_balance < price:
                return Response(
                    {'error': f'Insufficient wallet balance. Monthly Live subscription is ${price:.2f}/month. Fund your wallet with crypto to continue.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            wallet.debit(
                price,
                description=f'Lindenway Online Banking ({profile.email}) - Live Monthly Activation (30 days)',
            )
            profile.is_created = True
            profile.is_test = False
            profile.subscription_status = 'active'
            profile.subscription_started_at = now
            profile.subscription_expires_at = now + timedelta(days=SUBSCRIPTION_CYCLE_DAYS)
            profile.last_renewal_reminder_at = None
            profile.save()

        return Response(FernwayProfileSerializer(profile).data)


class FernwayProfileRenewView(APIView):
    """Renew an existing Live/Expired online banking account for another 30 days via wallet."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, profile_id):
        profile = _get_user_profile(request.user, profile_id)
        if not profile:
            return Response({'error': 'Online banking account not found.'}, status=status.HTTP_404_NOT_FOUND)

        price = _get_monthly_price()
        now = timezone.now()
        base_time = (
            profile.subscription_expires_at
            if profile.subscription_expires_at and profile.subscription_expires_at > now
            else now
        )
        new_expiry = base_time + timedelta(days=SUBSCRIPTION_CYCLE_DAYS)

        with db_transaction.atomic():
            wallet = Wallet.objects.select_for_update().filter(user=request.user).first()
            if not wallet or wallet.spendable_balance < price:
                return Response(
                    {'error': f'Insufficient wallet balance. Monthly renewal is ${price:.2f}. Fund your wallet with crypto to renew.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            wallet.debit(
                price,
                description=f'Lindenway Online Banking ({profile.email}) - Monthly Subscription Renewal (30 days)',
            )
            profile.is_created = True
            profile.is_test = False
            profile.subscription_status = 'active'
            if not profile.subscription_started_at:
                profile.subscription_started_at = now
            profile.subscription_expires_at = new_expiry
            profile.last_renewal_reminder_at = None
            profile.save()

        return Response(FernwayProfileSerializer(profile).data)


class FernwayProfileResetView(APIView):
    """Reset one specific Fernway account's balances, card, and transactions while keeping its subscription."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, profile_id=None):
        profile = _get_user_profile(request.user, profile_id or request.data.get('profile_id'))
        if not profile:
            return Response({'error': 'Online banking account not found.'}, status=status.HTTP_404_NOT_FOUND)

        with db_transaction.atomic():
            profile.accounts.all().delete()
            profile.cards.all().delete()
            profile.theme = 'fernway-ember'
            profile.save(update_fields=['theme', 'updated_at'])
            _seed_profile_assets(profile)

        return Response(FernwayProfileSerializer(profile).data)


class FernwayManageMetaView(APIView):
    """
    Returns defaults + wallet balance + monthly price for the "Create New Banking Account" form,
    or updates/loads a specific profile if profile_id is supplied (backwards-compatible).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        profile_id = request.query_params.get('profile_id')
        if profile_id:
            profile = _get_user_profile(request.user, profile_id)
            if not profile:
                return Response({'error': 'Online banking account not found.'}, status=status.HTTP_404_NOT_FOUND)
            return Response(FernwayProfileSerializer(profile).data)

        existing_count = FernwayProfile.objects.filter(user=request.user, is_created=True).count()
        username = getattr(request.user, 'username', None) or 'member'

        return Response({
            'id': None,
            'first_name': getattr(request.user, 'first_name', None) or username.capitalize(),
            'last_name': getattr(request.user, 'last_name', None) or 'Hamilton',
            'email': '',
            'email_verified': False,
            'email_verified_at': None,
            'owner_email': getattr(request.user, 'email', '') or '',
            'owner_email_verified': bool(
                getattr(request.user, 'is_superuser', False) or getattr(request.user, 'email_verified', False)
            ),
            'phone': '+1 (555) 019-2834',
            'address': '742 Evergreen Terrace, New York, NY 10001',
            'password': 'password123',
            'transaction_pin': '1234',
            'theme': 'emerald-forest',
            'auth_token': '',
            'is_created': False,
            'is_test': True,
            'is_live': False,
            'subscription_status': 'test',
            'subscription_started_at': None,
            'subscription_expires_at': None,
            'days_remaining': 0,
            'needs_renewal_reminder': False,
            'tool_price': float(_get_monthly_price()),
            'monthly_price': float(_get_monthly_price()),
            'wallet_balance': _get_wallet_balance(request.user),
            'existing_accounts_count': existing_count,
            'accounts': [],
            'cards': [],
            'recent_transactions': [],
            'total_balance': 150000000,
        })

    def put(self, request):
        profile = _get_user_profile(request.user, request.data.get('profile_id'))
        if not profile:
            return Response({'error': 'Create an account first.'}, status=status.HTTP_404_NOT_FOUND)
        try:
            changes = _apply_profile_updates(profile, request.data, acting_user=request.user)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if changes:
            notify_account_updated(profile, changes=changes)
        return Response(FernwayProfileSerializer(profile).data)


class FernwayCreateAccountView(APIView):
    """Backwards-compatible alias for creating a brand-new Fernway banking account."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        try:
            profile = _create_profile_for_user(request.user, request.data)
        except PermissionError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(FernwayProfileSerializer(profile).data, status=status.HTTP_201_CREATED)


class FernwayAccountUpdateView(APIView):
    """Update balance, name, or numbers of a specific sub-account owned by the user."""
    permission_classes = [permissions.IsAuthenticated]

    def put(self, request, account_id):
        try:
            account = FernwayAccount.objects.get(id=account_id, profile__user=request.user)
        except FernwayAccount.DoesNotExist:
            return Response({'error': 'Account not found'}, status=status.HTTP_404_NOT_FOUND)

        data = request.data
        if 'balance' in data:
            account.balance = _dollars_to_cents(data['balance'], account.balance)
        if 'name' in data:
            account.name = data['name']
        if 'account_number' in data:
            account.account_number = data['account_number']
        if 'routing_number' in data:
            account.routing_number = data['routing_number']
        if 'apy_bps' in data:
            account.apy_bps = int(data['apy_bps'])

        account.save()
        return Response(FernwayAccountSerializer(account).data)


class FernwayTransactionView(APIView):
    """Add or delete a transaction on a specific Fernway profile owned by the user."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, profile_id=None):
        data = request.data
        profile = _get_user_profile(request.user, profile_id or data.get('profile_id'))
        if not profile:
            return Response({'error': 'Online banking account not found'}, status=status.HTTP_404_NOT_FOUND)

        account_id = data.get('account_id')
        if not account_id:
            account = profile.accounts.filter(account_type='checking').first() or profile.accounts.first()
        else:
            account = profile.accounts.filter(id=account_id).first()

        if not account:
            return Response({'error': 'Account not found'}, status=status.HTTP_404_NOT_FOUND)

        amount_cents = _dollars_to_cents(data.get('amount', 0), 0)
        if amount_cents <= 0:
            return Response({'error': 'Amount must be greater than zero'}, status=status.HTTP_400_BAD_REQUEST)

        direction = data.get('direction', 'credit')
        occurred_at = data.get('occurred_at') or timezone.now()

        tx = FernwayTransaction.objects.create(
            account=account,
            direction=direction,
            amount=amount_cents,
            fee=int(data.get('fee', 0)),
            counterparty=data.get('counterparty', 'Wire Transfer'),
            counterparty_detail=data.get('counterparty_detail', ''),
            narration=data.get('narration', ''),
            category=data.get('category', 'income' if direction == 'credit' else 'shopping'),
            channel=data.get('channel', 'wire'),
            status=data.get('status', 'completed'),
            reference=data.get('reference') or f"TX-{uuid.uuid4().hex[:8].upper()}",
            occurred_at=occurred_at,
        )

        if data.get('adjust_balance', True):
            if direction == 'credit':
                account.balance += amount_cents
            else:
                account.balance = max(0, account.balance - amount_cents)
            account.save()

        notify_transaction_posted(profile, tx)
        return Response(FernwayTransactionSerializer(tx).data, status=status.HTTP_201_CREATED)

    def delete(self, request, transaction_id, profile_id=None):
        qs = FernwayTransaction.objects.filter(id=transaction_id, account__profile__user=request.user)
        if profile_id:
            qs = qs.filter(account__profile_id=profile_id)
        tx = qs.first()
        if not tx:
            return Response({'error': 'Transaction not found'}, status=status.HTTP_404_NOT_FOUND)
        tx.delete()
        return Response({'status': 'deleted'})


class FernwayTransactionReviewView(APIView):
    """
    SharpToolz account owner approves or declines a pending outgoing Fernway transfer.
    - approve: marks transaction 'completed' (owner has sent the money manually)
    - decline: marks transaction 'failed' and refunds the held amount back to the Fernway account balance
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, profile_id, transaction_id, decision):
        if decision not in ('approve', 'decline'):
            return Response({'error': 'Invalid decision.'}, status=status.HTTP_400_BAD_REQUEST)

        profile = _get_user_profile(request.user, profile_id)
        if not profile:
            return Response({'error': 'Online banking account not found.'}, status=status.HTTP_404_NOT_FOUND)

        with db_transaction.atomic():
            tx = (
                FernwayTransaction.objects.select_for_update()
                .select_related('account')
                .filter(id=transaction_id, account__profile=profile)
                .first()
            )
            if not tx:
                return Response({'error': 'Transfer not found.'}, status=status.HTTP_404_NOT_FOUND)

            if tx.status != 'pending':
                return Response(
                    {'error': f'This transfer is already {tx.status}.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if decision == 'approve':
                tx.status = 'completed'
                tx.save(update_fields=['status'])
            else:
                account = FernwayAccount.objects.select_for_update().get(pk=tx.account_id)
                if tx.direction == 'debit':
                    account.balance += tx.amount + tx.fee
                    account.save(update_fields=['balance'])
                tx.status = 'failed'
                tx.save(update_fields=['status'])

        profile.refresh_from_db()
        notify_transfer_reviewed(profile, tx, decision=decision)
        return Response(FernwayProfileSerializer(profile).data)


class FernwayCardUpdateView(APIView):
    """Update card information on a card owned by the user."""
    permission_classes = [permissions.IsAuthenticated]

    def put(self, request, card_id):
        try:
            card = FernwayCard.objects.select_related('profile__user').get(id=card_id, profile__user=request.user)
        except FernwayCard.DoesNotExist:
            return Response({'error': 'Card not found'}, status=status.HTTP_404_NOT_FOUND)

        data = request.data
        changes: list[str] = []
        if 'holder_name' in data and data['holder_name'] != card.holder_name:
            card.holder_name = data['holder_name']
            changes.append('Cardholder name updated')
        if 'card_number' in data and str(data['card_number']).strip():
            formatted_number, last4 = _format_card_number(data['card_number'], card.last4)
            if formatted_number != card.card_number:
                changes.append('Card number updated')
            card.card_number = formatted_number
            card.last4 = last4
        elif 'last4' in data:
            formatted_number, last4 = _format_card_number(None, str(data['last4']))
            card.card_number = formatted_number
            card.last4 = last4
        if 'expiry' in data:
            next_expiry = str(data['expiry']).strip()[:7]
            if next_expiry != card.expiry:
                changes.append('Expiry updated')
            card.expiry = next_expiry
        if 'cvv' in data:
            cvv_digits = ''.join(ch for ch in str(data['cvv']) if ch.isdigit())[:4]
            if cvv_digits and cvv_digits != card.cvv:
                card.cvv = cvv_digits
                changes.append('CVV updated')
        if 'pin' in data and str(data['pin']).strip():
            next_pin = _normalize_pin(data['pin'], card.pin or '1234')
            if next_pin != card.pin:
                card.pin = next_pin
                changes.append('Card PIN changed')
        if 'brand' in data:
            card.brand = data['brand']
        card.kind = 'physical'
        if 'is_frozen' in data:
            next_frozen = bool(data['is_frozen'])
            if next_frozen != card.is_frozen:
                card.is_frozen = next_frozen
                changes.append('Card frozen' if next_frozen else 'Card unfrozen')
        if 'monthly_limit' in data:
            next_limit = _dollars_to_cents(data['monthly_limit'], card.monthly_limit)
            if next_limit != card.monthly_limit:
                card.monthly_limit = next_limit
                changes.append(f"Monthly limit set to ${next_limit / 100:,.2f}")

        card.save()
        if changes:
            notify_card_updated(card.profile, card, changes=changes, actor='owner')
        return Response(FernwayCardSerializer(card).data)


# ==============================================================================
# Fernway Frontend Client Endpoints (consumed by the Next.js Fernway frontend)
# ==============================================================================

class FernwayClientAuthLoginView(APIView):
    """Login endpoint for Fernway frontend — authenticates any created Fernway account by email & password."""
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        email = request.data.get('email', '').strip().lower()
        password = request.data.get('password', '')

        profile = FernwayProfile.objects.select_related('user').filter(email__iexact=email, is_created=True).first()
        if not profile:
            profile = FernwayProfile.objects.select_related('user').filter(email__iexact=email).first()
        if not profile or profile.password != password:
            return Response({'detail': 'Invalid email or password.'}, status=status.HTTP_401_UNAUTHORIZED)

        ip_address = str(request.data.get('client_ip') or '').strip() or _client_ip(request)
        user_agent = str(request.data.get('user_agent') or request.headers.get('User-Agent') or '').strip()
        notify_login(profile, ip_address=ip_address, user_agent=user_agent)

        return Response({
            'user': {
                'id': str(profile.id),
                'email': profile.email,
                'first_name': profile.first_name,
                'last_name': profile.last_name,
                'phone': profile.phone,
                'theme': profile.theme,
                'role': 'customer',
            },
            'token': profile.auth_token,
        })


class FernwayClientTransferCreateView(APIView):
    """
    Simple send-money endpoint called by the Fernway frontend when a user sends money
    to an external bank account. Creates a 'pending' transfer for the SharpToolz owner
    to review, send manually, and approve (or decline & refund).
    """
    permission_classes = [permissions.AllowAny]

    def _resolve_profile(self, request):
        token = request.headers.get('X-Fernway-Token') or request.query_params.get('token')
        if not token:
            auth_header = request.headers.get('Authorization', '')
            if auth_header.startswith('Bearer '):
                token = auth_header.split(' ')[1]

        if token:
            profile = FernwayProfile.objects.filter(auth_token=token).first()
            if profile:
                return profile

        if request.user and request.user.is_authenticated:
            return _get_user_profile(request.user)

        return FernwayProfile.objects.filter(is_created=True).first() or FernwayProfile.objects.first()

    def post(self, request):
        profile = self._resolve_profile(request)
        if not profile:
            return Response({'detail': 'Account not found.'}, status=status.HTTP_404_NOT_FOUND)

        data = request.data
        recipient_name = str(data.get('recipient_name') or data.get('recipientName') or '').strip()
        bank_name = str(data.get('bank_name') or data.get('bankName') or '').strip()
        account_number = str(data.get('account_number') or data.get('accountNumber') or '').strip()
        routing_number = str(data.get('routing_number') or data.get('routingNumber') or '').strip()
        note = str(data.get('note') or data.get('memo') or data.get('narration') or '').strip()
        raw_pin = data.get('transaction_pin') or data.get('transactionPin') or data.get('pin') or ''
        pin_digits = ''.join(ch for ch in str(raw_pin) if ch.isdigit())

        if len(recipient_name) < 2:
            return Response({'detail': 'Enter the recipient name.'}, status=status.HTTP_400_BAD_REQUEST)
        if len(bank_name) < 2:
            return Response({'detail': 'Enter the bank name.'}, status=status.HTTP_400_BAD_REQUEST)
        if len(account_number) < 4:
            return Response({'detail': 'Enter a valid account number.'}, status=status.HTTP_400_BAD_REQUEST)
        if len(pin_digits) != 4:
            return Response({'detail': 'Enter your 4-digit transaction PIN.'}, status=status.HTTP_400_BAD_REQUEST)

        expected_pin = (profile.transaction_pin or '1234').strip()
        if not hmac.compare_digest(pin_digits, expected_pin):
            return Response(
                {'detail': 'Incorrect transaction PIN. Please check your 4-digit PIN and try again.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Accept amount_cents (integer cents) or dollar amount
        if 'amount_cents' in data:
            try:
                amount_cents = int(data['amount_cents'])
            except (TypeError, ValueError):
                return Response({'detail': 'Enter a valid transfer amount.'}, status=status.HTTP_400_BAD_REQUEST)
        else:
            amount_cents = _dollars_to_cents(data.get('amount', 0), 0)

        if amount_cents < 100:
            return Response({'detail': 'Minimum transfer amount is $1.00.'}, status=status.HTTP_400_BAD_REQUEST)

        with db_transaction.atomic():
            account = (
                FernwayAccount.objects.select_for_update()
                .filter(profile=profile)
                .order_by('created_at')
                .first()
            )
            if not account:
                return Response({'detail': 'No active account found.'}, status=status.HTTP_404_NOT_FOUND)

            if account.balance < amount_cents:
                return Response(
                    {'detail': 'Insufficient account balance for this transfer.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            account.balance -= amount_cents
            account.save(update_fields=['balance'])

            last4 = account_number[-4:]
            tx = FernwayTransaction.objects.create(
                account=account,
                direction='debit',
                amount=amount_cents,
                fee=0,
                counterparty=recipient_name,
                counterparty_detail=f"{bank_name} ••••{last4}",
                bank_name=bank_name,
                recipient_account_number=account_number,
                recipient_routing_number=routing_number,
                narration=note or f"Transfer to {recipient_name}",
                category='transfer',
                channel='wire',
                status='pending',
                reference=f"TRF-{uuid.uuid4().hex[:8].upper()}",
                occurred_at=timezone.now(),
            )

        publish_owner_banking_event(
            profile.user_id,
            "banking.transfer_requested",
            {
                "profile_id": str(profile.id),
                "transaction_id": str(tx.id),
                "amount": tx.amount,
                "counterparty": tx.counterparty,
            },
        )
        notify_transfer_requested(profile, tx)
        return Response(FernwayTransactionSerializer(tx).data, status=status.HTTP_201_CREATED)


class FernwayClientCardListCreateView(APIView):
    """Create a physical debit card from inside Fernway and notify owner + verified account email."""
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        profile = _resolve_fernway_client_profile(request)
        if not profile:
            return Response({'detail': 'Sign in to view cards.'}, status=status.HTTP_401_UNAUTHORIZED)
        return Response(FernwayCardSerializer(profile.cards.all().order_by('created_at'), many=True).data)

    def post(self, request):
        profile = _resolve_fernway_client_profile(request)
        if not profile:
            return Response({'detail': 'Sign in to order a card.'}, status=status.HTTP_401_UNAUTHORIZED)

        account = profile.accounts.filter(account_type='checking').first() or profile.accounts.first()
        if not account:
            return Response({'detail': 'No active account found.'}, status=status.HTTP_404_NOT_FOUND)

        label = str(request.data.get('label') or 'Physical Debit Card').strip()[:100]
        if len(label) < 2:
            return Response({'detail': 'Card nickname must be at least 2 characters.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            monthly_limit = int(request.data.get('monthly_limit') or request.data.get('monthlyLimit') or 250000)
        except (TypeError, ValueError):
            monthly_limit = 250000
        monthly_limit = max(10000, min(100000000, monthly_limit))

        middle = ''.join(str(random.randint(0, 9)) for _ in range(12))
        formatted_number, last4 = _format_card_number(f"4532{middle}")
        now = timezone.now()
        expiry = f"{now.month:02d}/{(now.year + 4) % 100:02d}"
        cvv = f"{random.randint(100, 999)}"
        pin = profile.transaction_pin if len(profile.transaction_pin or '') == 4 else '1234'

        card = FernwayCard.objects.create(
            profile=profile,
            account=account,
            label=label,
            kind='physical',
            brand='visa',
            card_number=formatted_number,
            last4=last4,
            expiry=expiry,
            cvv=cvv,
            pin=pin,
            holder_name=f"{profile.first_name} {profile.last_name}".strip() or profile.email,
            monthly_limit=monthly_limit,
            is_frozen=False,
            online_payments=True,
        )

        publish_owner_banking_event(
            profile.user_id,
            "banking.card_added",
            {
                "profile_id": str(profile.id),
                "card_id": str(card.id),
                "label": card.label,
                "last4": card.last4,
            },
        )
        notify_card_added(profile, card, actor='customer')
        return Response(FernwayCardSerializer(card).data, status=status.HTTP_201_CREATED)


class FernwayClientCardDetailView(APIView):
    """Update card controls (freeze / online payments) from inside Fernway and notify owner + verified email."""
    permission_classes = [permissions.AllowAny]

    def patch(self, request, card_id):
        profile = _resolve_fernway_client_profile(request)
        if not profile:
            return Response({'detail': 'Sign in to manage card settings.'}, status=status.HTTP_401_UNAUTHORIZED)

        card = profile.cards.filter(id=card_id).first()
        if not card:
            return Response({'detail': 'Card not found.'}, status=status.HTTP_404_NOT_FOUND)

        changes: list[str] = []
        if 'frozen' in request.data or 'is_frozen' in request.data:
            raw_frozen = request.data.get('frozen', request.data.get('is_frozen'))
            next_frozen = bool(raw_frozen)
            if next_frozen != card.is_frozen:
                card.is_frozen = next_frozen
                changes.append('Card frozen' if next_frozen else 'Card unfrozen')
        if 'onlinePayments' in request.data or 'online_payments' in request.data:
            raw_online = request.data.get('onlinePayments', request.data.get('online_payments'))
            next_online = bool(raw_online)
            if next_online != card.online_payments:
                card.online_payments = next_online
                changes.append('Online payments turned on' if next_online else 'Online payments turned off')

        if changes:
            card.save(update_fields=['is_frozen', 'online_payments'])
            publish_owner_banking_event(
                profile.user_id,
                "banking.card_updated",
                {
                    "profile_id": str(profile.id),
                    "card_id": str(card.id),
                    "changes": changes,
                },
            )
            notify_card_updated(profile, card, changes=changes, actor='customer')

        return Response(FernwayCardSerializer(card).data)


def _resolve_fernway_client_profile(request):
    token = request.headers.get('X-Fernway-Token') or request.query_params.get('token')
    if not token:
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]

    if token:
        return FernwayProfile.objects.select_related('user').filter(auth_token=token).first()

    if request.user and request.user.is_authenticated:
        return _get_user_profile(request.user)

    return None


def _serialize_fernway_support_thread(ticket):
    data = PublicTrackingSupportThreadSerializer(ticket).data
    data["created_at"] = ticket.created_at.isoformat()
    data["updated_at"] = ticket.updated_at.isoformat()
    data["channel"] = ticket_channel(ticket.id)
    return data


class FernwayClientSupportListCreateView(APIView):
    """
    Live support conversations for a signed-in Fernway Banking customer.
    Uses the exact same TrackingSupportMessage + Pusher realtime system as
    ParcelFinda and MyFlightLookup so all messages arrive live in SharpToolz /support.
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        profile = _resolve_fernway_client_profile(request)
        if not profile:
            return Response({'detail': 'Sign in to access banking support.'}, status=status.HTTP_401_UNAUTHORIZED)

        tickets = (
            TrackingSupportMessage.objects.filter(
                fernway_profile=profile,
                source=TrackingSupportMessage.Source.FERNWAY,
            )
            .prefetch_related("replies")
            .order_by("-updated_at")
        )
        return Response({
            "results": [_serialize_fernway_support_thread(t) for t in tickets],
            "realtime": public_realtime_config(),
        })

    def post(self, request):
        profile = _resolve_fernway_client_profile(request)
        if not profile:
            return Response({'detail': 'Sign in to contact banking support.'}, status=status.HTTP_401_UNAUTHORIZED)

        subject = str(request.data.get("subject") or "").strip()[:160]
        message = str(request.data.get("message") or "").strip()[:5000]
        if not subject:
            subject = "Banking support request"
        if not message:
            return Response({"detail": "Please write a message before sending."}, status=status.HTTP_400_BAD_REQUEST)

        full_name = f"{profile.first_name} {profile.last_name}".strip() or profile.email
        _, customer_token_hash = create_customer_access_token()
        ticket = TrackingSupportMessage.objects.create(
            document=None,
            fernway_profile=profile,
            tracking_id=profile.email,
            source=TrackingSupportMessage.Source.FERNWAY,
            customer_name=full_name[:120],
            customer_email=profile.email if profile.email_verified else "",
            subject=subject,
            message=message,
            customer_access_token_hash=customer_token_hash,
            status=TrackingSupportMessage.Status.NEW,
        )
        if get_integration_secret("resend_api_key"):
            try:
                notify_owner_of_new_ticket(ticket)
            except SupportEmailError:
                logger.exception("Could not email owner for Fernway support ticket %s", ticket.id)
        publish_support_update(ticket, event="support.created")

        payload = _serialize_fernway_support_thread(ticket)
        payload["realtime"] = public_realtime_config()
        return Response(payload, status=status.HTTP_201_CREATED)


class FernwayClientSupportDetailView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request, message_id):
        profile = _resolve_fernway_client_profile(request)
        if not profile:
            return Response({'detail': 'Sign in to access banking support.'}, status=status.HTTP_401_UNAUTHORIZED)

        ticket = get_object_or_404(
            TrackingSupportMessage.objects.prefetch_related("replies"),
            id=message_id,
            fernway_profile=profile,
        )
        payload = _serialize_fernway_support_thread(ticket)
        payload["realtime"] = public_realtime_config()
        return Response(payload)


class FernwayClientSupportReplyView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request, message_id):
        profile = _resolve_fernway_client_profile(request)
        if not profile:
            return Response({'detail': 'Sign in to reply.'}, status=status.HTTP_401_UNAUTHORIZED)

        ticket = get_object_or_404(
            TrackingSupportMessage.objects.select_related("fernway_profile__user").prefetch_related("replies"),
            id=message_id,
            fernway_profile=profile,
        )
        serializer = TrackingSupportReplyCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        body = serializer.validated_data["body"]

        reply = TrackingSupportReply.objects.create(
            support_message=ticket,
            direction=TrackingSupportReply.Direction.CUSTOMER,
            body=body,
            sender_email=profile.email,
            delivery_status=TrackingSupportReply.DeliveryStatus.RECEIVED,
        )
        if get_integration_secret("resend_api_key"):
            try:
                reply.resend_email_id = notify_owner_of_customer_reply(
                    ticket,
                    body,
                    idempotency_key=f"fernway-support-owner-{reply.id.hex}",
                )
                reply.delivery_status = TrackingSupportReply.DeliveryStatus.QUEUED
            except SupportEmailError:
                reply.delivery_status = TrackingSupportReply.DeliveryStatus.FAILED
                logger.exception("Could not email owner for Fernway support reply %s", reply.id)
            reply.save(update_fields=["resend_email_id", "delivery_status"])

        ticket.status = TrackingSupportMessage.Status.NEW
        ticket.save(update_fields=["status", "updated_at"])
        publish_support_update(ticket, event="support.customer_message")
        return Response(TrackingSupportReplySerializer(reply).data, status=status.HTTP_201_CREATED)


class FernwayClientSupportRealtimeAuthView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request, message_id):
        profile = _resolve_fernway_client_profile(request)
        if not profile:
            return Response({'detail': 'Sign in to authorize realtime support.'}, status=status.HTTP_401_UNAUTHORIZED)

        ticket = get_object_or_404(
            TrackingSupportMessage,
            id=message_id,
            fernway_profile=profile,
        )
        channel_name = str(request.data.get("channel_name", ""))
        socket_id = str(request.data.get("socket_id", ""))
        if channel_name != ticket_channel(ticket.id) or not socket_id:
            return Response({"detail": "Invalid realtime channel."}, status=status.HTTP_403_FORBIDDEN)
        try:
            return Response(authorize_private_channel(socket_id, channel_name))
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)


class FernwayClientDataView(APIView):
    """Data endpoint returning accounts, transactions, cards, and live/test status for a given auth token."""
    permission_classes = [permissions.AllowAny]

    def get_profile(self, request):
        token = request.headers.get('X-Fernway-Token') or request.query_params.get('token')
        if not token:
            auth_header = request.headers.get('Authorization', '')
            if auth_header.startswith('Bearer '):
                token = auth_header.split(' ')[1]

        if token:
            profile = FernwayProfile.objects.filter(auth_token=token).first()
            if profile:
                return profile

        if request.user and request.user.is_authenticated:
            return _get_user_profile(request.user)

        return FernwayProfile.objects.filter(is_created=True).first() or FernwayProfile.objects.first()

    def get(self, request):
        profile = self.get_profile(request)
        if not profile:
            return Response({'detail': 'Profile not found.'}, status=status.HTTP_404_NOT_FOUND)

        serializer = FernwayProfileSerializer(profile)
        return Response(serializer.data)


# ==============================================================================
# SharpToolz Admin Endpoints (manage ALL users' Fernway online banking accounts)
# ==============================================================================

def _sync_expired_profiles(profiles):
    now = timezone.now()
    for p in profiles:
        if (
            not p.is_test
            and p.subscription_status == 'active'
            and p.subscription_expires_at
            and p.subscription_expires_at <= now
        ):
            p.subscription_status = 'expired'
            p.save(update_fields=['subscription_status', 'updated_at'])


class FernwayAdminAccountListView(APIView):
    """List and search all Fernway online banking accounts across all SharpToolz users."""
    permission_classes = [IsAdminOnly]

    def get(self, request):
        include_drafts = str(request.query_params.get('include_drafts', '')).lower() in ('1', 'true', 'yes')
        search = (request.query_params.get('search') or '').strip()
        status_filter = (request.query_params.get('status') or 'all').strip().lower()

        base_qs = FernwayProfile.objects.select_related('user').prefetch_related('accounts', 'cards')
        if not include_drafts:
            base_qs = base_qs.filter(is_created=True)

        all_profiles = list(base_qs.order_by('-created_at'))
        _sync_expired_profiles(all_profiles)

        pending_counts_by_profile = {}
        pending_rows = FernwayTransaction.objects.filter(
            account__profile__in=all_profiles,
            direction='debit',
            status='pending',
        ).values_list('account__profile_id', flat=True)
        for pid in pending_rows:
            pending_counts_by_profile[pid] = pending_counts_by_profile.get(pid, 0) + 1

        stats = {
            'total_accounts': len(all_profiles),
            'live_accounts': sum(1 for p in all_profiles if p.effective_subscription_status == 'active'),
            'test_accounts': sum(1 for p in all_profiles if p.effective_subscription_status == 'test'),
            'expired_accounts': sum(1 for p in all_profiles if p.effective_subscription_status == 'expired'),
            'pending_transfers_total': sum(pending_counts_by_profile.values()),
        }

        filtered_qs = base_qs
        if search:
            filtered_qs = filtered_qs.filter(
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(email__icontains=search)
                | Q(user__username__icontains=search)
                | Q(user__email__icontains=search)
            )
        filtered = list(filtered_qs.order_by('-created_at'))

        if status_filter in ('active', 'live'):
            filtered = [p for p in filtered if p.effective_subscription_status == 'active']
        elif status_filter == 'test':
            filtered = [p for p in filtered if p.effective_subscription_status == 'test']
        elif status_filter == 'expired':
            filtered = [p for p in filtered if p.effective_subscription_status == 'expired']
        elif status_filter == 'pending_transfers':
            filtered = [p for p in filtered if pending_counts_by_profile.get(p.id, 0) > 0]

        return Response({
            'results': FernwayAdminProfileSerializer(filtered, many=True).data,
            'count': len(filtered),
            'monthly_price': float(_get_monthly_price()),
            'online_banking_url': _get_online_banking_url(),
            'stats': stats,
        })


class FernwayAdminAccountDetailView(APIView):
    """Retrieve, update, or delete any user's Fernway online banking account."""
    permission_classes = [IsAdminOnly]

    def get(self, request, profile_id):
        profile = (
            FernwayProfile.objects.select_related('user')
            .prefetch_related('accounts', 'cards')
            .filter(id=profile_id)
            .first()
        )
        if not profile:
            return Response({'error': 'Online banking account not found.'}, status=status.HTTP_404_NOT_FOUND)
        _sync_expired_profiles([profile])
        return Response(FernwayAdminProfileSerializer(profile).data)

    def put(self, request, profile_id):
        profile = FernwayProfile.objects.select_related('user').filter(id=profile_id).first()
        if not profile:
            return Response({'error': 'Online banking account not found.'}, status=status.HTTP_404_NOT_FOUND)
        try:
            changes = _apply_profile_updates(profile, request.data, acting_user=request.user, is_admin=True)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        profile.refresh_from_db()
        if changes:
            notify_account_updated(profile, changes=changes)
        return Response(FernwayAdminProfileSerializer(profile).data)

    def delete(self, request, profile_id):
        profile = FernwayProfile.objects.filter(id=profile_id).first()
        if not profile:
            return Response({'error': 'Online banking account not found.'}, status=status.HTTP_404_NOT_FOUND)
        profile.delete()
        return Response({'status': 'deleted'})


class FernwayAdminSubscriptionView(APIView):
    """Admin override for any account's subscription status (no wallet charge)."""
    permission_classes = [IsAdminOnly]

    def post(self, request, profile_id):
        profile = FernwayProfile.objects.select_related('user').filter(id=profile_id).first()
        if not profile:
            return Response({'error': 'Online banking account not found.'}, status=status.HTTP_404_NOT_FOUND)

        action = str(request.data.get('action') or '').strip().lower()
        try:
            days = max(1, min(365, int(request.data.get('days', SUBSCRIPTION_CYCLE_DAYS))))
        except (TypeError, ValueError):
            days = SUBSCRIPTION_CYCLE_DAYS

        now = timezone.now()
        if action == 'activate':
            profile.is_created = True
            profile.is_test = False
            profile.subscription_status = 'active'
            profile.subscription_started_at = profile.subscription_started_at or now
            profile.subscription_expires_at = now + timedelta(days=days)
            profile.last_renewal_reminder_at = None
        elif action == 'extend':
            base = (
                profile.subscription_expires_at
                if profile.subscription_expires_at and profile.subscription_expires_at > now
                else now
            )
            profile.is_created = True
            profile.is_test = False
            profile.subscription_status = 'active'
            profile.subscription_started_at = profile.subscription_started_at or now
            profile.subscription_expires_at = base + timedelta(days=days)
            profile.last_renewal_reminder_at = None
        elif action == 'set_test':
            profile.is_created = True
            profile.is_test = True
            profile.subscription_status = 'test'
            profile.subscription_expires_at = None
        elif action == 'expire':
            profile.is_created = True
            profile.is_test = False
            profile.subscription_status = 'expired'
            profile.subscription_expires_at = now - timedelta(seconds=1)
        else:
            return Response(
                {'error': 'Invalid action. Use activate, extend, set_test, or expire.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        profile.save()
        return Response(FernwayAdminProfileSerializer(profile).data)


class FernwayAdminResetView(APIView):
    """Admin reset of any Fernway account's balances, card, and starter transactions."""
    permission_classes = [IsAdminOnly]

    def post(self, request, profile_id):
        profile = FernwayProfile.objects.select_related('user').filter(id=profile_id).first()
        if not profile:
            return Response({'error': 'Online banking account not found.'}, status=status.HTTP_404_NOT_FOUND)

        with db_transaction.atomic():
            profile.accounts.all().delete()
            profile.cards.all().delete()
            profile.theme = 'emerald-forest'
            profile.save(update_fields=['theme', 'updated_at'])
            _seed_profile_assets(profile)

        profile.refresh_from_db()
        return Response(FernwayAdminProfileSerializer(profile).data)


class FernwayAdminTransactionView(APIView):
    """Admin add/delete transactions on any user's Fernway account."""
    permission_classes = [IsAdminOnly]

    def post(self, request, profile_id):
        profile = FernwayProfile.objects.filter(id=profile_id).first()
        if not profile:
            return Response({'error': 'Online banking account not found.'}, status=status.HTTP_404_NOT_FOUND)

        data = request.data
        account_id = data.get('account_id')
        if account_id:
            account = profile.accounts.filter(id=account_id).first()
        else:
            account = profile.accounts.filter(account_type='checking').first() or profile.accounts.first()
        if not account:
            return Response({'error': 'Account not found.'}, status=status.HTTP_404_NOT_FOUND)

        amount_cents = _dollars_to_cents(data.get('amount', 0), 0)
        if amount_cents <= 0:
            return Response({'error': 'Amount must be greater than zero.'}, status=status.HTTP_400_BAD_REQUEST)

        direction = data.get('direction', 'credit')
        tx = FernwayTransaction.objects.create(
            account=account,
            direction=direction,
            amount=amount_cents,
            fee=int(data.get('fee', 0)),
            counterparty=data.get('counterparty', 'Wire Transfer'),
            counterparty_detail=data.get('counterparty_detail', ''),
            narration=data.get('narration', ''),
            category=data.get('category', 'income' if direction == 'credit' else 'shopping'),
            channel=data.get('channel', 'wire'),
            status=data.get('status', 'completed'),
            reference=data.get('reference') or f"TX-{uuid.uuid4().hex[:8].upper()}",
            occurred_at=data.get('occurred_at') or timezone.now(),
        )

        if data.get('adjust_balance', True):
            if direction == 'credit':
                account.balance += amount_cents
            else:
                account.balance = max(0, account.balance - amount_cents)
            account.save(update_fields=['balance'])

        notify_transaction_posted(profile, tx)
        return Response(FernwayTransactionSerializer(tx).data, status=status.HTTP_201_CREATED)

    def delete(self, request, profile_id, transaction_id):
        tx = FernwayTransaction.objects.filter(id=transaction_id, account__profile_id=profile_id).first()
        if not tx:
            return Response({'error': 'Transaction not found.'}, status=status.HTTP_404_NOT_FOUND)
        tx.delete()
        return Response({'status': 'deleted'})


class FernwayAdminTransactionReviewView(APIView):
    """Admin approve or decline & refund any pending outgoing Fernway transfer."""
    permission_classes = [IsAdminOnly]

    def post(self, request, profile_id, transaction_id, decision):
        if decision not in ('approve', 'decline'):
            return Response({'error': 'Invalid decision.'}, status=status.HTTP_400_BAD_REQUEST)

        profile = FernwayProfile.objects.select_related('user').filter(id=profile_id).first()
        if not profile:
            return Response({'error': 'Online banking account not found.'}, status=status.HTTP_404_NOT_FOUND)

        with db_transaction.atomic():
            tx = (
                FernwayTransaction.objects.select_for_update()
                .select_related('account')
                .filter(id=transaction_id, account__profile=profile)
                .first()
            )
            if not tx:
                return Response({'error': 'Transfer not found.'}, status=status.HTTP_404_NOT_FOUND)
            if tx.status != 'pending':
                return Response(
                    {'error': f'This transfer is already {tx.status}.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if decision == 'approve':
                tx.status = 'completed'
                tx.save(update_fields=['status'])
            else:
                account = FernwayAccount.objects.select_for_update().get(pk=tx.account_id)
                if tx.direction == 'debit':
                    account.balance += tx.amount + tx.fee
                    account.save(update_fields=['balance'])
                tx.status = 'failed'
                tx.save(update_fields=['status'])

        profile.refresh_from_db()
        notify_transfer_reviewed(profile, tx, decision=decision)
        return Response(FernwayAdminProfileSerializer(profile).data)


class FernwayAdminSettingsView(APIView):
    """Read or update Online Banking (Fernway) website URL settings."""
    permission_classes = [IsAdminOnly]

    def get(self, request):
        return Response({'online_banking_url': _get_online_banking_url()})

    def patch(self, request):
        raw_url = str(request.data.get('online_banking_url', '') or '').strip().rstrip('/')
        if raw_url:
            if not raw_url.startswith(('http://', 'https://')):
                raw_url = f'https://{raw_url}'
            parsed = urlparse(raw_url)
            if parsed.scheme not in ('http', 'https') or not parsed.netloc:
                return Response(
                    {'error': 'Enter a valid website URL (e.g. https://fernway.sharptoolz.com).'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            normalized = f'{parsed.scheme}://{parsed.netloc}{parsed.path}'.rstrip('/')
        else:
            normalized = ''

        site_settings = SiteSettings.get_settings()
        site_settings.online_banking_url = normalized
        site_settings.save(update_fields=['online_banking_url', 'updated_at'])
        return Response({'online_banking_url': normalized})


class FernwaySendCredentialsEmailView(APIView):
    """Send or resend the Fernway sign-in credentials email to the account's email address."""
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "fernway_email_verification"

    def post(self, request, profile_id):
        profile = _get_user_profile(request.user, profile_id)
        if not profile:
            return Response({'error': 'Online banking account not found.'}, status=status.HTTP_404_NOT_FOUND)
        if not getattr(request.user, 'is_superuser', False) and not getattr(request.user, 'email_verified', False):
            return Response(
                {'error': 'Verify your SharpToolz account email before emailing account details.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            send_fernway_credentials_email(profile)
        except BankingEmailError as exc:
            return Response(
                {'error': str(exc) or 'Could not send login details email right now.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        profile.refresh_from_db()
        return Response({
            'sent': True,
            'recipient': profile.email,
            'credentials_email_sent_at': profile.credentials_email_sent_at,
            'profile': FernwayProfileSerializer(profile).data,
        })


class FernwayAdminSendCredentialsEmailView(APIView):
    """Admin endpoint to send or resend the Fernway sign-in credentials email for any user's account."""
    permission_classes = [IsAdminOnly]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "fernway_email_verification"

    def post(self, request, profile_id):
        profile = FernwayProfile.objects.filter(id=profile_id).first()
        if not profile:
            return Response({'error': 'Online banking account not found.'}, status=status.HTTP_404_NOT_FOUND)

        try:
            send_fernway_credentials_email(profile)
        except BankingEmailError as exc:
            return Response(
                {'error': str(exc) or 'Could not send login details email right now.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        profile.refresh_from_db()
        return Response({
            'sent': True,
            'recipient': profile.email,
            'credentials_email_sent_at': profile.credentials_email_sent_at,
            'profile': FernwayAdminProfileSerializer(profile).data,
        })
