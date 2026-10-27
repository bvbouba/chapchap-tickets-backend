from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from saleor.printme import PrintJobStatus
from saleor.printme.models import PrintJob

pytestmark = pytest.mark.django_db


def _job(**overrides):
    values = {
        "ses_message_id": "ses-message-1",
        "sender": "sender@example.com",
        "recipient": "print@in.bavasim.com",
    }
    values.update(overrides)
    return PrintJob.objects.create(**values)


def test_print_job_persists_identity_fields():
    # given / when
    job = _job()

    # then
    job.refresh_from_db()
    assert job.id is not None
    assert job.ses_message_id == "ses-message-1"
    assert job.sender == "sender@example.com"
    assert job.recipient == "print@in.bavasim.com"
    assert job.status == PrintJobStatus.PENDING_EMAIL
    assert job.failed_attempts == 0
    assert job.expires_at is None
    assert job.locked_at is None


def test_print_job_status_choices():
    # given
    expected = [
        (PrintJobStatus.PENDING_EMAIL, "Pending email"),
        (PrintJobStatus.NOTIFIED, "Notified"),
        (PrintJobStatus.REJECTED, "Rejected"),
        (PrintJobStatus.EXPIRED, "Expired"),
    ]

    # then
    assert PrintJobStatus.CHOICES == expected
    assert [value for value, _label in PrintJob._meta.get_field("status").choices] == [
        status for status, _label in expected
    ]

    job = _job()
    for status, _label in expected:
        job.status = status
        job.save(update_fields=["status"])
        job.refresh_from_db()
        assert job.status == status


def test_print_job_ses_message_id_is_unique():
    # given
    _job(ses_message_id="duplicate-message")

    # when / then
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            _job(
                ses_message_id="duplicate-message",
                sender="other@example.com",
            )


def test_print_job_expiry_and_lock_fields():
    # given
    expires_at = timezone.now().replace(microsecond=0) + timedelta(hours=24)
    locked_at = timezone.now().replace(microsecond=0)

    # when
    job = _job(expires_at=expires_at, locked_at=locked_at, failed_attempts=5)

    # then
    job.refresh_from_db()
    assert job.expires_at == expires_at
    assert job.locked_at == locked_at
    assert job.failed_attempts == 5
