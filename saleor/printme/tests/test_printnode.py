import base64
import json
from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone

from saleor.printme import PrintJobStatus
from saleor.printme.models import PrintSubmission, PrintSubmissionStatus
from saleor.printme.services.codes import issue_access_code
from saleor.printme.services.printnode import (
    PrintNodeAPIError,
    PrintNodeAuthError,
    PrintNodeClient,
    PrintNodeConfigError,
    PrintNodeNetworkError,
    PrintNodeTimeoutError,
    _RejectRedirect,
)
from saleor.printme.services.variant_prices import (
    CatalogEntry,
    PriceError,
    choose_rate,
)
from saleor.printme.tests.test_kiosk import (
    _SETTINGS,
    _client,
    _document,
    _job,
    _post,
    _Store,
)

pytestmark = pytest.mark.django_db

_KEY = "11111111-1111-4111-8111-111111111111"
_KEY_2 = "22222222-2222-4222-8222-222222222222"
_PDF = b"%PDF-1.4\nowned"
_SECRET = "super-secret-printnode-key"
_PUBLIC = {"ok", "submission_id", "status", "title", "message"}


class _Node:
    def __init__(self):
        self.printer_id = 42
        self.calls = []
        self.states = []
        self.error = None
        self.state = "new"
        self.state_error = None
        self.job_id = 9385459475

    def submit_pdf(
        self, *, pdf, title, copies, sides, color_mode, idempotency_key, pages="all"
    ):
        self.calls.append(
            {
                "pdf": pdf,
                "title": title,
                "copies": copies,
                "sides": sides,
                "color_mode": color_mode,
                "pages": pages,
                "idempotency_key": idempotency_key,
            }
        )
        if self.error is not None:
            raise self.error
        return self.job_id

    def fetch_state(self, printnode_job_id):
        self.states.append(printnode_job_id)
        if self.state_error is not None:
            raise self.state_error
        return self.state


def _wire(monkeypatch, node=None, pdf=_PDF):
    job = _job()
    code = issue_access_code(job)
    document = _document(job)
    store = _Store({document.object_key: pdf})
    printer = node or _Node()
    monkeypatch.setattr("saleor.printme.kiosk.views.open_storage", lambda: store)
    monkeypatch.setattr("saleor.printme.kiosk.views.open_printnode", lambda: printer)
    client, token, _page = _client()
    opened = _post(client, token, "/printme/kiosk/session/", {"code": code})
    assert opened.status_code == 200
    return client, token, job, document, printer


def _print(client, token, document, **overrides):
    payload = {
        "document_id": str(document.id),
        "copies": 1,
        "pages": "all",
        "color": "black-and-white",
        "sides": "one-sided",
        "idempotency_key": _KEY,
    }
    payload.update(overrides)
    return _post(client, token, "/printme/kiosk/prints/", payload)


def _status(client, token, submission_id):
    return _post(client, token, f"/printme/kiosk/prints/{submission_id}/", {})


@override_settings(**_SETTINGS)
def test_authorized_print_submits_the_stored_pdf(monkeypatch):
    client, token, job, document, node = _wire(monkeypatch)

    response = _print(
        client,
        token,
        document,
        copies=2,
        color="color",
        sides="two-sided",
        printer_id=999,
        price="12.00",
        job_id="33333333-3333-4333-8333-333333333333",
    )

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    body = response.json()
    assert set(body) == _PUBLIC
    assert body["ok"] is True
    assert body["status"] == "submitted"
    assert body["title"] == "Print job accepted"
    assert body["message"] == "Print job accepted. Sending it to the printer."
    assert body["submission_id"] == _KEY
    text = response.content.decode()
    assert "Printing completed." not in text
    assert "9385459475" not in text
    assert "printer_id" not in text
    assert "12.00" not in text
    assert "raw/documents" not in text
    assert node.calls == [
        {
            "pdf": _PDF,
            "title": "flyer.pdf",
            "copies": 2,
            "sides": "two-sided",
            "color_mode": "color",
            "pages": "all",
            "idempotency_key": _KEY,
        }
    ]
    saved = PrintSubmission.objects.get()
    assert saved.job_id == job.id
    assert saved.document_id == document.id
    assert saved.printer_id == 42
    assert saved.printnode_job_id == 9385459475
    assert saved.copies == 2
    assert saved.pages == "all"
    assert saved.color_mode == "color"
    assert saved.sides == "two-sided"
    assert saved.status == PrintSubmissionStatus.SUBMITTED
    assert saved.quoted_total is None
    assert saved.currency == ""
    assert saved.submitted_at is not None
    job.refresh_from_db()
    assert job.failed_attempts == 0


@override_settings(**_SETTINGS)
def test_another_orders_document_is_rejected(monkeypatch):
    client, token, _job_row, _document_row, node = _wire(monkeypatch)
    foreign_job = _job(ses_message_id="ses-foreign")
    issue_access_code(foreign_job)
    foreign = _document(
        foreign_job,
        original_filename="secret.pdf",
        object_key="raw/documents/secret.pdf",
    )

    response = _print(client, token, foreign)

    assert response.status_code == 404
    assert response.json() == {
        "ok": False,
        "error": "That document is not available.",
    }
    assert node.calls == []
    assert PrintSubmission.objects.count() == 0
    assert b"secret.pdf" not in response.content


@override_settings(**_SETTINGS)
def test_expired_job_is_rejected(monkeypatch):
    client, token, job, document, node = _wire(monkeypatch)
    job.expires_at = timezone.now() - timedelta(minutes=1)
    job.save(update_fields=["expires_at"])

    response = _print(client, token, document)

    assert response.status_code == 403
    assert response.json()["ok"] is False
    assert "expired" not in response.content.decode()
    assert node.calls == []
    assert PrintSubmission.objects.count() == 0
    job.refresh_from_db()
    assert job.failed_attempts == 0


@override_settings(**_SETTINGS)
def test_locked_job_is_rejected(monkeypatch):
    client, token, job, document, node = _wire(monkeypatch)
    job.locked_at = timezone.now()
    job.save(update_fields=["locked_at"])

    response = _print(client, token, document)

    assert response.status_code == 403
    assert "locked" not in response.content.decode()
    assert node.calls == []
    job.refresh_from_db()
    assert job.failed_attempts == 0


@override_settings(**_SETTINGS)
def test_rejected_job_is_rejected(monkeypatch):
    client, token, job, document, node = _wire(monkeypatch)
    job.status = PrintJobStatus.REJECTED
    job.save(update_fields=["status"])

    response = _print(client, token, document)

    assert response.status_code == 403
    assert node.calls == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("pages", "3-1", "That page range is out of order."),
        ("copies", 0, "Choose between 1 and 99 copies."),
        ("copies", 100, "Choose between 1 and 99 copies."),
        ("copies", True, "Choose between 1 and 99 copies."),
        ("copies", "2", "Choose between 1 and 99 copies."),
        ("color", "grayscale", "That color option is not available."),
        ("sides", "duplex", "That sides option is not available."),
    ],
)
@override_settings(**_SETTINGS)
def test_unsupported_options_are_rejected(monkeypatch, field, value, message):
    client, token, _job_row, document, node = _wire(monkeypatch)

    response = _print(client, token, document, **{field: value})

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": message}
    assert node.calls == []
    assert PrintSubmission.objects.count() == 0


@override_settings(**_SETTINGS, PRINTME_PRINTNODE_API_KEY=_SECRET)
def test_printnode_authentication_failure_is_recorded(monkeypatch, caplog):
    node = _Node()
    node.error = PrintNodeAuthError("PrintNode rejected the API key")
    client, token, _job_row, document, node = _wire(monkeypatch, node)

    response = _print(client, token, document)

    assert response.status_code == 502
    body = response.json()
    assert body["status"] == "failed"
    assert body["ok"] is False
    assert body["message"] == "Printing is not available right now."
    assert _SECRET not in response.content.decode()
    assert _SECRET not in caplog.text
    saved = PrintSubmission.objects.get()
    assert saved.status == PrintSubmissionStatus.FAILED
    assert saved.error_detail == "authentication"
    assert saved.printnode_job_id is None
    assert len(node.calls) == 1


@override_settings(**_SETTINGS)
def test_printnode_timeout_is_recorded(monkeypatch):
    node = _Node()
    node.error = PrintNodeTimeoutError("PrintNode request timed out")
    client, token, _job_row, document, node = _wire(monkeypatch, node)

    response = _print(client, token, document)

    assert response.status_code == 502
    assert response.json()["status"] == "failed"
    assert "did not confirm" in response.json()["message"]
    assert "Printing completed." not in response.content.decode()
    saved = PrintSubmission.objects.get()
    assert saved.error_detail == "timeout"
    assert saved.printnode_job_id is None


@override_settings(**_SETTINGS)
def test_printnode_api_rejection_is_recorded(monkeypatch):
    node = _Node()
    node.error = PrintNodeAPIError("PrintNode rejected the print job")
    client, token, _job_row, document, _node = _wire(monkeypatch, node)

    response = _print(client, token, document)

    assert response.status_code == 502
    message = "The printer rejected this print. Please try again."
    assert response.json()["message"] == message
    assert PrintSubmission.objects.get().error_detail == "api"


@override_settings(**_SETTINGS)
def test_duplicate_print_submission_does_not_call_printnode_again(monkeypatch):
    client, token, _job_row, document, node = _wire(monkeypatch)

    first = _print(client, token, document)
    second = _print(client, token, document)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["submission_id"] == second.json()["submission_id"]
    assert len(node.calls) == 1
    assert PrintSubmission.objects.count() == 1


@override_settings(**_SETTINGS)
def test_a_second_print_while_one_is_active_is_rejected(monkeypatch):
    client, token, _job_row, document, node = _wire(monkeypatch)
    _print(client, token, document)

    response = _print(client, token, document, idempotency_key=_KEY_2)

    assert response.status_code == 409
    assert response.json()["error"] == "A print is already in progress."
    assert response.json()["title"] == "Printing in progress"
    assert len(node.calls) == 1
    assert PrintSubmission.objects.count() == 1


@override_settings(**_SETTINGS)
def test_stale_accepted_submission_does_not_block_a_new_print(monkeypatch):
    client, token, job, document, node = _wire(monkeypatch)
    stale = PrintSubmission.objects.create(
        id=_KEY,
        job=job,
        document=document,
        copies=1,
        pages="all",
        color_mode="black-and-white",
        sides="one-sided",
        status=PrintSubmissionStatus.ACCEPTED,
    )
    PrintSubmission.objects.filter(pk=stale.pk).update(
        created_at=timezone.now() - timedelta(minutes=5)
    )

    response = _print(client, token, document, idempotency_key=_KEY_2)

    assert response.status_code == 200
    assert response.json()["status"] == "submitted"
    assert len(node.calls) == 1
    stale.refresh_from_db()
    assert stale.status == PrintSubmissionStatus.FAILED
    assert stale.error_detail == "timeout"


@override_settings(**_SETTINGS)
def test_status_moves_from_submitted_to_printing_and_completed(monkeypatch):
    client, token, _job_row, document, node = _wire(monkeypatch)
    created = _print(client, token, document)
    assert created.json()["status"] == "submitted"
    submission_id = created.json()["submission_id"]

    node.state = "queued"
    waiting = _status(client, token, submission_id)
    assert waiting.status_code == 200
    assert waiting.json()["status"] == "submitted"

    node.state = "sent_to_client"
    printing = _status(client, token, submission_id)
    assert printing.json()["status"] == "printing"
    assert printing.json()["message"] == "Printing in progress."

    node.state = "new"
    still_printing = _status(client, token, submission_id)
    assert still_printing.json()["status"] == "printing"

    node.state = "done"
    finished = _status(client, token, submission_id)
    assert finished.status_code == 200
    assert finished.json()["status"] == "completed"
    assert finished.json()["message"] == "Printing completed."
    assert "9385459475" not in finished.content.decode()
    assert node.states == [9385459475, 9385459475, 9385459475, 9385459475]
    assert PrintSubmission.objects.get().status == PrintSubmissionStatus.COMPLETED


@override_settings(**_SETTINGS)
def test_status_poll_failure_keeps_the_last_status(monkeypatch):
    client, token, _job_row, document, node = _wire(monkeypatch)
    created = _print(client, token, document)
    node.state_error = PrintNodeTimeoutError("PrintNode request timed out")

    response = _status(client, token, created.json()["submission_id"])

    assert response.status_code == 200
    assert response.json()["status"] == "submitted"
    assert PrintSubmission.objects.get().status == PrintSubmissionStatus.SUBMITTED


@override_settings(**_SETTINGS)
def test_printnode_reports_an_error_as_failed(monkeypatch):
    client, token, _job_row, document, node = _wire(monkeypatch)
    created = _print(client, token, document)
    node.state = "error"

    response = _status(client, token, created.json()["submission_id"])

    assert response.status_code == 502
    assert response.json()["status"] == "failed"
    message = "The printer rejected this print. Please try again."
    assert response.json()["message"] == message


@override_settings(**_SETTINGS)
def test_print_and_status_require_the_kiosk_session(monkeypatch):
    client, token, _job_row, document, node = _wire(monkeypatch)
    created = _print(client, token, document)
    submission_id = created.json()["submission_id"]
    cleared = _post(client, token, "/printme/kiosk/session/clear/", {})
    assert cleared.status_code == 200

    denied_status = _status(client, token, submission_id)
    denied_print = _print(client, token, document, idempotency_key=_KEY_2)

    assert denied_status.status_code == 404
    assert denied_print.status_code == 404
    assert submission_id not in denied_status.content.decode()
    assert len(node.calls) == 1
    assert PrintSubmission.objects.get().status == PrintSubmissionStatus.SUBMITTED


@override_settings(**_SETTINGS)
def test_another_session_cannot_read_the_print_status(monkeypatch):
    client, token, _job_row, document, _node = _wire(monkeypatch)
    created = _print(client, token, document)
    submission_id = created.json()["submission_id"]
    other = _job(ses_message_id="ses-other")
    other_code = issue_access_code(other)
    other_client, other_token, _page = _client()
    opened = _post(
        other_client,
        other_token,
        "/printme/kiosk/session/",
        {"code": other_code},
    )
    assert opened.status_code == 200

    response = _status(other_client, other_token, submission_id)

    assert response.status_code == 404
    assert response.json()["error"] == "That document is not available."
    assert "submitted" not in response.content.decode()


@override_settings(**_SETTINGS)
def test_missing_printnode_configuration_fails_before_the_network(monkeypatch, caplog):
    job = _job()
    code = issue_access_code(job)
    document = _document(job)
    calls = []
    monkeypatch.setattr(
        "saleor.printme.services.printnode._urllib_exchange",
        lambda request, timeout: calls.append(request) or (200, b"1"),
    )
    client, token, _page = _client()
    opened = _post(client, token, "/printme/kiosk/session/", {"code": code})
    assert opened.status_code == 200

    response = _print(client, token, document)

    assert response.status_code == 503
    assert response.json()["status"] == "failed"
    assert response.json()["message"] == "Printing is not available right now."
    assert calls == []
    assert "PRINTME_PRINTNODE_API_KEY is not configured" in caplog.text
    assert PrintSubmission.objects.get().error_detail == "unavailable"


def test_submit_pdf_uses_the_printnode_pdf_format():
    recorded = []

    def exchange(request, timeout):
        recorded.append((request, timeout))
        return 200, b"501"

    client = PrintNodeClient(_SECRET, "77", exchange=exchange)
    job_id = client.submit_pdf(
        pdf=_PDF,
        title="flyer.pdf",
        copies=2,
        sides="two-sided",
        color_mode="black-and-white",
        idempotency_key=_KEY,
    )

    assert job_id == 501
    request, timeout = recorded[0]
    assert timeout == 30
    assert request.full_url == "https://api.printnode.com/printjobs"
    assert request.get_method() == "POST"
    payload = json.loads(request.data.decode())
    assert payload["printerId"] == 77
    assert payload["contentType"] == "pdf_base64"
    assert payload["content"] == base64.b64encode(_PDF).decode("ascii")
    assert payload["source"] == "PrintMe"
    assert payload["options"] == {
        "copies": 2,
        "duplex": "long-edge",
        "color": False,
    }
    assert "pages" not in payload["options"]
    token = base64.b64encode(f"{_SECRET}:".encode()).decode("ascii")
    assert request.get_header("Authorization") == f"Basic {token}"
    assert request.get_header("X-idempotency-key") == _KEY
    assert _SECRET not in payload["content"]


def test_one_sided_color_option_is_sent():
    recorded = []

    def exchange(request, timeout):
        recorded.append(json.loads(request.data.decode()))
        return 200, b"8"

    client = PrintNodeClient("printnode-key", "15", exchange=exchange)
    client.submit_pdf(
        pdf=_PDF,
        title="notes.pdf",
        copies=1,
        sides="one-sided",
        color_mode="color",
        idempotency_key=_KEY,
    )

    assert recorded[0]["options"] == {
        "copies": 1,
        "duplex": "one-sided",
        "color": True,
    }


def test_printnode_state_is_read_over_https():
    recorded = []

    def exchange(request, timeout):
        recorded.append((request, timeout))
        return 200, b'[{"id": 501, "state": "sent_to_client"}]'

    client = PrintNodeClient("printnode-key", "15", exchange=exchange)

    assert client.fetch_state(501) == "sent_to_client"
    request, timeout = recorded[0]
    assert timeout == 10
    assert request.full_url == "https://api.printnode.com/printjobs/501"
    assert request.get_method() == "GET"
    assert request.data is None


def test_printnode_authentication_failure_hides_the_response_body():
    def exchange(request, timeout):
        return 401, _SECRET.encode()

    client = PrintNodeClient(_SECRET, "15", exchange=exchange)
    with pytest.raises(PrintNodeAuthError) as caught:
        client.submit_pdf(
            pdf=_PDF,
            title="flyer.pdf",
            copies=1,
            sides="one-sided",
            color_mode="black-and-white",
            idempotency_key=_KEY,
        )

    assert _SECRET not in str(caught.value)


def test_printnode_timeout_and_api_rejection():
    def timed_out(request, timeout):
        raise TimeoutError()

    client = PrintNodeClient("printnode-key", "15", exchange=timed_out)
    with pytest.raises(PrintNodeTimeoutError):
        client.fetch_state(4)

    def rejected(request, timeout):
        return 422, b'{"message":"printer offline"}'

    client = PrintNodeClient("printnode-key", "15", exchange=rejected)
    with pytest.raises(PrintNodeAPIError) as caught:
        client.submit_pdf(
            pdf=_PDF,
            title="flyer.pdf",
            copies=1,
            sides="one-sided",
            color_mode="black-and-white",
            idempotency_key=_KEY,
        )

    assert "printer offline" not in str(caught.value)


def test_missing_or_invalid_printnode_settings_do_not_echo_the_value():
    with pytest.raises(PrintNodeConfigError) as missing_key:
        PrintNodeClient("", "15")
    assert str(missing_key.value) == "PRINTME_PRINTNODE_API_KEY is not configured"
    with pytest.raises(PrintNodeConfigError) as missing_printer:
        PrintNodeClient("printnode-key", "")
    assert (
        str(missing_printer.value) == "PRINTME_PRINTNODE_PRINTER_ID is not configured"
    )
    with pytest.raises(PrintNodeConfigError) as bad_printer:
        PrintNodeClient("printnode-key", "abc")
    assert str(bad_printer.value) == (
        "PRINTME_PRINTNODE_PRINTER_ID must be a positive integer"
    )
    assert "abc" not in str(bad_printer.value)


def test_printnode_redirects_are_rejected():
    with pytest.raises(PrintNodeNetworkError):
        _RejectRedirect().redirect_request(None, None, 302, "Found", {}, "http://evil")


def _pages(count):
    return b"%PDF-1.4\n" + b"1 0 obj << /Type /Page >> endobj\n" * count


def _quote(client, token, document, **overrides):
    payload = {
        "document_id": str(document.id),
        "copies": 1,
        "pages": "all",
        "color": "black-and-white",
        "sides": "one-sided",
        "price": "12.00",
    }
    payload.update(overrides)
    return _post(client, token, "/printme/kiosk/quote/", payload)


def _entry(**overrides):
    values = {
        "variant_id": 1,
        "paper_size": "a4",
        "color_mode": "bw",
        "listed": True,
        "currency": "XOF",
        "channel_currency": "XOF",
        "unit_amount": Decimal("100.00"),
    }
    values.update(overrides)
    return CatalogEntry(**values)


def _catalog():
    return [
        _entry(),
        _entry(variant_id=2, color_mode="color", unit_amount=Decimal("100.00")),
        _entry(variant_id=3, paper_size="a6", unit_amount=Decimal("150.00")),
        _entry(
            variant_id=4,
            paper_size="a6",
            color_mode="color",
            unit_amount=Decimal("200.00"),
        ),
    ]


def _use_catalog(monkeypatch, rows):
    monkeypatch.setattr(
        "saleor.printme.services.variant_prices.load_channel_catalog",
        lambda: rows,
    )


@override_settings(**_SETTINGS)
def test_custom_page_range_is_sent_to_printnode(monkeypatch):
    client, token, _job_row, document, node = _wire(monkeypatch, pdf=_pages(10))

    response = _print(client, token, document, pages="1-3, 6, 8-10")

    assert response.status_code == 200
    assert node.calls[0]["pages"] == "1-3,6,8-10"
    assert PrintSubmission.objects.get().pages == "1-3,6,8-10"
    quoted = _quote(client, token, document, pages="1-3,4")
    assert quoted.status_code == 200
    assert quoted.json()["pages_label"] == "1-4"


@override_settings(**_SETTINGS)
def test_page_range_outside_the_document_is_rejected(monkeypatch):
    client, token, _job_row, document, node = _wire(monkeypatch, pdf=_pages(2))

    response = _print(client, token, document, pages="1-3")

    assert response.status_code == 400
    assert response.json()["message"] == "Choose pages that are in this document."
    assert node.calls == []


@override_settings(**_SETTINGS)
def test_unknown_page_count_rejects_a_custom_range(monkeypatch):
    client, token, _job_row, document, node = _wire(monkeypatch)

    response = _print(client, token, document, pages="1")

    assert response.status_code == 400
    assert response.json()["message"] == (
        "That page range cannot be checked for this document."
    )
    assert node.calls == []


@override_settings(**_SETTINGS)
def test_empty_page_range_is_rejected(monkeypatch):
    client, token, _job_row, document, node = _wire(monkeypatch, pdf=_pages(2))

    response = _print(client, token, document, pages="   ")

    assert response.status_code == 400
    assert response.json()["error"] == "Enter a page range."
    assert node.calls == []
    assert PrintSubmission.objects.count() == 0


@override_settings(**_SETTINGS)
def test_quote_without_a_rate_does_not_invent_a_price(monkeypatch):
    client, token, _job_row, document, node = _wire(monkeypatch, pdf=_pages(4))

    response = _quote(client, token, document, copies=2)

    assert response.status_code == 200
    body = response.json()
    assert body["pricing_available"] is False
    assert body["reference"] == "Document 1"
    assert body["price"] == "Pricing is not yet available."
    assert body["total"] == "Pricing is not yet available."
    assert body["job_total"] == "Pricing is not yet available."
    assert "12.00" not in response.content.decode()
    assert node.calls == []
    assert PrintSubmission.objects.count() == 0


@override_settings(**_SETTINGS)
def test_saleor_side_price_is_stored_and_ignores_the_browser_price(monkeypatch):
    client, token, _job_row, document, _node = _wire(monkeypatch, pdf=_pages(4))
    _use_catalog(monkeypatch, _catalog())

    quoted = _quote(client, token, document, copies=2, pages="1-3")
    printed = _print(
        client,
        token,
        document,
        copies=2,
        pages="1-3",
        price="12.00",
    )

    assert quoted.status_code == 200
    body = quoted.json()
    assert body["pricing_available"] is True
    assert body["currency"] == "XOF"
    assert body["paper_label"] == "A4"
    assert body["price"] == "100 XOF"
    assert body["count"] == 2
    assert body["total"] == "600 XOF"
    assert body["job_total"] == "600 XOF"
    assert printed.status_code == 200
    assert "12.00" not in printed.content.decode()
    saved = PrintSubmission.objects.get()
    assert saved.quoted_total == Decimal("600.00")
    assert saved.unit_price == Decimal("100.00")
    assert saved.billable_quantity == 6
    assert saved.currency == "XOF"
    assert saved.paper_size == "a4"
    assert saved.price_variant_id == 1


@override_settings(**_SETTINGS)
def test_each_paper_and_color_uses_its_own_variant(monkeypatch):
    client, token, _job_row, document, _node = _wire(monkeypatch, pdf=_pages(1))
    _use_catalog(monkeypatch, _catalog())
    expected = {
        ("a4", "black-and-white"): "100 XOF",
        ("a4", "color"): "100 XOF",
        ("a6", "black-and-white"): "150 XOF",
        ("a6", "color"): "200 XOF",
    }

    for (paper, color), total in expected.items():
        response = _quote(
            client,
            token,
            document,
            paper_size=paper,
            color=color,
        )
        assert response.status_code == 200
        assert response.json()["total"] == total
        assert response.json()["paper_label"] == paper.upper()


@override_settings(**_SETTINGS)
def test_two_sided_printing_still_charges_every_page(monkeypatch):
    client, token, _job_row, document, _node = _wire(monkeypatch, pdf=_pages(10))
    _use_catalog(monkeypatch, _catalog())

    response = _quote(
        client,
        token,
        document,
        copies=2,
        color="black-and-white",
        sides="two-sided",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["price"] == "100 XOF"
    assert body["total"] == "2000 XOF"
    assert body["sides_label"] == "Two-sided"


@override_settings(**_SETTINGS)
def test_a_later_price_change_does_not_change_the_stored_total(monkeypatch):
    client, token, _job_row, document, _node = _wire(monkeypatch, pdf=_pages(10))
    rows = _catalog()
    _use_catalog(monkeypatch, rows)

    printed = _print(client, token, document, copies=2)
    saved = PrintSubmission.objects.get()
    assert printed.status_code == 200
    assert saved.quoted_total == Decimal("2000.00")
    assert saved.unit_price == Decimal("100.00")

    rows[0] = _entry(unit_amount=Decimal("0.15"))
    saved.refresh_from_db()
    assert saved.quoted_total == Decimal("2000.00")
    assert saved.unit_price == Decimal("100.00")
    requoted = _quote(client, token, document, copies=1)
    assert requoted.json()["price"] == "0.15 XOF"
    assert requoted.json()["total"] == "1.50 XOF"


@override_settings(**_SETTINGS)
@pytest.mark.parametrize(
    ("rows", "extra", "message"),
    [
        ([], {}, "Pricing is not configured for this print."),
        (
            [_entry(listed=False)],
            {},
            "Pricing is not configured for this print.",
        ),
        (
            [_entry(unit_amount=None)],
            {},
            "Pricing is not configured for this print.",
        ),
        (
            [_entry(currency="CAD")],
            {},
            "That price is not in the channel currency.",
        ),
        (
            [],
            {"paper_size": "letter"},
            "That paper size is not available.",
        ),
        (
            [],
            {"color": "grayscale"},
            "That color option is not available.",
        ),
    ],
)
def test_missing_or_invalid_saleor_prices_are_rejected(
    monkeypatch, rows, extra, message
):
    client, token, _job_row, document, node = _wire(monkeypatch, pdf=_pages(2))
    _use_catalog(monkeypatch, rows)

    response = _quote(client, token, document, **extra)

    assert response.status_code == 400
    assert response.json()["error"] == message
    assert node.calls == []
    assert PrintSubmission.objects.count() == 0


def test_two_matching_variants_are_rejected():
    with pytest.raises(PriceError) as caught:
        choose_rate(
            [_entry(), _entry(variant_id=9)],
            paper_size="a4",
            color_mode="black-and-white",
        )
    assert caught.value.code == "ambiguous"


@override_settings(**_SETTINGS)
def test_print_stops_before_printnode_when_the_listing_is_missing(monkeypatch):
    client, token, _job_row, document, node = _wire(monkeypatch, pdf=_pages(2))
    _use_catalog(monkeypatch, [_entry(listed=False)])

    response = _print(client, token, document)

    assert response.status_code == 400
    assert response.json()["message"] == "Pricing is not configured for this print."
    assert node.calls == []
    saved = PrintSubmission.objects.get()
    assert saved.status == PrintSubmissionStatus.FAILED
    assert saved.quoted_total is None


def test_custom_pages_are_included_in_the_printnode_options():
    recorded = []

    def exchange(request, timeout):
        recorded.append(json.loads(request.data.decode()))
        return 200, b"9"

    client = PrintNodeClient("printnode-key", "15", exchange=exchange)
    client.submit_pdf(
        pdf=_PDF,
        title="notes.pdf",
        copies=1,
        sides="one-sided",
        color_mode="black-and-white",
        pages="1-3,6",
        idempotency_key=_KEY,
    )

    assert recorded[0]["options"]["pages"] == "1-3,6"

