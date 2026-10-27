"""Private object storage for PrintMe documents.

The S3 adapter reads ``PRINTME_AWS_*``, ``PRINTME_S3_BUCKET``, and
``PRINTME_S3_PREFIX``. It does not use Saleor's media credentials. Object
keys stay under that prefix. This module does not build public URLs,
presigned URLs, or public ACLs.

Accepted PDFs are stored only under ``raw/documents/``. Raw SES messages are
addressed under ``raw/inbound/`` and are deleted by the worker only after a
durable outcome, and only when that inbound prefix validates the key.

Planned S3 lifecycle rules, created in AWS and not by this code: expire
``raw/inbound/`` after 7 days, and expire ``raw/documents/`` after 7 days so
an upload orphaned by a crash cannot outlive the access code.
"""

from dataclasses import dataclass
from typing import Protocol

from django.conf import settings

_MISSING_CODES = {"404", "NoSuchKey", "NotFound"}
_MAX_KEY_LENGTH = 1024
RAW_INBOUND_PREFIX = "raw/inbound/"
DOCUMENT_PREFIX = "raw/documents/"


class StorageError(Exception):
    """Object storage could not complete an operation."""


class StorageConfigError(StorageError):
    """PrintMe storage settings are missing or invalid."""


class StorageKeyError(StorageError):
    """The object key is outside the configured PrintMe prefix."""


@dataclass(frozen=True)
class StoredObject:
    bucket: str
    key: str
    byte_size: int


class ObjectStorage(Protocol):
    bucket: str
    prefix: str

    def allows(self, bucket: str, key: str) -> bool:
        """Return whether the location is inside this store."""

    def upload(self, key: str, content: bytes, *, content_type: str) -> StoredObject:
        """Store ``content`` and return its private location."""

    def read_metadata(self, key: str) -> StoredObject | None:
        """Return size and location, or None when the object is absent."""

    def read(self, key: str) -> bytes | None:
        """Return object bytes, or None when the object is absent."""

    def delete(self, key: str) -> None:
        """Delete one object inside the configured prefix."""


class S3ObjectStorage:
    """S3 adapter bound to the PrintMe bucket and prefix."""

    def __init__(
        self, client=None, *, bucket: str | None = None, prefix: str | None = None
    ):
        self._client = client
        self.bucket = bucket if bucket is not None else settings.PRINTME_S3_BUCKET
        self.prefix = normalize_prefix(
            settings.PRINTME_S3_PREFIX if prefix is None else prefix
        )
        if not _valid_bucket(self.bucket):
            raise StorageConfigError("PRINTME_S3_BUCKET is not configured")

    def allows(self, bucket: str, key: str) -> bool:
        if bucket != self.bucket or not isinstance(key, str):
            return False
        try:
            return self._full_key(key) == key.strip()
        except StorageKeyError:
            return False

    def upload(self, key: str, content: bytes, *, content_type: str) -> StoredObject:
        object_key = self._full_key(key)
        if not isinstance(content, (bytes, bytearray)):
            raise StorageError("Object content must be bytes")
        body = bytes(content)
        self._s3().put_object(
            Bucket=self.bucket,
            Key=object_key,
            Body=body,
            ContentType=content_type,
            ServerSideEncryption="AES256",
            CacheControl="private, no-store",
        )
        return StoredObject(bucket=self.bucket, key=object_key, byte_size=len(body))

    def read_metadata(self, key: str) -> StoredObject | None:
        object_key = self._full_key(key)
        try:
            response = self._s3().head_object(Bucket=self.bucket, Key=object_key)
        except StorageError:
            raise
        except Exception as exc:
            if _error_code(exc) in _MISSING_CODES:
                return None
            raise StorageError("Object metadata could not be read") from exc
        try:
            size = int(response["ContentLength"])
        except (KeyError, TypeError, ValueError) as exc:
            raise StorageError("Object metadata is incomplete") from exc
        return StoredObject(bucket=self.bucket, key=object_key, byte_size=size)

    def read(self, key: str) -> bytes | None:
        object_key = self._full_key(key)
        try:
            response = self._s3().get_object(Bucket=self.bucket, Key=object_key)
        except Exception as exc:
            if _error_code(exc) in _MISSING_CODES:
                return None
            raise StorageError("Object could not be read") from None
        body = response.get("Body")
        try:
            read = getattr(body, "read", None)
            if not callable(read):
                raise StorageError("Object could not be read")
            length = response.get("ContentLength")
            limit = int(settings.PRINTME_MAX_BYTES)
            if isinstance(length, int) and length > limit:
                raise StorageError("Object could not be read")
            content = read()
            if not isinstance(content, bytes) or len(content) > limit:
                raise StorageError("Object could not be read")
            return content
        except StorageError:
            raise
        except Exception:
            raise StorageError("Object could not be read") from None
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def delete(self, key: str) -> None:
        object_key = self._full_key(key)
        self._s3().delete_object(Bucket=self.bucket, Key=object_key)

    def _full_key(self, key: str) -> str:
        if not isinstance(key, str):
            raise StorageKeyError("Object key is outside the PrintMe prefix")
        cleaned = key.strip()
        if (
            not cleaned
            or cleaned.startswith("/")
            or "\\" in cleaned
            or cleaned.endswith("/")
        ):
            raise StorageKeyError("Object key is outside the PrintMe prefix")
        relative = cleaned.removeprefix(self.prefix)
        parts = relative.split("/")
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise StorageKeyError("Object key is outside the PrintMe prefix")
        full = f"{self.prefix}{relative}"
        if not full.startswith(self.prefix) or len(full) > _MAX_KEY_LENGTH:
            raise StorageKeyError("Object key is outside the PrintMe prefix")
        return full

    def _s3(self):
        if self._client is not None:
            return self._client
        key_id = str(settings.PRINTME_AWS_ACCESS_KEY_ID or "")
        secret = str(settings.PRINTME_AWS_SECRET_ACCESS_KEY or "")
        region = str(settings.PRINTME_AWS_REGION or "")
        if not key_id or not secret or not region:
            raise StorageConfigError("PrintMe AWS credentials are not configured")
        import boto3

        self._client = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=key_id,
            aws_secret_access_key=secret,
        )
        return self._client


def raw_inbound_key(storage: ObjectStorage, bucket: str, key: str) -> bool:
    """Return whether ``key`` is a raw SES object this worker may delete."""

    return _explicit_key(storage, bucket, key, RAW_INBOUND_PREFIX)


def accepted_document_key(storage: ObjectStorage, bucket: str, key: str) -> bool:
    """Return whether ``key`` is an accepted PDF under ``raw/documents/``."""

    return _explicit_key(storage, bucket, key, DOCUMENT_PREFIX)


def _explicit_key(storage: ObjectStorage, bucket: str, key: str, prefix: str) -> bool:
    if not isinstance(key, str) or not key.startswith(prefix):
        return False
    if any(part in {"", ".", ".."} for part in key.split("/")):
        return False
    return bool(storage.allows(bucket, key))


def normalize_prefix(prefix: str) -> str:
    if not isinstance(prefix, str):
        raise StorageConfigError("PRINTME_S3_PREFIX is not configured")
    cleaned = prefix.strip().lstrip("/")
    if not cleaned or "\\" in cleaned:
        raise StorageConfigError("PRINTME_S3_PREFIX is not configured")
    parts = [part for part in cleaned.split("/") if part != ""]
    if not parts or any(part in {".", ".."} for part in parts):
        raise StorageConfigError("PRINTME_S3_PREFIX is not configured")
    return "/".join(parts) + "/"


def _valid_bucket(bucket: str) -> bool:
    if not isinstance(bucket, str):
        return False
    if len(bucket) < 3 or len(bucket) > 63 or "/" in bucket or "\\" in bucket:
        return False
    return " " not in bucket


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    error = response.get("Error", {})
    if not isinstance(error, dict):
        return ""
    return str(error.get("Code", ""))
