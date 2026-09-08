import logging
import re
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
from eth_account import Account
from web3 import HTTPProvider, Web3
from web3.exceptions import TransactionNotFound
from web3.logs import DISCARD

from wallet.providers import PaymentProviderError, PayoutTransferStatus, validate_bep20_address


logger = logging.getLogger(__name__)

TRANSACTION_HASH_RE = re.compile(r"^0x[a-fA-F0-9]{64}$")

ERC20_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "from", "type": "address"},
            {"indexed": True, "name": "to", "type": "address"},
            {"indexed": False, "name": "value", "type": "uint256"},
        ],
        "name": "Transfer",
        "type": "event",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "recipient", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


@dataclass(frozen=True)
class VerifiedTokenTransfer:
    transaction_hash: str
    sender_address: str
    recipient_address: str
    amount: Decimal
    block_number: int
    confirmations: int
    confirmed: bool


@dataclass(frozen=True)
class PreparedTokenTransfer:
    transaction_hash: str
    signed_transaction: str
    gas_cost_wei: int = 0
    amount: Decimal = Decimal("0")


def validate_transaction_hash(value: str) -> str:
    tx_hash = str(value or "").strip()
    if not TRANSACTION_HASH_RE.fullmatch(tx_hash):
        raise ValueError("Enter a valid blockchain transaction hash.")
    return tx_hash.lower()


class BSCWalletClient:
    """Read and sign BEP-20 USDT transactions through a BSC JSON-RPC node."""

    def __init__(self, web3=None):
        if web3 is None:
            if not settings.BSC_RPC_URL:
                raise PaymentProviderError("BNB Chain RPC is not configured.")
            web3 = Web3(
                HTTPProvider(
                    settings.BSC_RPC_URL,
                    request_kwargs={"timeout": settings.BSC_RPC_TIMEOUT_SECONDS},
                )
            )
        self.web3 = web3
        try:
            token_address = Web3.to_checksum_address(settings.BSC_USDT_CONTRACT_ADDRESS)
        except ValueError as exc:
            raise PaymentProviderError("The configured BEP-20 USDT contract is invalid.") from exc
        self.contract = self.web3.eth.contract(address=token_address, abi=ERC20_ABI)

    @staticmethod
    def deposit_configured() -> bool:
        if not (
            settings.BSC_RPC_URL
            and settings.BSC_GATEWAY_WALLET_ADDRESS
            and settings.BSC_USDT_CONTRACT_ADDRESS
        ):
            return False
        try:
            validate_bep20_address(settings.BSC_GATEWAY_WALLET_ADDRESS)
            validate_bep20_address(settings.BSC_USDT_CONTRACT_ADDRESS)
        except ValueError:
            return False
        return True

    @staticmethod
    def payout_configured() -> bool:
        if not BSCWalletClient.deposit_configured() or not settings.BSC_GATEWAY_PRIVATE_KEY:
            return False
        try:
            account = Account.from_key(settings.BSC_GATEWAY_PRIVATE_KEY)
        except (TypeError, ValueError):
            return False
        return account.address.lower() == settings.BSC_GATEWAY_WALLET_ADDRESS.lower()

    def _assert_chain(self):
        try:
            chain_id = int(self.web3.eth.chain_id)
        except Exception as exc:
            raise PaymentProviderError("Could not connect to BNB Chain.") from exc
        if chain_id != settings.BSC_CHAIN_ID:
            raise PaymentProviderError("The configured RPC is connected to the wrong blockchain.")

    def _decimals(self) -> int:
        try:
            decimals = int(self.contract.functions.decimals().call())
        except Exception as exc:
            raise PaymentProviderError("Could not read the USDT token configuration.") from exc
        if not 0 <= decimals <= 36:
            raise PaymentProviderError("The configured USDT token returned invalid decimals.")
        return decimals

    def get_available_usdt_balance(self) -> Decimal:
        if not self.deposit_configured():
            raise PaymentProviderError("The direct BNB Chain gateway is not configured.")
        self._assert_chain()
        try:
            wallet_address = Web3.to_checksum_address(settings.BSC_GATEWAY_WALLET_ADDRESS)
            raw_balance = int(self.contract.functions.balanceOf(wallet_address).call())
        except Exception as exc:
            raise PaymentProviderError("Could not read the gateway USDT balance.") from exc
        return Decimal(raw_balance) / (Decimal(10) ** self._decimals())

    def get_usdt_balance(self, address: str) -> Decimal:
        self._assert_chain()
        try:
            account = Web3.to_checksum_address(validate_bep20_address(address))
            raw_balance = int(self.contract.functions.balanceOf(account).call())
        except ValueError as exc:
            raise PaymentProviderError(str(exc)) from exc
        except Exception as exc:
            raise PaymentProviderError("Could not read the deposit-address USDT balance.") from exc
        return Decimal(raw_balance) / (Decimal(10) ** self._decimals())

    def get_native_balance_wei(self, address: str) -> int:
        self._assert_chain()
        try:
            account = Web3.to_checksum_address(validate_bep20_address(address))
            return int(self.web3.eth.get_balance(account))
        except ValueError as exc:
            raise PaymentProviderError(str(exc)) from exc
        except Exception as exc:
            raise PaymentProviderError("Could not read the deposit-address BNB balance.") from exc

    def latest_block_number(self) -> int:
        self._assert_chain()
        try:
            return int(self.web3.eth.block_number)
        except Exception as exc:
            raise PaymentProviderError("Could not read the latest BNB Chain block.") from exc

    def find_usdt_deposit_hashes(
        self,
        *,
        recipient_address: str,
        from_block: int,
        to_block: int,
    ) -> list[str]:
        """Find USDT transfers to one address over a small, provider-safe block range."""
        self._assert_chain()
        try:
            recipient = Web3.to_checksum_address(validate_bep20_address(recipient_address))
        except ValueError as exc:
            raise PaymentProviderError(str(exc)) from exc
        if from_block < 0 or to_block < from_block:
            return []
        if to_block - from_block + 1 > settings.BSC_LOG_BLOCK_RANGE:
            raise PaymentProviderError(
                f"BNB Chain log queries may cover at most {settings.BSC_LOG_BLOCK_RANGE} blocks."
            )

        transfer_topic = Web3.keccak(text="Transfer(address,address,uint256)").hex()
        recipient_topic = "0x" + ("0" * 24) + recipient.removeprefix("0x").lower()
        try:
            logs = self.web3.eth.get_logs(
                {
                    "address": self.contract.address,
                    "fromBlock": from_block,
                    "toBlock": to_block,
                    "topics": [transfer_topic, None, recipient_topic],
                }
            )
        except Exception as exc:
            raise PaymentProviderError("Could not scan BNB Chain for incoming USDT.") from exc

        hashes = []
        for event in logs:
            value = event.get("transactionHash")
            tx_hash = value.hex() if hasattr(value, "hex") else str(value)
            if not tx_hash.startswith("0x"):
                tx_hash = f"0x{tx_hash}"
            normalized = validate_transaction_hash(tx_hash)
            if normalized not in hashes:
                hashes.append(normalized)
        return hashes

    def verify_usdt_deposit(
        self,
        transaction_hash: str,
        *,
        recipient_address: str | None = None,
    ) -> VerifiedTokenTransfer:
        if not self.deposit_configured():
            raise PaymentProviderError("The direct BNB Chain gateway is not configured.")
        self._assert_chain()
        try:
            normalized_hash = validate_transaction_hash(transaction_hash)
        except ValueError as exc:
            raise PaymentProviderError(str(exc)) from exc

        try:
            receipt = self.web3.eth.get_transaction_receipt(normalized_hash)
        except TransactionNotFound as exc:
            raise PaymentProviderError("Transaction is not visible on BNB Chain yet.") from exc
        except Exception as exc:
            raise PaymentProviderError("Could not verify the transaction on BNB Chain.") from exc

        if int(receipt["status"]) != 1:
            raise PaymentProviderError("The blockchain transaction failed.")

        expected_recipient = recipient_address or settings.BSC_GATEWAY_WALLET_ADDRESS
        try:
            expected_recipient = Web3.to_checksum_address(
                validate_bep20_address(expected_recipient)
            )
        except ValueError as exc:
            raise PaymentProviderError(str(exc)) from exc
        gateway_address = expected_recipient.lower()
        token_address = settings.BSC_USDT_CONTRACT_ADDRESS.lower()
        matching_transfers = []
        try:
            decoded_events = self.contract.events.Transfer().process_receipt(receipt, errors=DISCARD)
        except Exception as exc:
            raise PaymentProviderError("Could not decode the USDT transfer receipt.") from exc

        for event in decoded_events:
            if str(event["address"]).lower() != token_address:
                continue
            args = event["args"]
            if str(args["to"]).lower() == gateway_address:
                matching_transfers.append(args)
        if not matching_transfers:
            raise PaymentProviderError("This transaction did not send USDT to the assigned payment address.")

        raw_amount = sum(int(item["value"]) for item in matching_transfers)
        amount = Decimal(raw_amount) / (Decimal(10) ** self._decimals())
        if amount <= 0:
            raise PaymentProviderError("The USDT transfer amount must be greater than zero.")

        block_number = int(receipt["blockNumber"])
        try:
            current_block = int(self.web3.eth.block_number)
        except Exception as exc:
            raise PaymentProviderError("Could not read the latest BNB Chain block.") from exc
        confirmations = max(0, current_block - block_number + 1)
        sender = str(matching_transfers[0]["from"])
        return VerifiedTokenTransfer(
            transaction_hash=normalized_hash,
            sender_address=sender,
            recipient_address=expected_recipient,
            amount=amount,
            block_number=block_number,
            confirmations=confirmations,
            confirmed=confirmations >= settings.BSC_REQUIRED_CONFIRMATIONS,
        )

    def withdraw_usdt(self, *, to: str, amount: Decimal, idempotency_key: str) -> str:
        del idempotency_key  # The deterministic on-chain transaction hash is the receipt/idempotency record.
        prepared = self.prepare_usdt_transfer(to=to, amount=amount)
        return self.broadcast_prepared_transfer(prepared)

    @staticmethod
    def _transaction_hash(raw_transaction: bytes) -> str:
        value = Web3.keccak(raw_transaction).hex()
        return f"0x{value.removeprefix('0x')}".lower()

    def prepare_native_transfer(self, *, to: str, amount_wei: int) -> PreparedTokenTransfer:
        """Sign the small BNB transfer that pays a deposit address's sweep gas."""
        if not self.payout_configured():
            raise PaymentProviderError("The direct BNB Chain gateway wallet is not configured.")
        try:
            recipient = Web3.to_checksum_address(validate_bep20_address(to))
        except ValueError as exc:
            raise PaymentProviderError(str(exc)) from exc
        amount_wei = int(amount_wei)
        if amount_wei <= 0:
            raise PaymentProviderError("The BNB gas-funding amount must be greater than zero.")

        self._assert_chain()
        account = Account.from_key(settings.BSC_GATEWAY_PRIVATE_KEY)
        try:
            gas_limit = 21_000
            gas_price = int(self.web3.eth.gas_price)
            if int(self.web3.eth.get_balance(account.address)) < amount_wei + gas_limit * gas_price:
                raise PaymentProviderError("The gateway wallet does not have enough BNB for sweep gas.")
            transaction = {
                "from": account.address,
                "to": recipient,
                "value": amount_wei,
                "chainId": settings.BSC_CHAIN_ID,
                "nonce": self.web3.eth.get_transaction_count(account.address, "pending"),
                "gas": gas_limit,
                "gasPrice": gas_price,
            }
            signed = account.sign_transaction(transaction)
        except PaymentProviderError:
            raise
        except Exception as exc:
            logger.warning("Direct BSC gas-funding signing failed: %s", type(exc).__name__)
            raise PaymentProviderError("Sweep gas funding could not be prepared.") from exc
        return PreparedTokenTransfer(
            transaction_hash=self._transaction_hash(signed.raw_transaction),
            signed_transaction=signed.raw_transaction.hex(),
            gas_cost_wei=gas_limit * gas_price,
        )

    def prepare_usdt_sweep(
        self,
        *,
        private_key: str,
        source_address: str,
    ) -> PreparedTokenTransfer:
        """Sign a full-balance transfer from a unique deposit address to the treasury."""
        if not self.payout_configured():
            raise PaymentProviderError("The direct BNB Chain gateway wallet is not configured.")
        try:
            source = Web3.to_checksum_address(validate_bep20_address(source_address))
            destination = Web3.to_checksum_address(
                validate_bep20_address(settings.BSC_GATEWAY_WALLET_ADDRESS)
            )
            account = Account.from_key(private_key)
        except (TypeError, ValueError) as exc:
            raise PaymentProviderError("The deposit address credentials are invalid.") from exc
        if account.address.lower() != source.lower():
            raise PaymentProviderError("The stored key does not match the deposit address.")

        self._assert_chain()
        decimals = self._decimals()
        try:
            raw_amount = int(self.contract.functions.balanceOf(source).call())
            if raw_amount <= 0:
                raise PaymentProviderError("The deposit address has no USDT to sweep.")
            transfer = self.contract.functions.transfer(destination, raw_amount)
            estimated_gas = int(transfer.estimate_gas({"from": source}))
            gas_limit = max(
                estimated_gas,
                estimated_gas * settings.BSC_GAS_LIMIT_MULTIPLIER_PERCENT // 100,
            )
            gas_price = int(self.web3.eth.gas_price)
            transaction = transfer.build_transaction(
                {
                    "from": source,
                    "chainId": settings.BSC_CHAIN_ID,
                    "nonce": self.web3.eth.get_transaction_count(source, "pending"),
                    "gas": gas_limit,
                    "gasPrice": gas_price,
                }
            )
            signed = account.sign_transaction(transaction)
        except PaymentProviderError:
            raise
        except Exception as exc:
            logger.warning("Direct BSC sweep signing failed: %s", type(exc).__name__)
            raise PaymentProviderError("The USDT sweep could not be prepared.") from exc
        return PreparedTokenTransfer(
            transaction_hash=self._transaction_hash(signed.raw_transaction),
            signed_transaction=signed.raw_transaction.hex(),
            gas_cost_wei=gas_limit * gas_price,
            amount=Decimal(raw_amount) / (Decimal(10) ** decimals),
        )

    def prepare_usdt_transfer(self, *, to: str, amount: Decimal) -> PreparedTokenTransfer:
        """Sign a transfer without broadcasting it, so its hash can be persisted first."""
        if not settings.BSC_LIVE_PAYOUTS_ENABLED:
            raise PaymentProviderError("Live direct-wallet payouts are disabled in the environment.")
        if not self.payout_configured():
            raise PaymentProviderError("The direct BNB Chain payout wallet is not configured.")
        try:
            recipient = Web3.to_checksum_address(validate_bep20_address(to))
        except ValueError as exc:
            raise PaymentProviderError(str(exc)) from exc
        amount = Decimal(amount)
        if amount <= 0:
            raise PaymentProviderError("Payout amount must be greater than zero.")

        self._assert_chain()
        account = Account.from_key(settings.BSC_GATEWAY_PRIVATE_KEY)
        decimals = self._decimals()
        raw_amount = int(amount * (Decimal(10) ** decimals))
        if Decimal(raw_amount) / (Decimal(10) ** decimals) != amount:
            raise PaymentProviderError("Payout amount has more decimals than the USDT token supports.")

        transfer = self.contract.functions.transfer(recipient, raw_amount)
        try:
            nonce = self.web3.eth.get_transaction_count(account.address, "pending")
            estimated_gas = int(transfer.estimate_gas({"from": account.address}))
            gas_limit = max(
                estimated_gas,
                estimated_gas * settings.BSC_GAS_LIMIT_MULTIPLIER_PERCENT // 100,
            )
            transaction = transfer.build_transaction(
                {
                    "from": account.address,
                    "chainId": settings.BSC_CHAIN_ID,
                    "nonce": nonce,
                    "gas": gas_limit,
                    "gasPrice": int(self.web3.eth.gas_price),
                }
            )
            signed = account.sign_transaction(transaction)
            expected_hash = self._transaction_hash(signed.raw_transaction)
        except Exception as exc:
            logger.warning("Direct BSC payout signing failed: %s", type(exc).__name__)
            raise PaymentProviderError("The USDT payout could not be prepared for BNB Chain.") from exc
        return PreparedTokenTransfer(
            transaction_hash=expected_hash.lower(),
            signed_transaction=signed.raw_transaction.hex(),
        )

    def broadcast_prepared_transfer(self, prepared: PreparedTokenTransfer) -> str:
        """Broadcast exactly the signed transaction that was persisted by the caller."""
        try:
            raw_transaction = bytes.fromhex(prepared.signed_transaction.removeprefix("0x"))
        except ValueError as exc:
            raise PaymentProviderError("The stored signed payout transaction is invalid.") from exc
        expected_hash = self._transaction_hash(raw_transaction)
        if expected_hash != prepared.transaction_hash.lower():
            raise PaymentProviderError("The stored payout transaction hash does not match its signed data.")

        try:
            existing = self.web3.eth.get_transaction(expected_hash)
        except TransactionNotFound:
            existing = None
        except Exception as exc:
            raise PaymentProviderError("Could not check the prepared BNB Chain payout.") from exc
        if existing is not None:
            return expected_hash

        try:
            broadcast_hash = self.web3.eth.send_raw_transaction(raw_transaction).hex()
        except Exception as exc:
            # A timeout or "already known" response can happen after the node
            # accepted the bytes. Resolve that ambiguity by looking up the exact
            # deterministic hash before allowing a retry.
            try:
                if self.web3.eth.get_transaction(expected_hash) is not None:
                    return expected_hash
            except Exception:
                pass
            logger.warning("Direct BSC payout broadcast failed: %s", type(exc).__name__)
            raise PaymentProviderError("The USDT payout could not be submitted to BNB Chain.") from exc
        broadcast_hash = f"0x{broadcast_hash.removeprefix('0x')}".lower()
        if broadcast_hash != expected_hash:
            raise PaymentProviderError("BNB Chain returned an unexpected transaction hash.")
        return expected_hash

    def get_transfer_status(self, transaction_hash: str) -> PayoutTransferStatus:
        try:
            normalized_hash = validate_transaction_hash(transaction_hash)
        except ValueError as exc:
            raise PaymentProviderError(str(exc)) from exc
        self._assert_chain()
        try:
            receipt = self.web3.eth.get_transaction_receipt(normalized_hash)
        except TransactionNotFound:
            return PayoutTransferStatus(status="pending", transaction_hash=normalized_hash)
        except Exception as exc:
            raise PaymentProviderError("Could not reconcile the BNB Chain transaction.") from exc

        if int(receipt["status"]) != 1:
            return PayoutTransferStatus(status="failed", transaction_hash=normalized_hash)
        confirmations = max(0, int(self.web3.eth.block_number) - int(receipt["blockNumber"]) + 1)
        state = "completed" if confirmations >= settings.BSC_REQUIRED_CONFIRMATIONS else "pending"
        return PayoutTransferStatus(
            status=state,
            transaction_hash=normalized_hash,
            confirmations=confirmations,
        )
