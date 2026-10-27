"""Access codes for a print job.

Verification stores and checks a Django password hash. The plaintext is
returned once to the caller and is not written on the job.

``code_ciphertext`` is separate from that hash. It exists only so a future
send can be retried with the same code. It is written when ``PRINTME_CODE_KEY``
holds a Fernet key: 32 url-safe base64-encoded bytes from
``cryptography.fernet.Fernet.generate_key()``. Keep that key in the PrintMe
worker environment, not in ``SECRET_KEY`` and not in the shared Saleor env
file. Losing or rotating the key makes stored ciphertext unreadable; existing
password hashes still verify codes that were already delivered. An empty key
leaves ``code_ciphertext`` blank, so the plaintext return value is the only
copy available for the first send.
"""

import secrets
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.utils import timezone

from .. import PrintJobStatus
from ..models import PrintJob

_CODE_BYTES = 18
_MAX_CANDIDATE_LENGTH = 256
_UNUSABLE_STATUSES = {PrintJobStatus.EXPIRED, PrintJobStatus.REJECTED}


class CodeKeyError(Exception):
    """``PRINTME_CODE_KEY`` is missing, invalid, or cannot decrypt a code."""


@dataclass(frozen=True)
class AccessCodeVerification:
    """Public verification result. Failures carry no reason code."""

    ok: bool


_ACCEPTED = AccessCodeVerification(ok=True)
_REJECTED = AccessCodeVerification(ok=False)


def generate_access_code() -> str:
    """Return a new code from ``secrets.token_urlsafe``."""

    return secrets.token_urlsafe(_CODE_BYTES)


def ensure_access_code_key() -> None:
    """Raise ``CodeKeyError`` when the retry key is missing or invalid."""

    _fernet()


def issue_access_code(job: PrintJob) -> str:
    """Hash a new code onto ``job`` and return the plaintext once.

    Callers must not log the return value. The row stores the hash and,
    when a Fernet key is configured, a separate ciphertext.
    """

    code = generate_access_code()
    code_hash = make_password(code)
    ciphertext = _ciphertext_for(code)
    expires_at = timezone.now() + timedelta(hours=_ttl_hours())
    job.code_hash = code_hash
    job.code_ciphertext = ciphertext
    job.failed_attempts = 0
    job.locked_at = None
    job.expires_at = expires_at
    job.save(
        update_fields=[
            "code_hash",
            "code_ciphertext",
            "failed_attempts",
            "locked_at",
            "expires_at",
            "updated_at",
        ]
    )
    return code


def open_access_code(ciphertext: str) -> str:
    """Decrypt a stored code for a send retry.

    The result is plaintext and must not be logged.
    """

    try:
        token = ciphertext.encode("ascii")
    except (AttributeError, UnicodeEncodeError):
        raise CodeKeyError("Stored access code could not be decrypted") from None
    try:
        plaintext = _fernet().decrypt(token)
    except (CodeKeyError, InvalidToken, TypeError, ValueError):
        raise CodeKeyError("Stored access code could not be decrypted") from None
    return plaintext.decode("utf-8")


def verify_access_code(job: PrintJob | None, candidate: str) -> AccessCodeVerification:
    """Check ``candidate`` without disclosing why a check failed.

    Unknown, incorrect, expired, and locked attempts all return the same
    rejected result. An incorrect attempt on a still-usable job increments
    ``failed_attempts`` and sets ``locked_at`` at the configured limit.
    """

    if not isinstance(candidate, str):
        _burn_guess(secrets.token_urlsafe(_CODE_BYTES))
        return _REJECTED
    if not isinstance(job, PrintJob):
        _burn_guess(candidate)
        return _REJECTED

    try:
        with transaction.atomic():
            current = PrintJob.objects.select_for_update().get(pk=job.pk)
            if _is_unusable(current):
                _burn_guess(candidate)
                return _REJECTED
            if len(candidate) > _MAX_CANDIDATE_LENGTH or not check_password(
                candidate, current.code_hash
            ):
                _register_failure(current)
                return _REJECTED
            return _ACCEPTED
    except PrintJob.DoesNotExist:
        _burn_guess(candidate)
        return _REJECTED


def _ciphertext_for(code: str) -> str:
    key = str(settings.PRINTME_CODE_KEY or "")
    if not key:
        return ""
    token = _fernet(key).encrypt(code.encode("utf-8"))
    return token.decode("ascii")


def _fernet(key: str | None = None) -> Fernet:
    raw_key = str(settings.PRINTME_CODE_KEY or "") if key is None else key
    if not raw_key:
        raise CodeKeyError("PRINTME_CODE_KEY is not configured")
    try:
        material = raw_key.encode("ascii")
    except UnicodeEncodeError:
        raise CodeKeyError("PRINTME_CODE_KEY is not a valid Fernet key") from None
    try:
        return Fernet(material)
    except (TypeError, ValueError):
        raise CodeKeyError("PRINTME_CODE_KEY is not a valid Fernet key") from None


def _ttl_hours() -> int:
    return int(settings.PRINTME_CODE_TTL_HOURS)


def _max_attempts() -> int:
    return int(settings.PRINTME_MAX_VERIFY_ATTEMPTS)


def _is_unusable(job: PrintJob) -> bool:
    if job.status in _UNUSABLE_STATUSES or job.locked_at is not None:
        return True
    if not job.code_hash or job.expires_at is None:
        return True
    if job.expires_at <= timezone.now():
        return True
    return job.failed_attempts >= _max_attempts()


def _register_failure(job: PrintJob) -> None:
    job.failed_attempts += 1
    if job.failed_attempts >= _max_attempts():
        job.locked_at = timezone.now()
    job.save(update_fields=["failed_attempts", "locked_at", "updated_at"])


def _burn_guess(candidate: str) -> None:
    """Spend a hash check so rejected branches do similar work."""

    probe = candidate if 0 < len(candidate) <= _MAX_CANDIDATE_LENGTH else "invalid"
    check_password(probe, _dummy_hash())


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return make_password(secrets.token_urlsafe(_CODE_BYTES))
