import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

import pyotp
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from .models import AdminTwoFactorProfile


CHALLENGE_COOKIE_NAME = "admin_2fa_challenge"
CHALLENGE_COOKIE_PATH = "/api/accounts/two-factor/"
CHALLENGE_CACHE_PREFIX = "admin-2fa-challenge"
RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class TwoFactorChallengeError(Exception):
    pass


@dataclass(frozen=True)
class Challenge:
    token: str
    user_id: int
    attempts: int
    expires_at: int
    pending_secret: str | None = None


def is_privileged_user(user) -> bool:
    return bool(user and user.is_active and (user.is_staff or user.is_superuser))


def _fernet() -> Fernet:
    raw_key = settings.ADMIN_2FA_ENCRYPTION_KEY.encode("utf-8")
    derived_key = hashlib.sha256(b"sharptoolz-admin-2fa\0" + raw_key).digest()
    return Fernet(base64.urlsafe_b64encode(derived_key))


def encrypt_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode("ascii")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("ascii")
    except (InvalidToken, ValueError) as exc:
        raise TwoFactorChallengeError("Two-factor configuration is unavailable.") from exc


def profile_for_user(user) -> AdminTwoFactorProfile | None:
    try:
        return user.admin_two_factor
    except AdminTwoFactorProfile.DoesNotExist:
        return None


def is_enabled_for_user(user) -> bool:
    return profile_for_user(user) is not None


def _cache_key(token: str) -> str:
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    return f"{CHALLENGE_CACHE_PREFIX}:{digest}"


def _user_agent_hash(request) -> str:
    user_agent = request.META.get("HTTP_USER_AGENT", "")
    return hashlib.sha256(user_agent.encode("utf-8", errors="ignore")).hexdigest()


def _challenge_timeout(expires_at: int) -> int:
    return max(1, expires_at - int(time.time()))


def create_challenge(user, request) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = int(time.time()) + settings.ADMIN_2FA_CHALLENGE_SECONDS
    cache.set(
        _cache_key(token),
        {
            "user_id": user.pk,
            "attempts": 0,
            "expires_at": expires_at,
            "user_agent_hash": _user_agent_hash(request),
            "pending_secret": None,
        },
        timeout=settings.ADMIN_2FA_CHALLENGE_SECONDS,
    )
    return token


def load_challenge(request) -> Challenge:
    token = request.COOKIES.get(CHALLENGE_COOKIE_NAME, "")
    if not token:
        raise TwoFactorChallengeError("Your admin verification session has expired. Sign in again.")

    payload = cache.get(_cache_key(token))
    if not payload or payload.get("expires_at", 0) <= int(time.time()):
        cache.delete(_cache_key(token))
        raise TwoFactorChallengeError("Your admin verification session has expired. Sign in again.")

    if not hmac.compare_digest(payload.get("user_agent_hash", ""), _user_agent_hash(request)):
        cache.delete(_cache_key(token))
        raise TwoFactorChallengeError("Your admin verification session is invalid. Sign in again.")

    return Challenge(
        token=token,
        user_id=int(payload["user_id"]),
        attempts=int(payload.get("attempts", 0)),
        expires_at=int(payload["expires_at"]),
        pending_secret=payload.get("pending_secret"),
    )


def _update_challenge(challenge: Challenge, **changes) -> Challenge:
    existing = cache.get(_cache_key(challenge.token)) or {}
    payload = {
        "user_id": challenge.user_id,
        "attempts": challenge.attempts,
        "expires_at": challenge.expires_at,
        "pending_secret": challenge.pending_secret,
        "user_agent_hash": existing.get("user_agent_hash", ""),
    }
    payload.update(changes)
    cache.set(
        _cache_key(challenge.token),
        payload,
        timeout=_challenge_timeout(challenge.expires_at),
    )
    return Challenge(
        token=challenge.token,
        user_id=challenge.user_id,
        attempts=int(payload["attempts"]),
        expires_at=challenge.expires_at,
        pending_secret=payload.get("pending_secret"),
    )


def ensure_pending_secret(challenge: Challenge) -> tuple[Challenge, str]:
    if challenge.pending_secret:
        return challenge, decrypt_secret(challenge.pending_secret)
    secret = pyotp.random_base32(length=32)
    encrypted = encrypt_secret(secret)
    return _update_challenge(challenge, pending_secret=encrypted), secret


def register_failed_attempt(challenge: Challenge) -> None:
    attempts = challenge.attempts + 1
    if attempts >= settings.ADMIN_2FA_MAX_ATTEMPTS:
        consume_challenge(challenge)
        raise TwoFactorChallengeError("Too many incorrect codes. Sign in again.")
    _update_challenge(challenge, attempts=attempts)


def consume_challenge(challenge: Challenge) -> None:
    cache.delete(_cache_key(challenge.token))


def set_challenge_cookie(response, token: str) -> None:
    response.set_cookie(
        CHALLENGE_COOKIE_NAME,
        token,
        max_age=settings.ADMIN_2FA_CHALLENGE_SECONDS,
        secure=settings.JWT_COOKIE_SECURE,
        httponly=True,
        samesite=settings.JWT_COOKIE_SAMESITE,
        path=CHALLENGE_COOKIE_PATH,
    )


def clear_challenge_cookie(response) -> None:
    response.delete_cookie(
        CHALLENGE_COOKIE_NAME,
        path=CHALLENGE_COOKIE_PATH,
        samesite=settings.JWT_COOKIE_SAMESITE,
    )


def provisioning_uri(user, secret: str) -> str:
    account_name = user.email or user.username
    return pyotp.TOTP(secret).provisioning_uri(
        name=account_name,
        issuer_name=settings.ADMIN_2FA_ISSUER,
    )


def _matching_counter(secret: str, code: str) -> int | None:
    normalized = "".join(character for character in str(code) if character.isdigit())
    if len(normalized) != 6:
        return None

    totp = pyotp.TOTP(secret)
    current_counter = int(time.time()) // totp.interval
    for offset in (-1, 0, 1):
        counter = current_counter + offset
        if counter >= 0 and hmac.compare_digest(totp.at(counter * totp.interval), normalized):
            return counter
    return None


def _new_recovery_codes() -> list[str]:
    codes = []
    for _ in range(8):
        raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def enroll_user(user, secret: str, code: str) -> list[str] | None:
    counter = _matching_counter(secret, code)
    if counter is None:
        return None

    recovery_codes = _new_recovery_codes()
    AdminTwoFactorProfile.objects.update_or_create(
        user=user,
        defaults={
            "encrypted_secret": encrypt_secret(secret),
            "recovery_code_hashes": [make_password(item.replace("-", "")) for item in recovery_codes],
            "confirmed_at": timezone.now(),
            "last_used_counter": counter,
        },
    )
    return recovery_codes


def _accept_totp_code(profile: AdminTwoFactorProfile, code: str) -> bool:
    secret = decrypt_secret(profile.encrypted_secret)
    counter = _matching_counter(secret, code)
    if counter is None or counter <= profile.last_used_counter:
        return False

    profile.last_used_counter = counter
    profile.save(update_fields=["last_used_counter", "updated_at"])
    return True


@transaction.atomic
def verify_totp_code(user, code: str) -> bool:
    """Verify a fresh authenticator code without accepting recovery codes."""
    try:
        profile = AdminTwoFactorProfile.objects.select_for_update().get(user=user)
    except AdminTwoFactorProfile.DoesNotExist:
        return False

    return _accept_totp_code(profile, code)


@transaction.atomic
def verify_user_code(user, code: str) -> bool:
    try:
        profile = AdminTwoFactorProfile.objects.select_for_update().get(user=user)
    except AdminTwoFactorProfile.DoesNotExist:
        return False

    if _accept_totp_code(profile, code):
        return True

    normalized_recovery = str(code).strip().upper().replace("-", "")
    if len(normalized_recovery) != 10:
        return False
    for index, hashed_code in enumerate(profile.recovery_code_hashes):
        if check_password(normalized_recovery, hashed_code):
            hashes = list(profile.recovery_code_hashes)
            hashes.pop(index)
            profile.recovery_code_hashes = hashes
            profile.save(update_fields=["recovery_code_hashes", "updated_at"])
            return True
    return False
