"""Find a print order from a release code and load its documents.

The code is stored as a password hash, so it cannot be queried directly.
Open orders are checked with ``check_password``. ``verify_access_code`` runs
only after exactly one order matches, which keeps a miss from locking a
different customer's order.
"""

import logging
from uuid import UUID

from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.utils import timezone

from .. import PrintJobStatus
from ..models import PrintDocument, PrintDocumentStatus, PrintJob
from ..services.codes import job_can_release, verify_access_code
from ..services.parse_email import display_filename
from ..services.storage import ObjectStorage, StorageConfigError, StorageError

logger = logging.getLogger(__name__)

KIOSK_SESSION_KEY = "printme_kiosk_job"
_PDF_MAGIC = b"%PDF-"


def find_verified_job(candidate: str) -> PrintJob | None:
    """Return the single open order that matches ``candidate``.

    Any other result, including no match and an ambiguous match, is rejected
    through ``verify_access_code`` without naming a customer order.
    """

    if not isinstance(candidate, str):
        verify_access_code(None, "")
        return None
    candidate = candidate.upper()
    matched: list[PrintJob] = []
    for job in _open_jobs():
        if not job_can_release(job):
            continue
        if check_password(candidate, job.code_hash):
            matched.append(job)
            if len(matched) > 1:
                break
    if len(matched) != 1:
        verify_access_code(None, candidate)
        return None
    if not verify_access_code(matched[0], candidate).ok:
        return None
    return matched[0]


def document_cards(job: PrintJob) -> list[dict[str, str]]:
    """Return display fields for stored documents on ``job``."""

    cards = []
    documents = job.documents.filter(status=PrintDocumentStatus.STORED).order_by(
        "position"
    )
    for document in documents:
        filename = display_filename(document.original_filename) or "Document"
        cards.append(
            {
                "id": str(document.id),
                "filename": filename,
                "format": _safe_format(document.printable_format),
            }
        )
    return cards


def read_document_pdf(
    job_id: UUID, document_id: UUID, storage: ObjectStorage
) -> bytes | None:
    """Return PDF bytes when ``document_id`` belongs to ``job_id``.

    A missing document, another order's document, and an unreadable object
    all return None. The object key is not included in that result.
    """

    document = (
        PrintDocument.objects.select_related("job")
        .filter(
            pk=document_id,
            job_id=job_id,
            status=PrintDocumentStatus.STORED,
        )
        .first()
    )
    if document is None or not job_can_release(document.job):
        return None
    if not storage.allows(document.bucket, document.object_key):
        return None
    try:
        content = storage.read(document.object_key)
    except StorageConfigError:
        raise
    except StorageError:
        logger.warning("printme kiosk preview unavailable")
        return None
    if not isinstance(content, bytes) or not content.startswith(_PDF_MAGIC):
        return None
    return content


def _open_jobs():
    """Return orders that can still accept a release code.

    ``verify_access_code`` repeats these rules under a row lock. The query
    only limits how many password hashes are checked.
    """

    max_attempts = int(settings.PRINTME_MAX_VERIFY_ATTEMPTS)
    return (
        PrintJob.objects.exclude(
            status__in=(PrintJobStatus.EXPIRED, PrintJobStatus.REJECTED)
        )
        .exclude(code_hash="")
        .filter(
            locked_at__isnull=True,
            expires_at__gt=timezone.now(),
            failed_attempts__lt=max_attempts,
        )
    )


def _safe_format(value: str) -> str:
    text = str(value or "").lower()
    if text.isalnum() and len(text) <= 8:
        return text
    return "pdf"
