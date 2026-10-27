"""Create one print job for an inbound SES message.

The raw message is parsed with ``parse_email``. An accepted PDF is uploaded
through the storage interface, and the database row keeps only metadata.
Access codes come from the existing code service. This module does not send
email and does not log message bodies, document bytes, or codes.
"""

import uuid
from dataclasses import dataclass
from email.utils import parseaddr
from typing import BinaryIO

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import DatabaseError, IntegrityError, transaction

from .. import PrintJobStatus
from ..models import PrintJob
from .codes import CodeKeyError, ensure_access_code_key, issue_access_code
from .parse_email import parse_email
from .storage import (
    DOCUMENT_PREFIX,
    ObjectStorage,
    S3ObjectStorage,
    StorageError,
    StoredObject,
    accepted_document_key,
)


class IngestOutcome:
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    FAILED = "failed"


class IngestFailure:
    MALFORMED = "malformed"
    UNEXPECTED_RECIPIENT = "unexpected_recipient"
    UNEXPECTED_OBJECT = "unexpected_object"
    MULTIPLE_PDFS = "multiple_pdfs"
    STORAGE = "storage"
    DATABASE = "database"
    CODE_KEY = "code_key"


@dataclass(frozen=True)
class IngestResult:
    """Ingestion outcome. The access code is not included."""

    outcome: str
    job_id: uuid.UUID | None = None
    failure_reason: str = ""


def ingest_inbound_email(
    source: bytes | bytearray | memoryview | BinaryIO,
    *,
    ses_message_id: str,
    recipient: str,
    bucket: str,
    object_key: str,
    storage: ObjectStorage | None = None,
) -> IngestResult:
    """Ingest one SES message and its private PDF.

    ``bucket`` and ``object_key`` identify the raw message already stored by
    SES. They must sit in the configured PrintMe bucket and prefix. The PDF
    is written to a new key, so a duplicate notification cannot replace the
    original object.
    """

    store = storage if storage is not None else S3ObjectStorage()
    message_id = _message_id(ses_message_id)
    if message_id is None:
        return _result(IngestOutcome.REJECTED, IngestFailure.MALFORMED)

    if not store.allows(bucket, object_key):
        return _result(IngestOutcome.REJECTED, IngestFailure.UNEXPECTED_OBJECT)

    existing = _existing(message_id)
    if existing is not None:
        return IngestResult(outcome=IngestOutcome.DUPLICATE, job_id=existing.id)

    parsed = parse_email(source)
    if not parsed.ok:
        return _reject_message(
            message_id,
            sender=parsed.sender,
            recipient=recipient,
            reason=parsed.failure_reason or IngestFailure.MALFORMED,
        )
    if len(parsed.attachments) != 1:
        return _reject_message(
            message_id,
            sender=parsed.sender,
            recipient=recipient,
            reason=IngestFailure.MULTIPLE_PDFS,
        )
    if _inbound_address() is None:
        return _result(IngestOutcome.FAILED, IngestFailure.UNEXPECTED_RECIPIENT)
    if _recipient_rejected(recipient):
        return _reject_message(
            message_id,
            sender=parsed.sender,
            recipient=recipient,
            reason=IngestFailure.UNEXPECTED_RECIPIENT,
        )
    stored_recipient = _normalize_email(recipient)
    if stored_recipient is None:
        return _reject_message(
            message_id,
            sender=parsed.sender,
            recipient=recipient,
            reason=IngestFailure.MALFORMED,
        )

    try:
        ensure_access_code_key()
    except CodeKeyError:
        return _result(IngestOutcome.FAILED, IngestFailure.CODE_KEY)

    document = parsed.attachments[0].content
    relative_key = f"documents/{uuid.uuid4().hex}.pdf"
    if not _document_destination(store, relative_key):
        return _result(IngestOutcome.FAILED, IngestFailure.STORAGE)
    uploaded: StoredObject | None = None
    try:
        uploaded = store.upload(
            relative_key,
            document,
            content_type="application/pdf",
        )
        if not accepted_document_key(store, uploaded.bucket, uploaded.key):
            if store.allows(uploaded.bucket, uploaded.key):
                _delete_if_unowned(store, uploaded)
            return _result(IngestOutcome.FAILED, IngestFailure.STORAGE)
        with transaction.atomic():
            job = PrintJob.objects.create(
                ses_message_id=message_id,
                sender=parsed.sender,
                recipient=stored_recipient,
                bucket=uploaded.bucket,
                object_key=uploaded.key,
                byte_size=uploaded.byte_size,
                status=PrintJobStatus.PENDING_EMAIL,
            )
            issue_access_code(job)
    except IntegrityError:
        _delete_if_unowned(store, uploaded)
        winner = _existing(message_id)
        return IngestResult(
            outcome=IngestOutcome.DUPLICATE,
            job_id=winner.id if winner is not None else None,
        )
    except CodeKeyError:
        _delete_if_unowned(store, uploaded)
        return _result(IngestOutcome.FAILED, IngestFailure.CODE_KEY)
    except DatabaseError:
        _delete_if_unowned(store, uploaded)
        return _result(IngestOutcome.FAILED, IngestFailure.DATABASE)
    except StorageError:
        return _result(IngestOutcome.FAILED, IngestFailure.STORAGE)

    return IngestResult(outcome=IngestOutcome.ACCEPTED, job_id=job.id)


def validated_message_id(value: object) -> str | None:
    """Return a SES id that is safe to store and log.

    Whitespace and control characters are rejected rather than stripped, so
    a crafted id cannot forge a log line.
    """

    if not isinstance(value, str) or not value or len(value) > 255:
        return None
    for character in value:
        if character.isspace() or ord(character) < 32 or ord(character) == 127:
            return None
    return value


def _message_id(value: str) -> str | None:
    return validated_message_id(value)


def _existing(message_id: str) -> PrintJob | None:
    return PrintJob.objects.filter(ses_message_id=message_id).first()


def _inbound_address() -> str | None:
    return _normalize_email(str(settings.PRINTME_INBOUND_ADDRESS or ""))


def _recipient_rejected(recipient: str) -> bool:
    expected = _inbound_address()
    if expected is None:
        return True
    return _normalize_email(recipient) != expected


def _document_destination(storage: ObjectStorage, relative_key: str) -> bool:
    prefix = getattr(storage, "prefix", None)
    bucket = getattr(storage, "bucket", None)
    if not isinstance(prefix, str) or not isinstance(bucket, str):
        return False
    full = (
        relative_key if relative_key.startswith(prefix) else f"{prefix}{relative_key}"
    )
    if not full.startswith(DOCUMENT_PREFIX):
        return False
    return accepted_document_key(storage, bucket, full)


def _normalize_email(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    _display_name, address = parseaddr(value.strip())
    address = address.strip().lower()
    if not address or len(address) > 254 or "\n" in address or "\r" in address:
        return None
    try:
        validate_email(address)
    except ValidationError:
        return None
    return address


def _reject_message(
    message_id: str,
    *,
    sender: str,
    recipient: str,
    reason: str,
) -> IngestResult:
    stored_sender = _normalize_email(sender)
    stored_recipient = _normalize_email(recipient)
    if stored_sender is None or stored_recipient is None:
        return _result(IngestOutcome.REJECTED, reason)
    try:
        with transaction.atomic():
            job = PrintJob.objects.create(
                ses_message_id=message_id,
                sender=stored_sender,
                recipient=stored_recipient,
                status=PrintJobStatus.REJECTED,
                failure_reason=reason[:255],
            )
    except IntegrityError:
        winner = _existing(message_id)
        return IngestResult(
            outcome=IngestOutcome.DUPLICATE,
            job_id=winner.id if winner is not None else None,
        )
    return IngestResult(
        outcome=IngestOutcome.REJECTED,
        job_id=job.id,
        failure_reason=reason,
    )


def _delete_if_unowned(storage: ObjectStorage, uploaded: StoredObject | None) -> None:
    if uploaded is None:
        return
    if PrintJob.objects.filter(
        bucket=uploaded.bucket, object_key=uploaded.key
    ).exists():
        return
    try:
        storage.delete(uploaded.key)
    except StorageError:
        return


def _result(outcome: str, reason: str) -> IngestResult:
    return IngestResult(outcome=outcome, failure_reason=reason)
