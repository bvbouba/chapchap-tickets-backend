import sys
import types
from datetime import timedelta
from email.message import EmailMessage as MimeEmail

import pytest
from cryptography.fernet import Fernet
from django.contrib.auth.hashers import check_password
from django.core.mail import EmailMessage
from django.test import override_settings
from django.utils import timezone

from saleor.printme import PrintJobStatus
from saleor.printme.models import PrintDocument, PrintJob
from saleor.printme.services.codes import open_access_code, verify_access_code
from saleor.printme.services.ingest import ingest_inbound_email
from saleor.printme.services.notify import NotifyOutcome, notify_print_job
from saleor.printme.services.storage import StoredObject

pytestmark = pytest.mark.django_db

PDF = b"%PDF-1.4\nPRINTME-PDF-MARKER\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
SENDER = "sender@example.com"
RECIPIENT = "print@in.bavasim.com"
BUCKET = "printme-test-bucket"
FROM_ADDRESS = "noreply@bavasim.com"


class MemoryStorage:
    def __init__(self):
        self.bucket = BUCKET
        self.prefix = "raw/"
        self.objects = {}

    def allows(self, bucket, key):
        return (
            bucket == self.bucket
            and isinstance(key, str)
            and key.startswith(self.prefix)
        )

    def upload(self, key, content, *, content_type):
        full = f"{self.prefix}{key}"
        self.objects[full] = bytes(content)
        return StoredObject(bucket=self.bucket, key=full, byte_size=len(content))

    def read_metadata(self, key):
        return None

    def delete(self, key):
        self.objects.pop(key, None)


class FakeSes:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    def send_email(self, **kwargs):
        if self.fail:
            raise RuntimeError("ses unavailable")
        self.calls.append(kwargs)
        return {"MessageId": "outbound-1"}

    def send_raw_email(self, **kwargs):
        raise AssertionError("raw email requested")


@pytest.fixture
def code_key():
    return Fernet.generate_key().decode()


@pytest.fixture
def printme_settings(code_key):
    with override_settings(
        PRINTME_CODE_KEY=code_key,
        PRINTME_INBOUND_ADDRESS=RECIPIENT,
        PRINTME_SES_FROM_ADDRESS=FROM_ADDRESS,
        PRINTME_CODE_TTL_HOURS=24,
    ):
        yield


def _pdf_message():
    message = MimeEmail()
    message["From"] = f"Print Customer <{SENDER}>"
    message["To"] = RECIPIENT
    message["Subject"] = "Print job"
    message.set_content("Please print the attached file.")
    message.add_attachment(
        PDF, maintype="application", subtype="pdf", filename="job.pdf"
    )
    return message.as_bytes()


def _accepted_job():
    storage = MemoryStorage()
    result = ingest_inbound_email(
        _pdf_message(),
        ses_message_id="ses-message-1",
        recipient=RECIPIENT,
        bucket=BUCKET,
        object_key="raw/inbound/ses-message-1",
        storage=storage,
    )
    job = PrintJob.objects.get(pk=result.job_id)
    return job, storage


def _bodies(monkeypatch):
    seen = []
    real_send = EmailMessage.send

    def record(self, fail_silently=False):
        seen.append(self.body)
        assert self.attachments == []
        return real_send(self, fail_silently=fail_silently)

    monkeypatch.setattr(EmailMessage, "send", record)
    return seen


def test_confirmation_uses_the_console_backend(printme_settings, monkeypatch, capsys):
    # given
    job, storage = _accepted_job()
    code = open_access_code(job.code_ciphertext)
    bodies = _bodies(monkeypatch)

    def forbid_new_code():
        raise AssertionError("new access code")

    monkeypatch.setattr(
        "saleor.printme.services.codes.generate_access_code",
        forbid_new_code,
    )

    # when
    result = notify_print_job(job)

    # then
    captured = capsys.readouterr().out
    assert result.outcome == NotifyOutcome.SENT
    assert len(bodies) == 1
    assert len(code) == 8
    assert set(code) <= set("0123456789ABCDEF")
    assert f"Release code: {code}" in bodies[0]
    assert "expires in 24 hours" in bodies[0]
    assert "print center" in bodies[0]
    assert "Document:\njob.pdf" in bodies[0]
    assert "Your document is ready to print" in bodies[0]
    assert "PRINTME-PDF-MARKER" not in bodies[0]
    assert "PRINTME-PDF-MARKER" not in captured
    assert PDF not in captured.encode()
    job.refresh_from_db()
    assert job.status == PrintJobStatus.NOTIFIED
    assert job.code_ciphertext == ""
    assert check_password(code, job.code_hash)
    assert storage.objects
    assert b"PRINTME-PDF-MARKER" in next(iter(storage.objects.values()))


def test_confirmation_lists_each_document_name(printme_settings, monkeypatch, caplog):
    # given
    job, _storage = _accepted_job()
    code = open_access_code(job.code_ciphertext)
    job.documents.all().delete()
    job.document_name = "one.pdf\ntwo.pdf"
    job.save(update_fields=["document_name", "updated_at"])
    bodies = _bodies(monkeypatch)
    caplog.set_level("INFO", logger="saleor.printme")

    # when
    result = notify_print_job(job)

    # then
    assert result.outcome == NotifyOutcome.SENT
    assert "Documents:\none.pdf\ntwo.pdf" in bodies[0]
    assert f"Release code: {code}" in bodies[0]
    assert "expires in 24 hours" in bodies[0]
    assert code not in caplog.text


def test_confirmation_names_accepted_and_rejected_files(
    printme_settings, monkeypatch
):
    # given
    job, _storage = _accepted_job()
    job.rejected_files = "photo.png\tunsupported"
    job.save(update_fields=["rejected_files", "updated_at"])
    bodies = _bodies(monkeypatch)

    # when
    result = notify_print_job(job)

    # then
    assert result.outcome == NotifyOutcome.SENT
    assert "Document:\njob.pdf" in bodies[0]
    assert "Not printed:\nphoto.png — not a PDF or Word document" in bodies[0]
    assert "Release code:" in bodies[0]


def test_rejection_email_lists_every_rejected_file(printme_settings, monkeypatch):
    # given
    job = PrintJob.objects.create(
        ses_message_id="all-rejected",
        sender=SENDER,
        recipient=RECIPIENT,
        status=PrintJobStatus.REJECTED,
        failure_reason="no_supported_document",
        rejected_files="photo.png\tunsupported\nbig.pdf\toversized",
    )
    bodies = _bodies(monkeypatch)

    # when
    result = notify_print_job(job)

    # then
    assert result.outcome == NotifyOutcome.SENT
    assert "Release code:" not in bodies[0]
    assert "photo.png — not a PDF or Word document" in bodies[0]
    assert "big.pdf — larger than 20 MB" in bodies[0]
    job.refresh_from_db()
    assert job.code_hash == ""


def test_child_documents_replace_the_legacy_name_list(printme_settings, monkeypatch):
    # given
    job, _storage = _accepted_job()
    job.document_name = "legacy.pdf"
    job.save(update_fields=["document_name", "updated_at"])
    document = job.documents.get()
    document.original_filename = "stored.pdf"
    document.save(update_fields=["original_filename"])
    PrintDocument.objects.create(
        job=job,
        position=2,
        original_filename="second.pdf",
        source_format="pdf",
        bucket=BUCKET,
        object_key="raw/documents/second.pdf",
        byte_size=4,
    )
    bodies = _bodies(monkeypatch)

    # when
    notify_print_job(job)

    # then
    assert "Documents:\nstored.pdf\nsecond.pdf" in bodies[0]
    assert "legacy.pdf" not in bodies[0]


def test_rejection_notification_has_no_access_code(
    printme_settings, monkeypatch, capsys
):
    # given
    job = PrintJob.objects.create(
        ses_message_id="rejected-message",
        sender=SENDER,
        recipient=RECIPIENT,
        status=PrintJobStatus.REJECTED,
        failure_reason="non_pdf",
    )
    bodies = _bodies(monkeypatch)

    # when
    result = notify_print_job(job)
    duplicate = notify_print_job(job)

    # then
    captured = capsys.readouterr().out
    assert result.outcome == NotifyOutcome.SENT
    assert duplicate.outcome == NotifyOutcome.SKIPPED
    assert len(bodies) == 1
    assert "not a PDF" in bodies[0]
    assert "Access code:" not in bodies[0]
    assert "PRINTME-PDF-MARKER" not in bodies[0]
    assert "PRINTME-PDF-MARKER" not in captured
    job.refresh_from_db()
    assert job.status == PrintJobStatus.REJECTED
    assert job.code_hash == ""
    assert job.code_ciphertext == ""
    assert job.locked_at is not None


def test_failed_send_retries_the_same_code(printme_settings, monkeypatch):
    # given
    job, _storage = _accepted_job()
    code = open_access_code(job.code_ciphertext)
    ciphertext = job.code_ciphertext
    code_hash = job.code_hash
    attempts = {"count": 0}
    real_send = EmailMessage.send

    def flaky(self, fail_silently=False):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise OSError("backend unavailable")
        assert self.attachments == []
        return real_send(self, fail_silently=fail_silently)

    monkeypatch.setattr(EmailMessage, "send", flaky)

    # when
    failed = notify_print_job(job)
    job.refresh_from_db()

    # then
    assert failed.outcome == NotifyOutcome.FAILED
    assert job.status == PrintJobStatus.PENDING_EMAIL
    assert job.code_ciphertext == ciphertext
    assert open_access_code(job.code_ciphertext) == code

    # when
    retried = notify_print_job(job)

    # then
    assert retried.outcome == NotifyOutcome.SENT
    job.refresh_from_db()
    assert job.status == PrintJobStatus.NOTIFIED
    assert job.code_ciphertext == ""
    assert job.code_hash == code_hash
    assert check_password(code, job.code_hash)


def test_duplicate_notification_does_not_send_again(printme_settings, monkeypatch):
    # given
    job, _storage = _accepted_job()
    bodies = _bodies(monkeypatch)
    notify_print_job(job)

    # when
    duplicate = notify_print_job(job)

    # then
    assert duplicate.outcome == NotifyOutcome.SKIPPED
    assert len(bodies) == 1
    job.refresh_from_db()
    assert job.status == PrintJobStatus.NOTIFIED
    assert job.code_ciphertext == ""


@override_settings(PRINTME_EMAIL_BACKEND="ses")
def test_ses_confirmation_is_mocked_and_omits_the_pdf(printme_settings):
    # given
    job, _storage = _accepted_job()
    code = open_access_code(job.code_ciphertext)
    client = FakeSes()

    # when
    result = notify_print_job(job, ses_client=client)

    # then
    assert result.outcome == NotifyOutcome.SENT
    assert len(client.calls) == 1
    call = client.calls[0]
    assert set(call) == {"Source", "Destination", "Message"}
    assert call["Source"] == FROM_ADDRESS
    assert call["Destination"] == {"ToAddresses": [SENDER]}
    body = call["Message"]["Body"]["Text"]["Data"]
    assert call["Message"]["Subject"]["Data"] == "Your document is ready to print"
    assert f"Release code: {code}" in body
    assert "Document:\njob.pdf" in body
    assert "PRINTME-PDF-MARKER" not in body
    assert "%PDF-" not in body
    job.refresh_from_db()
    assert job.code_ciphertext == ""


@override_settings(PRINTME_EMAIL_BACKEND="ses")
def test_ses_client_error_logs_only_the_error_code(printme_settings, caplog):
    # given
    from botocore.exceptions import ClientError

    job, _storage = _accepted_job()
    code = open_access_code(job.code_ciphertext)
    ciphertext = job.code_ciphertext
    caplog.set_level("INFO", logger="saleor.printme")

    class RejectingSes:
        def send_email(self, **kwargs):
            raise ClientError(
                {
                    "Error": {
                        "Code": "MessageRejected",
                        "Message": f"DO-NOT-LOG {code} {SENDER}",
                    }
                },
                "SendEmail",
            )

    # when
    result = notify_print_job(job, ses_client=RejectingSes())

    # then
    assert result.outcome == NotifyOutcome.FAILED
    assert "printme notify ClientError MessageRejected" in caplog.text
    assert code not in caplog.text
    assert "DO-NOT-LOG" not in caplog.text
    assert SENDER not in caplog.text
    job.refresh_from_db()
    assert job.status == PrintJobStatus.PENDING_EMAIL
    assert job.code_ciphertext == ciphertext


@override_settings(PRINTME_EMAIL_BACKEND="ses")
def test_ses_failure_without_an_error_code_logs_the_class_only(
    printme_settings, caplog
):
    # given
    job, _storage = _accepted_job()
    code = open_access_code(job.code_ciphertext)
    caplog.set_level("INFO", logger="saleor.printme")
    client = FakeSes(fail=True)

    # when
    result = notify_print_job(job, ses_client=client)

    # then
    assert result.outcome == NotifyOutcome.FAILED
    assert "printme notify RuntimeError" in caplog.text
    assert "ses unavailable" not in caplog.text
    assert code not in caplog.text
    job.refresh_from_db()
    assert job.status == PrintJobStatus.PENDING_EMAIL
    assert job.code_ciphertext


@override_settings(
    PRINTME_EMAIL_BACKEND="ses",
    PRINTME_AWS_REGION="",
    PRINTME_AWS_ACCESS_KEY_ID="",
    PRINTME_AWS_SECRET_ACCESS_KEY="",
)
def test_ses_without_credentials_does_not_mark_the_job_notified(printme_settings):
    # given
    job, _storage = _accepted_job()
    ciphertext = job.code_ciphertext

    # when
    result = notify_print_job(job)

    # then
    assert result.outcome == NotifyOutcome.FAILED
    job.refresh_from_db()
    assert job.status == PrintJobStatus.PENDING_EMAIL
    assert job.code_ciphertext == ciphertext


def test_ses_client_uses_printme_credentials(monkeypatch, printme_settings):
    # given
    calls = {}

    def client(service, **kwargs):
        calls["service"] = service
        calls["kwargs"] = kwargs
        return FakeSes()

    boto3 = types.ModuleType("boto3")
    boto3.client = client
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    job, _storage = _accepted_job()

    # when
    with override_settings(
        PRINTME_EMAIL_BACKEND="ses",
        PRINTME_AWS_REGION="us-east-1",
        PRINTME_AWS_ACCESS_KEY_ID="printme-key",
        PRINTME_AWS_SECRET_ACCESS_KEY="printme-secret",
    ):
        result = notify_print_job(job)

    # then
    assert result.outcome == NotifyOutcome.SENT
    assert calls["service"] == "ses"
    assert calls["kwargs"] == {
        "region_name": "us-east-1",
        "aws_access_key_id": "printme-key",
        "aws_secret_access_key": "printme-secret",
    }


def test_expired_job_is_not_reopened_or_sent(printme_settings, monkeypatch):
    # given
    job, _storage = _accepted_job()
    code = open_access_code(job.code_ciphertext)
    ciphertext = job.code_ciphertext
    job.status = PrintJobStatus.EXPIRED
    job.save(update_fields=["status", "updated_at"])
    bodies = _bodies(monkeypatch)

    # when
    result = notify_print_job(job)

    # then
    assert result.outcome == NotifyOutcome.FAILED
    assert bodies == []
    job.refresh_from_db()
    assert job.status == PrintJobStatus.EXPIRED
    assert job.code_ciphertext == ciphertext
    assert verify_access_code(job, code).ok is False


def test_past_expiry_does_not_decrypt_or_change_the_job(printme_settings, monkeypatch):
    # given
    job, _storage = _accepted_job()
    ciphertext = job.code_ciphertext
    code_hash = job.code_hash
    job.expires_at = timezone.now() - timedelta(seconds=1)
    job.save(update_fields=["expires_at", "updated_at"])
    bodies = _bodies(monkeypatch)

    def explode(*args, **kwargs):
        raise AssertionError("access code decrypted")

    monkeypatch.setattr(
        "saleor.printme.services.notify.open_access_code",
        explode,
    )

    # when
    result = notify_print_job(job)

    # then
    assert result.outcome == NotifyOutcome.SKIPPED
    assert bodies == []
    job.refresh_from_db()
    assert job.status == PrintJobStatus.PENDING_EMAIL
    assert job.code_ciphertext == ciphertext
    assert job.code_hash == code_hash
    assert job.expires_at is not None


def test_sender_newline_is_not_sent(printme_settings, monkeypatch):
    # given
    job, _storage = _accepted_job()
    ciphertext = job.code_ciphertext
    job.sender = "sender@example.com\nBcc: evil@example.com"
    job.save(update_fields=["sender", "updated_at"])
    bodies = _bodies(monkeypatch)

    # when
    result = notify_print_job(job)

    # then
    assert result.outcome == NotifyOutcome.FAILED
    assert bodies == []
    job.refresh_from_db()
    assert job.status == PrintJobStatus.PENDING_EMAIL
    assert job.code_ciphertext == ciphertext
