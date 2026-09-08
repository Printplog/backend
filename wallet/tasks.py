import logging
import uuid
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN

from celery import shared_task
from celery.exceptions import Retry
from django.conf import settings
from django.db import close_old_connections, transaction
from django.db.utils import OperationalError
from django.db.models import F
from django.utils import timezone
from wallet.models import (
    DepositBonus,
    DirectBSCDepositAddress,
    OnChainDeposit,
    RevenueDistributionBatch,
    RevenueDistributionConfig,
    RevenueDistributionPayout,
    RevenueShareRecipient,
    Transaction,
    Wallet,
)
from wallet.providers import (
    CPayClient,
    PaymentProviderError,
    direct_bsc_enabled,
    gateway_label,
    get_payout_provider,
    live_payouts_enabled,
    payout_provider_configured,
)


logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    name="wallet.tasks.register_bsc_deposit_address",
    autoretry_for=(PaymentProviderError,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=5,
)
def register_bsc_deposit_address(self, route_id: str):
    """Subscribe a generated address to the production Alchemy webhook."""
    try:
        route = DirectBSCDepositAddress.objects.get(pk=route_id)
    except DirectBSCDepositAddress.DoesNotExist:
        return {"status": "missing"}
    from wallet.alchemy import address_registration_configured, register_webhook_address

    if not address_registration_configured():
        return {"status": "not_configured"}
    register_webhook_address(route.address)
    return {"status": "registered", "address": route.address}


@shared_task(name="wallet.tasks.scan_pending_bsc_deposits")
def scan_pending_bsc_deposits():
    """Discover incoming USDT for active direct-payment addresses."""
    close_old_connections()
    scanned = 0
    detected = 0
    errors = 0
    try:
        route_ids = list(
            DirectBSCDepositAddress.objects.filter(
                transaction__gateway="direct_bsc",
                transaction__status=Transaction.Status.PENDING,
            ).values_list("id", flat=True)[:250]
        )
        from wallet.deposits import DepositClaimError, scan_direct_bsc_deposit

        for route_id in route_ids:
            try:
                result = scan_direct_bsc_deposit(route_id=route_id)
                scanned += 1
                if result:
                    detected += 1
            except (PaymentProviderError, DepositClaimError) as exc:
                errors += 1
                logger.warning("Automatic BSC scan failed for route %s: %s", route_id, exc)
        return {"scanned": scanned, "detected": detected, "errors": errors}
    finally:
        close_old_connections()


@shared_task(bind=True, name="wallet.tasks.sweep_bsc_deposit", max_retries=40)
def sweep_bsc_deposit(self, route_id: str):
    """Collect confirmed USDT from a unique address into the treasury wallet."""
    close_old_connections()
    try:
        from wallet.deposits import sweep_direct_bsc_deposit

        try:
            result = sweep_direct_bsc_deposit(route_id=route_id)
        except DirectBSCDepositAddress.DoesNotExist:
            return {"status": "missing"}
        except (PaymentProviderError, OperationalError) as exc:
            try:
                DirectBSCDepositAddress.objects.filter(pk=route_id).update(
                    sweep_error=str(exc)[:1000]
                )
            except OperationalError:
                # SQLite can still hold the same local-development write lock;
                # the task retry is more important than persisting this message.
                pass
            raise self.retry(exc=exc, countdown=15)

        if result["status"] in {
            "gas_submitted",
            "gas_confirming",
            "sweep_submitted",
            "sweep_confirming",
        }:
            raise self.retry(countdown=10)
        return result
    finally:
        close_old_connections()


@shared_task(name="wallet.tasks.recover_pending_bsc_sweeps")
def recover_pending_bsc_sweeps():
    """Requeue confirmed deposits whose collection was interrupted."""
    if not settings.BSC_LIVE_SWEEPS_ENABLED:
        return {"queued": 0, "status": "disabled"}
    route_ids = list(
        DirectBSCDepositAddress.objects.filter(
            transaction__onchain_deposit__status=OnChainDeposit.Status.CONFIRMED,
            sweep_status__in=(
                DirectBSCDepositAddress.SweepStatus.PENDING,
                DirectBSCDepositAddress.SweepStatus.FUNDING,
                DirectBSCDepositAddress.SweepStatus.SWEEPING,
            ),
        ).values_list("id", flat=True)[:100]
    )
    for route_id in route_ids:
        sweep_bsc_deposit.delay(str(route_id))
    return {"queued": len(route_ids)}


@shared_task
def expire_deposit_bonuses():
    """Mark active, past-due bonuses expired and decrement each wallet's cached bonus_balance."""
    now = timezone.now()
    due = DepositBonus.objects.filter(
        status=DepositBonus.Status.ACTIVE,
        expires_at__isnull=False,
        expires_at__lte=now,
    )
    count = 0
    for bonus in due.iterator():
        with transaction.atomic():
            wallet = Wallet.objects.select_for_update().get(pk=bonus.wallet_id)
            b = DepositBonus.objects.select_for_update().get(pk=bonus.pk)
            if b.status != DepositBonus.Status.ACTIVE:
                continue
            remaining = b.amount_remaining
            b.amount_remaining = Decimal("0.00")
            b.status = DepositBonus.Status.EXPIRED
            b.save(update_fields=["amount_remaining", "status"])
            if remaining:
                wallet.bonus_balance = wallet.bonus_balance - remaining
                wallet.save(update_fields=["bonus_balance"])
            count += 1
        try:
            from wallet.views import send_wallet_update
            send_wallet_update(wallet.user, False)
        except Exception as e:
            logger.warning("wallet update after bonus expiry failed: %s", e)
    return count


ACTIVE_BATCH_STATUSES = (
    RevenueDistributionBatch.Status.PREPARING,
    RevenueDistributionBatch.Status.SENDING,
    RevenueDistributionBatch.Status.SUBMITTED,
    RevenueDistributionBatch.Status.FAILED,
)

STALE_DISTRIBUTION_AFTER = timedelta(minutes=5)


def _requeue_stale_distribution_batch():
    """Resume a batch whose execution handoff or worker was lost."""
    stale_before = timezone.now() - STALE_DISTRIBUTION_AFTER
    with transaction.atomic():
        batch = (
            RevenueDistributionBatch.objects.select_for_update()
            .filter(
                status__in=(
                    RevenueDistributionBatch.Status.PREPARING,
                    RevenueDistributionBatch.Status.SENDING,
                ),
                updated_at__lte=stale_before,
            )
            .order_by("created_at")
            .first()
        )
        if not batch:
            return None

        batch.status = RevenueDistributionBatch.Status.PREPARING
        batch.error_message = ""
        batch.save(update_fields=["status", "error_message", "updated_at"])
        batch_id = str(batch.id)
        transaction.on_commit(lambda: execute_revenue_distribution.delay(batch_id))

    return {"created": False, "reason": "stale_batch_requeued", "batch_id": batch_id}


@shared_task(
    bind=True,
    name="wallet.tasks.check_revenue_distribution",
    autoretry_for=(PaymentProviderError,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=3,
)
def check_revenue_distribution(self, force=False):
    """Create one locked batch for the whole-dollar available balance."""
    close_old_connections()
    try:
        current_config = RevenueDistributionConfig.get_config()
        if not (current_config.enabled or force):
            return {"created": False, "reason": "disabled"}
        if not payout_provider_configured():
            return {"created": False, "reason": "payout_provider_not_configured"}

        recovery = _requeue_stale_distribution_batch()
        if recovery:
            return recovery

        provider = get_payout_provider()
        balance = provider.get_available_usdt_balance()

        with transaction.atomic():
            config = RevenueDistributionConfig.objects.select_for_update().get(pk=1)
            config.last_available_balance = balance
            config.last_balance_checked_at = timezone.now()
            config.save(update_fields=["last_available_balance", "last_balance_checked_at", "updated_at"])

            if not (config.enabled or force):
                return {"created": False, "reason": "disabled", "balance": str(balance)}
            if not live_payouts_enabled():
                return {"created": False, "reason": "live_payouts_disabled", "balance": str(balance)}
            if RevenueDistributionBatch.objects.filter(status__in=ACTIVE_BATCH_STATUSES).exists():
                return {"created": False, "reason": "batch_in_progress", "balance": str(balance)}

            recipients = list(RevenueShareRecipient.objects.filter(is_active=True).order_by("created_at"))
            if not recipients:
                return {"created": False, "reason": "no_recipients", "balance": str(balance)}
            if sum((item.percentage for item in recipients), Decimal("0")) != Decimal("100.00"):
                return {"created": False, "reason": "allocation_not_100", "balance": str(balance)}

            threshold = config.threshold_amount
            if balance < threshold:
                return {"created": False, "reason": "below_threshold", "balance": str(balance)}
            # Once the threshold is reached, distribute the real available
            # balance while leaving only its fractional USDT remainder.
            amount = balance.quantize(Decimal("1"), rounding=ROUND_DOWN).quantize(Decimal("0.000001"))
            batch = RevenueDistributionBatch.objects.create(
                amount=amount,
                threshold_amount=threshold,
                balance_before=balance,
            )

            allocated = Decimal("0")
            for index, recipient in enumerate(recipients):
                if index == len(recipients) - 1:
                    payout_amount = amount - allocated
                else:
                    payout_amount = (amount * recipient.percentage / Decimal("100")).quantize(
                        Decimal("0.000001"), rounding=ROUND_DOWN
                    )
                    allocated += payout_amount
                payout_id = uuid.uuid4()
                RevenueDistributionPayout.objects.create(
                    id=payout_id,
                    batch=batch,
                    recipient=recipient,
                    recipient_name=recipient.name,
                    recipient_email=recipient.email,
                    bep20_address=recipient.bep20_address,
                    percentage=recipient.percentage,
                    amount=payout_amount,
                    idempotency_key=f"sharptoolz-revenue-{payout_id}",
                )

            transaction.on_commit(lambda: execute_revenue_distribution.delay(str(batch.id)))
            return {"created": True, "batch_id": str(batch.id), "amount": str(amount)}
    finally:
        close_old_connections()


@shared_task(bind=True, name="wallet.tasks.execute_revenue_distribution", max_retries=5)
def execute_revenue_distribution(self, batch_id: str):
    """Submit each recipient transfer with a provider idempotency key."""
    close_old_connections()
    try:
        with transaction.atomic():
            batch = RevenueDistributionBatch.objects.select_for_update().get(pk=batch_id)
            if batch.status == RevenueDistributionBatch.Status.COMPLETED:
                return {"status": "completed"}
            if batch.status == RevenueDistributionBatch.Status.SUBMITTED:
                return {"status": "submitted"}
            if batch.status == RevenueDistributionBatch.Status.SENDING:
                return {"status": "already_sending"}
            batch.status = RevenueDistributionBatch.Status.SENDING
            batch.error_message = ""
            batch.save(update_fields=["status", "error_message", "updated_at"])

        provider = get_payout_provider()
        payouts = RevenueDistributionPayout.objects.filter(batch_id=batch_id).order_by("created_at")
        for payout in payouts:
            if payout.provider_transaction_id and not direct_bsc_enabled():
                continue
            try:
                if direct_bsc_enabled():
                    from wallet.blockchain import PreparedTokenTransfer

                    if payout.signed_transaction and payout.provider_transaction_id:
                        prepared = PreparedTokenTransfer(
                            transaction_hash=payout.provider_transaction_id,
                            signed_transaction=payout.signed_transaction,
                        )
                    else:
                        prepared = provider.prepare_usdt_transfer(
                            to=payout.bep20_address,
                            amount=payout.amount,
                        )
                        RevenueDistributionPayout.objects.filter(pk=payout.pk).update(
                            provider_transaction_id=prepared.transaction_hash,
                            transaction_hash=prepared.transaction_hash,
                            signed_transaction=prepared.signed_transaction,
                        )
                    provider_id = provider.broadcast_prepared_transfer(prepared)
                else:
                    provider_id = provider.withdraw_usdt(
                        to=payout.bep20_address,
                        amount=payout.amount,
                        idempotency_key=payout.idempotency_key,
                    )
            except PaymentProviderError as exc:
                RevenueDistributionPayout.objects.filter(pk=payout.pk).update(
                    status=RevenueDistributionPayout.Status.FAILED,
                    error_message=str(exc),
                    attempt_count=F("attempt_count") + 1,
                )
                RevenueDistributionBatch.objects.filter(pk=batch_id).update(
                    status=RevenueDistributionBatch.Status.FAILED,
                    error_message=f"One or more {gateway_label()} transfers could not be submitted.",
                )
                raise self.retry(exc=exc, countdown=min(30 * (2 ** self.request.retries), 600))

            RevenueDistributionPayout.objects.filter(pk=payout.pk).update(
                provider_transaction_id=provider_id,
                status=RevenueDistributionPayout.Status.SUBMITTED,
                error_message="",
                attempt_count=F("attempt_count") + 1,
                submitted_at=timezone.now(),
            )

        RevenueDistributionBatch.objects.filter(pk=batch_id).update(
            status=RevenueDistributionBatch.Status.SUBMITTED,
            submitted_at=timezone.now(),
            error_message="",
        )
        reconcile_revenue_distributions.apply_async(countdown=120)
        return {"status": "submitted", "batch_id": batch_id}
    except Retry:
        raise
    finally:
        close_old_connections()


@shared_task(
    bind=True,
    name="wallet.tasks.reconcile_revenue_distributions",
    autoretry_for=(PaymentProviderError,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def reconcile_revenue_distributions(self):
    provider = get_payout_provider()
    submitted = RevenueDistributionPayout.objects.filter(
        status=RevenueDistributionPayout.Status.SUBMITTED,
        provider_transaction_id__gt="",
    ).select_related("batch")
    touched_batches = set()
    for payout in submitted.iterator():
        provider_status = provider.get_transfer_status(payout.provider_transaction_id)
        touched_batches.add(payout.batch_id)
        if provider_status.status == "completed":
            payout.status = RevenueDistributionPayout.Status.COMPLETED
            payout.transaction_hash = provider_status.transaction_hash
            payout.completed_at = timezone.now()
            payout.error_message = ""
            payout.save(update_fields=[
                "status", "transaction_hash", "completed_at", "error_message", "updated_at",
            ])
            _queue_payout_email(payout)
        elif provider_status.status == "failed":
            payout.status = RevenueDistributionPayout.Status.FAILED
            payout.transaction_hash = provider_status.transaction_hash
            payout.error_message = f"{gateway_label()} reported a failed transfer."
            payout.save(update_fields=["status", "transaction_hash", "error_message", "updated_at"])

    for batch_id in touched_batches:
        _refresh_batch_status(batch_id)
    return {"checked": submitted.count(), "batches": len(touched_batches)}


def _refresh_batch_status(batch_id):
    with transaction.atomic():
        batch = RevenueDistributionBatch.objects.select_for_update().get(pk=batch_id)
        statuses = list(batch.payouts.values_list("status", flat=True))
        if statuses and all(status == RevenueDistributionPayout.Status.COMPLETED for status in statuses):
            batch.status = RevenueDistributionBatch.Status.COMPLETED
            batch.completed_at = timezone.now()
            batch.error_message = ""
        elif RevenueDistributionPayout.Status.FAILED in statuses:
            batch.status = RevenueDistributionBatch.Status.FAILED
            batch.error_message = f"One or more {gateway_label()} transfers failed. Review and retry this batch."
        else:
            batch.status = RevenueDistributionBatch.Status.SUBMITTED
        batch.save(update_fields=["status", "completed_at", "error_message", "updated_at"])


def _queue_payout_email(payout):
    queued_at = timezone.now()
    claimed = RevenueDistributionPayout.objects.filter(
        pk=payout.pk,
        email_sent_at__isnull=True,
    ).update(email_sent_at=queued_at)
    if not claimed:
        return
    from api.utils.email_service import EmailService

    accepted = EmailService.send_revenue_distribution_payout(
        recipient_name=payout.recipient_name,
        recipient_email=payout.recipient_email,
        amount=payout.amount,
        percentage=payout.percentage,
        transaction_id=payout.provider_transaction_id,
        transaction_hash=payout.transaction_hash,
    )
    if not accepted:
        RevenueDistributionPayout.objects.filter(pk=payout.pk, email_sent_at=queued_at).update(
            email_sent_at=None
        )


@shared_task(bind=True, name="wallet.tasks.reconcile_onchain_deposit", max_retries=20)
def reconcile_onchain_deposit(self, receipt_id: str):
    """Finish a claimed direct deposit after it reaches the confirmation threshold."""
    close_old_connections()
    try:
        try:
            receipt = OnChainDeposit.objects.select_related(
                "transaction__wallet__user"
            ).get(pk=receipt_id)
        except OnChainDeposit.DoesNotExist:
            return {"status": "missing"}
        if receipt.status == OnChainDeposit.Status.CONFIRMED:
            return {"status": "confirmed"}

        from wallet.deposits import DepositClaimError, verify_and_credit_onchain_deposit

        try:
            result = verify_and_credit_onchain_deposit(
                transaction_id=receipt.transaction_id,
                user_id=receipt.transaction.wallet.user_id,
                transaction_hash=receipt.transaction_hash,
            )
        except DepositClaimError as exc:
            logger.warning("Direct deposit %s could not be credited: %s", receipt_id, exc)
            return {"status": "rejected", "reason": str(exc)}
        except PaymentProviderError as exc:
            raise self.retry(exc=exc, countdown=30)

        if not result.confirmed:
            return {"status": "pending", "confirmations": result.confirmations}
        return {"status": "confirmed", "credited": result.credited}
    finally:
        close_old_connections()
