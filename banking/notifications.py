import logging
import secrets
import threading
import time
import uuid

import requests
from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.html import strip_tags

from api.models import SiteSettings
from api.utils.integration_secrets import get_integration_secret

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com"
CHALLENGE_TTL_SECONDS = 10 * 60
GRANT_TTL_SECONDS = 30 * 60
MAX_CODE_ATTEMPTS = 5
SIGNING_SALT = "fernway-account-email-verification"


class BankingEmailError(Exception):
    pass


class BankingVerificationError(Exception):
    pass


def _format_usd_cents(cents: int) -> str:
    return f"${abs(int(cents)) / 100:,.2f}"


def _format_timestamp(dt=None) -> str:
    now = timezone.localtime(dt or timezone.now())
    return now.strftime("%b %d, %Y at %I:%M %p %Z").strip()


def _send_resend_email(*, from_email: str, recipient: str, subject: str, html: str, text: str, tags=None, idempotency_key=None):
    api_key = get_integration_secret("resend_api_key")
    if not api_key:
        raise BankingEmailError("Resend is not configured.")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = str(idempotency_key)[:256]
    payload = {
        "from": from_email,
        "to": [recipient],
        "subject": subject,
        "html": html,
        "text": text,
    }
    if tags:
        payload["tags"] = tags
    try:
        response = requests.post(
            f"{RESEND_API_URL}/emails",
            headers=headers,
            json=payload,
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.exception("Resend request failed for Fernway email to %s", recipient)
        raise BankingEmailError("Could not deliver email via Resend.") from exc
    if not data.get("id"):
        raise BankingEmailError("Resend did not return an email ID.")
    return data["id"]


def _dispatch_best_effort(fn):
    """Run synchronously during tests for determinism, and non-blocking in live requests."""
    if getattr(settings, "TESTING", False):
        try:
            fn()
        except Exception:
            logger.exception("Banking notification dispatch failed")
        return

    def _runner():
        try:
            fn()
        except Exception:
            logger.exception("Background banking notification dispatch failed")

    threading.Thread(target=_runner, daemon=True).start()


# ==============================================================================
# 4-Digit Email Verification (for Fernway account email in SharpToolz Settings)
# ==============================================================================

def _verification_cache_key(challenge_id: str) -> str:
    return f"fernway-email-verification:{challenge_id}"


def _code_digest(challenge_id: str, code: str) -> str:
    return salted_hmac("fernway-email-code", f"{challenge_id}:{code}").hexdigest()


def mask_email(email: str) -> str:
    local, _, domain = email.strip().lower().partition("@")
    if not domain:
        return email
    prefix = local[:2] if len(local) >= 2 else local[:1]
    return f"{prefix}{'*' * max(1, len(local) - len(prefix))}@{domain}"


def request_fernway_email_verification(*, user_id: int, email: str) -> str:
    normalized = email.strip().lower()
    if not normalized or "@" not in normalized:
        raise BankingVerificationError("Enter a valid email address.")

    challenge_id = secrets.token_urlsafe(24)
    code = f"{secrets.randbelow(10_000):04d}"
    challenge = {
        "user_id": int(user_id),
        "email": normalized,
        "code_digest": _code_digest(challenge_id, code),
        "attempts": 0,
        "expires_at": int(time.time()) + CHALLENGE_TTL_SECONDS,
    }
    cache.set(_verification_cache_key(challenge_id), challenge, timeout=CHALLENGE_TTL_SECONDS)

    html = render_to_string(
        "emails/banking/fernway_verification_code.html",
        {
            "code": code,
            "current_year": timezone.now().year,
        },
    )
    text = (
        f"Your Fernway email verification code is {code}. "
        "Enter this 4-digit code in SharpToolz to verify this email for Fernway notifications. "
        "It expires in 10 minutes."
    )
    try:
        _send_resend_email(
            from_email=settings.FERNWAY_SECURITY_FROM_EMAIL,
            recipient=normalized,
            subject=f"{code} is your Fernway verification code",
            html=html,
            text=text,
            tags=[
                {"name": "product", "value": "fernway"},
                {"name": "purpose", "value": "email_verification"},
            ],
            idempotency_key=f"fernway-verify-{challenge_id}",
        )
    except Exception:
        cache.delete(_verification_cache_key(challenge_id))
        raise

    return challenge_id


def confirm_fernway_email_verification(*, user_id: int, challenge_id: str, code: str) -> tuple[str, dict]:
    key = _verification_cache_key(challenge_id)
    challenge = cache.get(key)
    if not challenge or int(challenge.get("user_id", -1)) != int(user_id):
        raise BankingVerificationError("This verification code has expired. Request a new one.")

    if challenge["attempts"] >= MAX_CODE_ATTEMPTS:
        cache.delete(key)
        raise BankingVerificationError("Too many incorrect attempts. Request a new code.")

    cleaned_code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(cleaned_code) != 4 or not constant_time_compare(
        challenge["code_digest"], _code_digest(challenge_id, cleaned_code)
    ):
        challenge["attempts"] += 1
        remaining = max(1, challenge["expires_at"] - int(time.time()))
        cache.set(key, challenge, timeout=remaining)
        raise BankingVerificationError("The verification code is incorrect.")

    cache.delete(key)
    payload = {
        "user_id": int(user_id),
        "email": challenge["email"],
    }
    token = signing.dumps(payload, salt=SIGNING_SALT, compress=True)
    return token, payload


def read_fernway_email_verification_grant(*, token: str, expected_user_id: int, expected_email: str) -> dict:
    if not token:
        raise BankingVerificationError("Verify this email address with a 4-digit code before saving.")
    try:
        payload = signing.loads(token, salt=SIGNING_SALT, max_age=GRANT_TTL_SECONDS)
    except signing.SignatureExpired as exc:
        raise BankingVerificationError("Email verification expired. Request a new 4-digit code.") from exc
    except signing.BadSignature as exc:
        raise BankingVerificationError("Email verification is invalid. Verify this email again.") from exc

    if not isinstance(payload, dict):
        raise BankingVerificationError("Email verification is invalid.")
    if int(payload.get("user_id", -1)) != int(expected_user_id):
        raise BankingVerificationError("Email verification does not belong to this account.")
    if str(payload.get("email", "")).strip().lower() != expected_email.strip().lower():
        raise BankingVerificationError("Email address changed after verification. Verify the new email.")
    return payload


def _owner_verification_cache_key(challenge_id: str) -> str:
    return f"sharptoolz-owner-email-verification:{challenge_id}"


def _owner_code_digest(challenge_id: str, code: str) -> str:
    return salted_hmac("sharptoolz-owner-email-code", f"{challenge_id}:{code}").hexdigest()


def request_owner_email_verification(*, user) -> str:
    email = (getattr(user, "email", "") or "").strip().lower()
    if not email or "@" not in email:
        raise BankingVerificationError("Your SharpToolz account does not have a valid email address.")

    challenge_id = secrets.token_urlsafe(24)
    code = f"{secrets.randbelow(10_000):04d}"
    challenge = {
        "user_id": int(user.id),
        "email": email,
        "code_digest": _owner_code_digest(challenge_id, code),
        "attempts": 0,
        "expires_at": int(time.time()) + CHALLENGE_TTL_SECONDS,
    }
    cache.set(_owner_verification_cache_key(challenge_id), challenge, timeout=CHALLENGE_TTL_SECONDS)

    try:
        site_settings = SiteSettings.get_settings()
    except Exception:
        site_settings = None

    frontend_url = (getattr(settings, "FRONTEND_URL", "") or "https://sharptoolz.com").rstrip("/")
    html = render_to_string(
        "emails/banking/owner_verification_code.html",
        {
            "code": code,
            "current_year": timezone.now().year,
            "support_email": (getattr(site_settings, "support_email", None) or "support@sharptoolz.com"),
            "site_settings": site_settings,
            "frontend_url": frontend_url,
        },
    )
    text = (
        f"Your SharpToolz email verification code is {code}. "
        "Enter this 4-digit code in SharpToolz to verify your account email for Online Banking alerts. "
        "It expires in 10 minutes."
    )
    from_email = (
        getattr(settings, "SHARPTOOLZ_NOTIFICATIONS_FROM_EMAIL", "")
        or settings.DEFAULT_FROM_EMAIL
        or "SharpToolz <notifications@sharptoolz.com>"
    )
    try:
        _send_resend_email(
            from_email=from_email,
            recipient=email,
            subject=f"{code} is your SharpToolz verification code",
            html=html,
            text=text,
            tags=[
                {"name": "product", "value": "sharptoolz_banking"},
                {"name": "purpose", "value": "owner_email_verification"},
            ],
            idempotency_key=f"stz-owner-verify-{challenge_id}",
        )
    except Exception:
        cache.delete(_owner_verification_cache_key(challenge_id))
        raise

    return challenge_id


def confirm_owner_email_verification(*, user, challenge_id: str, code: str) -> str:
    key = _owner_verification_cache_key(challenge_id)
    challenge = cache.get(key)
    if not challenge or int(challenge.get("user_id", -1)) != int(user.id):
        raise BankingVerificationError("This verification code has expired. Request a new one.")

    if challenge["attempts"] >= MAX_CODE_ATTEMPTS:
        cache.delete(key)
        raise BankingVerificationError("Too many incorrect attempts. Request a new code.")

    cleaned_code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(cleaned_code) != 4 or not constant_time_compare(
        challenge["code_digest"], _owner_code_digest(challenge_id, cleaned_code)
    ):
        challenge["attempts"] += 1
        remaining = max(1, challenge["expires_at"] - int(time.time()))
        cache.set(key, challenge, timeout=remaining)
        raise BankingVerificationError("The verification code is incorrect.")

    current_email = (getattr(user, "email", "") or "").strip().lower()
    if challenge["email"] != current_email:
        cache.delete(key)
        raise BankingVerificationError("Your SharpToolz account email changed. Request a new code.")

    cache.delete(key)
    user.email_verified = True
    user.email_verified_at = timezone.now()
    user.save(update_fields=["email_verified", "email_verified_at"])
    return current_email


# ==============================================================================
# Dual-Branded Notification Senders (SharpToolz Owner + Verified Fernway Email)
# ==============================================================================

def _send_owner_email(
    *,
    profile,
    subject: str,
    badge: str,
    title: str,
    subtitle: str,
    details: list[dict],
    action_path: str = "",
    action_label: str = "",
    event_tag: str = "activity",
):
    owner = getattr(profile, "user", None)
    owner_email = (getattr(owner, "email", "") or "").strip()
    if not owner_email or not get_integration_secret("resend_api_key"):
        return

    frontend_url = (getattr(settings, "FRONTEND_URL", "") or "https://sharptoolz.com").rstrip("/")
    action_url = f"{frontend_url}{action_path}" if action_path else ""
    try:
        site_settings = SiteSettings.get_settings()
    except Exception:
        site_settings = None

    context = {
        "badge": badge,
        "title": title,
        "subtitle": subtitle,
        "details": details,
        "action_url": action_url,
        "action_label": action_label,
        "current_year": timezone.now().year,
        "support_email": (getattr(site_settings, "support_email", None) or "support@sharptoolz.com"),
        "site_settings": site_settings,
        "frontend_url": frontend_url,
    }
    html = render_to_string("emails/banking/owner_activity.html", context)
    text_lines = [title, subtitle, ""] + [f"{row['label']}: {row['value']}" for row in details]
    if action_url:
        text_lines += ["", f"{action_label}: {action_url}"]
    text = "\n".join(line for line in text_lines if line is not None)

    from_email = getattr(settings, "SHARPTOOLZ_NOTIFICATIONS_FROM_EMAIL", "") or settings.DEFAULT_FROM_EMAIL or "SharpToolz <notifications@sharptoolz.com>"
    _send_resend_email(
        from_email=from_email,
        recipient=owner_email,
        subject=subject,
        html=html,
        text=text,
        tags=[
            {"name": "product", "value": "sharptoolz_banking"},
            {"name": "event", "value": event_tag},
        ],
        idempotency_key=f"stz-owner-{event_tag}-{uuid.uuid4().hex}",
    )


def _send_fernway_customer_email(
    *,
    profile,
    subject: str,
    badge: str,
    title: str,
    intro: str,
    details: list[dict],
    footer_note: str = "",
    event_tag: str = "alert",
):
    recipient = (getattr(profile, "email", "") or "").strip()
    if not getattr(profile, "email_verified", False) or not recipient:
        return
    if not get_integration_secret("resend_api_key"):
        return

    context = {
        "badge": badge,
        "title": title,
        "intro": intro,
        "details": details,
        "footer_note": footer_note,
        "current_year": timezone.now().year,
    }
    html = render_to_string("emails/banking/fernway_alert.html", context)
    text_lines = [f"Fernway — {title}", intro, ""] + [f"{row['label']}: {row['value']}" for row in details]
    if footer_note:
        text_lines += ["", footer_note]
    text = strip_tags("\n".join(text_lines))

    _send_resend_email(
        from_email=settings.FERNWAY_SECURITY_FROM_EMAIL,
        recipient=recipient,
        subject=subject,
        html=html,
        text=text,
        tags=[
            {"name": "product", "value": "fernway"},
            {"name": "event", "value": event_tag},
        ],
        idempotency_key=f"fw-alert-{event_tag}-{uuid.uuid4().hex}",
    )


def notify_login(profile, *, ip_address: str = "", user_agent: str = ""):
    holder = f"{profile.first_name} {profile.last_name}".strip() or profile.email
    when = _format_timestamp()
    ip_label = ip_address or "Unknown IP"
    device_label = (user_agent or "Web browser")[:120]

    owner_details = [
        {"label": "Account Holder", "value": holder},
        {"label": "Fernway Email", "value": profile.email, "mono": True, "highlight": True},
        {"label": "Time", "value": when},
        {"label": "IP Address", "value": ip_label, "mono": True},
        {"label": "Device", "value": device_label},
    ]
    customer_details = [
        {"label": "Account", "value": holder},
        {"label": "Sign-In Email", "value": profile.email, "mono": True},
        {"label": "Time", "value": when},
        {"label": "IP Address", "value": ip_label, "mono": True},
    ]

    def _run():
        _send_owner_email(
            profile=profile,
            subject=f"Fernway Login Alert: {holder} signed in",
            badge="Fernway Activity",
            title="New sign-in to your Fernway account",
            subtitle=f"Someone signed in to {profile.email} on Fernway Online Banking.",
            details=owner_details,
            action_path=f"/online-banking/{profile.id}",
            action_label="Open Account in SharpToolz",
            event_tag="login",
        )
        _send_fernway_customer_email(
            profile=profile,
            subject="Security Alert: New sign-in to your Fernway account",
            badge="Sign-In Alert",
            title="New sign-in detected",
            intro=f"Hi {profile.first_name}, your Fernway Online Banking account was just signed in to.",
            details=customer_details,
            footer_note="If this was you, no action is needed. If you do not recognize this sign-in, contact Fernway Support immediately.",
            event_tag="login",
        )

    _dispatch_best_effort(_run)


def notify_transfer_requested(profile, tx):
    holder = f"{profile.first_name} {profile.last_name}".strip() or profile.email
    amount_str = _format_usd_cents(tx.amount)
    when = _format_timestamp(tx.occurred_at)
    acct_masked = f"••••{tx.recipient_account_number[-4:]}" if tx.recipient_account_number else "N/A"

    owner_details = [
        {"label": "Account", "value": f"{holder} ({profile.email})"},
        {"label": "Amount", "value": amount_str, "highlight": True, "mono": True},
        {"label": "Recipient", "value": tx.counterparty},
        {"label": "Bank", "value": tx.bank_name or tx.counterparty_detail or "External Bank"},
        {"label": "Account Number", "value": tx.recipient_account_number or "N/A", "mono": True},
        {"label": "Routing Number", "value": tx.recipient_routing_number or "N/A", "mono": True},
        {"label": "Reference", "value": tx.reference, "mono": True},
        {"label": "Submitted", "value": when},
    ]
    customer_details = [
        {"label": "Amount", "value": amount_str, "highlight": True, "mono": True},
        {"label": "Recipient", "value": tx.counterparty},
        {"label": "Destination Bank", "value": f"{tx.bank_name or 'External Bank'} ({acct_masked})"},
        {"label": "Reference", "value": tx.reference, "mono": True},
        {"label": "Status", "value": "Pending"},
        {"label": "Time", "value": when},
    ]

    def _run():
        _send_owner_email(
            profile=profile,
            subject=f"Withdrawal Request ({amount_str}) · {holder}",
            badge="Approval Needed",
            title=f"New transfer request for {amount_str}",
            subtitle="Review the recipient details and approve or decline & refund from SharpToolz.",
            details=owner_details,
            action_path=f"/online-banking/{profile.id}?tab=transactions",
            action_label="Review Transfer Request",
            event_tag="transfer_requested",
        )
        _send_fernway_customer_email(
            profile=profile,
            subject=f"Transfer Pending: {amount_str} to {tx.counterparty}",
            badge="Transfer Pending",
            title="Your transaction is in progress",
            intro=f"Hi {profile.first_name}, please wait while your transaction to {tx.counterparty} is being completed.",
            details=customer_details,
            footer_note="You will receive a confirmation email once your transaction is completed.",
            event_tag="transfer_requested",
        )

    _dispatch_best_effort(_run)


def notify_transfer_reviewed(profile, tx, *, decision: str):
    amount_str = _format_usd_cents(tx.amount)
    when = _format_timestamp()
    approved = decision == "approve"
    customer_details = [
        {"label": "Amount", "value": amount_str, "highlight": True, "mono": True},
        {"label": "Recipient", "value": tx.counterparty},
        {"label": "Bank", "value": tx.bank_name or tx.counterparty_detail or "External Bank"},
        {"label": "Reference", "value": tx.reference, "mono": True},
        {"label": "Status", "value": "Completed" if approved else "Declined & Refunded"},
        {"label": "Updated", "value": when},
    ]

    def _run():
        _send_fernway_customer_email(
            profile=profile,
            subject=(
                f"Transfer Completed: {amount_str} to {tx.counterparty}"
                if approved
                else f"Transfer Declined & Refunded: {amount_str}"
            ),
            badge="Transfer Completed" if approved else "Transfer Refunded",
            title="Your transfer has been completed" if approved else "Your transfer was declined",
            intro=(
                f"Hi {profile.first_name}, your outgoing transfer of {amount_str} to {tx.counterparty} has been approved and processed."
                if approved
                else f"Hi {profile.first_name}, your transfer request of {amount_str} to {tx.counterparty} could not be processed and the full amount has been refunded to your account balance."
            ),
            details=customer_details,
            footer_note="Open Fernway Online Banking anytime to review your updated balance and transaction history.",
            event_tag=f"transfer_{decision}",
        )

    _dispatch_best_effort(_run)


def notify_card_added(profile, card, *, actor: str = "customer"):
    holder = f"{profile.first_name} {profile.last_name}".strip() or profile.email
    limit_str = _format_usd_cents(card.monthly_limit)
    when = _format_timestamp()
    details = [
        {"label": "Card Label", "value": card.label},
        {"label": "Card Number", "value": f"{card.brand.upper()} •••• {card.last4}", "mono": True},
        {"label": "Expiry", "value": card.expiry, "mono": True},
        {"label": "Monthly Limit", "value": limit_str, "highlight": True, "mono": True},
        {"label": "Issued", "value": when},
    ]

    def _run():
        if actor == "customer":
            _send_owner_email(
                profile=profile,
                subject=f"New Card Added on Fernway · {holder}",
                badge="Card Activity",
                title=f"A new debit card was added ({card.label})",
                subtitle=f"A card ending in {card.last4} was issued inside Fernway for {profile.email}.",
                details=[{"label": "Account", "value": f"{holder} ({profile.email})"}] + details,
                action_path=f"/online-banking/{profile.id}",
                action_label="View Account in SharpToolz",
                event_tag="card_added",
            )
        _send_fernway_customer_email(
            profile=profile,
            subject=f"New Card Issued: {card.label} (•••• {card.last4})",
            badge="Card Issued",
            title="Your new Fernway debit card is ready",
            intro=f"Hi {profile.first_name}, a new physical debit card has been added to your Fernway account.",
            details=details,
            footer_note="You can freeze your card or adjust card controls anytime from the Cards screen.",
            event_tag="card_added",
        )

    _dispatch_best_effort(_run)


def notify_card_updated(profile, card, *, changes: list[str], actor: str = "customer"):
    if not changes:
        return
    holder = f"{profile.first_name} {profile.last_name}".strip() or profile.email
    summary = ", ".join(changes)
    when = _format_timestamp()
    details = [
        {"label": "Card", "value": f"{card.label} (•••• {card.last4})"},
        {"label": "Change", "value": summary, "highlight": True},
        {"label": "Card Status", "value": "Frozen" if card.is_frozen else "Active"},
        {"label": "Online Payments", "value": "Enabled" if card.online_payments else "Disabled"},
        {"label": "Updated", "value": when},
    ]

    def _run():
        if actor == "customer":
            _send_owner_email(
                profile=profile,
                subject=f"Card Updated on Fernway · {summary}",
                badge="Card Activity",
                title=f"Card settings changed ({card.last4})",
                subtitle=f"Card controls were updated inside Fernway for {profile.email}.",
                details=[{"label": "Account", "value": f"{holder} ({profile.email})"}] + details,
                action_path=f"/online-banking/{profile.id}",
                action_label="Open Account in SharpToolz",
                event_tag="card_updated",
            )
        _send_fernway_customer_email(
            profile=profile,
            subject=f"Fernway Card Alert: {summary}",
            badge="Card Security",
            title="Your card settings were updated",
            intro=f"Hi {profile.first_name}, a change was made to your Fernway debit card ending in {card.last4}.",
            details=details,
            footer_note="If you did not make this change, contact Fernway Support immediately.",
            event_tag="card_updated",
        )

    _dispatch_best_effort(_run)


def _fernway_portal_base_url() -> str:
    import os

    try:
        raw = (SiteSettings.get_settings().online_banking_url or "").strip().rstrip("/")
        if raw:
            return raw
    except Exception:
        pass
    env_url = (os.getenv("FERNWAY_APP_URL") or "").strip().rstrip("/")
    if env_url:
        return env_url
    domain = getattr(settings, "FERNWAY_SUPPORT_DOMAIN", "fernwaybank.com") or "fernwaybank.com"
    return f"https://{domain}"


def _fernway_portal_host() -> str:
    try:
        from urllib.parse import urlparse

        parsed = urlparse(_fernway_portal_base_url())
        if parsed.netloc:
            return parsed.netloc
    except Exception:
        pass
    return getattr(settings, "FERNWAY_SUPPORT_DOMAIN", "fernwaybank.com") or "fernwaybank.com"


def send_fernway_credentials_email(profile) -> str:
    """Email the Fernway online banking portal link & login credentials directly to the account email."""
    recipient = (getattr(profile, "email", "") or "").strip()
    if not recipient or "@" not in recipient:
        raise BankingEmailError("This banking account does not have a valid sign-in email.")

    portal_base = _fernway_portal_base_url()
    portal_host = _fernway_portal_host()
    sign_in_url = f"{portal_base}/sign-in"
    full_name = f"{profile.first_name} {profile.last_name}".strip() or "Account Holder"
    primary_account = profile.accounts.first()

    details = [
        {"label": "Account Holder", "value": full_name},
        {"label": "Portal Website", "value": portal_host, "mono": True, "highlight": True},
        {"label": "Sign-In Email", "value": recipient, "mono": True},
        {"label": "Password", "value": profile.password, "mono": True},
        {"label": "Transaction PIN", "value": profile.transaction_pin or "1234", "mono": True},
    ]
    if primary_account and getattr(primary_account, "account_number", None):
        details.append({"label": "Account Number", "value": primary_account.account_number, "mono": True})

    html = render_to_string(
        "emails/banking/fernway_credentials.html",
        {
            "first_name": profile.first_name or full_name,
            "details": details,
            "sign_in_url": sign_in_url,
            "portal_host": portal_host,
            "current_year": timezone.now().year,
        },
    )
    text_lines = [
        f"Hi {profile.first_name or full_name},",
        "",
        "Your Fernway online banking account is ready.",
        f"Sign-In Portal: {sign_in_url}",
        f"Sign-In Email: {recipient}",
        f"Password: {profile.password}",
        f"Transaction PIN: {profile.transaction_pin or '1234'}",
    ]
    if primary_account and getattr(primary_account, "account_number", None):
        text_lines.append(f"Account Number: {primary_account.account_number}")
    text = "\n".join(text_lines)

    email_id = _send_resend_email(
        from_email=settings.FERNWAY_SECURITY_FROM_EMAIL,
        recipient=recipient,
        subject="Your Fernway Online Banking Sign-In Details",
        html=html,
        text=text,
        tags=[
            {"name": "product", "value": "fernway"},
            {"name": "event", "value": "credentials_email"},
        ],
        idempotency_key=f"fernway-creds-{profile.id}-{uuid.uuid4().hex[:12]}",
    )
    profile.credentials_email_sent_at = timezone.now()
    profile.save(update_fields=["credentials_email_sent_at", "updated_at"])
    return email_id


def notify_transaction_posted(profile, tx):
    amount_str = _format_usd_cents(tx.amount)
    is_credit = tx.direction == "credit"
    sign_amount = f"+{amount_str}" if is_credit else f"-{amount_str}"
    when = _format_timestamp(tx.occurred_at)
    details = [
        {"label": "Amount", "value": sign_amount, "highlight": True, "mono": True},
        {"label": "Type", "value": "Deposit / Credit" if is_credit else "Debit / Expense"},
        {"label": "Counterparty", "value": tx.counterparty},
        {"label": "Description", "value": tx.narration or tx.counterparty_detail or "Account activity"},
        {"label": "Reference", "value": tx.reference, "mono": True},
        {"label": "Posted", "value": when},
    ]

    def _run():
        portal_host = _fernway_portal_host()
        _send_fernway_customer_email(
            profile=profile,
            subject=(
                f"Deposit Received: {sign_amount} from {tx.counterparty}"
                if is_credit
                else f"Debit Alert: {sign_amount} at {tx.counterparty}"
            ),
            badge="Credit Alert" if is_credit else "Debit Alert",
            title="Funds credited to your account" if is_credit else "New account debit posted",
            intro=(
                f"Hi {profile.first_name}, {amount_str} from {tx.counterparty} has been credited to your Fernway balance."
                if is_credit
                else f"Hi {profile.first_name}, a debit of {amount_str} ({tx.counterparty}) was posted to your Fernway account."
            ),
            details=details,
            footer_note=f"Sign in at {portal_host} to review your full statement.",
            event_tag="transaction_posted",
        )

    _dispatch_best_effort(_run)


def notify_account_updated(profile, *, changes: list[str]):
    if not changes:
        return
    when = _format_timestamp()
    details = [
        {"label": "Account", "value": f"{profile.first_name} {profile.last_name}".strip()},
        {"label": "Email", "value": profile.email, "mono": True},
        {"label": "Updates Applied", "value": ", ".join(changes), "highlight": True},
        {"label": "Time", "value": when},
    ]

    def _run():
        _send_fernway_customer_email(
            profile=profile,
            subject="Account Update: Changes saved to your Fernway profile",
            badge="Account Update",
            title="Your Fernway account was updated",
            intro=f"Hi {profile.first_name}, the following changes were saved to your Fernway Online Banking account.",
            details=details,
            footer_note="If you have questions about these updates, reach out via live support inside Fernway.",
            event_tag="account_updated",
        )

    _dispatch_best_effort(_run)
