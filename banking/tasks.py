import logging
from datetime import timedelta

from celery import shared_task
from django.db import close_old_connections
from django.utils import timezone

from api.utils.email_service import EmailService
from .models import FernwayProfile
from .views import _get_monthly_price

logger = logging.getLogger(__name__)


@shared_task(name="banking.tasks.send_monthly_renewal_reminders")
def send_monthly_renewal_reminders() -> dict:
    """
    Daily task:
    1. Mark any live Fernway banking subscriptions past their 30-day expiry as 'expired'.
    2. Email owners whose live banking accounts expire within 5 days (or just expired),
       throttled to at most once every 3 days per banking account.
    """
    close_old_connections()
    now = timezone.now()
    reminder_window = now + timedelta(days=5)
    cooldown_cutoff = now - timedelta(days=3)
    monthly_price = _get_monthly_price()

    expired_updated = 0
    reminders_sent = 0

    try:
        # 1. Transition expired subscriptions
        expired_qs = FernwayProfile.objects.filter(
            is_created=True,
            is_test=False,
            subscription_status='active',
            subscription_expires_at__lte=now,
        )
        expired_updated = expired_qs.update(subscription_status='expired', updated_at=now)

        # 2. Send reminders for accounts expiring in <= 5 days or expired
        due_profiles = (
            FernwayProfile.objects.select_related('user')
            .filter(
                is_created=True,
                is_test=False,
                subscription_expires_at__isnull=False,
                subscription_expires_at__lte=reminder_window,
            )
        )

        for profile in due_profiles.iterator():
            if profile.last_renewal_reminder_at and profile.last_renewal_reminder_at > cooldown_cutoff:
                continue
            if not profile.user or not profile.user.email:
                continue

            sent = EmailService.send_banking_renewal_reminder(
                user=profile.user,
                account_name=f"{profile.first_name} {profile.last_name}".strip(),
                account_email=profile.email,
                account_id=str(profile.id),
                expires_at=profile.subscription_expires_at,
                days_remaining=profile.days_remaining,
                is_expired=(profile.effective_subscription_status == 'expired'),
                monthly_price=monthly_price,
            )
            if sent:
                profile.last_renewal_reminder_at = now
                profile.save(update_fields=['last_renewal_reminder_at', 'updated_at'])
                reminders_sent += 1
    except Exception:
        logger.exception("Error running Fernway monthly renewal reminders")
        raise
    finally:
        close_old_connections()

    return {"expired_updated": expired_updated, "reminders_sent": reminders_sent}
