import base64
import logging
import re
from decimal import Decimal

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from django.conf import settings


BEP20_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
logger = logging.getLogger(__name__)

CRYPTAPI_PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC3FT0Ym8b3myVxhQW7ESuuu6lo
dGAsUJs4fq+Ey//jm27jQ7HHHDmP1YJO7XE7Jf/0DTEJgcw4EZhJFVwsk6d3+4fy
Bsn0tKeyGMiaE6cVkX0cy6Y85o8zgc/CwZKc0uw6d5siAo++xl2zl+RGMXCELQVE
ox7pp208zTvown577wIDAQAB
-----END PUBLIC KEY-----"""


class PaymentProviderError(Exception):
    pass


def validate_bep20_address(value: str) -> str:
    address = str(value or "").strip()
    if not BEP20_ADDRESS_RE.fullmatch(address):
        raise ValueError("Enter a valid BEP20 address (0x followed by 40 hexadecimal characters).")
    return address


def verify_cryptapi_signature(raw_body: bytes, signature: str) -> bool:
    if not signature:
        return False
    try:
        public_key = serialization.load_pem_public_key(CRYPTAPI_PUBLIC_KEY)
        public_key.verify(
            base64.b64decode(signature, validate=True),
            raw_body,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


class CryptAPIClient:
    def __init__(self, session=None):
        self.session = session or requests.Session()

    def create_address(self, *, destination_address: str, callback_url: str) -> dict:
        validate_bep20_address(destination_address)
        url = f"{settings.CRYPTAPI_BASE_URL.rstrip('/')}/{settings.CRYPTAPI_TICKER}/create/"
        try:
            response = self.session.get(
                url,
                params={
                    "callback": callback_url,
                    "address": destination_address,
                    "confirmations": "1",
                    "pending": "0",
                    "post": "1",
                    "json": "1",
                    "priority": "default",
                },
                timeout=settings.CRYPTAPI_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise PaymentProviderError("CryptAPI could not create a payment address.") from exc

        if payload.get("status") not in {None, "success"}:
            raise PaymentProviderError("CryptAPI rejected the payment-address request.")
        address_in = payload.get("address_in")
        address_out = payload.get("address_out")
        if not address_in or address_out != destination_address:
            raise PaymentProviderError("CryptAPI returned an invalid payment route.")
        return payload


class CPayClient:
    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.base_url = settings.CPAY_BASE_URL.rstrip("/")

    @staticmethod
    def deposit_configured() -> bool:
        return bool(
            settings.CPAY_PUBLIC_KEY
            and settings.CPAY_PRIVATE_KEY
            and settings.CPAY_BEP20_USDT_CURRENCY_ID
        )

    @staticmethod
    def payout_configured() -> bool:
        return bool(
            CPayClient.deposit_configured()
            and settings.CPAY_PAYOUT_WALLET_ID
            and settings.CPAY_PAYOUT_WALLET_PASSPHRASE
        )

    def _request(self, method: str, path: str, *, token=None, **kwargs) -> dict:
        headers = dict(kwargs.pop("headers", {}) or {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                timeout=settings.CPAY_TIMEOUT_SECONDS,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise PaymentProviderError("CPay request failed.") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise PaymentProviderError("CPay returned an invalid response.") from exc

        if not response.ok:
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            safe_message = payload.get("message") or data.get("message")
            logger.warning(
                "CPay rejected %s %s with HTTP %s: %s",
                method,
                path,
                response.status_code,
                safe_message,
            )
            raise PaymentProviderError("CPay rejected the request.")
        if payload.get("status") == "fail":
            raise PaymentProviderError("CPay rejected the request.")
        return payload

    def _authenticate(self, *, wallet=False) -> str:
        if not self.deposit_configured():
            raise PaymentProviderError("CPay account credentials are not configured.")
        body = {
            "publicKey": settings.CPAY_PUBLIC_KEY,
            "privateKey": settings.CPAY_PRIVATE_KEY,
        }
        if wallet:
            if not self.payout_configured():
                raise PaymentProviderError("CPay payout wallet credentials are not configured.")
            return self._authenticate_wallet(
                wallet_id=settings.CPAY_PAYOUT_WALLET_ID,
                passphrase=settings.CPAY_PAYOUT_WALLET_PASSPHRASE,
            )
        payload = self._request("POST", "/api/public/auth", json=body)
        token = payload.get("token") or (payload.get("data") or {}).get("token")
        if not token:
            raise PaymentProviderError("CPay authentication returned no token.")
        return token

    def _authenticate_wallet(self, *, wallet_id: str, passphrase: str) -> str:
        if not self.deposit_configured() or not wallet_id or not passphrase:
            raise PaymentProviderError("CPay wallet credentials are not configured.")
        payload = self._request(
            "POST",
            "/api/public/auth",
            json={
                "publicKey": settings.CPAY_PUBLIC_KEY,
                "privateKey": settings.CPAY_PRIVATE_KEY,
                "walletId": wallet_id,
                "passphrase": passphrase,
            },
        )
        token = payload.get("token") or (payload.get("data") or {}).get("token")
        if not token:
            raise PaymentProviderError("CPay wallet authentication returned no token.")
        return token

    def create_client_wallet(self) -> dict:
        token = self._authenticate()
        payload = self._request(
            "POST",
            f"/api/public/wallet/{settings.CPAY_BEP20_USDT_CURRENCY_ID}",
            token=token,
            json={
                "typeWallet": "user",
                "setMain": False,
                "walletVersion": "v2",
            },
        )
        data = payload.get("data") or {}
        wallet_id = data.get("id") or data.get("_id")
        address = data.get("address")
        passphrase = data.get("passphrase")
        if not wallet_id or not passphrase:
            raise PaymentProviderError("CPay returned incomplete wallet credentials.")
        try:
            validate_bep20_address(address)
        except ValueError as exc:
            raise PaymentProviderError("CPay returned an invalid BEP20 address.") from exc
        return {"id": str(wallet_id), "address": address, "passphrase": passphrase}

    def get_available_usdt_balance(self) -> Decimal:
        token = self._authenticate(wallet=True)
        payload = self._request("GET", "/api/public/wallet", token=token)
        data = payload.get("data") or {}
        currency_id = settings.CPAY_BEP20_USDT_CURRENCY_ID

        # Current CPay v2 responses group every network currency under
        # ``balances`` and nest value/hold inside ``balance``.
        for item in data.get("balances") or []:
            currency = item.get("currency") or {}
            if str(currency.get("id") or currency.get("_id")) == currency_id:
                balance = item.get("balance") or {}
                value = Decimal(str(balance.get("value") or "0"))
                hold = Decimal(str(balance.get("hold") or "0"))
                return max(Decimal("0"), value - hold)

        # Keep compatibility with the older documented response shape.
        for item in data.get("tokens") or []:
            if str(item.get("currencyId")) == currency_id:
                balance = Decimal(str(item.get("balance") or "0"))
                hold = Decimal(str(item.get("holdBalance") or "0"))
                return max(Decimal("0"), balance - hold)
        raise PaymentProviderError("The configured BEP20 USDT token was not found in the CPay wallet balance.")

    def withdraw_usdt(self, *, to: str, amount: Decimal, idempotency_key: str) -> str:
        validate_bep20_address(to)
        if not settings.CPAY_LIVE_PAYOUTS_ENABLED:
            raise PaymentProviderError("Live CPay payouts are disabled in the environment.")
        token = self._authenticate(wallet=True)
        body = {
            "to": to,
            # CPay's live API validates monetary amounts as decimal strings.
            # Sending a JSON number is rejected before a transaction is created.
            "amount": format(amount, "f"),
            "currencyToken": settings.CPAY_BEP20_USDT_CURRENCY_ID,
        }
        if settings.CPAY_PAYOUT_WALLET_PASSWORD:
            body["password"] = settings.CPAY_PAYOUT_WALLET_PASSWORD
        if settings.CPAY_PAYOUT_SIGNATURE:
            body["sign"] = settings.CPAY_PAYOUT_SIGNATURE
        payload = self._request(
            "POST",
            "/api/public/withdrawal",
            token=token,
            headers={"Idempotency-Key": idempotency_key},
            json=body,
        )
        transaction_id = (payload.get("data") or {}).get("id")
        if not transaction_id:
            raise PaymentProviderError("CPay accepted no payout transaction.")
        return str(transaction_id)

    def find_transaction(
        self,
        transaction_id: str,
        *,
        wallet_id: str | None = None,
        passphrase: str | None = None,
    ) -> dict | None:
        if wallet_id is not None or passphrase is not None:
            if not wallet_id or not passphrase:
                raise PaymentProviderError("Both CPay wallet ID and passphrase are required.")
            token = self._authenticate_wallet(wallet_id=wallet_id, passphrase=passphrase)
        else:
            token = self._authenticate(wallet=True)
        payload = self._request(
            "GET",
            "/api/public/transaction/list",
            token=token,
            params={"search": transaction_id, "page": 1, "limit": 10, "order": "DESC"},
        )
        target_id = str(transaction_id)
        for entity in (payload.get("data") or {}).get("entities") or []:
            if str(entity.get("_id") or entity.get("id") or "") == target_id:
                return entity

        # CPay currently returns no entities when its `search` parameter is an
        # exact Withdrawal ID, even though the same transaction is present in
        # the unfiltered list. Reconciliation must still compare exact IDs; it
        # must never infer a match from the result ordering alone.
        page_size = 50
        for page in range(1, 6):
            fallback = self._request(
                "GET",
                "/api/public/transaction/list",
                token=token,
                params={"page": page, "limit": page_size, "order": "DESC"},
            )
            entities = (fallback.get("data") or {}).get("entities") or []
            for entity in entities:
                if str(entity.get("_id") or entity.get("id") or "") == target_id:
                    return entity
            if len(entities) < page_size:
                break
        return None
