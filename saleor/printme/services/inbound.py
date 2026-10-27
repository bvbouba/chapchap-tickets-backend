"""Turn one SQS delivery into a stored print job and a notification.

The management command polls SQS in its own process. This module does not
register Celery tasks. Logs include the SES message id and a status only.
"""

import json
import logging
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import unquote

from .. import PrintJobStatus
from ..models import PrintJob
from .ingest import IngestOutcome, ingest_inbound_email, validated_message_id
from .notify import NotifyOutcome, notify_print_job
from .storage import ObjectStorage, StorageError, raw_inbound_key

logger = logging.getLogger("saleor.printme")

_RECEIVED = "Received"
_SNS_SKIP = {"SubscriptionConfirmation", "UnsubscribeConfirmation"}


class NoticeError(Exception):
    """The queue body is not a usable inbound-mail notification."""


@dataclass(frozen=True)
class InboundNotice:
    ses_message_id: str
    bucket: str
    object_key: str
    recipient: str


class QueueClient(Protocol):
    def delete(self, receipt_handle: str) -> None:
        """Acknowledge one queue delivery."""


def parse_queue_body(body: str) -> InboundNotice:
    """Read a raw SES receipt or an SNS notification that wraps one."""

    payload = _json_object(body)
    if payload.get("Type") in _SNS_SKIP:
        raise NoticeError("malformed")
    if payload.get("Type") == "Notification":
        message = payload.get("Message")
        if not isinstance(message, str):
            raise NoticeError("malformed")
        payload = _json_object(message)
    kind = payload.get("notificationType") or payload.get("eventType")
    if kind != _RECEIVED:
        raise NoticeError("malformed")
    mail = payload.get("mail")
    receipt = payload.get("receipt")
    if not isinstance(mail, dict) or not isinstance(receipt, dict):
        raise NoticeError("malformed")
    if _verdict_failed(receipt):
        raise NoticeError("malformed")
    message_id = validated_message_id(mail.get("messageId"))
    action = receipt.get("action")
    if (
        message_id is None
        or not isinstance(action, dict)
        or action.get("type") not in {None, "S3"}
    ):
        raise NoticeError("malformed")
    bucket = action.get("bucketName")
    object_key = action.get("objectKey")
    if not isinstance(bucket, str) or not isinstance(object_key, str):
        raise NoticeError("malformed")
    recipient = _recipient(receipt, mail)
    if recipient is None:
        raise NoticeError("malformed")
    return InboundNotice(
        ses_message_id=message_id,
        bucket=bucket.strip(),
        object_key=unquote(object_key.strip()),
        recipient=recipient,
    )


def handle_delivery(
    body: str,
    receipt_handle: str,
    *,
    queue: QueueClient,
    storage: ObjectStorage,
    ses_client=None,
) -> str:
    """Process one delivery. Delete it only after a durable handled outcome."""

    try:
        notice = parse_queue_body(body)
    except NoticeError:
        logger.info("printme inbound - malformed")
        return "malformed"

    message_id = notice.ses_message_id
    if not storage.allows(notice.bucket, notice.object_key):
        logger.info("printme inbound %s unexpected_object", message_id)
        return "unexpected_object"

    job = PrintJob.objects.filter(ses_message_id=message_id).first()
    if job is not None and _already_handled(job):
        return _finish(queue, receipt_handle, storage, notice, message_id, "duplicate")
    if job is None:
        try:
            raw = storage.read(notice.object_key)
        except StorageError:
            logger.info("printme inbound %s storage", message_id)
            return "storage"
        if raw is None:
            logger.info("printme inbound %s storage", message_id)
            return "storage"
        try:
            ingested = ingest_inbound_email(
                raw,
                ses_message_id=message_id,
                recipient=notice.recipient,
                bucket=notice.bucket,
                object_key=notice.object_key,
                storage=storage,
            )
        except Exception:
            logger.info("printme inbound %s error", message_id)
            return "error"
        job = _job_for(ingested.job_id, message_id)
        if ingested.outcome == IngestOutcome.FAILED or (
            ingested.outcome == IngestOutcome.REJECTED and job is None
        ):
            logger.info("printme inbound %s %s", message_id, ingested.outcome)
            return ingested.outcome
        if job is None:
            logger.info("printme inbound %s %s", message_id, ingested.outcome)
            return ingested.outcome
        if _already_handled(job):
            return _finish(
                queue, receipt_handle, storage, notice, message_id, "duplicate"
            )

    notified = notify_print_job(job, ses_client=ses_client)
    if notified.outcome in {NotifyOutcome.SENT, NotifyOutcome.SKIPPED}:
        return _finish(
            queue, receipt_handle, storage, notice, message_id, notified.outcome
        )
    logger.info("printme inbound %s notify_failed", message_id)
    return "notify_failed"


def _json_object(body: str) -> dict:
    try:
        payload = json.loads(body)
    except (TypeError, json.JSONDecodeError):
        raise NoticeError("malformed") from None
    if not isinstance(payload, dict):
        raise NoticeError("malformed")
    return payload


def _verdict_failed(receipt: dict) -> bool:
    for name in ("spamVerdict", "virusVerdict"):
        verdict = receipt.get(name)
        if not isinstance(verdict, dict):
            continue
        if str(verdict.get("status", "")).upper() == "FAIL":
            return True
    return False


def _recipient(receipt: dict, mail: dict) -> str | None:
    for source in (receipt.get("recipients"), mail.get("destination")):
        if isinstance(source, list) and source and isinstance(source[0], str):
            recipient = source[0].strip()
            if recipient:
                return recipient
    return None


def _job_for(job_id, message_id: str) -> PrintJob | None:
    if job_id is not None:
        job = PrintJob.objects.filter(pk=job_id).first()
        if job is not None:
            return job
    return PrintJob.objects.filter(ses_message_id=message_id).first()


def _already_handled(job: PrintJob) -> bool:
    if job.status in {PrintJobStatus.NOTIFIED, PrintJobStatus.EXPIRED}:
        return True
    return job.status == PrintJobStatus.REJECTED and job.locked_at is not None


def _finish(
    queue: QueueClient,
    receipt_handle: str,
    storage: ObjectStorage,
    notice: InboundNotice,
    message_id: str,
    status: str,
) -> str:
    """Delete the raw message, then acknowledge the queue delivery.

    A delete failure leaves the delivery unacknowledged so a later attempt
    can retry it. A key outside ``raw/inbound/`` is left in place.
    """

    if not raw_inbound_key(storage, notice.bucket, notice.object_key):
        logger.info("printme inbound %s raw_retained", message_id)
        return _ack(queue, receipt_handle, message_id, status)
    try:
        storage.delete(notice.object_key)
    except Exception:
        logger.info("printme inbound %s raw_delete_failed", message_id)
        return "raw_delete_failed"
    return _ack(queue, receipt_handle, message_id, status)


def _ack(queue: QueueClient, receipt_handle: str, message_id: str, status: str) -> str:
    try:
        queue.delete(receipt_handle)
    except Exception:
        logger.info("printme inbound %s ack_failed", message_id)
        return "ack_failed"
    logger.info("printme inbound %s %s", message_id, status)
    return status
