import importlib

import pytest
from django.apps import apps
from django.db import IntegrityError, transaction

from saleor.printme.backfill import backfill_print_documents
from saleor.printme.models import PrintDocument, PrintJob

pytestmark = pytest.mark.django_db


def _job(message_id, **overrides):
    values = {
        "ses_message_id": message_id,
        "sender": "sender@example.com",
        "recipient": "print@in.bavasim.com",
        "status": "pending_email",
        "code_hash": "stored-hash",
        "code_ciphertext": "stored-cipher",
    }
    values.update(overrides)
    return PrintJob.objects.create(**values)


def test_migration_backfill_copies_one_document_and_keeps_the_job():
    # given
    legacy = _job(
        "legacy-message",
        bucket="printme-test-bucket",
        object_key="raw/documents/legacy.pdf",
        document_name="Legacy.pdf\nignored.pdf",
        byte_size=12,
    )
    _job("empty-message")
    _job(
        "outside-message",
        bucket="printme-test-bucket",
        object_key="other/file.pdf",
        byte_size=4,
    )

    # when
    created = backfill_print_documents(PrintJob, PrintDocument)
    migration = importlib.import_module(
        "saleor.printme.migrations.0003_printdocument"
    )
    migration.backfill_documents(apps, None)

    # then
    assert created == 1
    document = legacy.documents.get()
    assert document.position == 1
    assert document.original_filename == "Legacy.pdf"
    assert document.source_format == "pdf"
    assert document.printable_format == "pdf"
    assert document.bucket == "printme-test-bucket"
    assert document.object_key == "raw/documents/legacy.pdf"
    assert document.byte_size == 12
    assert document.status == "stored"
    legacy.refresh_from_db()
    assert legacy.code_hash == "stored-hash"
    assert legacy.code_ciphertext == "stored-cipher"
    assert legacy.object_key == "raw/documents/legacy.pdf"
    assert PrintDocument.objects.count() == 1
    assert backfill_print_documents(PrintJob, PrintDocument) == 0


def test_document_position_and_storage_key_are_unique():
    # given
    job = _job("unique-message")
    PrintDocument.objects.create(
        job=job,
        position=1,
        original_filename="one.pdf",
        source_format="pdf",
        bucket="printme-test-bucket",
        object_key="raw/documents/one.pdf",
        byte_size=1,
    )

    # when / then
    with pytest.raises(IntegrityError), transaction.atomic():
        PrintDocument.objects.create(
            job=job,
            position=1,
            original_filename="again.pdf",
            source_format="pdf",
            bucket="printme-test-bucket",
            object_key="raw/documents/again.pdf",
            byte_size=1,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        PrintDocument.objects.create(
            job=job,
            position=2,
            original_filename="copy.pdf",
            source_format="pdf",
            bucket="printme-test-bucket",
            object_key="raw/documents/one.pdf",
            byte_size=1,
        )
