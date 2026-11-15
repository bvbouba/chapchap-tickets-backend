from dataclasses import fields
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from django.contrib.auth.hashers import check_password
from django.test import override_settings
from django.utils import timezone

from saleor.printme.models import PrintDocument, PrintJob
from saleor.printme.services.codes import (
    AccessCodeVerification,
    CodeKeyError,
    issue_access_code,
    open_access_code,
    verify_access_code,
)

pytestmark = pytest.mark.django_db

_REJECTED = AccessCodeVerification(ok=False)


def _job(**overrides):
    values = {
        "ses_message_id": "ses-message-1",
        "sender": "sender@example.com",
        "recipient": "print@in.bavasim.com",
    }
    values.update(overrides)
    return PrintJob.objects.create(**values)


@override_settings(PRINTME_CODE_TTL_HOURS=24, PRINTME_CODE_KEY="")
def test_generated_code_uses_digits_and_letters(monkeypatch):
    import saleor.printme.services.codes as codes

    seen = []

    def choice(alphabet):
        seen.append(alphabet)
        return "C"

    monkeypatch.setattr(codes.secrets, "choice", choice)

    assert codes.generate_access_code() == "CCCCCCCC"
    assert seen == ["0123456789ABCDEF"] * 8


def test_code_hash_differs_from_plaintext(caplog):
    # given
    job = _job()

    # when
    code = issue_access_code(job)

    # then
    assert len(code) == 8
    assert set(code) <= set("0123456789ABCDEF")
    assert job.code_hash
    assert job.code_hash != code
    assert code not in job.code_hash
    assert check_password(code, job.code_hash)
    assert job.code_ciphertext == ""
    assert code not in caplog.text


def test_correct_code_succeeds():
    # given
    job = _job()
    code = issue_access_code(job)

    # when
    result = verify_access_code(job, code)

    # then
    assert result == AccessCodeVerification(ok=True)
    job.refresh_from_db()
    assert job.failed_attempts == 0
    assert job.locked_at is None


def test_incorrect_code_fails():
    # given
    job = _job()
    code = issue_access_code(job)

    # when
    result = verify_access_code(job, f"{code}x")

    # then
    assert result == _REJECTED
    job.refresh_from_db()
    assert job.failed_attempts == 1
    assert job.locked_at is None


@override_settings(PRINTME_CODE_TTL_HOURS=24)
def test_expired_code_fails():
    # given
    job = _job()
    code = issue_access_code(job)
    assert job.expires_at is not None
    assert job.expires_at - timezone.now() > timedelta(hours=23, minutes=59)
    assert job.expires_at - timezone.now() <= timedelta(hours=24)
    job.expires_at = timezone.now() - timedelta(seconds=1)
    job.save(update_fields=["expires_at"])

    # when
    result = verify_access_code(job, code)

    # then
    assert result == _REJECTED
    job.refresh_from_db()
    assert job.failed_attempts == 0
    assert job.locked_at is None


@override_settings(PRINTME_MAX_VERIFY_ATTEMPTS=5)
def test_five_failed_attempts_lock_the_job():
    # given
    job = _job()
    code = issue_access_code(job)

    # when
    for _attempt in range(4):
        assert verify_access_code(job, "wrong-code") == _REJECTED
    fifth = verify_access_code(job, "wrong-code")

    # then
    assert fifth == _REJECTED
    job.refresh_from_db()
    assert job.failed_attempts == 5
    assert job.locked_at is not None
    assert verify_access_code(job, code) == _REJECTED
    job.refresh_from_db()
    assert job.failed_attempts == 5


def test_public_failure_results_are_equivalent():
    # given
    incorrect_job = _job(ses_message_id="incorrect")
    correct = issue_access_code(incorrect_job)
    expired_job = _job(ses_message_id="expired")
    expired_code = issue_access_code(expired_job)
    expired_job.expires_at = timezone.now() - timedelta(minutes=5)
    expired_job.save(update_fields=["expires_at"])
    locked_job = _job(ses_message_id="locked")
    locked_code = issue_access_code(locked_job)
    locked_job.locked_at = timezone.now()
    locked_job.failed_attempts = 5
    locked_job.save(update_fields=["locked_at", "failed_attempts"])

    # when
    incorrect = verify_access_code(incorrect_job, "wrong-code")
    expired = verify_access_code(expired_job, expired_code)
    locked = verify_access_code(locked_job, locked_code)
    unknown = verify_access_code(None, correct)

    # then
    assert incorrect == expired == locked == unknown == _REJECTED
    assert incorrect.ok is False
    assert [field.name for field in fields(incorrect)] == ["ok"]


@override_settings(PRINTME_CODE_KEY=Fernet.generate_key().decode())
def test_ciphertext_is_separate_from_the_password_hash():
    # given
    job = _job()

    # when
    code = issue_access_code(job)

    # then
    assert job.code_ciphertext
    assert job.code_ciphertext != code
    assert code not in job.code_ciphertext
    assert job.code_ciphertext != job.code_hash
    assert check_password(code, job.code_hash)
    assert check_password(code, job.code_ciphertext) is False
    assert open_access_code(job.code_ciphertext) == code


@override_settings(PRINTME_CODE_KEY="not-a-fernet-key")
def test_invalid_code_key_does_not_store_a_code():
    # given
    job = _job()

    # when
    with pytest.raises(CodeKeyError) as raised:
        issue_access_code(job)

    # then
    assert "not-a-fernet-key" not in str(raised.value)
    job.refresh_from_db()
    assert job.code_hash == ""
    assert job.code_ciphertext == ""


def test_eight_digit_code_keeps_leading_zeros(monkeypatch, caplog):
    # given
    import saleor.printme.services.codes as codes

    monkeypatch.setattr(codes, "generate_access_code", lambda: "00001234")
    job = _job()

    # when
    code = issue_access_code(job)

    # then
    assert code == "00001234"
    assert verify_access_code(job, code) == AccessCodeVerification(ok=True)
    assert verify_access_code(job, "1234") == _REJECTED
    assert code not in caplog.text
    job.refresh_from_db()
    assert job.failed_attempts == 1
    assert job.expires_at - timezone.now() > timedelta(hours=23, minutes=59)
    PrintDocument.objects.create(
        job=job,
        position=1,
        original_filename="job.pdf",
        source_format="pdf",
        bucket="printme-test-bucket",
        object_key="raw/documents/job.pdf",
        byte_size=4,
    )
    job.refresh_from_db()
    assert verify_access_code(job, "00001234").ok is True
    job.locked_at = timezone.now()
    job.save(update_fields=["locked_at", "updated_at"])
    assert verify_access_code(job, "00001234").ok is False
