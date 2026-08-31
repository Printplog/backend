import uuid
from decimal import Decimal, InvalidOperation, ROUND_DOWN

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.two_factor import is_enabled_for_user, verify_settings_totp_code
from analytics.utils import log_action
from api.permissions import IsSuperUser
from wallet.models import (
    RevenueDistributionBatch,
    RevenueDistributionConfig,
    RevenueDistributionPayout,
    RevenueShareRecipient,
)
from wallet.providers import CPayClient, PaymentProviderError, validate_bep20_address
from wallet.tasks import check_revenue_distribution, execute_revenue_distribution


def _serialize_payout(payout):
    return {
        "id": str(payout.id),
        "recipient_name": payout.recipient_name,
        "recipient_email": payout.recipient_email,
        "bep20_address": payout.bep20_address,
        "percentage": str(payout.percentage),
        "amount": str(payout.amount),
        "status": payout.status,
        "provider_transaction_id": payout.provider_transaction_id,
        "transaction_hash": payout.transaction_hash,
        "error_message": payout.error_message,
        "submitted_at": payout.submitted_at.isoformat() if payout.submitted_at else None,
        "completed_at": payout.completed_at.isoformat() if payout.completed_at else None,
    }


def _serialize_batch(batch):
    return {
        "id": str(batch.id),
        "amount": str(batch.amount),
        "threshold_amount": str(batch.threshold_amount),
        "balance_before": str(batch.balance_before),
        "status": batch.status,
        "error_message": batch.error_message,
        "created_at": batch.created_at.isoformat(),
        "submitted_at": batch.submitted_at.isoformat() if batch.submitted_at else None,
        "completed_at": batch.completed_at.isoformat() if batch.completed_at else None,
        "payouts": [_serialize_payout(payout) for payout in batch.payouts.all()],
    }


def _serialize_recipient(recipient):
    return {
        "id": str(recipient.id),
        "name": recipient.name,
        "email": recipient.email,
        "bep20_address": recipient.bep20_address,
        "percentage": str(recipient.percentage),
    }


def _require_totp(request):
    code = str(request.data.get("two_factor_code") or "").strip()
    if not code:
        return "Authenticator code is required."
    if not is_enabled_for_user(request.user):
        return "Two-factor authentication is not configured for this admin account."
    if not verify_settings_totp_code(request.user, code):
        return "Invalid or already-used authenticator code."
    return None


class DistributionDashboardView(APIView):
    permission_classes = [IsSuperUser]
    throttle_scope = "admin_read"

    def get(self, request):
        config = RevenueDistributionConfig.get_config()
        recipients = RevenueShareRecipient.objects.filter(is_active=True).order_by("created_at")
        batches = RevenueDistributionBatch.objects.prefetch_related("payouts").all()[:20]
        allocation = sum((item.percentage for item in recipients), Decimal("0"))
        return Response(
            {
                "configuration": {
                    "enabled": config.enabled,
                    "threshold_amount": str(config.threshold_amount),
                    "last_available_balance": (
                        str(config.last_available_balance)
                        if config.last_available_balance is not None
                        else None
                    ),
                    "last_balance_checked_at": (
                        config.last_balance_checked_at.isoformat()
                        if config.last_balance_checked_at
                        else None
                    ),
                    "network": "BEP20",
                    "currency": "USDT",
                    "allocation_total": str(allocation),
                    "deposit_routing_enabled": settings.CPAY_DEPOSIT_ROUTING_ENABLED,
                    "deposit_provider_configured": bool(
                        CPayClient.deposit_configured()
                        and settings.CRYPTAPI_CALLBACK_BASE_URL
                        and settings.CRYPTAPI_REQUIRE_SIGNATURE
                    ),
                    "live_payouts_enabled": settings.CPAY_LIVE_PAYOUTS_ENABLED,
                    "payout_provider_configured": CPayClient.payout_configured(),
                },
                "recipients": [_serialize_recipient(item) for item in recipients],
                "batches": [_serialize_batch(batch) for batch in batches],
            }
        )


class DistributionConfigurationView(APIView):
    permission_classes = [IsSuperUser]
    throttle_scope = "admin_2fa"

    def post(self, request):
        try:
            enabled = bool(request.data.get("enabled", False))
            threshold = Decimal(str(request.data.get("threshold_amount", "100"))).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError, ValueError):
            return Response({"detail": "Enter a valid distribution threshold."}, status=status.HTTP_400_BAD_REQUEST)
        if threshold <= 0:
            return Response({"detail": "Distribution threshold must be greater than zero."}, status=status.HTTP_400_BAD_REQUEST)

        raw_recipients = request.data.get("recipients")
        if not isinstance(raw_recipients, list) or not 1 <= len(raw_recipients) <= 20:
            return Response({"detail": "Add between 1 and 20 recipients."}, status=status.HTTP_400_BAD_REQUEST)

        cleaned = []
        emails = set()
        addresses = set()
        for index, item in enumerate(raw_recipients):
            if not isinstance(item, dict):
                return Response({"detail": f"Recipient {index + 1} is invalid."}, status=status.HTTP_400_BAD_REQUEST)
            name = str(item.get("name") or "").strip()
            email = str(item.get("email") or "").strip().lower()
            if not name:
                return Response({"detail": f"Recipient {index + 1} needs a name."}, status=status.HTTP_400_BAD_REQUEST)
            try:
                validate_email(email)
                address = validate_bep20_address(item.get("bep20_address"))
                percentage = Decimal(str(item.get("percentage"))).quantize(Decimal("0.01"))
            except (ValidationError, ValueError, InvalidOperation, TypeError):
                return Response(
                    {"detail": f"Recipient {index + 1} has an invalid email, BEP20 address, or percentage."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not Decimal("0") < percentage <= Decimal("100"):
                return Response({"detail": "Every percentage must be between 0 and 100."}, status=status.HTTP_400_BAD_REQUEST)
            if email in emails or address.lower() in addresses:
                return Response({"detail": "Recipient emails and BEP20 addresses must be unique."}, status=status.HTTP_400_BAD_REQUEST)
            emails.add(email)
            addresses.add(address.lower())
            cleaned.append(
                {
                    "id": str(item.get("id") or "").strip(),
                    "name": name[:120],
                    "email": email,
                    "bep20_address": address,
                    "percentage": percentage,
                }
            )

        allocation = sum((item["percentage"] for item in cleaned), Decimal("0"))
        if enabled and allocation != Decimal("100.00"):
            return Response(
                {"detail": "Active recipient percentages must total exactly 100%."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        RevenueDistributionConfig.get_config()
        with transaction.atomic():
            totp_error = _require_totp(request)
            if totp_error:
                return Response({"detail": totp_error}, status=status.HTTP_403_FORBIDDEN)

            config = RevenueDistributionConfig.objects.select_for_update().get(pk=1)
            config.enabled = enabled
            config.threshold_amount = threshold
            config.save(update_fields=["enabled", "threshold_amount", "updated_at"])

            active_ids = []
            existing_ids = set(
                str(item) for item in RevenueShareRecipient.objects.values_list("id", flat=True)
            )
            RevenueShareRecipient.objects.update(is_active=False)
            for item in cleaned:
                recipient_id = item.pop("id")
                if recipient_id:
                    if recipient_id not in existing_ids:
                        transaction.set_rollback(True)
                        return Response({"detail": "A recipient no longer exists. Refresh and try again."}, status=status.HTTP_409_CONFLICT)
                    recipient = RevenueShareRecipient.objects.get(pk=recipient_id)
                    for field, value in item.items():
                        setattr(recipient, field, value)
                    recipient.is_active = True
                    recipient.save()
                else:
                    recipient = RevenueShareRecipient.objects.create(**item, is_active=True)
                active_ids.append(recipient.id)
            RevenueShareRecipient.objects.exclude(id__in=active_ids).update(is_active=False)

            log_action(
                actor=request.user,
                action="UPDATE_REVENUE_DISTRIBUTION",
                target="CPay revenue distribution",
                ip_address=request.META.get("REMOTE_ADDR"),
                details={
                    "enabled": enabled,
                    "threshold_amount": str(threshold),
                    "recipient_count": len(active_ids),
                    "allocation_total": str(allocation),
                },
            )

        return Response({"detail": "Revenue distribution configuration saved."})


class DistributionBalanceView(APIView):
    permission_classes = [IsSuperUser]
    throttle_scope = "admin_read"

    def post(self, request):
        if not CPayClient.payout_configured():
            return Response({"detail": "CPay payout wallet credentials are not configured."}, status=status.HTTP_409_CONFLICT)
        try:
            balance = CPayClient().get_available_usdt_balance()
        except PaymentProviderError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        RevenueDistributionConfig.objects.filter(pk=1).update(
            last_available_balance=balance,
            last_balance_checked_at=timezone.now(),
        )
        return Response({"available_balance": str(balance), "checked_at": timezone.now().isoformat()})


class DistributionRunView(APIView):
    permission_classes = [IsSuperUser]
    throttle_scope = "admin_2fa"

    def post(self, request):
        totp_error = _require_totp(request)
        if totp_error:
            return Response({"detail": totp_error}, status=status.HTTP_403_FORBIDDEN)
        if not settings.CPAY_LIVE_PAYOUTS_ENABLED:
            return Response({"detail": "Live CPay payouts are locked by the server environment."}, status=status.HTTP_409_CONFLICT)
        result = check_revenue_distribution.delay(force=True)
        log_action(
            actor=request.user,
            action="RUN_REVENUE_DISTRIBUTION",
            target="CPay revenue distribution",
            ip_address=request.META.get("REMOTE_ADDR"),
            details={"task_id": result.id},
        )
        return Response({"detail": "Distribution check queued.", "task_id": result.id}, status=status.HTTP_202_ACCEPTED)


class DistributionRetryView(APIView):
    permission_classes = [IsSuperUser]
    throttle_scope = "admin_2fa"

    def post(self, request, batch_id):
        totp_error = _require_totp(request)
        if totp_error:
            return Response({"detail": totp_error}, status=status.HTTP_403_FORBIDDEN)
        if not settings.CPAY_LIVE_PAYOUTS_ENABLED:
            return Response({"detail": "Live CPay payouts are locked by the server environment."}, status=status.HTTP_409_CONFLICT)

        try:
            available_balance = CPayClient().get_available_usdt_balance()
        except PaymentProviderError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        whole_balance = available_balance.quantize(Decimal("1"), rounding=ROUND_DOWN).quantize(
            Decimal("0.000001")
        )
        if whole_balance <= 0:
            return Response({"detail": "No whole USDT is available to retry."}, status=status.HTTP_409_CONFLICT)

        with transaction.atomic():
            try:
                batch = RevenueDistributionBatch.objects.select_for_update().get(
                    pk=batch_id,
                    status=RevenueDistributionBatch.Status.FAILED,
                )
            except RevenueDistributionBatch.DoesNotExist:
                return Response({"detail": "Failed distribution batch not found."}, status=status.HTTP_404_NOT_FOUND)
            payouts = list(batch.payouts.select_for_update().order_by("created_at"))
            has_submitted_transfer = any(
                payout.provider_transaction_id
                or payout.status in {
                    RevenueDistributionPayout.Status.SUBMITTED,
                    RevenueDistributionPayout.Status.COMPLETED,
                }
                for payout in payouts
            )
            if not has_submitted_transfer:
                batch.amount = whole_balance
                batch.balance_before = available_balance
                allocated = Decimal("0")
                for index, payout in enumerate(payouts):
                    if index == len(payouts) - 1:
                        payout.amount = whole_balance - allocated
                    else:
                        payout.amount = (
                            whole_balance * payout.percentage / Decimal("100")
                        ).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
                        allocated += payout.amount
                    payout.save(update_fields=["amount", "updated_at"])

            for payout in payouts:
                if payout.status != RevenueDistributionPayout.Status.FAILED:
                    continue
                # CPay can replay a rejected validation response for an
                # idempotency key even when no provider transaction exists.
                payout.provider_transaction_id = ""
                payout.idempotency_key = f"sharptoolz-revenue-{payout.id}-retry-{uuid.uuid4()}"
                payout.status = RevenueDistributionPayout.Status.PENDING
                payout.error_message = ""
                payout.save(update_fields=[
                    "provider_transaction_id", "idempotency_key", "status", "error_message", "updated_at",
                ])
            batch.status = RevenueDistributionBatch.Status.PREPARING
            batch.error_message = ""
            batch.save(update_fields=[
                "amount", "balance_before", "status", "error_message", "updated_at",
            ])
            transaction.on_commit(lambda: execute_revenue_distribution.delay(str(batch.id)))

        return Response({"detail": "Failed transfers queued for retry."}, status=status.HTTP_202_ACCEPTED)
