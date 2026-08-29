import logging
import uuid
from decimal import Decimal, ROUND_DOWN

from celery import shared_task
from celery.exceptions import Retry
from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone
from wallet.models import (
    DepositBonus,
    RevenueDistributionBatch,
    RevenueDistributionConfig,
    RevenueDistributionPayout,
    RevenueShareRecipient,
    Wallet,
)
from wallet.providers import CPayClient, PaymentProviderError


logger = logging.getLogger(__name__)


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
    """Create one locked distribution batch for all complete threshold tranches."""
    close_old_connections()
    try:
        current_config = RevenueDistributionConfig.get_config()
        if not (current_config.enabled or force):
            return {"created": False, "reason": "disabled"}
        if not CPayClient.payout_configured():
            return {"created": False, "reason": "payout_provider_not_configured"}
        provider = CPayClient()
        balance = provider.get_available_usdt_balance()

        with transaction.atomic():
            config = RevenueDistributionConfig.objects.select_for_update().get(pk=1)
            config.last_available_balance = balance
            config.last_balance_checked_at = timezone.now()
            config.save(update_fields=["last_available_balance", "last_balance_checked_at", "updated_at"])

            if not (config.enabled or force):
                return {"created": False, "reason": "disabled", "balance": str(balance)}
            if not settings.CPAY_LIVE_PAYOUTS_ENABLED:
                return {"created": False, "reason": "live_payouts_disabled", "balance": str(balance)}
            if RevenueDistributionBatch.objects.filter(status__in=ACTIVE_BATCH_STATUSES).exists():
                return {"created": False, "reason": "batch_in_progress", "balance": str(balance)}

            recipients = list(RevenueShareRecipient.objects.filter(is_active=True).order_by("created_at"))
            if not recipients:
                return {"created": False, "reason": "no_recipients", "balance": str(balance)}
            if sum((item.percentage for item in recipients), Decimal("0")) != Decimal("100.00"):
                return {"created": False, "reason": "allocation_not_100", "balance": str(balance)}

            threshold = config.threshold_amount
            tranche_count = min(int(balance // threshold), settings.CPAY_MAX_TRANCHES_PER_RUN)
            if tranche_count < 1:
                return {"created": False, "reason": "below_threshold", "balance": str(balance)}
            amount = (threshold * tranche_count).quantize(Decimal("0.000001"))
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
            batch.status = RevenueDistributionBatch.Status.SENDING
            batch.error_message = ""
            batch.save(update_fields=["status", "error_message", "updated_at"])

        provider = CPayClient()
        payouts = RevenueDistributionPayout.objects.filter(batch_id=batch_id).order_by("created_at")
        for payout in payouts:
            if payout.provider_transaction_id:
                continue
            try:
                provider_id = provider.withdraw_usdt(
                    to=payout.bep20_address,
                    amount=payout.amount,
                    idempotency_key=payout.idempotency_key,
                )
            except PaymentProviderError as exc:
                RevenueDistributionPayout.objects.filter(pk=payout.pk).update(
                    status=RevenueDistributionPayout.Status.FAILED,
                    error_message=str(exc),
                    attempt_count=payout.attempt_count + 1,
                )
                RevenueDistributionBatch.objects.filter(pk=batch_id).update(
                    status=RevenueDistributionBatch.Status.FAILED,
                    error_message="One or more CPay transfers could not be submitted.",
                )
                raise self.retry(exc=exc, countdown=min(30 * (2 ** self.request.retries), 600))

            RevenueDistributionPayout.objects.filter(pk=payout.pk).update(
                provider_transaction_id=provider_id,
                status=RevenueDistributionPayout.Status.SUBMITTED,
                error_message="",
                attempt_count=payout.attempt_count + 1,
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
    provider = CPayClient()
    submitted = RevenueDistributionPayout.objects.filter(
        status=RevenueDistributionPayout.Status.SUBMITTED,
        provider_transaction_id__gt="",
    ).select_related("batch")
    touched_batches = set()
    for payout in submitted.iterator():
        entity = provider.find_transaction(payout.provider_transaction_id)
        if not entity:
            continue
        provider_status = str(entity.get("systemStatus") or entity.get("status") or "")
        touched_batches.add(payout.batch_id)
        if provider_status in {"Done", "DepositComplete", "ReceiveComplete"}:
            hashes = (entity.get("info") or {}).get("hashs") or []
            payout.status = RevenueDistributionPayout.Status.COMPLETED
            payout.transaction_hash = str(hashes[-1]) if hashes else ""
            payout.completed_at = timezone.now()
            payout.error_message = ""
            payout.save(update_fields=[
                "status", "transaction_hash", "completed_at", "error_message", "updated_at",
            ])
            _queue_payout_email(payout)
        elif provider_status in {"Error", "Failed"}:
            payout.status = RevenueDistributionPayout.Status.FAILED
            payout.error_message = f"CPay reported {provider_status}."
            payout.save(update_fields=["status", "error_message", "updated_at"])

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
            batch.error_message = "One or more CPay transfers failed. Review and retry this batch."
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
