from dataclasses import fields
from email.message import EmailMessage

import pytest
from cryptography.fernet import Fernet
from django.contrib.auth.hashers import check_password
from django.core.mail import EmailMessage as DjangoEmailMessage
from django.core.mail import EmailMultiAlternatives
from django.db import DatabaseError
from django.test import override_settings

from saleor.printme import PrintJobStatus
from saleor.printme.models import PrintJob
from saleor.printme.services.codes import open_access_code
from saleor.printme.services.ingest import (
    IngestFailure,
    IngestOutcome,
    _delete_if_unowned,
    ingest_inbound_email,
)
from saleor.printme.services.parse_email import EmailRejection
from saleor.printme.services.storage import StorageError, StoredObject

pytestmark = pytest.mark.django_db

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
OTHER_PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
SENDER = "sender@example.com"
RECIPIENT = "print@in.bavasim.com"
MESSAGE_ID = "ses-message-1"
BUCKET = "printme-test-bucket"
SOURCE_KEY = "raw/inbound/ses-message-1"


class MemoryStorage:
    def __init__(self):
        self.bucket = BUCKET
        self.prefix = "raw/"
        self.objects = {}
        self.deletes = []
        self.fail_upload = False
        self.force_key = None

    def allows(self, bucket, key):
        return (
            bucket == self.bucket
            and isinstance(key, str)
            and key.startswith(self.prefix)
            and ".." not in key.split("/")
        )

    def upload(self, key, content, *, content_type):
        if self.fail_upload:
            raise StorageError("upload failed")
        full = self.force_key or f"{self.prefix}{key}"
        self.objects[full] = (bytes(content), content_type)
        return StoredObject(bucket=self.bucket, key=full, byte_size=len(content))

    def read_metadata(self, key):
        full = key if key.startswith(self.prefix) else f"{self.prefix}{key}"
        content = self.objects.get(full)
        if content is None:
            return None
        return StoredObject(bucket=self.bucket, key=full, byte_size=len(content[0]))

    def delete(self, key):
        self.deletes.append(key)
        self.objects.pop(key, None)


@pytest.fixture
def code_key():
    return Fernet.generate_key().decode()


@pytest.fixture
def printme_settings(code_key):
    with override_settings(
        PRINTME_CODE_KEY=code_key,
        PRINTME_INBOUND_ADDRESS=RECIPIENT,
        PRINTME_S3_BUCKET=BUCKET,
        PRINTME_S3_PREFIX="raw/",
    ):
        yield


@pytest.fixture(autouse=True)
def forbid_email(monkeypatch):
    def fail_send(*args, **kwargs):
        raise AssertionError("confirmation email sent")

    monkeypatch.setattr(DjangoEmailMessage, "send", fail_send)
    monkeypatch.setattr(EmailMultiAlternatives, "send", fail_send)
    monkeypatch.setattr("django.core.mail.send_mail", fail_send)


def _message(pdf=PDF, *, filename="notes.txt", content_type="application/octet-stream"):
    maintype, subtype = content_type.split("/", 1)
    message = EmailMessage()
    message["From"] = f"Print Customer <{SENDER}>"
    message["To"] = RECIPIENT
    message["Subject"] = "Print job"
    message.set_content("Please print the attached file.")
    message.add_attachment(pdf, maintype=maintype, subtype=subtype, filename=filename)
    return message.as_bytes()


def _ingest(storage, source=None, **overrides):
    values = {
        "ses_message_id": MESSAGE_ID,
        "recipient": RECIPIENT,
        "bucket": BUCKET,
        "object_key": SOURCE_KEY,
        "storage": storage,
    }
    values.update(overrides)
    return ingest_inbound_email(_message() if source is None else source, **values)


def _assert_document_absent(job, document):
    for field in job._meta.fields:
        value = getattr(job, field.name)
        if isinstance(value, str) and value:
            assert document not in value.encode()


def test_valid_pdf_is_stored_as_metadata(printme_settings, caplog):
    # given
    storage = MemoryStorage()

    # when
    result = _ingest(storage)

    # then
    assert result.outcome == IngestOutcome.ACCEPTED
    assert result.failure_reason == ""
    assert [field.name for field in fields(result)] == [
        "outcome",
        "job_id",
        "failure_reason",
    ]
    job = PrintJob.objects.get(pk=result.job_id)
    assert job.ses_message_id == MESSAGE_ID
    assert job.sender == SENDER
    assert job.recipient == RECIPIENT
    assert job.status == PrintJobStatus.PENDING_EMAIL
    assert job.bucket == BUCKET
    assert job.object_key.startswith("raw/documents/")
    assert job.byte_size == len(PDF)
    assert job.code_hash
    assert job.code_ciphertext
    assert storage.objects[job.object_key][0] == PDF
    code = open_access_code(job.code_ciphertext)
    assert check_password(code, job.code_hash)
    assert code not in job.code_hash
    assert code not in job.code_ciphertext
    _assert_document_absent(job, PDF)
    _assert_document_absent(job, code.encode())
    assert PDF not in caplog.text.encode()
    assert code not in caplog.text


def test_invalid_attachment_is_rejected_without_storage(printme_settings):
    # given
    storage = MemoryStorage()
    source = _message(
        b"not a pdf", filename="report.pdf", content_type="application/pdf"
    )

    # when
    result = _ingest(storage, source)

    # then
    assert result.outcome == IngestOutcome.REJECTED
    assert result.failure_reason == EmailRejection.NON_PDF
    job = PrintJob.objects.get(pk=result.job_id)
    assert job.status == PrintJobStatus.REJECTED
    assert job.bucket == ""
    assert job.object_key == ""
    assert job.byte_size is None
    assert job.code_hash == ""
    assert job.code_ciphertext == ""
    assert storage.objects == {}
    assert storage.deletes == []
    _assert_document_absent(job, b"not a pdf")


def test_multiple_pdfs_are_rejected_without_storage(printme_settings):
    # given
    storage = MemoryStorage()
    message = EmailMessage()
    message["From"] = f"Print Customer <{SENDER}>"
    message["To"] = RECIPIENT
    message.set_content("Two files.")
    message.add_attachment(
        PDF, maintype="application", subtype="pdf", filename="one.pdf"
    )
    message.add_attachment(
        OTHER_PDF, maintype="application", subtype="pdf", filename="two.pdf"
    )

    # when
    result = _ingest(storage, message.as_bytes())

    # then
    assert result.outcome == IngestOutcome.REJECTED
    assert result.failure_reason == IngestFailure.MULTIPLE_PDFS
    job = PrintJob.objects.get(pk=result.job_id)
    assert job.status == PrintJobStatus.REJECTED
    assert job.object_key == ""
    assert job.byte_size is None
    assert storage.objects == {}


def test_unexpected_recipient_is_rejected(printme_settings):
    # given
    storage = MemoryStorage()

    # when
    result = _ingest(storage, recipient="other@example.com")

    # then
    assert result.outcome == IngestOutcome.REJECTED
    assert result.failure_reason == IngestFailure.UNEXPECTED_RECIPIENT
    job = PrintJob.objects.get(pk=result.job_id)
    assert job.status == PrintJobStatus.REJECTED
    assert job.object_key == ""
    assert storage.objects == {}


def test_duplicate_notification_does_not_replace_the_document(printme_settings):
    # given
    storage = MemoryStorage()
    first = _ingest(storage)
    job = PrintJob.objects.get(pk=first.job_id)
    original_key = job.object_key
    original_hash = job.code_hash

    # when
    second = _ingest(storage, _message(OTHER_PDF))

    # then
    assert second.outcome == IngestOutcome.DUPLICATE
    assert second.job_id == first.job_id
    assert PrintJob.objects.count() == 1
    job.refresh_from_db()
    assert job.object_key == original_key
    assert job.byte_size == len(PDF)
    assert job.code_hash == original_hash
    assert storage.objects[original_key][0] == PDF
    assert len(storage.objects) == 1
    assert storage.deletes == []


def test_unique_constraint_race_preserves_the_original_document(
    printme_settings, monkeypatch
):
    # given
    storage = MemoryStorage()
    existing = PrintJob.objects.create(
        ses_message_id=MESSAGE_ID,
        sender=SENDER,
        recipient=RECIPIENT,
        bucket=BUCKET,
        object_key="raw/documents/original.pdf",
        byte_size=12,
        status=PrintJobStatus.PENDING_EMAIL,
    )
    storage.objects[existing.object_key] = (b"%PDF-original", "application/pdf")
    original_filter = PrintJob.objects.filter
    state = {"hidden": False}

    def hide_existing_once(*args, **kwargs):
        if not state["hidden"] and kwargs.get("ses_message_id") == MESSAGE_ID:
            state["hidden"] = True
            return original_filter(ses_message_id="missing-message")
        return original_filter(*args, **kwargs)

    monkeypatch.setattr(PrintJob.objects, "filter", hide_existing_once)

    # when
    result = _ingest(storage, _message(OTHER_PDF))

    # then
    assert result.outcome == IngestOutcome.DUPLICATE
    assert result.job_id == existing.id
    assert PrintJob.objects.filter(ses_message_id=MESSAGE_ID).count() == 1
    existing.refresh_from_db()
    assert existing.object_key == "raw/documents/original.pdf"
    assert existing.byte_size == 12
    assert storage.objects[existing.object_key][0] == b"%PDF-original"
    assert existing.object_key not in storage.deletes
    assert storage.deletes
    assert all(key != existing.object_key for key in storage.deletes)


def test_cleanup_does_not_delete_an_object_owned_by_a_job(printme_settings):
    # given
    storage = MemoryStorage()
    job = PrintJob.objects.create(
        ses_message_id="owned-message",
        sender=SENDER,
        recipient=RECIPIENT,
        bucket=BUCKET,
        object_key="raw/documents/original.pdf",
        byte_size=3,
    )
    storage.objects[job.object_key] = (b"%PDF-original", "application/pdf")

    # when
    _delete_if_unowned(
        storage,
        StoredObject(bucket=BUCKET, key=job.object_key, byte_size=3),
    )

    # then
    assert storage.deletes == []
    assert storage.objects[job.object_key][0] == b"%PDF-original"


def test_upload_failure_creates_no_job(printme_settings):
    # given
    storage = MemoryStorage()
    storage.fail_upload = True

    # when
    result = _ingest(storage)

    # then
    assert result.outcome == IngestOutcome.FAILED
    assert result.failure_reason == IngestFailure.STORAGE
    assert result.job_id is None
    assert PrintJob.objects.count() == 0
    assert storage.deletes == []


def test_database_failure_after_upload_deletes_only_the_orphan(
    printme_settings, monkeypatch
):
    # given
    storage = MemoryStorage()
    keeper = PrintJob.objects.create(
        ses_message_id="keeper-message",
        sender=SENDER,
        recipient=RECIPIENT,
        bucket=BUCKET,
        object_key="raw/documents/keeper.pdf",
        byte_size=8,
    )
    storage.objects[keeper.object_key] = (b"%PDF-keeper", "application/pdf")

    def fail_create(*args, **kwargs):
        raise DatabaseError("database failed")

    monkeypatch.setattr(PrintJob.objects, "create", fail_create)

    # when
    result = _ingest(storage)

    # then
    assert result.outcome == IngestOutcome.FAILED
    assert result.failure_reason == IngestFailure.DATABASE
    assert "database failed" not in result.failure_reason
    assert PrintJob.objects.filter(ses_message_id=MESSAGE_ID).count() == 0
    assert keeper.object_key not in storage.deletes
    assert storage.deletes
    keeper.refresh_from_db()
    assert keeper.object_key == "raw/documents/keeper.pdf"
    assert storage.objects[keeper.object_key][0] == b"%PDF-keeper"


def test_empty_inbound_address_rejects_the_message(code_key):
    # given
    storage = MemoryStorage()

    # when
    with override_settings(PRINTME_CODE_KEY=code_key, PRINTME_INBOUND_ADDRESS=""):
        result = _ingest(storage)

    # then
    assert result.outcome == IngestOutcome.FAILED
    assert result.failure_reason == IngestFailure.UNEXPECTED_RECIPIENT
    assert result.job_id is None
    assert storage.objects == {}
    assert PrintJob.objects.count() == 0


def test_message_id_with_whitespace_is_rejected(printme_settings):
    # given
    storage = MemoryStorage()

    # when
    result = _ingest(storage, ses_message_id="ses\tmessage")

    # then
    assert result.outcome == IngestOutcome.REJECTED
    assert result.failure_reason == IngestFailure.MALFORMED
    assert storage.objects == {}
    assert PrintJob.objects.count() == 0


def test_pdf_is_stored_only_under_raw_documents(printme_settings):
    # given
    storage = MemoryStorage()
    outside = MemoryStorage()
    outside.prefix = "raw/incoming/"

    # when
    accepted = _ingest(storage)
    rejected = _ingest(
        outside,
        object_key="raw/incoming/message",
        ses_message_id="ses-message-2",
    )

    # then
    job = PrintJob.objects.get(pk=accepted.job_id)
    assert job.object_key.startswith("raw/documents/")
    assert job.object_key.endswith(".pdf")
    assert accepted.outcome == IngestOutcome.ACCEPTED
    assert rejected.outcome == IngestOutcome.FAILED
    assert rejected.failure_reason == IngestFailure.STORAGE
    assert outside.objects == {}


def test_unexpected_object_location_is_rejected(printme_settings):
    # given
    storage = MemoryStorage()

    # when
    result = _ingest(storage, bucket="other-bucket", object_key="elsewhere/message")

    # then
    assert result.outcome == IngestOutcome.REJECTED
    assert result.failure_reason == IngestFailure.UNEXPECTED_OBJECT
    assert PrintJob.objects.count() == 0
    assert storage.objects == {}
