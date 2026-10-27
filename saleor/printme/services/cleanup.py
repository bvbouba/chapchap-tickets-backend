"""Expire print jobs and delete their stored PDFs.

The PrintJob row and its SES message id stay in place so a later delivery of
the same message is still a duplicate. A missing object is already cleaned
up. Any other delete failure leaves the job unchanged.
"""

import logging

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone

from .. import PrintJobStatus
from ..models import PrintJob
from .storage import ObjectStorage

logger = logging.getLogger("saleor.printme")


def expire_due_jobs(*, storage: ObjectStorage, now=None) -> int:
    """Mark due jobs expired after their PDF delete succeeds."""

    moment = now or timezone.now()
    job_ids = list(
        PrintJob.objects.filter(expires_at__isnull=False, expires_at__lte=moment)
        .exclude(status=PrintJobStatus.EXPIRED)
        .order_by("expires_at")
        .values_list("pk", flat=True)
    )
    expired = 0
    for job_id in job_ids:
        if _expire_one(job_id, storage):
            expired += 1
    return expired


def _expire_one(job_id, storage: ObjectStorage) -> bool:
    try:
        with transaction.atomic():
            job = PrintJob.objects.select_for_update().get(pk=job_id)
            if job.status == PrintJobStatus.EXPIRED or job.expires_at is None:
                return False
            if not _remove_document(job, storage):
                return False
            job.status = PrintJobStatus.EXPIRED
            job.code_hash = ""
            job.code_ciphertext = ""
            job.save(
                update_fields=["status", "code_hash", "code_ciphertext", "updated_at"]
            )
    except ObjectDoesNotExist:
        return False
    logger.info("printme cleanup %s expired", job.ses_message_id)
    return True


def _remove_document(job: PrintJob, storage: ObjectStorage) -> bool:
    if not job.bucket and not job.object_key:
        return True
    if not storage.allows(job.bucket, job.object_key):
        logger.info("printme cleanup %s skipped", job.ses_message_id)
        return False
    try:
        metadata = storage.read_metadata(job.object_key)
    except Exception:
        logger.info("printme cleanup %s storage", job.ses_message_id)
        return False
    if metadata is None:
        return True
    try:
        storage.delete(job.object_key)
    except Exception:
        logger.info("printme cleanup %s storage", job.ses_message_id)
        return False
    return True
