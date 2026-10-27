"""Send PrintMe confirmation and rejection mail.

Confirmation mail reuses the ciphertext saved during ingestion. The job stays
``pending_email`` until the backend accepts the message. Only then does the
row become ``notified`` and the ciphertext get cleared.

If the process stops after the backend has accepted the message and before
that row update is committed, a later attempt sends the same confirmation
again. A retry does not create a new access code.

Rejected jobs stay ``rejected`` and the message does not include an access
code. ``locked_at`` on a rejected row means the rejection notice was accepted,
so a duplicate call does not send it again. That timestamp is not an
access-code lock.
"""

import smtplib
from dataclasses import dataclass
from email.utils import parseaddr

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from .. import PrintJobStatus
from ..models import PrintJob
from .codes import CodeKeyError, open_access_code

_CONFIRMATION_SUBJECT = "Your PrintMe access code"
_REJECTION_SUBJECT = "PrintMe could not print your document"
_GENERIC_REJECTION = (
    "We could not print this message. Send one PDF file to the print address."
)
_REJECTION_TEXT = {
    "non_pdf": (
        "We could not print this message because it included a file that is not a PDF. "
        "Send one PDF file."
    ),
    "missing_attachment": (
        "We could not print this message because it did not include a PDF file."
    ),
    "multiple_pdfs": (
        "We could not print this message because it included more than one PDF. "
        "Send one PDF file."
    ),
    "oversized": "We could not print this message because it was too large.",
    "malformed": "We could not print this message because it could not be read.",
    "unexpected_recipient": (
        "We could not print this message because it was sent to the wrong address."
    ),
}


class NotifyOutcome:
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class MailDeliveryError(Exception):
    """The mail backend did not accept the message."""


@dataclass(frozen=True)
class NotifyResult:
    """Notification outcome. The access code is not included."""

    outcome: str
    job_id: str | None = None


def notify_print_job(job: PrintJob | None, *, ses_client=None) -> NotifyResult:
    """Send the confirmation or rejection for ``job``.

    ``ses_client`` injects an SES client when ``PRINTME_EMAIL_BACKEND`` is
    ``ses``. Tests pass a fake client. The Django backend is used otherwise.
    """

    if not isinstance(job, PrintJob):
        return NotifyResult(outcome=NotifyOutcome.FAILED)
    try:
        with transaction.atomic():
            current = PrintJob.objects.select_for_update().get(pk=job.pk)
            result = _notify_locked(current, ses_client)
    except PrintJob.DoesNotExist:
        return NotifyResult(outcome=NotifyOutcome.FAILED)
    _mirror(current, job)
    return result


def _notify_locked(job: PrintJob, ses_client) -> NotifyResult:
    if job.status == PrintJobStatus.NOTIFIED:
        return NotifyResult(outcome=NotifyOutcome.SKIPPED, job_id=str(job.id))
    if job.status == PrintJobStatus.REJECTED:
        return _send_rejection(job, ses_client)
    if job.status == PrintJobStatus.PENDING_EMAIL and not _code_still_valid(job):
        return NotifyResult(outcome=NotifyOutcome.SKIPPED, job_id=str(job.id))
    if job.status != PrintJobStatus.PENDING_EMAIL or not job.code_ciphertext:
        return NotifyResult(outcome=NotifyOutcome.FAILED, job_id=str(job.id))

    try:
        code = open_access_code(job.code_ciphertext)
    except CodeKeyError:
        return NotifyResult(outcome=NotifyOutcome.FAILED, job_id=str(job.id))
    try:
        _deliver(
            ses_client,
            recipient=job.sender,
            subject=_CONFIRMATION_SUBJECT,
            body=_confirmation_body(code),
        )
    except MailDeliveryError:
        return NotifyResult(outcome=NotifyOutcome.FAILED, job_id=str(job.id))

    job.status = PrintJobStatus.NOTIFIED
    job.code_ciphertext = ""
    job.save(update_fields=["status", "code_ciphertext", "updated_at"])
    return NotifyResult(outcome=NotifyOutcome.SENT, job_id=str(job.id))


def _code_still_valid(job: PrintJob) -> bool:
    """Return whether the access code is still inside its expiry window."""

    return job.expires_at is not None and job.expires_at > timezone.now()


def _send_rejection(job: PrintJob, ses_client) -> NotifyResult:
    if job.locked_at is not None:
        return NotifyResult(outcome=NotifyOutcome.SKIPPED, job_id=str(job.id))
    try:
        _deliver(
            ses_client,
            recipient=job.sender,
            subject=_REJECTION_SUBJECT,
            body=_rejection_body(job.failure_reason),
        )
    except MailDeliveryError:
        return NotifyResult(outcome=NotifyOutcome.FAILED, job_id=str(job.id))
    job.code_ciphertext = ""
    job.locked_at = timezone.now()
    job.save(update_fields=["code_ciphertext", "locked_at", "updated_at"])
    return NotifyResult(outcome=NotifyOutcome.SENT, job_id=str(job.id))


def _deliver(ses_client, *, recipient: str, subject: str, body: str) -> None:
    sender = _address(str(settings.PRINTME_SES_FROM_ADDRESS or ""))
    to_address = _address(recipient)
    if sender is None or to_address is None:
        raise MailDeliveryError("PrintMe email was not accepted")
    backend = str(settings.PRINTME_EMAIL_BACKEND or "django").strip().lower()
    if backend == "ses":
        _send_ses(ses_client, sender, to_address, subject, body)
        return
    if backend != "django":
        raise MailDeliveryError("PrintMe email was not accepted")
    _send_django(sender, to_address, subject, body)


def _send_django(sender: str, recipient: str, subject: str, body: str) -> None:
    message = EmailMessage(
        subject=subject, body=body, from_email=sender, to=[recipient]
    )
    try:
        accepted = message.send(fail_silently=False)
    except (OSError, smtplib.SMTPException):
        raise MailDeliveryError("PrintMe email was not accepted") from None
    if accepted != 1 or message.attachments:
        raise MailDeliveryError("PrintMe email was not accepted")


def _send_ses(client, sender: str, recipient: str, subject: str, body: str) -> None:
    try:
        ses = _ses_client(client)
        ses.send_email(
            Source=sender,
            Destination={"ToAddresses": [recipient]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
            },
        )
    except MailDeliveryError:
        raise
    except Exception:
        raise MailDeliveryError("PrintMe email was not accepted") from None


def _ses_client(client):
    if client is not None:
        return client
    key_id = str(settings.PRINTME_AWS_ACCESS_KEY_ID or "")
    secret = str(settings.PRINTME_AWS_SECRET_ACCESS_KEY or "")
    region = str(settings.PRINTME_AWS_REGION or "")
    if not key_id or not secret or not region:
        raise MailDeliveryError("PrintMe email was not accepted")
    import boto3

    return boto3.client(
        "ses",
        region_name=region,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
    )


def _confirmation_body(code: str) -> str:
    hours = int(settings.PRINTME_CODE_TTL_HOURS)
    unit = "hour" if hours == 1 else "hours"
    return (
        "Your document is ready to print.\n"
        "\n"
        f"Access code: {code}\n"
        "\n"
        f"This code expires in {hours} {unit}. "
        "Present the code at the print center to collect your document.\n"
    )


def _rejection_body(reason: str) -> str:
    return _REJECTION_TEXT.get(reason, _GENERIC_REJECTION)


def _address(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    _name, address = parseaddr(value.strip())
    address = address.strip()
    if not address or len(address) > 254 or "\n" in address or "\r" in address:
        return None
    try:
        validate_email(address)
    except ValidationError:
        return None
    return address


def _mirror(source: PrintJob, target: PrintJob) -> None:
    if source is target:
        return
    target.status = source.status
    target.code_ciphertext = source.code_ciphertext
    target.code_hash = source.code_hash
    target.locked_at = source.locked_at
    target.failure_reason = source.failure_reason
    target.updated_at = source.updated_at
