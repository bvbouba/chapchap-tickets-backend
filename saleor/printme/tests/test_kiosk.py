import json
from datetime import timedelta
from pathlib import Path

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from saleor.printme import PrintJobStatus
from saleor.printme.kiosk.access import KIOSK_SESSION_KEY
from saleor.printme.models import PrintDocument, PrintDocumentStatus, PrintJob
from saleor.printme.services.codes import generate_access_code, issue_access_code

pytestmark = pytest.mark.django_db

_STATIC = Path(__file__).resolve().parents[1] / "static" / "printme"
_SCREENS = ("welcome", "code", "documents", "preview", "summary", "printing", "error")
_SETTINGS = {
    "TEMPLATES": [
        {
            "BACKEND": "django.template.backends.django.DjangoTemplates",
            "APP_DIRS": True,
            "OPTIONS": {"context_processors": []},
        }
    ],
    "MIDDLEWARE": [
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.middleware.common.CommonMiddleware",
        "django.middleware.csrf.CsrfViewMiddleware",
    ],
    "SESSION_ENGINE": "django.contrib.sessions.backends.signed_cookies",
}


class _Store:
    def __init__(self, files):
        self.files = files
        self.bucket = "printme-test-bucket"

    def allows(self, bucket, key):
        return bucket == self.bucket and key in self.files

    def read(self, key):
        return self.files.get(key)


def _job(**overrides):
    values = {
        "ses_message_id": "ses-message-1",
        "sender": "sender@example.com",
        "recipient": "print@example.com",
    }
    values.update(overrides)
    return PrintJob.objects.create(**values)


def _document(job, **overrides):
    values = {
        "job": job,
        "position": 1,
        "original_filename": "flyer.pdf",
        "source_format": "pdf",
        "printable_format": "pdf",
        "bucket": "printme-test-bucket",
        "object_key": "raw/documents/flyer.pdf",
        "byte_size": 8,
        "status": PrintDocumentStatus.STORED,
    }
    values.update(overrides)
    return PrintDocument.objects.create(**values)


def _client():
    client = Client(enforce_csrf_checks=True)
    page = client.get("/printme/kiosk/")
    token = client.cookies["csrftoken"].value
    return client, token, page


def _post(client, token, url, payload):
    return client.post(
        url,
        data=json.dumps(payload),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )


@override_settings(**_SETTINGS)
def test_kiosk_page_lists_every_screen():
    _client_obj, _token, response = _client()

    assert response.status_code == 200
    body = response.content.decode()
    for screen in _SCREENS:
        assert f'data-screen="{screen}"' in body
    assert 'data-idle-ms="120000"' in body
    assert 'data-idle-warning-ms="15000"' in body
    assert "Checking your release code…" in body
    assert "Pricing is not yet available." in body
    assert "All pages" in body
    assert "Custom range" in body
    assert "Not available yet." not in body
    assert "Fit page" not in body
    assert 'data-option="copies"' in body
    assert 'data-digit="A"' in body
    assert 'data-digit="F"' in body
    assert "Step 1 of 3" not in body
    assert "Step 2 of 3" not in body
    assert "Step 3 of 3" not in body
    assert 'class="start-print"' in body
    assert 'data-go-home' in body
    assert "Sort from A to Z" in body
    assert "Print job information" in body
    assert "Current print job total" in body
    assert 'class="screen-header"' in body
    assert 'class="screen-footer"' in body
    assert 'aria-label="Zoom in"' in body
    assert 'class="sr-only"' in body
    assert 'data-continue disabled' in body
    assert "No document has been sent to a printer." in body
    assert "This is a test print. Pricing is not yet available." in body
    assert "Printer connection is not configured yet." not in body
    assert 'data-print-url="/printme/kiosk/prints/"' in body
    assert "<iframe" not in body
    assert 'data-preview-canvas' in body
    assert 'class="screen-body entry"' in body
    assert 'class="entry-continue"' in body
    assert 'data-preview-base="/printme/kiosk/documents/"' in body
    assert "sample-flyer.pdf" not in body
    assert "verify_access_code" not in body
    assert "object_key" not in body
    assert "AKIA" not in body
    assert "PRINTME_AWS" not in body
    assert response.context["csrf_token"]


def test_kiosk_assets_align_the_keypad_and_clear_the_session():
    script = (_STATIC / "kiosk.js").read_text()
    mock = (_STATIC / "kiosk-mock.js").read_text()
    style = (_STATIC / "kiosk.css").read_text()

    assert ".keypad,\n.code-slots {\n  width: 100%;" in style
    assert "gap: 0.75rem;" in style
    assert "white-space: nowrap;" in style
    assert "min-height: 4.5rem;" in style
    assert "grid-template-columns: 1fr;" in style
    assert "screen-header" in style
    assert "screen-footer" in style
    assert "repeat(4, minmax(0, 1fr))" in style
    assert "toUpperCase()" in script
    assert "code.length >= 8" in script
    assert 'data-option-panel' in script
    assert 'show("preview");\n        return renderCurrentPage();' in script
    assert "function clearPage()" in script
    assert "function resetToWelcome()" in script
    assert "function pauseIdle()" in script
    assert "if (busy)" in script
    assert "clearServerSession" in script
    assert "setTimeout(showIdleWarning, idleMs)" in script
    assert "setTimeout(resetToWelcome, warningMs)" in script
    assert "continueButton.disabled = true" in script
    assert "openPreviewButton.disabled = true" in script
    assert "isCurrent(id)" in script
    assert "vendor/pdf.min.js" in script
    assert "<iframe" not in script
    assert "URL.revokeObjectURL" in script
    assert "pdfDoc.destroy" in script
    assert "localStorage" not in script
    assert "sessionStorage" not in script
    assert "sample-flyer.pdf" not in mock
    assert "Enter all 8 characters to continue." in mock
    assert "Checking your release code…" in mock
    assert "Downloading document…" in mock
    assert "Generating document preview…" in mock
    assert "Retrieving your documents…" in mock
    assert "No document has been sent to a printer." in mock
    assert "Submitting print job." in script
    assert "idempotency_key" in script
    assert "printWatching" in script
    assert "api.printnode.com" not in script
    assert "PRINTME_PRINTNODE" not in script
    assert "verify_access_code" not in script + mock
    assert "AKIA" not in script + mock + style
    assert "object_key" not in script + mock


@override_settings(**_SETTINGS)
def test_valid_code_returns_only_that_orders_documents(monkeypatch):
    job = _job()
    code = issue_access_code(job)
    own = _document(job, original_filename="notes.pdf")
    other = _job(ses_message_id="ses-message-2")
    issue_access_code(other)
    _document(
        other,
        original_filename="secret.pdf",
        object_key="raw/documents/secret.pdf",
        position=1,
    )
    client, token, _page = _client()

    response = _post(client, token, "/printme/kiosk/session/", {"code": code})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["documents"] == [
        {"id": str(own.id), "filename": "notes.pdf", "format": "pdf"}
    ]
    assert "secret.pdf" not in response.content.decode()
    assert "raw/documents" not in response.content.decode()
    assert client.session[KIOSK_SESSION_KEY] == str(job.id)
    job.refresh_from_db()
    assert job.failed_attempts == 0


@override_settings(**_SETTINGS)
def test_invalid_expired_and_locked_codes_look_the_same():
    job = _job()
    code = issue_access_code(job)
    expired = _job(ses_message_id="ses-expired")
    expired_code = issue_access_code(expired)
    expired.expires_at = timezone.now() - timedelta(minutes=1)
    expired.save(update_fields=["expires_at"])
    locked = _job(ses_message_id="ses-locked")
    locked_code = issue_access_code(locked)
    locked.locked_at = timezone.now()
    locked.save(update_fields=["locked_at"])
    rejected = _job(ses_message_id="ses-rejected", status=PrintJobStatus.REJECTED)
    rejected_code = issue_access_code(rejected)
    client, token, _page = _client()

    wrong = "11111111" if code == "00000000" else "00000000"
    responses = [
        _post(client, token, "/printme/kiosk/session/", {"code": wrong}),
        _post(client, token, "/printme/kiosk/session/", {"code": expired_code}),
        _post(client, token, "/printme/kiosk/session/", {"code": locked_code}),
        _post(client, token, "/printme/kiosk/session/", {"code": rejected_code}),
    ]

    bodies = [item.json() for item in responses]
    assert [item.status_code for item in responses] == [403, 403, 403, 403]
    assert bodies[0] == bodies[1] == bodies[2] == bodies[3]
    assert bodies[0]["ok"] is False
    assert "expired" not in response_text(bodies[0])
    for row in (job, expired, locked, rejected):
        row.refresh_from_db()
        assert row.failed_attempts == 0
    assert KIOSK_SESSION_KEY not in client.session


@override_settings(**_SETTINGS)
def test_empty_document_list_and_deleted_files():
    job = _job()
    code = issue_access_code(job)
    _document(job, status=PrintDocumentStatus.DELETED, position=1)
    client, token, _page = _client()

    response = _post(client, token, "/printme/kiosk/session/", {"code": code})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "documents": []}


@override_settings(**_SETTINGS)
def test_ambiguous_code_is_rejected_without_locking_either_order():
    first = _job()
    code = issue_access_code(first)
    second = _job(ses_message_id="ses-message-2")
    issue_access_code(second)
    second.code_hash = first.code_hash
    second.save(update_fields=["code_hash"])
    client, token, _page = _client()

    response = _post(client, token, "/printme/kiosk/session/", {"code": code})

    assert response.status_code == 403
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.failed_attempts == 0
    assert second.failed_attempts == 0
    assert first.locked_at is None
    assert second.locked_at is None


@override_settings(**_SETTINGS)
def test_preview_requires_the_verified_order_and_hides_storage(monkeypatch):
    job = _job()
    code = issue_access_code(job)
    own = _document(job)
    other = _job(ses_message_id="ses-message-2")
    issue_access_code(other)
    foreign = _document(
        other,
        object_key="raw/documents/secret.pdf",
        original_filename="secret.pdf",
    )
    store = _Store(
        {
            own.object_key: b"%PDF-1.4\nowned",
            foreign.object_key: b"%PDF-1.4\nsecret",
        }
    )
    monkeypatch.setattr("saleor.printme.kiosk.views.open_storage", lambda: store)
    client, token, _page = _client()
    opened = _post(client, token, "/printme/kiosk/session/", {"code": code})
    assert opened.status_code == 200

    owned = client.post(
        f"/printme/kiosk/documents/{own.id}/preview/",
        HTTP_X_CSRFTOKEN=token,
    )
    stolen = client.post(
        f"/printme/kiosk/documents/{foreign.id}/preview/",
        HTTP_X_CSRFTOKEN=token,
    )
    missing = client.post(
        "/printme/kiosk/documents/11111111-1111-1111-1111-111111111111/preview/",
        HTTP_X_CSRFTOKEN=token,
    )

    assert owned.status_code == 200
    assert owned["Content-Type"] == "application/pdf"
    assert owned["Cache-Control"] == "no-store"
    assert owned.content == b"%PDF-1.4\nowned"
    assert foreign.object_key.encode() not in owned.content
    assert stolen.status_code == 404
    assert stolen.content == b""
    assert missing.status_code == 404


@override_settings(**_SETTINGS)
def test_preview_without_a_session_and_unreadable_files_are_hidden(monkeypatch):
    job = _job()
    code = issue_access_code(job)
    document = _document(job)
    monkeypatch.setattr(
        "saleor.printme.kiosk.views.open_storage",
        lambda: _Store({document.object_key: None}),
    )
    client, token, _page = _client()

    denied = client.post(
        f"/printme/kiosk/documents/{document.id}/preview/",
        HTTP_X_CSRFTOKEN=token,
    )
    opened = _post(client, token, "/printme/kiosk/session/", {"code": code})
    unavailable = client.post(
        f"/printme/kiosk/documents/{document.id}/preview/",
        HTTP_X_CSRFTOKEN=token,
    )

    assert denied.status_code == 404
    assert opened.status_code == 200
    assert unavailable.status_code == 404
    assert document.object_key.encode() not in unavailable.content


@override_settings(**_SETTINGS)
def test_csrf_and_malformed_codes_are_rejected():
    job = _job()
    code = issue_access_code(job)
    client, token, _page = _client()

    missing = client.post(
        "/printme/kiosk/session/",
        data=json.dumps({"code": code}),
        content_type="application/json",
    )
    malformed = _post(client, token, "/printme/kiosk/session/", {"code": 12345678})
    short = _post(client, token, "/printme/kiosk/session/", {"code": "1234"})
    cleared = client.post("/printme/kiosk/session/clear/", HTTP_X_CSRFTOKEN=token)

    assert missing.status_code == 403
    assert malformed.status_code == 400
    assert short.status_code == 400
    assert malformed.json()["error"] == short.json()["error"]
    assert cleared.status_code == 200
    job.refresh_from_db()
    assert job.failed_attempts == 0


@override_settings(**_SETTINGS)
def test_new_codes_use_letters_and_old_digit_codes_still_open(monkeypatch):
    digit_job = _job(ses_message_id="ses-digit")
    monkeypatch.setattr(
        "saleor.printme.services.codes.generate_access_code",
        lambda: "93602666",
    )
    stored = issue_access_code(digit_job)
    monkeypatch.setattr(
        "saleor.printme.services.codes.generate_access_code",
        generate_access_code,
    )
    letter_job = _job(ses_message_id="ses-letter")
    letter_code = issue_access_code(letter_job)
    client, token, _page = _client()
    old_code = _post(client, token, "/printme/kiosk/session/", {"code": stored})
    client, token, _page = _client()
    typed_lower = _post(
        client,
        token,
        "/printme/kiosk/session/",
        {"code": letter_code.lower()},
    )

    assert stored == "93602666"
    assert set(letter_code) <= set("0123456789ABCDEF")
    assert len(letter_code) == 8
    assert old_code.status_code == 200
    assert typed_lower.status_code == 200
    assert typed_lower.json()["ok"] is True


def response_text(payload):
    return json.dumps(payload)


@override_settings(
    **_SETTINGS, PRINTME_S3_BUCKET="", PRINTME_AWS_SECRET_ACCESS_KEY="local-only-secret"
)
def test_preview_missing_bucket_reports_the_setting_name_only(caplog):
    job = _job()
    code = issue_access_code(job)
    document = _document(job)
    client, token, _page = _client()
    opened = _post(client, token, "/printme/kiosk/session/", {"code": code})
    assert opened.status_code == 200

    preview = client.post(
        f"/printme/kiosk/documents/{document.id}/preview/",
        HTTP_X_CSRFTOKEN=token,
    )

    assert preview.status_code == 404
    assert preview.content == b""
    assert "PRINTME_S3_BUCKET is not configured" in caplog.text
    assert "local-only-secret" not in caplog.text
    assert document.object_key not in caplog.text
    assert document.bucket not in caplog.text


@override_settings(
    **_SETTINGS,
    PRINTME_S3_BUCKET="printme-test-bucket",
    PRINTME_S3_PREFIX="raw/",
    PRINTME_AWS_REGION="",
    PRINTME_AWS_ACCESS_KEY_ID="",
    PRINTME_AWS_SECRET_ACCESS_KEY="",
)
def test_preview_missing_credentials_reports_the_setting_name_only(caplog):
    job = _job()
    code = issue_access_code(job)
    document = _document(job, object_key="raw/documents/flyer.pdf")
    client, token, _page = _client()
    opened = _post(client, token, "/printme/kiosk/session/", {"code": code})
    assert opened.status_code == 200

    preview = client.post(
        f"/printme/kiosk/documents/{document.id}/preview/",
        HTTP_X_CSRFTOKEN=token,
    )

    assert preview.status_code == 404
    assert preview.content == b""
    assert "PrintMe AWS credentials are not configured" in caplog.text
    assert document.object_key not in caplog.text
    assert "AKIA" not in caplog.text
