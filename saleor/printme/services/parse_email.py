"""Parse an inbound RFC 5322 message and keep only PDF attachments.

The parser returns structured data to its caller. It does not log message
bodies or document bytes, and it does not write attachments to storage.
"""

import binascii
from dataclasses import dataclass
from email import policy
from email.errors import (
    CloseBoundaryNotFoundDefect,
    InvalidBase64CharactersDefect,
    InvalidBase64LengthDefect,
    InvalidBase64PaddingDefect,
    InvalidMultipartContentTransferEncodingDefect,
    MessageDefect,
    MessageError,
    MultipartInvariantViolationDefect,
    NoBoundaryInMultipartDefect,
    StartBoundaryNotFoundDefect,
)
from email.parser import BytesParser
from email.utils import parseaddr
from typing import BinaryIO

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

_PDF_MAGIC = b"%PDF-"
_MAGIC_WINDOW = 1024
_SENDER_MAX_LENGTH = 254
_STRUCTURAL_DEFECTS = (
    CloseBoundaryNotFoundDefect,
    InvalidMultipartContentTransferEncodingDefect,
    MultipartInvariantViolationDefect,
    NoBoundaryInMultipartDefect,
    StartBoundaryNotFoundDefect,
)
_BASE64_DEFECTS = (
    InvalidBase64CharactersDefect,
    InvalidBase64LengthDefect,
    InvalidBase64PaddingDefect,
)
_TEXT_TYPES = {"text", "multipart"}


class EmailRejection:
    MALFORMED = "malformed"
    OVERSIZED = "oversized"
    MISSING_ATTACHMENT = "missing_attachment"
    NON_PDF = "non_pdf"


@dataclass(frozen=True)
class PdfAttachment:
    filename: str
    content: bytes


@dataclass(frozen=True)
class EmailParseResult:
    """A parsed message, or a fixed rejection code with no document bytes."""

    sender: str
    attachments: tuple[PdfAttachment, ...]
    failure_reason: str

    @property
    def ok(self) -> bool:
        return self.failure_reason == ""


def parse_email(
    source: bytes | bytearray | memoryview | BinaryIO,
    *,
    max_bytes: int | None = None,
) -> EmailParseResult:
    """Parse raw RFC 5322 bytes, or a binary stream of those bytes.

    ``max_bytes`` defaults to ``PRINTME_MAX_BYTES``. A stream is read only
    up to that limit plus one byte, so an oversized stream is rejected
    before the MIME parser runs.
    """

    limit = _limit(max_bytes)
    raw = _read_limited(source, limit)
    if isinstance(raw, EmailParseResult):
        return raw
    if not raw.strip():
        return _failure(EmailRejection.MALFORMED)

    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except (MessageDefect, MessageError, ValueError, AssertionError, binascii.Error):
        return _failure(EmailRejection.MALFORMED)

    if _has_structural_defect(message):
        return _failure(EmailRejection.MALFORMED)

    sender = _sender_address(message)
    if sender is None:
        return _failure(EmailRejection.MALFORMED)

    file_parts = [part for part in message.walk() if _is_file_part(part)]
    if not file_parts:
        return _failure(EmailRejection.MISSING_ATTACHMENT, sender)

    attachments: list[PdfAttachment] = []
    for index, part in enumerate(file_parts, start=1):
        content = _attachment_bytes(part, limit)
        if isinstance(content, str):
            return _failure(content, sender)
        if not _has_pdf_magic(content):
            return _failure(EmailRejection.NON_PDF, sender)
        attachments.append(
            PdfAttachment(
                filename=_safe_filename(part.get_filename(), index), content=content
            )
        )
    return EmailParseResult(
        sender=sender, attachments=tuple(attachments), failure_reason=""
    )


def _limit(max_bytes: int | None) -> int:
    if max_bytes is None:
        max_bytes = int(settings.PRINTME_MAX_BYTES)
    if max_bytes < 0:
        raise ValueError("max_bytes must be zero or greater")
    return max_bytes


def _read_limited(
    source: bytes | bytearray | memoryview | BinaryIO,
    max_bytes: int,
) -> bytes | EmailParseResult:
    if isinstance(source, memoryview):
        source = source.tobytes()
    if isinstance(source, (bytes, bytearray)):
        if len(source) > max_bytes:
            return _failure(EmailRejection.OVERSIZED)
        return bytes(source)

    read = getattr(source, "read", None)
    if not callable(read):
        return _failure(EmailRejection.MALFORMED)
    chunk = read(max_bytes + 1)
    if isinstance(chunk, str) or not isinstance(chunk, (bytes, bytearray)):
        return _failure(EmailRejection.MALFORMED)
    if len(chunk) > max_bytes:
        return _failure(EmailRejection.OVERSIZED)
    return bytes(chunk)


def _failure(reason: str, sender: str = "") -> EmailParseResult:
    return EmailParseResult(sender=sender, attachments=(), failure_reason=reason)


def _has_structural_defect(message) -> bool:
    for part in message.walk():
        for defect in part.defects:
            if isinstance(defect, _STRUCTURAL_DEFECTS):
                return True
    return False


def _sender_address(message) -> str | None:
    header = message.get("from")
    if not header:
        return None
    _display_name, address = parseaddr(str(header))
    address = address.strip()
    if not address or len(address) > _SENDER_MAX_LENGTH:
        return None
    try:
        validate_email(address)
    except ValidationError:
        return None
    return address


def _is_file_part(part) -> bool:
    if part.is_multipart():
        return False
    if part.get_content_disposition() == "attachment" or part.get_filename():
        return True
    return part.get_content_maintype() not in _TEXT_TYPES


def _attachment_bytes(part, max_bytes: int) -> bytes | str:
    if any(isinstance(defect, _BASE64_DEFECTS) for defect in part.defects):
        return EmailRejection.MALFORMED

    declared = _declared_length(part)
    if declared is not None and declared > max_bytes:
        return EmailRejection.OVERSIZED

    encoded = part.get_payload(decode=False)
    if isinstance(encoded, str):
        encoded_size = len(encoded.encode("utf-8"))
    elif isinstance(encoded, bytes):
        encoded_size = len(encoded)
    else:
        return EmailRejection.NON_PDF
    if encoded_size > max_bytes:
        return EmailRejection.OVERSIZED

    try:
        content = part.get_payload(decode=True)
    except (MessageDefect, MessageError, ValueError, TypeError, binascii.Error):
        return EmailRejection.MALFORMED
    if not isinstance(content, bytes):
        return EmailRejection.MALFORMED
    if len(content) > max_bytes:
        return EmailRejection.OVERSIZED
    return content


def _declared_length(part) -> int | None:
    header = part.get("content-length")
    if header is None:
        return None
    text = str(header).strip()
    if not text.isdigit():
        return None
    return int(text)


def _has_pdf_magic(content: bytes) -> bool:
    return _PDF_MAGIC in content[:_MAGIC_WINDOW]


def _safe_filename(filename: str | None, index: int) -> str:
    if not filename:
        return f"document-{index}.pdf"
    base = filename.replace("\\", "/").rsplit("/", 1)[-1].replace("\x00", "").strip()
    if not base or base in {".", ".."}:
        return f"document-{index}.pdf"
    return base[:255]
