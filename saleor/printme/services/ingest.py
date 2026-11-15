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
from ..models import PrintDocument, PrintDocumentStatus, PrintJob
from .codes import CodeKeyError, ensure_access_code_key, issue_access_code
from .convert import ConversionError, LibreOfficeConverter
from .parse_email import (
    EmailRejection,
    FileRejection,
    RejectedFile,
    display_filename,
    parse_email,
)
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
    UNSUPPORTED = "unsupported"
    NO_SUPPORTED_DOCUMENT = "no_supported_document"


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
    converter=None,
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

    engine = converter if converter is not None else LibreOfficeConverter()
    rejections = list(parsed.rejections)
    try:
        prepared, uploads = _prepare_documents(
            store, engine, list(parsed.attachments), rejections
        )
    except StorageError:
        return _result(IngestOutcome.FAILED, IngestFailure.STORAGE)
    if not prepared:
        _delete_uploads(store, uploads)
        return _reject_message(
            message_id,
            sender=parsed.sender,
            recipient=recipient,
            reason=_empty_reason(rejections),
            rejected_files=_encode_rejections(rejections),
        )

    names = _unique_names([item["filename"] for item in prepared])
    for item, name in zip(prepared, names, strict=True):
        item["filename"] = name
    try:
        with transaction.atomic():
            job = PrintJob.objects.create(
                ses_message_id=message_id,
                sender=parsed.sender,
                recipient=stored_recipient,
                bucket=prepared[0]["stored"].bucket,
                object_key=prepared[0]["stored"].key,
                byte_size=prepared[0]["stored"].byte_size,
                document_name="\n".join(item["filename"] for item in prepared)[:1024],
                rejected_files=_encode_rejections(rejections),
                status=PrintJobStatus.PENDING_EMAIL,
            )
            for position, item in enumerate(prepared, start=1):
                PrintDocument.objects.create(
                    job=job,
                    position=position,
                    original_filename=item["filename"][:255],
                    source_format=item["source_format"],
                    printable_format="pdf",
                    bucket=item["stored"].bucket,
                    object_key=item["stored"].key,
                    byte_size=item["stored"].byte_size,
                    status=PrintDocumentStatus.STORED,
                )
            issue_access_code(job)
    except IntegrityError:
        _delete_uploads(store, uploads)
        winner = _existing(message_id)
        return IngestResult(
            outcome=IngestOutcome.DUPLICATE,
            job_id=winner.id if winner is not None else None,
        )
    except CodeKeyError:
        _delete_uploads(store, uploads)
        return _result(IngestOutcome.FAILED, IngestFailure.CODE_KEY)
    except DatabaseError:
        _delete_uploads(store, uploads)
        return _result(IngestOutcome.FAILED, IngestFailure.DATABASE)
    except StorageError:
        _delete_uploads(store, uploads)
        return _result(IngestOutcome.FAILED, IngestFailure.STORAGE)

    return IngestResult(outcome=IngestOutcome.ACCEPTED, job_id=job.id)


def _prepare_documents(store, engine, attachments, rejections):
    prepared = []
    uploads: list[StoredObject] = []
    for attachment in attachments:
        try:
            pdf = engine.to_pdf(
                attachment.content, source_format=attachment.source_format
            )
        except ConversionError:
            rejections.append(
                _rejection(attachment.filename, FileRejection.CORRUPT)
            )
            continue
        if not isinstance(pdf, (bytes, bytearray)) or not pdf.startswith(b"%PDF-"):
            rejections.append(
                _rejection(attachment.filename, FileRejection.CORRUPT)
            )
            continue
        relative_key = f"documents/{uuid.uuid4().hex}.pdf"
        if not _document_destination(store, relative_key):
            _delete_uploads(store, uploads)
            raise StorageError("Object key is outside the PrintMe prefix")
        try:
            uploaded = store.upload(
                relative_key,
                bytes(pdf),
                content_type="application/pdf",
            )
        except StorageError:
            _delete_uploads(store, uploads)
            raise
        uploads.append(uploaded)
        if not accepted_document_key(store, uploaded.bucket, uploaded.key):
            _delete_uploads(store, uploads)
            raise StorageError("Object key is outside the PrintMe prefix")
        prepared.append(
            {
                "filename": display_filename(attachment.filename) or "document.pdf",
                "source_format": attachment.source_format,
                "stored": uploaded,
            }
        )
    return prepared, uploads


def _rejection(filename: str, reason: str):
    return RejectedFile(display_filename(filename) or "attachment", reason)


def _empty_reason(rejections) -> str:
    reasons = {item.reason for item in rejections}
    if not reasons or reasons == {FileRejection.UNSUPPORTED}:
        return EmailRejection.NON_PDF
    return IngestFailure.NO_SUPPORTED_DOCUMENT


def _encode_rejections(rejections) -> str:
    allowed = {
        FileRejection.UNSUPPORTED,
        FileRejection.OVERSIZED,
        FileRejection.CORRUPT,
        FileRejection.EXCESS,
    }
    lines = []
    for item in rejections:
        reason = item.reason if item.reason in allowed else FileRejection.UNSUPPORTED
        name = display_filename(item.filename) or "attachment"
        lines.append(f"{name}\t{reason}")
    return "\n".join(lines)[:4000]


def _unique_names(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    unique = []
    for name in names:
        count = seen.get(name, 0) + 1
        seen[name] = count
        if count == 1:
            unique.append(name)
            continue
        stem, dot, extension = name.rpartition(".")
        if dot:
            unique.append(f"{stem} ({count}){dot}{extension}")
        else:
            unique.append(f"{name} ({count})")
    return unique


def _delete_uploads(storage: ObjectStorage, uploads: list[StoredObject]) -> None:
    for uploaded in uploads:
        _delete_if_unowned(storage, uploaded)


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
    rejected_files: str = "",
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
                rejected_files=rejected_files,
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
    if PrintDocument.objects.filter(
        bucket=uploaded.bucket, object_key=uploaded.key
    ).exists():
        return
    try:
        storage.delete(uploaded.key)
    except StorageError:
        return


def _result(outcome: str, reason: str) -> IngestResult:
    return IngestResult(outcome=outcome, failure_reason=reason)
