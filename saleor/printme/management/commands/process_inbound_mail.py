"""Poll the PrintMe inbound queue in this process.

Run with ``python manage.py process_inbound_mail``. This command is not a
Celery task and is not added to ``CELERY_BEAT_SCHEDULE``.
"""

import logging
import signal

from django.conf import settings
from django.core.management.base import BaseCommand

from ...services.codes import CodeKeyError, ensure_access_code_key
from ...services.inbound import handle_delivery
from ...services.ingest import _inbound_address, _normalize_email
from ...services.storage import S3ObjectStorage

logger = logging.getLogger("saleor.printme")

_WAIT_SECONDS = 20
_VISIBILITY_SECONDS = 120
_MAX_MESSAGES = 1


class Command(BaseCommand):
    help = "Poll the PrintMe SQS queue and ingest inbound mail. Not a Celery task."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Receive one batch and exit.",
        )

    def handle(self, *args, **options):
        problem = worker_startup_error()
        if problem:
            self.stderr.write(problem)
            return

        queue_url = str(settings.PRINTME_SQS_QUEUE_URL)
        stop = {"value": False}

        def request_stop(signum, frame):
            stop["value"] = True

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        client = _sqs_client()
        storage = S3ObjectStorage()
        while not stop["value"]:
            try:
                _poll_once(client, queue_url, storage)
            except Exception:
                logger.info("printme inbound - error")
            if options["once"] or stop["value"]:
                break


def _poll_once(client, queue_url: str, storage) -> int:
    response = client.receive_message(
        QueueUrl=queue_url,
        MaxNumberOfMessages=_MAX_MESSAGES,
        WaitTimeSeconds=_WAIT_SECONDS,
        VisibilityTimeout=_VISIBILITY_SECONDS,
    )
    messages = response.get("Messages") or []
    queue = _SqsQueue(client, queue_url)
    for message in messages:
        body = message.get("Body")
        handle = message.get("ReceiptHandle")
        if not isinstance(body, str) or not isinstance(handle, str):
            logger.info("printme inbound - malformed")
            continue
        handle_delivery(body, handle, queue=queue, storage=storage)
    return len(messages)


class _SqsQueue:
    def __init__(self, client, queue_url: str):
        self.client = client
        self.queue_url = queue_url

    def delete(self, receipt_handle: str) -> None:
        self.client.delete_message(
            QueueUrl=self.queue_url, ReceiptHandle=receipt_handle
        )


def worker_startup_error() -> str | None:
    """Return why the worker must not poll, or None when it may start.

    The message names missing settings and never includes their values.
    """

    required = (
        "PRINTME_INBOUND_ADDRESS",
        "PRINTME_S3_BUCKET",
        "PRINTME_SQS_QUEUE_URL",
        "PRINTME_SES_FROM_ADDRESS",
        "PRINTME_AWS_REGION",
        "PRINTME_AWS_ACCESS_KEY_ID",
        "PRINTME_AWS_SECRET_ACCESS_KEY",
        "PRINTME_CODE_KEY",
    )
    missing = [
        name for name in required if not str(getattr(settings, name, "") or "").strip()
    ]
    if missing:
        return "PrintMe worker is missing " + ", ".join(missing)
    if str(settings.PRINTME_EMAIL_BACKEND or "").strip().lower() != "ses":
        return "PRINTME_EMAIL_BACKEND must be ses"
    try:
        ensure_access_code_key()
    except CodeKeyError:
        return "PRINTME_CODE_KEY is not a valid Fernet key"
    if _inbound_address() is None:
        return "PRINTME_INBOUND_ADDRESS is not a valid email address"
    if _normalize_email(str(settings.PRINTME_SES_FROM_ADDRESS or "")) is None:
        return "PRINTME_SES_FROM_ADDRESS is not a valid email address"
    return None


def _sqs_client():
    key_id = str(settings.PRINTME_AWS_ACCESS_KEY_ID or "")
    secret = str(settings.PRINTME_AWS_SECRET_ACCESS_KEY or "")
    region = str(settings.PRINTME_AWS_REGION or "")
    if not key_id or not secret or not region:
        raise RuntimeError("PrintMe AWS credentials are not configured")
    import boto3

    return boto3.client(
        "sqs",
        region_name=region,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
    )
