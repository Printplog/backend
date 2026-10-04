import base64
import hashlib
import hmac
import logging
import re
import time
import uuid
from email.utils import parseaddr

import requests
from django.conf import settings
from django.utils.html import escape, strip_tags

from api.models import TrackingSupportMessage, TrackingSupportReply
from api.utils.integration_secrets import get_integration_secret

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com"
REPLY_ADDRESS_RE = re.compile(r"^support\+([0-9a-f]{32})@([^@]+)$", re.IGNORECASE)


class SupportEmailError(Exception):
    pass


def _brand(ticket):
    if ticket.source == TrackingSupportMessage.Source.PARCEL_FINDA:
        return {
            "name": "ParcelFinda",
            "from": settings.PARCEL_SUPPORT_FROM_EMAIL,
            "domain": settings.PARCEL_SUPPORT_DOMAIN.lower(),
        }
    return {
        "name": "MyFlightLookup",
        "from": settings.FLIGHT_SUPPORT_FROM_EMAIL,
        "domain": settings.FLIGHT_SUPPORT_DOMAIN.lower(),
    }


def reply_address(ticket):
    return f"support+{ticket.id.hex}@{_brand(ticket)['domain']}"


def _email_html(title, body, ticket):
    safe_body = escape(body).replace("\n", "<br>")
    brand = _brand(ticket)
    return (
        '<div style="background:#f6f7f4;padding:32px 16px;font-family:Arial,sans-serif;color:#17231f">'
        '<div style="max-width:600px;margin:0 auto;background:#fff;border:1px solid #e4e8e5;padding:32px">'
        f'<p style="margin:0 0 20px;font-size:13px;color:#64716c">{escape(brand["name"])} support</p>'
        f'<h1 style="margin:0 0 20px;font-size:22px">{escape(title)}</h1>'
        f'<div style="font-size:15px;line-height:1.7">{safe_body}</div>'
        '<hr style="margin:28px 0;border:0;border-top:1px solid #e4e8e5">'
        f'<p style="margin:0;font-size:12px;color:#7b8581">Tracking ID: {escape(ticket.tracking_id)} &middot; '
        f'Ticket: {str(ticket.id)[:8]}</p>'
        '<p style="margin:10px 0 0;font-size:12px;color:#7b8581">Reply to this email to continue the conversation.</p>'
        '</div></div>'
    )


def _resend_request(method, path, *, json=None, idempotency_key=None):
    api_key = get_integration_secret("resend_api_key")
    if not api_key:
        raise SupportEmailError("Resend is not configured.")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key[:256]
    try:
        response = requests.request(
            method,
            f"{RESEND_API_URL}{path}",
            headers=headers,
            json=json,
            timeout=15,
        )
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.exception("Resend request failed for %s %s", method, path)
        raise SupportEmailError("The support email could not be delivered.") from exc


def _send(ticket, *, recipient, title, body, idempotency_key):
    payload = {
        "from": _brand(ticket)["from"],
        "to": [recipient],
        "reply_to": reply_address(ticket),
        "subject": f"Re: {ticket.subject} [Ticket {str(ticket.id)[:8]}]",
        "text": body,
        "html": _email_html(title, body, ticket),
        "tags": [
            {"name": "support_source", "value": ticket.source},
            {"name": "ticket", "value": ticket.id.hex},
        ],
    }
    result = _resend_request("POST", "/emails", json=payload, idempotency_key=idempotency_key)
    email_id = result.get("id")
    if not email_id:
        raise SupportEmailError("Resend did not return an email ID.")
    return email_id


def notify_owner_of_new_ticket(ticket):
    owner_email = ticket.document.buyer.email
    if not owner_email:
        logger.warning("Support ticket %s owner has no email address", ticket.id)
        return None
    return _send(
        ticket,
        recipient=owner_email,
        title=f"New message from {ticket.customer_name}",
        body=ticket.message,
        idempotency_key=f"support-new-{ticket.id.hex}",
    )


def send_owner_reply(ticket, body, *, idempotency_key):
    return _send(
        ticket,
        recipient=ticket.customer_email,
        title=f"Reply from {_brand(ticket)['name']} support",
        body=body,
        idempotency_key=idempotency_key,
    )


def notify_owner_of_customer_reply(ticket, body, *, idempotency_key):
    owner_email = ticket.document.buyer.email
    if not owner_email:
        raise SupportEmailError("The document owner has no email address.")
    return _send(
        ticket,
        recipient=owner_email,
        title=f"New reply from {ticket.customer_name}",
        body=body,
        idempotency_key=idempotency_key,
    )


def verify_webhook(payload, headers, *, tolerance_seconds=300):
    secret = get_integration_secret("resend_webhook_secret")
    message_id = headers.get("svix-id", "")
    timestamp = headers.get("svix-timestamp", "")
    signatures = headers.get("svix-signature", "")
    if not secret or not message_id or not timestamp or not signatures:
        raise SupportEmailError("Missing webhook verification data.")
    try:
        timestamp_int = int(timestamp)
    except ValueError as exc:
        raise SupportEmailError("Invalid webhook timestamp.") from exc
    if abs(int(time.time()) - timestamp_int) > tolerance_seconds:
        raise SupportEmailError("Expired webhook timestamp.")

    encoded_secret = secret[6:] if secret.startswith("whsec_") else secret
    try:
        secret_bytes = base64.b64decode(encoded_secret)
    except ValueError as exc:
        raise SupportEmailError("Invalid webhook secret.") from exc
    signed = message_id.encode() + b"." + timestamp.encode() + b"." + payload
    expected = base64.b64encode(hmac.new(secret_bytes, signed, hashlib.sha256).digest()).decode()
    candidates = [part[3:] for part in signatures.split() if part.startswith("v1,")]
    if not any(hmac.compare_digest(expected, candidate) for candidate in candidates):
        raise SupportEmailError("Invalid webhook signature.")


def _ticket_from_recipients(recipients):
    for raw_recipient in recipients or []:
        address = parseaddr(raw_recipient)[1].lower()
        match = REPLY_ADDRESS_RE.match(address)
        if not match:
            continue
        try:
            ticket_id = uuid.UUID(hex=match.group(1))
        except ValueError:
            continue
        ticket = TrackingSupportMessage.objects.select_related("document__buyer").filter(id=ticket_id).first()
        if ticket and match.group(2).lower() == _brand(ticket)["domain"]:
            return ticket
    return None


def process_inbound_email(data):
    resend_email_id = data.get("email_id", "")
    if not resend_email_id:
        raise SupportEmailError("Inbound email ID is missing.")
    if TrackingSupportReply.objects.filter(external_message_id=resend_email_id).exists():
        return "duplicate"

    ticket = _ticket_from_recipients(data.get("to"))
    if ticket is None:
        return "ignored"

    received = _resend_request("GET", f"/emails/receiving/{resend_email_id}")
    body = (received.get("text") or strip_tags(received.get("html") or "")).strip()
    if not body:
        body = "(This reply did not contain readable text.)"
    body = body[:10000]
    sender = parseaddr(received.get("from") or data.get("from") or "")[1].lower()
    owner_email = (ticket.document.buyer.email or "").lower()
    customer_email = ticket.customer_email.lower()

    if sender == customer_email:
        notify_owner_of_customer_reply(
            ticket,
            body,
            idempotency_key=f"support-inbound-owner-{resend_email_id}",
        )
        TrackingSupportReply.objects.create(
            support_message=ticket,
            direction=TrackingSupportReply.Direction.CUSTOMER,
            body=body,
            sender_email=sender,
            delivery_status=TrackingSupportReply.DeliveryStatus.RECEIVED,
            external_message_id=resend_email_id,
        )
        ticket.status = TrackingSupportMessage.Status.NEW
        ticket.save(update_fields=["status", "updated_at"])
        return "customer_reply"

    if sender == owner_email:
        outgoing_id = send_owner_reply(
            ticket,
            body,
            idempotency_key=f"support-inbound-customer-{resend_email_id}",
        )
        TrackingSupportReply.objects.create(
            support_message=ticket,
            direction=TrackingSupportReply.Direction.OWNER,
            body=body,
            sender_email=sender,
            delivery_status=TrackingSupportReply.DeliveryStatus.QUEUED,
            external_message_id=resend_email_id,
            resend_email_id=outgoing_id,
        )
        if ticket.status == TrackingSupportMessage.Status.NEW:
            ticket.status = TrackingSupportMessage.Status.READ
        ticket.save(update_fields=["status", "updated_at"])
        return "owner_reply"

    logger.warning("Ignoring support reply for ticket %s from an unknown sender", ticket.id)
    return "ignored_sender"


def update_delivery_status(event_type, data):
    email_id = data.get("email_id")
    if not email_id:
        return False
    status_map = {
        "email.sent": TrackingSupportReply.DeliveryStatus.SENT,
        "email.delivered": TrackingSupportReply.DeliveryStatus.DELIVERED,
        "email.delivery_delayed": TrackingSupportReply.DeliveryStatus.DELAYED,
        "email.bounced": TrackingSupportReply.DeliveryStatus.BOUNCED,
        "email.failed": TrackingSupportReply.DeliveryStatus.FAILED,
        "email.suppressed": TrackingSupportReply.DeliveryStatus.SUPPRESSED,
        "email.complained": TrackingSupportReply.DeliveryStatus.COMPLAINED,
    }
    delivery_status = status_map.get(event_type)
    if not delivery_status:
        return False
    return bool(TrackingSupportReply.objects.filter(resend_email_id=email_id).update(delivery_status=delivery_status))
