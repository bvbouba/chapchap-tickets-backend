from io import StringIO

import pytest
from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from saleor.printme.env import printme_int
from saleor.printme.management.commands.process_inbound_mail import (
    Command,
    worker_startup_error,
)


def _ready(**overrides):
    values = {
        "PRINTME_INBOUND_ADDRESS": "print@in.bavasim.com",
        "PRINTME_S3_BUCKET": "printme-test-bucket",
        "PRINTME_SQS_QUEUE_URL": "https://sqs.example.com/printme",
        "PRINTME_SES_FROM_ADDRESS": "noreply@bavasim.com",
        "PRINTME_AWS_REGION": "us-east-1",
        "PRINTME_AWS_ACCESS_KEY_ID": "printme-key",
        "PRINTME_AWS_SECRET_ACCESS_KEY": "printme-secret",
        "PRINTME_CODE_KEY": Fernet.generate_key().decode(),
        "PRINTME_EMAIL_BACKEND": "ses",
    }
    values.update(overrides)
    return values


@override_settings(
    PRINTME_INBOUND_ADDRESS="",
    PRINTME_S3_BUCKET="",
    PRINTME_SQS_QUEUE_URL="",
    PRINTME_SES_FROM_ADDRESS="",
    PRINTME_AWS_REGION="",
    PRINTME_AWS_ACCESS_KEY_ID="",
    PRINTME_AWS_SECRET_ACCESS_KEY="",
    PRINTME_CODE_KEY="",
)
def test_missing_configuration_does_not_poll(monkeypatch):
    # given
    calls = []
    monkeypatch.setattr(
        "saleor.printme.management.commands.process_inbound_mail._sqs_client",
        lambda: calls.append("client"),
    )
    stderr = StringIO()

    # when
    Command(stdout=StringIO(), stderr=stderr).handle(once=True)

    # then
    assert calls == []
    message = stderr.getvalue()
    assert "PRINTME_INBOUND_ADDRESS" in message
    assert "PRINTME_S3_BUCKET" in message
    assert "PRINTME_SQS_QUEUE_URL" in message
    assert "PRINTME_SES_FROM_ADDRESS" in message
    assert "PRINTME_AWS_REGION" in message
    assert "PRINTME_CODE_KEY" in message
    assert "printme-secret" not in message


def test_django_backend_is_rejected(monkeypatch):
    # given
    calls = []
    monkeypatch.setattr(
        "saleor.printme.management.commands.process_inbound_mail._sqs_client",
        lambda: calls.append("client"),
    )
    stderr = StringIO()

    # when
    with override_settings(**_ready(PRINTME_EMAIL_BACKEND="django")):
        message = worker_startup_error()
        Command(stdout=StringIO(), stderr=stderr).handle(once=True)

    # then
    assert message == "PRINTME_EMAIL_BACKEND must be ses"
    assert stderr.getvalue().strip() == message
    assert calls == []


def test_invalid_fernet_key_is_rejected():
    # when
    with override_settings(**_ready(PRINTME_CODE_KEY="not-a-fernet-key")):
        message = worker_startup_error()

    # then
    assert message == "PRINTME_CODE_KEY is not a valid Fernet key"
    assert "not-a-fernet-key" not in message


def test_complete_configuration_is_accepted():
    # when / then
    with override_settings(**_ready()):
        assert worker_startup_error() is None


def test_empty_integer_setting_uses_the_default(monkeypatch):
    # given
    monkeypatch.setenv("PRINTME_MAX_BYTES", "  ")

    # when / then
    assert printme_int("PRINTME_MAX_BYTES", 32, minimum=0) == 32


def test_invalid_integer_setting_is_rejected(monkeypatch):
    # given
    monkeypatch.setenv("PRINTME_CODE_TTL_HOURS", "nope")

    # when / then
    with pytest.raises(ImproperlyConfigured, match="must be an integer") as raised:
        printme_int("PRINTME_CODE_TTL_HOURS", 24, minimum=1)
    assert "nope" not in str(raised.value)


def test_integer_below_the_minimum_is_rejected(monkeypatch):
    # given
    monkeypatch.setenv("PRINTME_MAX_VERIFY_ATTEMPTS", "0")

    # when / then
    with pytest.raises(ImproperlyConfigured, match="must be 1 or greater"):
        printme_int("PRINTME_MAX_VERIFY_ATTEMPTS", 5, minimum=1)
