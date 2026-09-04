import logging
from datetime import timedelta

from celery import shared_task
from celery.exceptions import Retry, SoftTimeLimitExceeded
from django.core.files.base import ContentFile
from django.db import close_old_connections, transaction
from django.utils import timezone

from .models import DocumentRenderJob
from .render_events import broadcast_render_job
from .rendering import assemble_document_svg, render_svg_with_chromium, verify_render_output


logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    name="api.tasks.render_document",
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=45,
    time_limit=60,
)
def render_document(self, job_id: str) -> None:
    close_old_connections()
    try:
        with transaction.atomic():
            job = DocumentRenderJob.objects.select_for_update().get(pk=job_id)
            if job.status == DocumentRenderJob.Status.COMPLETED:
                return
            if (
                job.status == DocumentRenderJob.Status.RUNNING
                and job.started_at
                and job.started_at > timezone.now() - timedelta(seconds=55)
            ):
                raise self.retry(countdown=60, max_retries=2)
            job.status = DocumentRenderJob.Status.RUNNING
            job.error_code = ""
            job.started_at = timezone.now()
            job.save(update_fields=["status", "error_code", "started_at", "updated_at"])
        broadcast_render_job(job_id)

        job = DocumentRenderJob.objects.select_related("document", "document__template").prefetch_related(
            "document__fonts", "document__template__fonts"
        ).get(pk=job_id)
        svg = assemble_document_svg(job.document)
        payload = render_svg_with_chromium(svg, job.format)
        verify_render_output(payload, job.format)
        filename = f"{job.id}.{job.format}"
        job.output_file.save(filename, ContentFile(payload), save=False)
        try:
            job.output_size = len(payload)
            job.status = DocumentRenderJob.Status.COMPLETED
            job.completed_at = timezone.now()
            job.error_code = ""
            job.save(update_fields=[
                "output_file", "output_size", "status", "completed_at", "error_code", "updated_at",
            ])
            broadcast_render_job(job_id)
        except Exception:
            job.output_file.delete(save=False)
            raise
    except Retry:
        raise
    except Exception as exc:
        error_code = "render_timeout" if isinstance(exc, SoftTimeLimitExceeded) else "render_failed"
        logger.exception("Document render job %s failed with %s", job_id, error_code)
        DocumentRenderJob.objects.filter(pk=job_id).update(
            status=DocumentRenderJob.Status.FAILED,
            error_code=error_code,
            completed_at=timezone.now(),
        )
        broadcast_render_job(job_id)
    finally:
        close_old_connections()


@shared_task(name="api.tasks.cleanup_expired_document_renders")
def cleanup_expired_document_renders() -> int:
    expired = DocumentRenderJob.objects.filter(expires_at__lte=timezone.now())
    count = expired.count()
    for job in expired.iterator():
        job.delete()
    return count


@shared_task(
    bind=True,
    name="api.tasks.send_email",
    acks_late=True,
    reject_on_worker_lost=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
    soft_time_limit=30,
    time_limit=45,
)
def send_email(self, subject, template_name, context, recipient_list):
    """
    Deliver one transactional email off the request path.

    Kept deliberately dumb: it receives only JSON-serialisable values and does
    the template render itself, so a queued email never depends on objects that
    may have changed (or vanished) between enqueue and delivery.
    """
    from api.utils.email_service import EmailService

    try:
        EmailService.deliver(subject, template_name, context, recipient_list)
    finally:
        close_old_connections()

    return {"subject": subject, "recipients": len(recipient_list)}
