import base64
import hashlib
import json
import re
import time

import jwt
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


class PaymentSecretError(Exception):
    pass


CPAY_WALLET_ID_RE = re.compile(r"^[a-fA-F0-9]{24}$")


def _fernet() -> Fernet:
    raw_key = settings.PAYMENT_ENCRYPTION_KEY.encode("utf-8")
    derived_key = hashlib.sha256(b"sharptoolz-payment-provider\0" + raw_key).digest()
    return Fernet(base64.urlsafe_b64encode(derived_key))


def encrypt_payment_secret(secret: str) -> str:
    if not secret:
        raise PaymentSecretError("Cannot encrypt an empty payment secret.")
    return _fernet().encrypt(secret.encode("utf-8")).decode("ascii")


def decrypt_payment_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise PaymentSecretError("Payment provider secret cannot be decrypted.") from exc


def _cryptojs_decrypt(ciphertext: str, passphrase: bytes) -> bytes:
    """Decrypt CryptoJS/OpenSSL salted AES-256-CBC used by CPay callbacks."""
    try:
        raw = base64.b64decode(ciphertext, validate=True)
    except (ValueError, TypeError) as exc:
        raise PaymentSecretError("CPay callback data is not valid base64.") from exc
    if len(raw) < 32 or raw[:8] != b"Salted__":
        raise PaymentSecretError("CPay callback data has an invalid envelope.")

    salt = raw[8:16]
    material = b""
    block = b""
    while len(material) < 48:
        block = hashlib.md5(block + passphrase + salt).digest()  # noqa: S324 - required CryptoJS KDF compatibility
        material += block

    decryptor = Cipher(
        algorithms.AES(material[:32]),
        modes.CBC(material[32:48]),
    ).decryptor()
    padded = decryptor.update(raw[16:]) + decryptor.finalize()
    try:
        unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError as exc:
        raise PaymentSecretError("CPay callback data could not be decrypted.") from exc


def decrypt_cpay_callback(authorization_header: str, encrypted_data: str) -> tuple[str, dict]:
    """
    Decode CPay's callback envelope.

    CPay does not publish a JWT verification key. The decoded claim is therefore
    used only to locate the stored client wallet; callers must re-fetch and
    verify the transaction with that wallet's CPay credentials before crediting.
    """
    if not authorization_header.startswith("Bearer "):
        raise PaymentSecretError("CPay callback authorization is missing.")
    if not isinstance(encrypted_data, str) or not encrypted_data or len(encrypted_data) > 1_000_000:
        raise PaymentSecretError("CPay callback data is missing or too large.")

    token = authorization_header[7:].strip()
    try:
        claims = jwt.decode(
            token,
            options={"verify_signature": False, "verify_exp": False},
            algorithms=["HS256"],
        )
    except jwt.PyJWTError as exc:
        raise PaymentSecretError("CPay callback token is invalid.") from exc

    wallet_id = str(claims.get("id") or "")
    encrypted_salt = claims.get("salt")
    try:
        expires_at = int(claims.get("exp"))
    except (TypeError, ValueError) as exc:
        raise PaymentSecretError("CPay callback token expiry is invalid.") from exc
    if not CPAY_WALLET_ID_RE.fullmatch(wallet_id) or not isinstance(encrypted_salt, str):
        raise PaymentSecretError("CPay callback token is incomplete.")
    if expires_at <= int(time.time()):
        raise PaymentSecretError("CPay callback token has expired.")

    final_salt = _cryptojs_decrypt(encrypted_salt, wallet_id.encode("utf-8"))
    decrypted = _cryptojs_decrypt(encrypted_data, final_salt)
    try:
        payload = json.loads(decrypted.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PaymentSecretError("CPay callback payload is invalid.") from exc
    if not isinstance(payload, dict):
        raise PaymentSecretError("CPay callback payload must be an object.")
    return wallet_id, payload
