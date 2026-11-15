"""Touchscreen kiosk page and the release-code session behind it.

The browser receives document ids and display names. Storage keys and
credentials stay on the server.
"""

import json
import logging
import re
from uuid import UUID

from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import render
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from ..models import PrintJob, PrintSubmission
from ..services.pricing import public_quote
from ..services.printing import (
    PrintRejected,
    PrintRequestError,
    http_status,
    parse_print_options,
    public_submission,
    quote_print,
    refresh_submission,
    submit_print,
)
from ..services.printnode import open_printnode
from ..services.storage import S3ObjectStorage, StorageConfigError, StorageError
from .access import (
    KIOSK_SESSION_KEY,
    document_cards,
    find_verified_job,
    read_document_pdf,
)

logger = logging.getLogger(__name__)

_INCOMPLETE = "Enter all 8 characters to continue."
_CODE_PATTERN = re.compile(r"^[0-9A-F]{8}$")
_REJECTED = "That code cannot be used. Check the code and try again."
_PRINT_UNAVAILABLE = "That document is not available."
_PRINT_BAD = "The print request could not be read."
_MAX_PRINT_BODY = 4096


@ensure_csrf_cookie
@require_GET
def kiosk(request):
    """Serve the PrintMe kiosk interface."""

    return render(
        request,
        "printme/kiosk.html",
        {"csrf_token": get_token(request)},
    )


@csrf_protect
@require_POST
def kiosk_session(request):
    """Verify an 8-character code and store the matching order in the session."""

    code = _code_from_body(request)
    if code is None:
        return JsonResponse({"ok": False, "error": _INCOMPLETE}, status=400)
    job = find_verified_job(code)
    if job is None:
        _forget_job(request)
        return JsonResponse({"ok": False, "error": _REJECTED}, status=403)
    request.session.cycle_key()
    request.session[KIOSK_SESSION_KEY] = str(job.id)
    return JsonResponse({"ok": True, "documents": document_cards(job)})


@csrf_protect
@require_POST
def kiosk_clear(request):
    """Drop the kiosk order from the session."""

    _forget_job(request)
    return JsonResponse({"ok": True})


@csrf_protect
@require_POST
def kiosk_preview(request, document_id: UUID):
    """Return a PDF that belongs to the order in the current session."""

    job_id = _session_job_id(request)
    if job_id is None:
        return HttpResponse(status=404)
    try:
        content = read_document_pdf(job_id, document_id, open_storage())
    except StorageConfigError as exc:
        logger.warning("printme kiosk preview unavailable: %s", exc)
        return HttpResponse(status=404)
    except StorageError:
        logger.warning("printme kiosk preview unavailable")
        return HttpResponse(status=404)
    if content is None:
        return HttpResponse(status=404)
    response = HttpResponse(content, content_type="application/pdf")
    response["Content-Disposition"] = "inline"
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def open_storage() -> S3ObjectStorage:
    """Open the PrintMe object store. Tests replace this function."""

    return S3ObjectStorage()


@csrf_protect
@require_POST
def kiosk_print(request):
    """Submit the session's document to the configured printer."""

    job = _session_job(request)
    if job is None:
        return _print_json({"ok": False, "error": _PRINT_UNAVAILABLE}, 404)
    payload = _json_object(request)
    if payload is None:
        return _print_json({"ok": False, "error": _PRINT_BAD}, 400)
    try:
        options = parse_print_options(payload)
    except PrintRequestError as exc:
        return _print_json({"ok": False, "error": str(exc)}, exc.status_code)
    try:
        submission = submit_print(
            job=job,
            options=options,
            storage_factory=open_storage,
            client_factory=open_printnode,
        )
    except PrintRejected as exc:
        body = {"ok": False, "error": str(exc)}
        if exc.title:
            body["title"] = exc.title
            body["message"] = str(exc)
        return _print_json(body, exc.status_code)
    return _print_json(public_submission(submission), http_status(submission))


@csrf_protect
@require_POST
def kiosk_quote(request):
    """Price the session's document without sending it to a printer."""

    job = _session_job(request)
    if job is None:
        return _print_json({"ok": False, "error": _PRINT_UNAVAILABLE}, 404)
    payload = _json_object(request)
    if payload is None:
        return _print_json({"ok": False, "error": _PRINT_BAD}, 400)
    try:
        options = parse_print_options(payload, require_idempotency=False)
        quote = quote_print(job=job, options=options, storage_factory=open_storage)
    except PrintRequestError as exc:
        return _print_json({"ok": False, "error": str(exc)}, exc.status_code)
    except PrintRejected as exc:
        return _print_json({"ok": False, "error": str(exc)}, exc.status_code)
    return _print_json(public_quote(quote), 200)


@csrf_protect
@require_POST
def kiosk_print_status(request, submission_id: UUID):
    """Return the session's print status, refreshing it from PrintNode."""

    job = _session_job(request)
    if job is None:
        return _print_json({"ok": False, "error": _PRINT_UNAVAILABLE}, 404)
    if _json_object(request) is None:
        return _print_json({"ok": False, "error": _PRINT_BAD}, 400)
    submission = PrintSubmission.objects.filter(pk=submission_id, job=job).first()
    if submission is None:
        return _print_json({"ok": False, "error": _PRINT_UNAVAILABLE}, 404)
    submission = refresh_submission(submission, open_printnode)
    return _print_json(public_submission(submission), http_status(submission))


def _print_json(payload, status):
    response = JsonResponse(payload, status=status)
    response["Cache-Control"] = "no-store"
    return response


def _json_object(request):
    if len(request.body) > _MAX_PRINT_BODY:
        return None
    if not request.body:
        return {}
    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _session_job(request) -> PrintJob | None:
    job_id = _session_job_id(request)
    if job_id is None:
        return None
    return PrintJob.objects.filter(pk=job_id).first()


def _code_from_body(request) -> str | None:
    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    if not isinstance(code, str):
        return None
    code = code.upper()
    if _CODE_PATTERN.fullmatch(code) is None:
        return None
    return code


def _session_job_id(request) -> UUID | None:
    raw = request.session.get(KIOSK_SESSION_KEY)
    if not isinstance(raw, str):
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


def _forget_job(request) -> None:
    if KIOSK_SESSION_KEY in request.session:
        del request.session[KIOSK_SESSION_KEY]
