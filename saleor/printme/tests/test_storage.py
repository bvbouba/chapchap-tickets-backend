import sys
import types

import pytest
from django.test import override_settings

from saleor.printme.services.storage import (
    S3ObjectStorage,
    StorageConfigError,
    StorageError,
    StorageKeyError,
)

BUCKET = "printme-test-bucket"
PREFIX = "raw/"


class FakeS3:
    def __init__(self, *, missing=False, broken=False):
        self.calls = []
        self.missing = missing
        self.broken = broken

    def put_object(self, **kwargs):
        self.calls.append(("put_object", kwargs))
        return {}

    def head_object(self, **kwargs):
        self.calls.append(("head_object", kwargs))
        if self.missing:
            error = Exception("missing")
            error.response = {"Error": {"Code": "404"}}
            raise error
        if self.broken:
            raise RuntimeError("metadata failed")
        return {"ContentLength": len(kwargs["Key"])}

    def delete_object(self, **kwargs):
        self.calls.append(("delete_object", kwargs))
        return {}

    def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        if self.missing:
            error = Exception("missing")
            error.response = {"Error": {"Code": "NoSuchKey"}}
            raise error

        class _Body:
            def read(self):
                return b"raw-mail"

            def close(self):
                return None

        return {"Body": _Body(), "ContentLength": 8}

    def generate_presigned_url(self, *args, **kwargs):
        raise AssertionError("presigned URL requested")


def _storage(client, *, bucket=BUCKET, prefix=PREFIX):
    return S3ObjectStorage(client, bucket=bucket, prefix=prefix)


def test_upload_stays_private_and_inside_the_prefix():
    # given
    client = FakeS3()
    storage = _storage(client)

    # when
    stored = storage.upload(
        "documents/job.pdf",
        b"%PDF-1.4",
        content_type="application/pdf",
    )

    # then
    assert stored.bucket == BUCKET
    assert stored.key == "raw/documents/job.pdf"
    assert stored.byte_size == len(b"%PDF-1.4")
    put_name, put = client.calls[0]
    assert put_name == "put_object"
    assert set(put) == {
        "Bucket",
        "Key",
        "Body",
        "ContentType",
        "ServerSideEncryption",
        "CacheControl",
    }
    assert put["Bucket"] == BUCKET
    assert put["Key"].startswith(PREFIX)
    assert put["Body"] == b"%PDF-1.4"
    assert put["ServerSideEncryption"] == "AES256"
    assert put["CacheControl"] == "private, no-store"
    assert "ACL" not in put


def test_upload_rejects_keys_outside_the_prefix():
    # given
    client = FakeS3()
    storage = _storage(client)

    # when / then
    with pytest.raises(StorageKeyError):
        storage.upload("../outside.pdf", b"%PDF-1.4", content_type="application/pdf")
    assert client.calls == []


def test_source_location_must_already_use_the_configured_bucket_and_prefix():
    # given
    storage = _storage(FakeS3())

    # then
    assert storage.allows(BUCKET, "raw/inbound/message") is True
    assert storage.allows("other-bucket", "raw/inbound/message") is False
    assert storage.allows(BUCKET, "elsewhere/message") is False
    assert storage.allows(BUCKET, "raw/../secret") is False


def test_read_metadata_and_delete_use_the_configured_bucket():
    # given
    client = FakeS3()
    storage = _storage(client)

    # when
    metadata = storage.read_metadata("raw/documents/job.pdf")
    storage.delete("documents/job.pdf")

    # then
    assert metadata is not None
    assert metadata.bucket == BUCKET
    assert metadata.key == "raw/documents/job.pdf"
    assert client.calls[0][0] == "head_object"
    assert client.calls[0][1]["Bucket"] == BUCKET
    assert client.calls[1] == (
        "delete_object",
        {"Bucket": BUCKET, "Key": "raw/documents/job.pdf"},
    )


def test_read_stays_inside_the_configured_bucket():
    # given
    client = FakeS3()
    storage = _storage(client)

    # when
    content = storage.read("raw/inbound/message")

    # then
    assert content == b"raw-mail"
    assert client.calls == [
        ("get_object", {"Bucket": BUCKET, "Key": "raw/inbound/message"})
    ]
    with pytest.raises(StorageKeyError):
        storage.read("../outside")
    assert len(client.calls) == 1
    assert _storage(FakeS3(missing=True)).read("raw/inbound/message") is None


def test_missing_object_metadata_returns_none():
    # given
    storage = _storage(FakeS3(missing=True))

    # when / then
    assert storage.read_metadata("raw/documents/missing.pdf") is None


def test_unexpected_metadata_errors_are_wrapped():
    # given
    storage = _storage(FakeS3(broken=True))

    # when / then
    with pytest.raises(
        StorageError, match="Object metadata could not be read"
    ) as raised:
        storage.read_metadata("raw/documents/job.pdf")
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert "metadata failed" not in str(raised.value)


@override_settings(
    PRINTME_S3_BUCKET=BUCKET,
    PRINTME_S3_PREFIX=PREFIX,
    PRINTME_AWS_REGION="us-east-1",
    PRINTME_AWS_ACCESS_KEY_ID="printme-key",
    PRINTME_AWS_SECRET_ACCESS_KEY="printme-secret",
)
def test_s3_client_uses_printme_credentials(monkeypatch):
    # given
    calls = {}

    def client(service, **kwargs):
        calls["service"] = service
        calls["kwargs"] = kwargs
        return FakeS3()

    boto3 = types.ModuleType("boto3")
    boto3.client = client
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    storage = S3ObjectStorage()

    # when
    storage.read_metadata("raw/documents/job.pdf")

    # then
    assert calls["service"] == "s3"
    assert calls["kwargs"] == {
        "region_name": "us-east-1",
        "aws_access_key_id": "printme-key",
        "aws_secret_access_key": "printme-secret",
    }


@override_settings(
    PRINTME_S3_BUCKET=BUCKET,
    PRINTME_S3_PREFIX=PREFIX,
    PRINTME_AWS_REGION="",
    PRINTME_AWS_ACCESS_KEY_ID="",
    PRINTME_AWS_SECRET_ACCESS_KEY="",
)
def test_missing_printme_credentials_do_not_build_a_client():
    # given
    storage = S3ObjectStorage()

    # when / then
    with pytest.raises(StorageConfigError):
        storage.read_metadata("raw/documents/job.pdf")
