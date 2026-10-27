import json
from datetime import timedelta
from email.message import EmailMessage

import pytest
from cryptography.fernet import Fernet
from django.core.mail import EmailMessage as DjangoEmail
from django.test import override_settings
from django.utils import timezone

from saleor.printme import PrintJobStatus
from saleor.printme.management.commands.process_inbound_mail import _poll_once
from saleor.printme.models import PrintJob
from saleor.printme.services.inbound import handle_delivery, parse_queue_body
from saleor.printme.services.storage import S3ObjectStorage, StorageError, StoredObject

pytestmark = pytest.mark.django_db

PDF = b"%PDF-1.4\nPRINTME-PDF-MARKER\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
SENDER = "sender@example.com"
RECIPIENT = "print@in.bavasim.com"
BUCKET = "printme-test-bucket"
SOURCE_KEY = "raw/inbound/ses-message-1"
QUEUE_URL = "https://sqs.example.com/printme"


class MemoryStorage:
    def __init__(self):
        self.bucket = BUCKET
        self.prefix = "raw/"
        self.objects = {}
        self.reads = []
        self.deletes = []
        self.fail_read = False
        self.fail_delete = False

    def allows(self, bucket, key):
        return (
            bucket == self.bucket
            and isinstance(key, str)
            and key.startswith(self.prefix)
            and ".." not in key.split("/")
        )

    def read(self, key):
        self.reads.append(key)
        if self.fail_read:
            raise StorageError("read failed")
        return self.objects.get(key)

    def upload(self, key, content, *, content_type):
        full = key if str(key).startswith(self.prefix) else f"{self.prefix}{key}"
        self.objects[full] = bytes(content)
        return StoredObject(bucket=self.bucket, key=full, byte_size=len(content))

    def read_metadata(self, key):
        content = self.objects.get(key)
        if content is None:
            return None
        return StoredObject(bucket=self.bucket, key=key, byte_size=len(content))

    def delete(self, key):
        if self.fail_delete:
            raise StorageError("delete failed")
        self.deletes.append(key)
        self.objects.pop(key, None)


class FakeQueue:
    def __init__(self):
        self.deleted = []

    def delete(self, receipt_handle):
        self.deleted.append(receipt_handle)


class FakeSqs:
    def __init__(self, messages):
        self.messages = messages
        self.receive_kwargs = None
        self.deleted = []

    def receive_message(self, **kwargs):
        self.receive_kwargs = kwargs
        return {"Messages": self.messages}

    def delete_message(self, **kwargs):
        self.deleted.append(kwargs["ReceiptHandle"])


@pytest.fixture
def printme_settings():
    with override_settings(
        PRINTME_CODE_KEY=Fernet.generate_key().decode(),
        PRINTME_INBOUND_ADDRESS=RECIPIENT,
        PRINTME_SES_FROM_ADDRESS="noreply@bavasim.com",
        PRINTME_EMAIL_BACKEND="django",
        EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend",
    ):
        yield


def _raw_email():
    message = EmailMessage()
    message["From"] = f"Print Customer <{SENDER}>"
    message["To"] = RECIPIENT
    message.set_content("Please print the attached file.")
    message.add_attachment(
        PDF, maintype="application", subtype="pdf", filename="job.pdf"
    )
    return message.as_bytes()


def _body(
    *, bucket=BUCKET, key=SOURCE_KEY, message_id="ses-message-1", recipient=RECIPIENT
):
    ses = {
        "notificationType": "Received",
        "mail": {"messageId": message_id, "destination": [recipient]},
        "receipt": {
            "recipients": [recipient],
            "spamVerdict": {"status": "PASS"},
            "virusVerdict": {"status": "PASS"},
            "action": {"type": "S3", "bucketName": bucket, "objectKey": key},
        },
    }
    return json.dumps({"Type": "Notification", "Message": json.dumps(ses)})


def _record_sends(monkeypatch):
    seen = []
    real_send = DjangoEmail.send

    def record(self, fail_silently=False):
        seen.append(self.body)
        assert self.attachments == []
        return real_send(self, fail_silently=fail_silently)

    monkeypatch.setattr(DjangoEmail, "send", record)
    return seen


def test_valid_notification_is_ingested_and_acknowledged(
    printme_settings, monkeypatch, caplog
):
    # given
    storage = MemoryStorage()
    storage.objects[SOURCE_KEY] = _raw_email()
    sends = _record_sends(monkeypatch)
    client = FakeSqs([{"Body": _body(), "ReceiptHandle": "rh-1"}])
    caplog.set_level("INFO", logger="saleor.printme")

    # when
    _poll_once(client, QUEUE_URL, storage)

    # then
    assert client.receive_kwargs["WaitTimeSeconds"] == 20
    assert client.receive_kwargs["MaxNumberOfMessages"] == 1
    assert client.receive_kwargs["VisibilityTimeout"] == 120
    assert client.deleted == ["rh-1"]
    assert storage.reads == [SOURCE_KEY]
    job = PrintJob.objects.get()
    assert job.ses_message_id == "ses-message-1"
    assert job.status == PrintJobStatus.NOTIFIED
    assert job.code_ciphertext == ""
    assert job.code_hash
    assert len(sends) == 1
    assert "PRINTME-PDF-MARKER" not in sends[0]
    assert "PRINTME-PDF-MARKER" not in caplog.text
    assert "Access code:" not in caplog.text


def test_malformed_notification_is_not_acknowledged(printme_settings):
    # given
    storage = MemoryStorage()
    queue = FakeQueue()

    # when
    status = handle_delivery("{", "rh-bad", queue=queue, storage=storage)

    # then
    assert status == "malformed"
    assert queue.deleted == []
    assert storage.reads == []
    assert PrintJob.objects.count() == 0


def test_wrong_bucket_or_prefix_is_not_read(printme_settings):
    # given
    storage = MemoryStorage()
    storage.objects[SOURCE_KEY] = _raw_email()
    queue = FakeQueue()

    # when
    wrong_bucket = handle_delivery(
        _body(bucket="other-bucket"),
        "rh-bucket",
        queue=queue,
        storage=storage,
    )
    wrong_prefix = handle_delivery(
        _body(key="elsewhere/message"),
        "rh-prefix",
        queue=queue,
        storage=storage,
    )

    # then
    assert wrong_bucket == "unexpected_object"
    assert wrong_prefix == "unexpected_object"
    assert storage.reads == []
    assert queue.deleted == []
    assert PrintJob.objects.count() == 0


def test_duplicate_delivery_does_not_create_another_job(printme_settings, monkeypatch):
    # given
    storage = MemoryStorage()
    storage.objects[SOURCE_KEY] = _raw_email()
    sends = _record_sends(monkeypatch)
    queue = FakeQueue()
    body = _body()

    # when
    first = handle_delivery(body, "rh-1", queue=queue, storage=storage)
    second = handle_delivery(body, "rh-2", queue=queue, storage=storage)

    # then
    assert first == "sent"
    assert second == "duplicate"
    assert PrintJob.objects.count() == 1
    assert len(sends) == 1
    assert queue.deleted == ["rh-1", "rh-2"]
    assert sum(key.startswith("raw/documents/") for key in storage.objects) == 1


def test_transient_s3_failure_is_retried(printme_settings, monkeypatch):
    # given
    storage = MemoryStorage()
    storage.objects[SOURCE_KEY] = _raw_email()
    storage.fail_read = True
    sends = _record_sends(monkeypatch)
    queue = FakeQueue()
    body = _body()

    # when
    failed = handle_delivery(body, "rh-1", queue=queue, storage=storage)

    # then
    assert failed == "storage"
    assert queue.deleted == []
    assert PrintJob.objects.count() == 0

    # when
    storage.fail_read = False
    retried = handle_delivery(body, "rh-1", queue=queue, storage=storage)

    # then
    assert retried == "sent"
    assert queue.deleted == ["rh-1"]
    assert len(sends) == 1


def test_transient_mail_failure_acks_only_after_success(printme_settings, monkeypatch):
    # given
    storage = MemoryStorage()
    storage.objects[SOURCE_KEY] = _raw_email()
    queue = FakeQueue()
    attempts = {"count": 0}
    real_send = DjangoEmail.send

    def flaky(self, fail_silently=False):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise OSError("mail unavailable")
        return real_send(self, fail_silently=fail_silently)

    monkeypatch.setattr(DjangoEmail, "send", flaky)
    body = _body()

    # when
    failed = handle_delivery(body, "rh-1", queue=queue, storage=storage)

    # then
    assert failed == "notify_failed"
    assert queue.deleted == []
    assert SOURCE_KEY in storage.objects
    assert SOURCE_KEY not in storage.deletes
    job = PrintJob.objects.get()
    assert job.status == PrintJobStatus.PENDING_EMAIL
    assert job.code_ciphertext
    first_hash = job.code_hash

    # when
    retried = handle_delivery(body, "rh-1", queue=queue, storage=storage)

    # then
    assert retried == "sent"
    assert queue.deleted == ["rh-1"]
    job.refresh_from_db()
    assert job.status == PrintJobStatus.NOTIFIED
    assert job.code_ciphertext == ""
    assert job.code_hash == first_hash
    assert PrintJob.objects.count() == 1
    assert SOURCE_KEY not in storage.objects
    assert storage.deletes == [SOURCE_KEY]


def test_encoded_parent_segment_is_not_read(printme_settings):
    # given
    client = _RejectingS3()
    storage = S3ObjectStorage(client=client, bucket=BUCKET, prefix="raw/")
    queue = FakeQueue()
    body = _body(key="raw/inbound/%2e%2e/secret")

    # when
    status = handle_delivery(body, "rh-trav", queue=queue, storage=storage)

    # then
    assert parse_queue_body(body).object_key == "raw/inbound/../secret"
    assert status == "unexpected_object"
    assert client.calls == []
    assert queue.deleted == []


def test_virus_verdict_is_not_downloaded_or_acknowledged(printme_settings):
    # given
    storage = MemoryStorage()
    storage.objects[SOURCE_KEY] = _raw_email()
    queue = FakeQueue()
    payload = json.loads(_body())
    notice = json.loads(payload["Message"])
    notice["receipt"]["virusVerdict"] = {"status": "FAIL"}
    payload["Message"] = json.dumps(notice)

    # when
    status = handle_delivery(
        json.dumps(payload), "rh-virus", queue=queue, storage=storage
    )

    # then
    assert status == "malformed"
    assert storage.reads == []
    assert queue.deleted == []
    assert PrintJob.objects.count() == 0


def test_expired_duplicate_is_acknowledged_without_sending(
    printme_settings, monkeypatch
):
    # given
    storage = MemoryStorage()
    storage.objects[SOURCE_KEY] = _raw_email()
    sends = _record_sends(monkeypatch)
    queue = FakeQueue()
    handle_delivery(_body(), "rh-1", queue=queue, storage=storage)
    job = PrintJob.objects.get()
    job.status = PrintJobStatus.EXPIRED
    job.code_hash = ""
    job.code_ciphertext = "still-stored"
    job.save(update_fields=["status", "code_hash", "code_ciphertext", "updated_at"])
    sends.clear()

    # when
    status = handle_delivery(_body(), "rh-2", queue=queue, storage=storage)

    # then
    assert status == "duplicate"
    assert sends == []
    assert queue.deleted == ["rh-1", "rh-2"]
    job.refresh_from_db()
    assert job.status == PrintJobStatus.EXPIRED
    assert job.code_ciphertext == "still-stored"
    assert job.code_hash == ""


def test_rejected_message_without_a_row_is_not_acknowledged(printme_settings):
    # given
    storage = MemoryStorage()
    storage.objects[SOURCE_KEY] = b"this is not an email"
    queue = FakeQueue()

    # when
    status = handle_delivery(_body(), "rh-bad-mail", queue=queue, storage=storage)

    # then
    assert status == "rejected"
    assert queue.deleted == []
    assert SOURCE_KEY in storage.objects
    assert storage.deletes == []
    assert PrintJob.objects.count() == 0


def test_successful_delivery_deletes_only_the_raw_inbound_object(
    printme_settings, monkeypatch
):
    # given
    storage = MemoryStorage()
    storage.objects[SOURCE_KEY] = _raw_email()
    _record_sends(monkeypatch)
    queue = FakeQueue()

    # when
    status = handle_delivery(_body(), "rh-1", queue=queue, storage=storage)

    # then
    assert status == "sent"
    assert queue.deleted == ["rh-1"]
    assert storage.deletes == [SOURCE_KEY]
    assert SOURCE_KEY not in storage.objects
    documents = [key for key in storage.objects if key.startswith("raw/documents/")]
    assert len(documents) == 1


def test_raw_delete_failure_is_not_acknowledged(printme_settings, monkeypatch):
    # given
    storage = MemoryStorage()
    storage.objects[SOURCE_KEY] = _raw_email()
    storage.fail_delete = True
    _record_sends(monkeypatch)
    queue = FakeQueue()
    body = _body()

    # when
    failed = handle_delivery(body, "rh-1", queue=queue, storage=storage)

    # then
    assert failed == "raw_delete_failed"
    assert queue.deleted == []
    assert SOURCE_KEY in storage.objects
    job = PrintJob.objects.get()
    assert job.status == PrintJobStatus.NOTIFIED

    # when
    storage.fail_delete = False
    retried = handle_delivery(body, "rh-1", queue=queue, storage=storage)

    # then
    assert retried == "duplicate"
    assert queue.deleted == ["rh-1"]
    assert SOURCE_KEY not in storage.objects
    assert storage.deletes == [SOURCE_KEY]


def test_key_outside_raw_inbound_is_not_deleted(printme_settings, monkeypatch):
    # given
    retained = "raw/other/message"
    storage = MemoryStorage()
    storage.objects[retained] = _raw_email()
    _record_sends(monkeypatch)
    queue = FakeQueue()

    # when
    status = handle_delivery(_body(key=retained), "rh-1", queue=queue, storage=storage)

    # then
    assert status == "sent"
    assert queue.deleted == ["rh-1"]
    assert retained in storage.objects
    assert retained not in storage.deletes


def test_message_id_with_whitespace_is_not_logged(printme_settings, caplog):
    # given
    storage = MemoryStorage()
    queue = FakeQueue()
    caplog.set_level("INFO", logger="saleor.printme")

    # when
    status = handle_delivery(
        _body(message_id="ses message\nid"),
        "rh-id",
        queue=queue,
        storage=storage,
    )

    # then
    assert status == "malformed"
    assert queue.deleted == []
    assert storage.reads == []
    assert "ses message" not in caplog.text
    assert PrintJob.objects.count() == 0


def test_expired_pending_job_is_acknowledged_without_sending(
    printme_settings, monkeypatch
):
    # given
    storage = MemoryStorage()
    storage.objects[SOURCE_KEY] = _raw_email()
    sends = _record_sends(monkeypatch)
    queue = FakeQueue()
    job = PrintJob.objects.create(
        ses_message_id="ses-message-1",
        sender=SENDER,
        recipient=RECIPIENT,
        bucket=BUCKET,
        object_key="raw/documents/job.pdf",
        status=PrintJobStatus.PENDING_EMAIL,
        code_hash="pbkdf2_sha256$1$salt$hash",
        code_ciphertext="ciphertext-token",
        expires_at=timezone.now() - timedelta(minutes=1),
    )
    storage.objects[job.object_key] = PDF

    # when
    status = handle_delivery(_body(), "rh-old", queue=queue, storage=storage)

    # then
    assert status == "skipped"
    assert sends == []
    assert queue.deleted == ["rh-old"]
    assert SOURCE_KEY not in storage.objects
    assert job.object_key in storage.objects
    job.refresh_from_db()
    assert job.status == PrintJobStatus.PENDING_EMAIL
    assert job.code_ciphertext == "ciphertext-token"
    assert job.code_hash == "pbkdf2_sha256$1$salt$hash"


class _RejectingS3:
    def __init__(self):
        self.calls = []

    def get_object(self, **kwargs):
        self.calls.append(kwargs)
        raise AssertionError("object read")
