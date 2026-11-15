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
from ..models import PrintDocumentStatus, PrintJob
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
            if not _remove_all_documents(job, storage):
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


def _remove_all_documents(job: PrintJob, storage: ObjectStorage) -> bool:
    documents = list(job.documents.order_by("position"))
    if documents:
        for document in documents:
            if document.status != PrintDocumentStatus.STORED:
                continue
            if not _remove_stored_key(
                storage, document.bucket, document.object_key, job.ses_message_id
            ):
                return False
            document.status = PrintDocumentStatus.DELETED
            document.save(update_fields=["status"])
        return True
    return _remove_document(job, storage)


def _remove_stored_key(
    storage: ObjectStorage, bucket: str, key: str, message_id: str
) -> bool:
    if not bucket and not key:
        return True
    if not storage.allows(bucket, key):
        logger.info("printme cleanup %s skipped", message_id)
        return False
    try:
        metadata = storage.read_metadata(key)
    except Exception:
        logger.info("printme cleanup %s storage", message_id)
        return False
    if metadata is None:
        return True
    try:
        storage.delete(key)
    except Exception:
        logger.info("printme cleanup %s storage", message_id)
        return False
    return True


def _remove_document(job: PrintJob, storage: ObjectStorage) -> bool:
    return _remove_stored_key(
        storage, job.bucket, job.object_key, job.ses_message_id
    )
