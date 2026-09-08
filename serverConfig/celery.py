import os
from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "serverConfig.settings")

app = Celery("sharptoolz")

app.config_from_object("django.conf:settings", namespace="CELERY")

app.autodiscover_tasks()

app.conf.beat_schedule = {
    "analytics-prune-presence-every-minute": {
        "task": "analytics.tasks.prune_stale_presence",
        "schedule": crontab(minute="*"),
    },
    "analytics-snapshot-daily-counters": {
        "task": "analytics.tasks.snapshot_daily_counters",
        "schedule": crontab(minute=5, hour=0),
    },
    "wallet-expire-bonuses": {
        "task": "wallet.tasks.expire_deposit_bonuses",
        "schedule": crontab(minute=0),  # hourly
    },
    "wallet-check-revenue-distribution": {
        "task": "wallet.tasks.check_revenue_distribution",
        "schedule": crontab(minute="*"),
    },
    "wallet-scan-pending-bsc-deposits": {
        "task": "wallet.tasks.scan_pending_bsc_deposits",
        "schedule": crontab(minute="*"),
    },
    "wallet-recover-pending-bsc-sweeps": {
        "task": "wallet.tasks.recover_pending_bsc_sweeps",
        "schedule": crontab(minute="*"),
    },
    "wallet-reconcile-revenue-distributions": {
        "task": "wallet.tasks.reconcile_revenue_distributions",
        "schedule": crontab(minute="*/2"),
    },
    "api-cleanup-expired-renders": {
        "task": "api.tasks.cleanup_expired_document_renders",
        "schedule": crontab(minute=20),
    },
}


@app.task(bind=True)
def debug_task(self):
    print(f"Request: {self.request!r}")
