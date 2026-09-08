from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from eth_account import Account

from wallet.blockchain import (
    BSCWalletClient,
    PreparedTokenTransfer,
    VerifiedTokenTransfer,
    validate_transaction_hash,
)
from wallet.models import DirectBSCDepositAddress, OnChainDeposit, Transaction, Wallet
from wallet.provider_security import decrypt_payment_secret, encrypt_payment_secret
from wallet.providers import PaymentProviderError


class DepositClaimError(Exception):
    pass


@dataclass(frozen=True)
class DepositVerificationResult:
    transaction_id: str
    transaction_hash: str
    amount: Decimal
    confirmations: int
    required_confirmations: int
    confirmed: bool
    credited: bool


def _prepared_from_route(route, *, funding: bool) -> PreparedTokenTransfer:
    if funding:
        return PreparedTokenTransfer(
            transaction_hash=route.gas_funding_transaction_hash,
            signed_transaction=route.gas_funding_signed_transaction,
        )
    return PreparedTokenTransfer(
        transaction_hash=route.sweep_transaction_hash,
        signed_transaction=route.sweep_signed_transaction,
        amount=route.swept_amount,
    )


def sweep_direct_bsc_deposit(*, route_id, client=None) -> dict:
    """Idempotently fund sweep gas and collect a confirmed deposit into the treasury."""
    if not settings.BSC_LIVE_SWEEPS_ENABLED:
        return {"status": "disabled"}

    blockchain = client or BSCWalletClient()
    route = DirectBSCDepositAddress.objects.select_related("transaction").get(pk=route_id)
    if route.sweep_status == DirectBSCDepositAddress.SweepStatus.COMPLETED:
        return {
            "status": "completed",
            "transaction_hash": route.sweep_transaction_hash,
            "amount": str(route.swept_amount),
        }
    receipt = OnChainDeposit.objects.filter(transaction=route.transaction).first()
    if not receipt or receipt.status != OnChainDeposit.Status.CONFIRMED:
        return {"status": "deposit_not_confirmed"}

    if route.sweep_transaction_hash:
        status = blockchain.get_transfer_status(route.sweep_transaction_hash)
        if status.status == "pending":
            return {"status": "sweep_confirming", "confirmations": status.confirmations}
        if status.status == "failed":
            DirectBSCDepositAddress.objects.filter(pk=route.pk).update(
                sweep_status=DirectBSCDepositAddress.SweepStatus.FAILED,
                sweep_error="The on-chain USDT sweep failed.",
            )
            return {"status": "failed"}
        DirectBSCDepositAddress.objects.filter(pk=route.pk).update(
            sweep_status=DirectBSCDepositAddress.SweepStatus.COMPLETED,
            sweep_error="",
            swept_at=timezone.now(),
        )
        return {
            "status": "completed",
            "transaction_hash": route.sweep_transaction_hash,
            "amount": str(route.swept_amount),
        }

    if route.gas_funding_transaction_hash:
        funding_status = blockchain.get_transfer_status(route.gas_funding_transaction_hash)
        if funding_status.status == "pending":
            return {"status": "gas_confirming", "confirmations": funding_status.confirmations}
        if funding_status.status == "failed":
            DirectBSCDepositAddress.objects.filter(pk=route.pk).update(
                sweep_status=DirectBSCDepositAddress.SweepStatus.FAILED,
                sweep_error="The on-chain BNB gas-funding transfer failed.",
            )
            return {"status": "failed"}

    private_key = decrypt_payment_secret(route.encrypted_private_key)
    prepared_sweep = blockchain.prepare_usdt_sweep(
        private_key=private_key,
        source_address=route.address,
    )
    current_bnb = blockchain.get_native_balance_wei(route.address)
    target_bnb = (
        prepared_sweep.gas_cost_wei
        * settings.BSC_SWEEP_GAS_FUNDING_MULTIPLIER_PERCENT
        // 100
    )

    if current_bnb < prepared_sweep.gas_cost_wei:
        funding_amount = target_bnb - current_bnb
        with transaction.atomic():
            locked = DirectBSCDepositAddress.objects.select_for_update().get(pk=route.pk)
            if not locked.gas_funding_signed_transaction:
                funding = blockchain.prepare_native_transfer(
                    to=locked.address,
                    amount_wei=funding_amount,
                )
                locked.gas_funding_transaction_hash = funding.transaction_hash
                locked.gas_funding_signed_transaction = funding.signed_transaction
                locked.sweep_status = DirectBSCDepositAddress.SweepStatus.FUNDING
                locked.sweep_error = ""
                locked.save(
                    update_fields=[
                        "gas_funding_transaction_hash",
                        "gas_funding_signed_transaction",
                        "sweep_status",
                        "sweep_error",
                        "updated_at",
                    ]
                )
            funding = _prepared_from_route(locked, funding=True)
        blockchain.broadcast_prepared_transfer(funding)
        return {
            "status": "gas_submitted",
            "transaction_hash": funding.transaction_hash,
        }

    with transaction.atomic():
        locked = DirectBSCDepositAddress.objects.select_for_update().get(pk=route.pk)
        if not locked.sweep_signed_transaction:
            locked.sweep_transaction_hash = prepared_sweep.transaction_hash
            locked.sweep_signed_transaction = prepared_sweep.signed_transaction
            locked.swept_amount = prepared_sweep.amount
            locked.sweep_status = DirectBSCDepositAddress.SweepStatus.SWEEPING
            locked.sweep_error = ""
            locked.save(
                update_fields=[
                    "sweep_transaction_hash",
                    "sweep_signed_transaction",
                    "swept_amount",
                    "sweep_status",
                    "sweep_error",
                    "updated_at",
                ]
            )
        prepared_sweep = _prepared_from_route(locked, funding=False)
    blockchain.broadcast_prepared_transfer(prepared_sweep)
    return {
        "status": "sweep_submitted",
        "transaction_hash": prepared_sweep.transaction_hash,
        "amount": str(prepared_sweep.amount),
    }


def create_direct_bsc_deposit_address(*, payment: Transaction, client=None) -> DirectBSCDepositAddress:
    """Create a unique recoverable on-chain address for one payment request."""
    existing = DirectBSCDepositAddress.objects.filter(transaction=payment).first()
    if existing:
        return existing

    blockchain = client or BSCWalletClient()
    latest_block = blockchain.latest_block_number()
    account = Account.create()
    address = account.address
    encrypted_key = encrypt_payment_secret(account.key.hex())
    start_block = max(0, latest_block - settings.BSC_DEPOSIT_LOOKBACK_BLOCKS)
    try:
        route = DirectBSCDepositAddress.objects.create(
            transaction=payment,
            address=address,
            encrypted_private_key=encrypted_key,
            start_block=start_block,
            last_scanned_block=start_block,
        )
        from wallet.tasks import register_bsc_deposit_address

        transaction.on_commit(lambda: register_bsc_deposit_address.delay(str(route.id)))
        return route
    except IntegrityError:
        return DirectBSCDepositAddress.objects.get(transaction=payment)


def verify_and_credit_onchain_deposit(
    *,
    transaction_id,
    user_id,
    transaction_hash: str,
    client=None,
) -> DepositVerificationResult:
    """Claim one real USDT transfer and credit it exactly once after confirmation."""
    try:
        normalized_hash = validate_transaction_hash(transaction_hash)
    except ValueError as exc:
        raise DepositClaimError(str(exc)) from exc

    try:
        requested_tx = Transaction.objects.select_related("wallet__user").get(
            pk=transaction_id,
            wallet__user_id=user_id,
            type=Transaction.Type.DEPOSIT,
        )
    except Transaction.DoesNotExist as exc:
        raise DepositClaimError("Deposit request not found.") from exc

    existing_receipt = OnChainDeposit.objects.filter(transaction=requested_tx).first()
    if existing_receipt and existing_receipt.transaction_hash != normalized_hash:
        raise DepositClaimError("This deposit request is already linked to another transaction hash.")
    if existing_receipt and existing_receipt.status == OnChainDeposit.Status.CONFIRMED:
        return _result(existing_receipt, confirmed=True, credited=False)

    blockchain = client or BSCWalletClient()
    transfer = blockchain.verify_usdt_deposit(
        normalized_hash,
        recipient_address=requested_tx.address or settings.BSC_GATEWAY_WALLET_ADDRESS,
    )
    return _record_verified_transfer(requested_tx, transfer)


def scan_direct_bsc_deposit(*, route_id, client=None) -> DepositVerificationResult | None:
    """Scan Alchemy logs for a payment's unique address and claim its first USDT transfer."""
    route = DirectBSCDepositAddress.objects.select_related(
        "transaction__wallet__user"
    ).get(pk=route_id)
    payment = route.transaction

    existing = OnChainDeposit.objects.filter(transaction=payment).first()
    if existing:
        return verify_and_credit_onchain_deposit(
            transaction_id=payment.id,
            user_id=payment.wallet.user_id,
            transaction_hash=existing.transaction_hash,
            client=client,
        )
    if payment.status != Transaction.Status.PENDING:
        return None

    blockchain = client or BSCWalletClient()
    latest_block = blockchain.latest_block_number()
    cursor = max(route.start_block, route.last_scanned_block)
    while cursor < latest_block:
        from_block = cursor + 1
        to_block = min(latest_block, from_block + settings.BSC_LOG_BLOCK_RANGE - 1)
        hashes = blockchain.find_usdt_deposit_hashes(
            recipient_address=route.address,
            from_block=from_block,
            to_block=to_block,
        )
        DirectBSCDepositAddress.objects.filter(pk=route.pk).update(last_scanned_block=to_block)
        route.last_scanned_block = to_block
        cursor = to_block

        for transaction_hash in hashes:
            if OnChainDeposit.objects.filter(transaction_hash=transaction_hash).exists():
                continue
            result = verify_and_credit_onchain_deposit(
                transaction_id=payment.id,
                user_id=payment.wallet.user_id,
                transaction_hash=transaction_hash,
                client=blockchain,
            )
            DirectBSCDepositAddress.objects.filter(pk=route.pk).update(detected_at=timezone.now())
            return result
    return None


def _record_verified_transfer(
    requested_tx: Transaction,
    transfer: VerifiedTokenTransfer,
) -> DepositVerificationResult:
    with transaction.atomic():
        locked_tx = Transaction.objects.select_for_update().select_related("wallet__user").get(
            pk=requested_tx.pk
        )
        claimed_elsewhere = OnChainDeposit.objects.select_for_update().filter(
            transaction_hash=transfer.transaction_hash
        ).first()
        if claimed_elsewhere and claimed_elsewhere.transaction_id != locked_tx.id:
            raise DepositClaimError("This blockchain transaction has already funded another wallet.")

        linked_receipt = OnChainDeposit.objects.select_for_update().filter(transaction=locked_tx).first()
        if linked_receipt and linked_receipt.transaction_hash != transfer.transaction_hash:
            raise DepositClaimError("This deposit request is already linked to another transaction hash.")

        if linked_receipt is None:
            try:
                linked_receipt = OnChainDeposit.objects.create(
                    transaction=locked_tx,
                    transaction_hash=transfer.transaction_hash,
                    chain_id=settings.BSC_CHAIN_ID,
                    token_contract=settings.BSC_USDT_CONTRACT_ADDRESS,
                    sender_address=transfer.sender_address,
                    recipient_address=transfer.recipient_address,
                    amount=transfer.amount,
                    block_number=transfer.block_number,
                    confirmations=transfer.confirmations,
                )
            except IntegrityError as exc:
                raise DepositClaimError(
                    "This blockchain transaction has already been claimed."
                ) from exc

        linked_receipt.sender_address = transfer.sender_address
        linked_receipt.recipient_address = transfer.recipient_address
        linked_receipt.amount = transfer.amount
        linked_receipt.block_number = transfer.block_number
        linked_receipt.confirmations = transfer.confirmations
        linked_receipt.status = (
            OnChainDeposit.Status.CONFIRMED
            if transfer.confirmed
            else OnChainDeposit.Status.PENDING
        )
        locked_tx.tx_hash = transfer.transaction_hash
        locked_tx.address = transfer.recipient_address

        if not transfer.confirmed:
            locked_tx.description = "USDT deposit awaiting blockchain confirmations"
            locked_tx.save(update_fields=["tx_hash", "address", "description"])
            linked_receipt.save()
            _queue_recheck(linked_receipt.id)
            return _result(linked_receipt, confirmed=False, credited=False)

        if locked_tx.status == Transaction.Status.FAILED:
            raise DepositClaimError("This deposit request was cancelled before payment verification.")
        if locked_tx.status == Transaction.Status.COMPLETED:
            linked_receipt.save()
            return _result(linked_receipt, confirmed=True, credited=False)

        credited_amount = transfer.amount.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if credited_amount <= 0:
            raise DepositClaimError("The confirmed deposit is below the minimum creditable amount.")

        wallet = Wallet.objects.select_for_update().get(pk=locked_tx.wallet_id)
        wallet.credit(credited_amount, create_transaction=False)
        locked_tx.amount = credited_amount
        locked_tx.status = Transaction.Status.COMPLETED
        locked_tx.description = "USDT BEP20 deposit"
        locked_tx.save(update_fields=["amount", "status", "description", "tx_hash", "address"])

        linked_receipt.credited_amount = credited_amount
        linked_receipt.save()

        # Preserve the existing promotion and referral behavior for direct deposits.
        from wallet.views import _apply_deposit_rewards

        _apply_deposit_rewards(wallet, credited_amount, locked_tx)
        transaction.on_commit(lambda: _after_credit(wallet.user, linked_receipt.id))
        return _result(linked_receipt, confirmed=True, credited=True)


def _result(receipt, *, confirmed: bool, credited: bool) -> DepositVerificationResult:
    return DepositVerificationResult(
        transaction_id=str(receipt.transaction_id),
        transaction_hash=receipt.transaction_hash,
        amount=receipt.amount,
        confirmations=receipt.confirmations,
        required_confirmations=settings.BSC_REQUIRED_CONFIRMATIONS,
        confirmed=confirmed,
        credited=credited,
    )


def _queue_recheck(receipt_id):
    from wallet.tasks import reconcile_onchain_deposit

    transaction.on_commit(
        lambda: reconcile_onchain_deposit.apply_async(args=[str(receipt_id)], countdown=30)
    )


def _after_credit(user, receipt_id):
    from wallet.tasks import check_revenue_distribution, sweep_bsc_deposit
    from wallet.views import send_wallet_update

    send_wallet_update(user, True)
    route_id = DirectBSCDepositAddress.objects.filter(
        transaction__onchain_deposit__id=receipt_id
    ).values_list("id", flat=True).first()
    if route_id:
        sweep_bsc_deposit.apply_async(args=[str(route_id)], countdown=5)
    check_revenue_distribution.apply_async(countdown=30)
