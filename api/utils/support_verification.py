import secrets
import time

from django.core import signing
from django.core.cache import cache
from django.utils.crypto import constant_time_compare, salted_hmac

from .support_email import send_support_verification_code

CHALLENGE_TTL_SECONDS = 10 * 60
GRANT_TTL_SECONDS = 30 * 60
MAX_CODE_ATTEMPTS = 5
SIGNING_SALT = "tracking-support-email-verification"


class SupportVerificationError(Exception):
    pass


def _cache_key(challenge_id):
    return f"support-email-verification:{challenge_id}"


def _code_digest(challenge_id, code):
    return salted_hmac(
        "tracking-support-email-code",
        f"{challenge_id}:{code}",
    ).hexdigest()


def request_email_verification(*, tracking_id, source, email):
    challenge_id = secrets.token_urlsafe(24)
    code = f"{secrets.randbelow(10_000):04d}"
    challenge = {
        "tracking_id": tracking_id,
        "source": source,
        "email": email.strip().lower(),
        "code_digest": _code_digest(challenge_id, code),
        "attempts": 0,
        "expires_at": int(time.time()) + CHALLENGE_TTL_SECONDS,
    }
    cache.set(_cache_key(challenge_id), challenge, timeout=CHALLENGE_TTL_SECONDS)
    try:
        send_support_verification_code(
            source=source,
            recipient=challenge["email"],
            tracking_id=tracking_id,
            code=code,
            challenge_id=challenge_id,
        )
    except Exception:
        cache.delete(_cache_key(challenge_id))
        raise
    return challenge_id


def confirm_email_verification(*, challenge_id, code):
    key = _cache_key(challenge_id)
    challenge = cache.get(key)
    if not challenge:
        raise SupportVerificationError("This verification code has expired. Request a new one.")

    if challenge["attempts"] >= MAX_CODE_ATTEMPTS:
        cache.delete(key)
        raise SupportVerificationError("Too many incorrect attempts. Request a new code.")

    if not constant_time_compare(challenge["code_digest"], _code_digest(challenge_id, code.strip())):
        challenge["attempts"] += 1
        remaining = max(1, challenge["expires_at"] - int(time.time()))
        cache.set(key, challenge, timeout=remaining)
        raise SupportVerificationError("The verification code is incorrect.")

    cache.delete(key)
    payload = {
        "tracking_id": challenge["tracking_id"],
        "source": challenge["source"],
        "email": challenge["email"],
    }
    return signing.dumps(payload, salt=SIGNING_SALT, compress=True), payload


def read_email_verification_grant(token):
    try:
        payload = signing.loads(
            token,
            salt=SIGNING_SALT,
            max_age=GRANT_TTL_SECONDS,
        )
    except signing.SignatureExpired as exc:
        raise SupportVerificationError("Email verification has expired. Verify your email again.") from exc
    except signing.BadSignature as exc:
        raise SupportVerificationError("Email verification is invalid.") from exc

    required = {"tracking_id", "source", "email"}
    if not isinstance(payload, dict) or not required.issubset(payload):
        raise SupportVerificationError("Email verification is invalid.")
    return payload
