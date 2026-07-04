import logging
from decimal import Decimal
from celery import shared_task
from django.db import transaction
from django.utils import timezone
from wallet.models import DepositBonus, Wallet


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
            logging.getLogger(__name__).warning("wallet update after bonus expiry failed: %s", e)
    return count
