"""Send a private PDF to PrintNode over HTTPS.

The API key and printer id come from server settings. This module does not
read them from the request and does not log them. PrintNode's acceptance of
a job is not treated as a finished print.
"""

import base64
import json
import urllib.error
import urllib.request
from json import JSONDecodeError

from django.conf import settings

_API_ROOT = "https://api.printnode.com"
_SUBMIT_TIMEOUT = 30
_STATUS_TIMEOUT = 10
_MAX_RESPONSE_BYTES = 1_000_000
_PDF_MAGIC = b"%PDF-"


class PrintNodeError(Exception):
    """PrintNode could not accept or report a job."""

    code = "api"


class PrintNodeConfigError(PrintNodeError):
    """The API key or printer id is missing or unusable."""

    code = "unavailable"


class PrintNodeAuthError(PrintNodeError):
    """PrintNode rejected the API key."""

    code = "authentication"


class PrintNodeTimeoutError(PrintNodeError):
    """The PrintNode request timed out."""

    code = "timeout"


class PrintNodeNetworkError(PrintNodeError):
    """The PrintNode request did not complete."""

    code = "network"


class PrintNodeAPIError(PrintNodeError):
    """PrintNode rejected the job or returned a response we cannot use."""

    code = "api"


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so a PrintNode response cannot leave HTTPS."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PrintNodeNetworkError("PrintNode redirect was rejected")


def open_printnode() -> "PrintNodeClient":
    """Open the configured PrintNode account. Tests replace this function."""

    return PrintNodeClient(
        api_key=str(settings.PRINTME_PRINTNODE_API_KEY or ""),
        printer_id=str(settings.PRINTME_PRINTNODE_PRINTER_ID or ""),
    )


class PrintNodeClient:
    """HTTPS client for one PrintNode account and one printer."""

    def __init__(self, api_key: str, printer_id: str, exchange=None):
        self._api_key = _validated_key(api_key)
        self.printer_id = _validated_printer(printer_id)
        self._exchange = exchange or _urllib_exchange

    def __repr__(self) -> str:
        return "PrintNodeClient()"

    def submit_pdf(
        self,
        *,
        pdf: bytes,
        title: str,
        copies: int,
        sides: str,
        color_mode: str,
        idempotency_key: str,
        pages: str = "all",
    ) -> int:
        """Upload ``pdf`` and return the PrintNode job id."""

        if not isinstance(pdf, bytes) or not pdf.startswith(_PDF_MAGIC):
            raise PrintNodeAPIError("PrintNode rejected the print job")
        if isinstance(copies, bool) or not isinstance(copies, int):
            raise PrintNodeAPIError("PrintNode rejected the print job")
        if copies < 1 or copies > 99:
            raise PrintNodeAPIError("PrintNode rejected the print job")
        if sides not in {"one-sided", "two-sided"}:
            raise PrintNodeAPIError("PrintNode rejected the print job")
        if color_mode not in {"black-and-white", "color"}:
            raise PrintNodeAPIError("PrintNode rejected the print job")
        if not isinstance(pages, str) or not pages or len(pages) > 128:
            raise PrintNodeAPIError("PrintNode rejected the print job")
        options = {
            "copies": copies,
            "duplex": "long-edge" if sides == "two-sided" else "one-sided",
            "color": color_mode == "color",
        }
        if pages != "all":
            options["pages"] = pages
        payload = {
            "printerId": self.printer_id,
            "title": _title(title),
            "contentType": "pdf_base64",
            "content": base64.b64encode(pdf).decode("ascii"),
            "source": "PrintMe",
            "options": options,
        }
        status, body = self._request(
            "POST",
            "/printjobs",
            payload,
            _SUBMIT_TIMEOUT,
            str(idempotency_key),
        )
        _raise_for_status(status)
        return _parse_job_id(body)

    def fetch_state(self, printnode_job_id: int) -> str:
        """Return PrintNode's latest state name for ``printnode_job_id``."""

        if isinstance(printnode_job_id, bool) or not isinstance(printnode_job_id, int):
            raise PrintNodeAPIError("PrintNode rejected the print job")
        if printnode_job_id <= 0:
            raise PrintNodeAPIError("PrintNode rejected the print job")
        status, body = self._request(
            "GET",
            f"/printjobs/{printnode_job_id}",
            None,
            _STATUS_TIMEOUT,
            None,
        )
        _raise_for_status(status)
        return _parse_state(body)

    def _request(self, method, path, payload, timeout, idempotency_key):
        if not _API_ROOT.startswith("https://") or not path.startswith("/"):
            raise PrintNodeConfigError("PrintNode is not configured")
        url = _API_ROOT + path
        if not url.startswith("https://"):
            raise PrintNodeConfigError("PrintNode is not configured")
        token = base64.b64encode(f"{self._api_key}:".encode("ascii")).decode("ascii")
        headers = {
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "User-Agent": "PrintMe",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["X-Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(
            url, data=data, headers=headers, method=method
        )
        try:
            return self._exchange(request, timeout)
        except PrintNodeError:
            raise
        except TimeoutError:
            raise PrintNodeTimeoutError("PrintNode request timed out") from None


def _validated_key(value: str) -> str:
    key = str(value or "").strip()
    if not key or any(character.isspace() for character in key):
        raise PrintNodeConfigError("PRINTME_PRINTNODE_API_KEY is not configured")
    try:
        key.encode("ascii")
    except UnicodeEncodeError:
        raise PrintNodeConfigError(
            "PRINTME_PRINTNODE_API_KEY is not configured"
        ) from None
    return key


def _validated_printer(value: str) -> int:
    raw = str(value or "").strip()
    if not raw:
        raise PrintNodeConfigError("PRINTME_PRINTNODE_PRINTER_ID is not configured")
    if not raw.isdigit():
        raise PrintNodeConfigError(
            "PRINTME_PRINTNODE_PRINTER_ID must be a positive integer"
        )
    number = int(raw)
    if number <= 0:
        raise PrintNodeConfigError(
            "PRINTME_PRINTNODE_PRINTER_ID must be a positive integer"
        )
    return number


def _title(value: str) -> str:
    cleaned = "".join(
        character for character in str(value or "") if character.isprintable()
    )
    cleaned = " ".join(cleaned.split()).strip()
    return (cleaned or "Document")[:128]


def _raise_for_status(status: int) -> None:
    if status in (401, 403):
        raise PrintNodeAuthError("PrintNode rejected the API key")
    if status < 200 or status >= 300:
        raise PrintNodeAPIError("PrintNode rejected the print job")


def _parse_job_id(body: bytes) -> int:
    payload = _json_body(body)
    if isinstance(payload, list) and len(payload) == 1:
        payload = payload[0]
    if isinstance(payload, bool) or not isinstance(payload, int) or payload <= 0:
        raise PrintNodeAPIError("PrintNode rejected the print job")
    return payload


def _parse_state(body: bytes) -> str:
    payload = _json_body(body)
    job = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(job, dict):
        raise PrintNodeAPIError("PrintNode rejected the print job")
    state = job.get("state")
    if isinstance(state, str) and state.strip():
        return state.strip()
    if isinstance(state, list) and state:
        last = state[-1]
        if isinstance(last, dict) and isinstance(last.get("state"), str):
            text = last["state"].strip()
            if text:
                return text
        if isinstance(last, str) and last.strip():
            return last.strip()
    raise PrintNodeAPIError("PrintNode rejected the print job")


def _json_body(body: bytes):
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, JSONDecodeError):
        raise PrintNodeAPIError("PrintNode rejected the print job") from None


def _discard(response) -> None:
    try:
        response.read(_MAX_RESPONSE_BYTES)
    except (OSError, TimeoutError):
        return
    finally:
        close = getattr(response, "close", None)
        if close is not None:
            close()


def _urllib_exchange(request, timeout: int) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(_RejectRedirect)
    try:
        response = opener.open(request, timeout=timeout)
    except PrintNodeError:
        raise
    except TimeoutError:
        raise PrintNodeTimeoutError("PrintNode request timed out") from None
    except urllib.error.HTTPError as exc:
        _discard(exc)
        return exc.code, b""
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, TimeoutError):
            raise PrintNodeTimeoutError("PrintNode request timed out") from None
        raise PrintNodeNetworkError("PrintNode could not be reached") from None
    try:
        body = response.read(_MAX_RESPONSE_BYTES + 1)
    finally:
        response.close()
    if len(body) > _MAX_RESPONSE_BYTES:
        raise PrintNodeAPIError("PrintNode rejected the print job")
    return response.status, body
