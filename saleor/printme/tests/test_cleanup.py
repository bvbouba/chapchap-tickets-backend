from datetime import timedelta

import pytest
from django.utils import timezone

from saleor.printme import PrintJobStatus
from saleor.printme.models import PrintJob
from saleor.printme.services.cleanup import expire_due_jobs
from saleor.printme.services.codes import verify_access_code
from saleor.printme.services.notify import NotifyOutcome, notify_print_job
from saleor.printme.services.storage import StorageError, StoredObject

pytestmark = pytest.mark.django_db

BUCKET = "printme-test-bucket"
PDF_KEY = "raw/documents/job.pdf"
PDF = b"%PDF-1.4\nPRINTME-PDF-MARKER\n%%EOF\n"


class MemoryStorage:
    def __init__(self):
        self.bucket = BUCKET
        self.prefix = "raw/"
        self.objects = {}
        self.deletes = []
        self.fail_delete = False

    def allows(self, bucket, key):
        return (
            bucket == self.bucket
            and isinstance(key, str)
            and key.startswith(self.prefix)
            and ".." not in key.split("/")
        )

    def read(self, key):
        return self.objects.get(key)

    def upload(self, key, content, *, content_type):
        self.objects[key] = bytes(content)
        return StoredObject(bucket=self.bucket, key=key, byte_size=len(content))

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


def _job(*, key=PDF_KEY, hours=-1, status=PrintJobStatus.NOTIFIED, **overrides):
    values = {
        "ses_message_id": "ses-message-1",
        "sender": "sender@example.com",
        "recipient": "print@in.bavasim.com",
        "bucket": BUCKET,
        "object_key": key,
        "byte_size": len(PDF),
        "status": status,
        "code_hash": "pbkdf2_sha256$1$salt$hash",
        "code_ciphertext": "ciphertext-token",
        "expires_at": timezone.now() + timedelta(hours=hours),
    }
    values.update(overrides)
    return PrintJob.objects.create(**values)


def test_cleanup_deletes_the_stored_document(caplog):
    # given
    storage = MemoryStorage()
    storage.objects[PDF_KEY] = PDF
    job = _job()
    future = _job(
        ses_message_id="still-valid",
        key="raw/documents/future.pdf",
        hours=24,
    )
    storage.objects[future.object_key] = PDF
    caplog.set_level("INFO", logger="saleor.printme")

    # when
    expired = expire_due_jobs(storage=storage)

    # then
    assert expired == 1
    job.refresh_from_db()
    assert job.status == PrintJobStatus.EXPIRED
    assert job.ses_message_id == "ses-message-1"
    assert job.code_hash == ""
    assert job.code_ciphertext == ""
    assert job.object_key == PDF_KEY
    assert storage.deletes == [PDF_KEY]
    assert PDF_KEY not in storage.objects
    future.refresh_from_db()
    assert future.status == PrintJobStatus.NOTIFIED
    assert future.object_key in storage.objects
    assert b"PRINTME-PDF-MARKER" not in caplog.text.encode()
    assert "ciphertext-token" not in caplog.text


def test_missing_object_is_already_cleaned_up():
    # given
    storage = MemoryStorage()
    job = _job()

    # when
    expired = expire_due_jobs(storage=storage)

    # then
    assert expired == 1
    assert storage.deletes == []
    job.refresh_from_db()
    assert job.status == PrintJobStatus.EXPIRED
    assert job.code_hash == ""
    assert job.code_ciphertext == ""
    assert job.ses_message_id == "ses-message-1"


def test_deletion_failure_does_not_mark_the_job_expired():
    # given
    storage = MemoryStorage()
    storage.objects[PDF_KEY] = PDF
    storage.fail_delete = True
    job = _job()

    # when
    expired = expire_due_jobs(storage=storage)

    # then
    assert expired == 0
    assert storage.deletes == []
    assert storage.objects[PDF_KEY] == PDF
    job.refresh_from_db()
    assert job.status == PrintJobStatus.NOTIFIED
    assert job.code_hash
    assert job.code_ciphertext == "ciphertext-token"


def test_cleanup_skips_objects_outside_the_prefix():
    # given
    storage = MemoryStorage()
    storage.objects["other/secret.pdf"] = PDF
    job = _job(key="other/secret.pdf")

    # when
    expired = expire_due_jobs(storage=storage)

    # then
    assert expired == 0
    assert storage.deletes == []
    assert storage.objects["other/secret.pdf"] == PDF
    job.refresh_from_db()
    assert job.status == PrintJobStatus.NOTIFIED
    assert job.code_ciphertext == "ciphertext-token"


def test_repeated_cleanup_has_no_further_effect():
    # given
    storage = MemoryStorage()
    storage.objects[PDF_KEY] = PDF
    job = _job()
    expire_due_jobs(storage=storage)

    # when
    again = expire_due_jobs(storage=storage)

    # then
    assert again == 0
    assert storage.deletes == [PDF_KEY]
    job.refresh_from_db()
    assert job.status == PrintJobStatus.EXPIRED
    assert job.code_hash == ""
    assert job.code_ciphertext == ""
    assert job.ses_message_id == "ses-message-1"
    assert PrintJob.objects.count() == 1


def test_expired_job_cannot_validate_a_code_or_be_notified(monkeypatch):
    # given
    storage = MemoryStorage()
    storage.objects[PDF_KEY] = PDF
    job = _job()
    sends = []

    def record(self, fail_silently=False):
        sends.append(self.body)
        return 1

    monkeypatch.setattr(
        "django.core.mail.EmailMessage.send",
        record,
    )
    expire_due_jobs(storage=storage)
    job.refresh_from_db()

    # when
    verified = verify_access_code(job, "ciphertext-token")
    notified = notify_print_job(job)

    # then
    assert verified.ok is False
    assert notified.outcome == NotifyOutcome.FAILED
    assert sends == []
    job.refresh_from_db()
    assert job.status == PrintJobStatus.EXPIRED
    assert job.code_hash == ""
    assert job.code_ciphertext == ""
    assert PDF_KEY not in storage.objects
