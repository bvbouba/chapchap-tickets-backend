"""Turn a kiosk print request into one PrintSubmission.

The browser chooses a document id, copies, and the options this kiosk offers.
The printer id, the stored PDF, and the PrintNode account come from the server.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone

from ..kiosk.access import read_document_pdf
from ..models import (
    PrintDocument,
    PrintDocumentStatus,
    PrintJob,
    PrintSubmission,
    PrintSubmissionStatus,
)
from .codes import job_can_release
from .pages import (
    PAGE_MESSAGES,
    PageSelectionError,
    normalize_page_selection,
    selected_page_total,
    selection_syntax,
)
from .parse_email import display_filename
from .pricing import build_quote
from .printnode import PrintNodeConfigError, PrintNodeError
from .storage import StorageConfigError
from .variant_prices import PRICE_MESSAGES, PriceError

logger = logging.getLogger(__name__)

_STALE_AFTER = timedelta(seconds=90)
_UNAVAILABLE = "That document is not available."
_UNUSABLE = "That print order can no longer be used."
_IN_PROGRESS = "A print is already in progress."
_BAD_REQUEST = "The print request could not be read."
_COPIES = "Choose between 1 and 99 copies."
_COLOR = "That color option is not available."
_SIDES = "That sides option is not available."
_COLORS = {"black-and-white", "color"}
_SIDES_VALUES = {"one-sided", "two-sided"}

_MESSAGES = {
    PrintSubmissionStatus.ACCEPTED: "Print job accepted.",
    PrintSubmissionStatus.SUBMITTED: (
        "Print job accepted. Sending it to the printer."
    ),
    PrintSubmissionStatus.PRINTING: "Printing in progress.",
    PrintSubmissionStatus.COMPLETED: "Printing completed.",
    PrintSubmissionStatus.FAILED: "Printing failed. The document was not printed.",
}
_TITLES = {
    PrintSubmissionStatus.ACCEPTED: "Print job accepted",
    PrintSubmissionStatus.SUBMITTED: "Print job accepted",
    PrintSubmissionStatus.PRINTING: "Printing in progress",
    PrintSubmissionStatus.COMPLETED: "Printing completed",
    PrintSubmissionStatus.FAILED: "Printing failed",
}
_FAILURE_MESSAGES = {
    "authentication": "Printing is not available right now.",
    "unavailable": "Printing is not available right now.",
    "timeout": (
        "Printing failed. The printer did not confirm the job. Please try again."
    ),
    "network": (
        "Printing failed. The printer did not confirm the job. Please try again."
    ),
    "api": "The printer rejected this print. Please try again.",
    "storage": "Printing failed. The document was not printed.",
}
_PRINTNODE_TO_STATUS = {
    "new": PrintSubmissionStatus.SUBMITTED,
    "sent_to_client": PrintSubmissionStatus.PRINTING,
    "done": PrintSubmissionStatus.COMPLETED,
    "error": PrintSubmissionStatus.FAILED,
    "expired": PrintSubmissionStatus.FAILED,
    "deleted": PrintSubmissionStatus.FAILED,
    "disappeared": PrintSubmissionStatus.FAILED,
}


class PrintRequestError(Exception):
    """The browser payload cannot be printed."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class PrintRejected(Exception):
    """The session may not print this document."""

    def __init__(self, message: str, status_code: int, title: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.title = title


@dataclass(frozen=True)
class PrintOptions:
    document_id: UUID
    copies: int
    pages: str
    color_mode: str
    sides: str
    paper_size: str
    idempotency_key: UUID


def parse_print_options(
    payload: dict, *, require_idempotency: bool = True
) -> PrintOptions:
    """Read the fields the kiosk is allowed to choose.

    Printer ids, prices, and order ids in ``payload`` are ignored.
    A quote omits the idempotency key because it does not create a print.
    """

    if not isinstance(payload, dict):
        raise PrintRequestError(_BAD_REQUEST)
    copies = payload.get("copies")
    if isinstance(copies, bool) or not isinstance(copies, int) or not 1 <= copies <= 99:
        raise PrintRequestError(_COPIES)
    try:
        pages = selection_syntax(payload.get("pages"))
    except PageSelectionError as exc:
        raise PrintRequestError(str(exc)) from None
    color_mode = payload.get("color")
    if color_mode not in _COLORS:
        raise PrintRequestError(_COLOR)
    sides = payload.get("sides")
    if sides not in _SIDES_VALUES:
        raise PrintRequestError(_SIDES)
    paper_size = payload.get("paper_size", "a4")
    if paper_size is None:
        paper_size = "a4"
    if (
        not isinstance(paper_size, str)
        or paper_size.strip().lower() not in {"a4", "a6"}
    ):
        raise PrintRequestError(PRICE_MESSAGES["invalid_paper"])
    paper_size = paper_size.strip().lower()
    if require_idempotency:
        key = _uuid_field(payload.get("idempotency_key"))
    else:
        key = UUID(int=0)
    return PrintOptions(
        document_id=_uuid_field(payload.get("document_id")),
        copies=copies,
        pages=pages,
        color_mode=color_mode,
        sides=sides,
        paper_size=paper_size,
        idempotency_key=key,
    )


def submit_print(
    *, job: PrintJob, options: PrintOptions, storage_factory, client_factory
):
    """Record one print attempt and send its PDF to PrintNode."""

    try:
        submission, document, client = _reserve(job, options, client_factory)
    except IntegrityError:
        submission = _load_replay(job, options)
        document = None
        client = None
    if client is None:
        return submission
    return _deliver(submission, document, client, storage_factory)


def quote_print(*, job: PrintJob, options: PrintOptions, storage_factory):
    """Price the selected document without creating a print or calling PrintNode."""

    if not job_can_release(job):
        raise PrintRejected(_UNUSABLE, 403)
    document = PrintDocument.objects.filter(
        pk=options.document_id,
        job=job,
        status=PrintDocumentStatus.STORED,
    ).first()
    if document is None:
        raise PrintRejected(_UNAVAILABLE, 404)
    try:
        pdf = read_document_pdf(job.id, document.id, storage_factory())
    except StorageConfigError as exc:
        logger.warning("printme quote storage unavailable: %s", exc)
        raise PrintRejected("Printing is not available right now.", 503) from None
    if pdf is None:
        raise PrintRejected(_UNAVAILABLE, 404)
    try:
        normalized = normalize_page_selection(options.pages, pdf)
        page_total = selected_page_total(normalized, pdf)
    except PageSelectionError as exc:
        raise PrintRequestError(str(exc)) from None
    try:
        return build_quote(
            document,
            copies=options.copies,
            pages=normalized,
            color_mode=options.color_mode,
            sides=options.sides,
            paper_size=options.paper_size,
            page_total=page_total,
        )
    except PriceError as exc:
        raise PrintRequestError(str(exc)) from None


def refresh_submission(submission: PrintSubmission, client_factory) -> PrintSubmission:
    """Update ``submission`` from PrintNode when a later state is known.

    A status request that fails leaves the stored status in place. Accepting
    the original request is not marked completed here.
    """

    if submission.status not in (
        PrintSubmissionStatus.SUBMITTED,
        PrintSubmissionStatus.PRINTING,
    ):
        return submission
    if not submission.printnode_job_id:
        return submission
    try:
        client = client_factory()
    except PrintNodeConfigError as exc:
        logger.warning("printme print status unavailable: %s", exc)
        return submission
    try:
        state = client.fetch_state(int(submission.printnode_job_id))
    except PrintNodeError as exc:
        logger.warning(
            "printme submission %s status failed %s",
            submission.id,
            exc.code,
        )
        return submission
    mapped = status_for_printnode_state(state)
    if mapped is None or mapped == submission.status:
        return submission
    if (
        submission.status == PrintSubmissionStatus.PRINTING
        and mapped == PrintSubmissionStatus.SUBMITTED
    ):
        return submission
    submission.status = mapped
    if mapped == PrintSubmissionStatus.FAILED:
        failed = state.strip().lower() == "error"
        submission.error_detail = "api" if failed else "unavailable"
    submission.save(update_fields=["status", "error_detail", "updated_at"])
    return submission


def status_for_printnode_state(state: str) -> str | None:
    """Map a PrintNode state name onto a PrintSubmission status."""

    if not isinstance(state, str):
        return None
    return _PRINTNODE_TO_STATUS.get(state.strip().lower())


def public_submission(submission: PrintSubmission) -> dict[str, object]:
    """Return the status fields the kiosk may show."""

    return {
        "ok": submission.status != PrintSubmissionStatus.FAILED,
        "submission_id": str(submission.id),
        "status": submission.status,
        "title": _TITLES[submission.status],
        "message": customer_message(submission),
    }


def http_status(submission: PrintSubmission) -> int:
    if submission.status != PrintSubmissionStatus.FAILED:
        return 200
    if submission.error_detail == "unavailable":
        return 503
    if submission.error_detail in PAGE_MESSAGES or submission.error_detail in PRICE_MESSAGES:
        return 400
    return 502


def customer_message(submission: PrintSubmission) -> str:
    if submission.status == PrintSubmissionStatus.FAILED:
        if submission.error_detail in PAGE_MESSAGES:
            return PAGE_MESSAGES[submission.error_detail]
        if submission.error_detail in PRICE_MESSAGES:
            return PRICE_MESSAGES[submission.error_detail]
        return _FAILURE_MESSAGES.get(
            submission.error_detail,
            _MESSAGES[PrintSubmissionStatus.FAILED],
        )
    return _MESSAGES[submission.status]


def _reserve(job, options, client_factory):
    with transaction.atomic():
        try:
            current = PrintJob.objects.select_for_update().get(pk=job.pk)
        except PrintJob.DoesNotExist:
            raise PrintRejected(_UNAVAILABLE, 404) from None
        if not job_can_release(current):
            raise PrintRejected(_UNUSABLE, 403)
        document = (
            PrintDocument.objects.select_for_update()
            .filter(
                pk=options.document_id,
                job=current,
                status=PrintDocumentStatus.STORED,
            )
            .first()
        )
        if document is None:
            raise PrintRejected(_UNAVAILABLE, 404)
        existing = (
            PrintSubmission.objects.select_for_update()
            .filter(pk=options.idempotency_key)
            .first()
        )
        if existing is not None:
            if existing.job_id != current.id or existing.document_id != document.id:
                raise PrintRejected(_UNAVAILABLE, 404)
            return existing, None, None
        _expire_stale(current, document)
        if PrintSubmission.objects.filter(
            job=current,
            document=document,
            status__in=PrintSubmissionStatus.ACTIVE,
        ).exists():
            raise PrintRejected(_IN_PROGRESS, 409, "Printing in progress")
        try:
            client = client_factory()
        except PrintNodeConfigError as exc:
            logger.warning("printme print unavailable: %s", exc)
            failed = _create(
                current,
                document,
                options,
                printer_id=None,
                status=PrintSubmissionStatus.FAILED,
                error_detail="unavailable",
            )
            return failed, None, None
        created = _create(
            current,
            document,
            options,
            printer_id=client.printer_id,
            status=PrintSubmissionStatus.ACCEPTED,
            error_detail="",
        )
        return created, document, client


def _load_replay(job, options) -> PrintSubmission:
    existing = PrintSubmission.objects.filter(pk=options.idempotency_key).first()
    if (
        existing is None
        or existing.job_id != job.id
        or existing.document_id != options.document_id
    ):
        raise PrintRejected(_UNAVAILABLE, 404)
    return existing


def _deliver(submission, document, client, storage_factory):
    try:
        pdf = read_document_pdf(submission.job_id, document.id, storage_factory())
    except StorageConfigError as exc:
        logger.warning("printme print storage unavailable: %s", exc)
        return _fail(submission, "unavailable")
    if pdf is None:
        return _fail(submission, "storage")
    try:
        normalized = normalize_page_selection(submission.pages, pdf)
        page_total = selected_page_total(normalized, pdf)
    except PageSelectionError as exc:
        return _fail(submission, exc.code)
    try:
        quote = build_quote(
            document,
            copies=submission.copies,
            pages=normalized,
            color_mode=submission.color_mode,
            sides=submission.sides,
            paper_size=submission.paper_size or "a4",
            page_total=page_total,
        )
    except PriceError as exc:
        return _fail(submission, exc.code)
    submission.pages = normalized
    submission.currency = quote.currency
    submission.quoted_total = quote.total
    submission.unit_price = quote.unit_amount
    submission.price_variant_id = quote.variant_id
    submission.billable_quantity = quote.quantity if quote.available else None
    title = display_filename(document.original_filename) or "Document"
    try:
        remote_id = client.submit_pdf(
            pdf=pdf,
            title=title,
            copies=submission.copies,
            sides=submission.sides,
            color_mode=submission.color_mode,
            pages=normalized,
            idempotency_key=str(submission.id),
        )
    except PrintNodeError as exc:
        logger.warning("printme submission %s failed %s", submission.id, exc.code)
        submission.save(
            update_fields=[
                "pages",
                "currency",
                "quoted_total",
                "unit_price",
                "price_variant_id",
                "billable_quantity",
                "updated_at",
            ]
        )
        return _fail(submission, exc.code)
    submission.printnode_job_id = remote_id
    submission.status = PrintSubmissionStatus.SUBMITTED
    submission.submitted_at = timezone.now()
    submission.save(
        update_fields=[
            "pages",
            "currency",
            "quoted_total",
            "unit_price",
            "price_variant_id",
            "billable_quantity",
            "printnode_job_id",
            "status",
            "submitted_at",
            "updated_at",
        ]
    )
    logger.info("printme submission %s submitted", submission.id)
    return submission


def _create(job, document, options, *, printer_id, status, error_detail):
    return PrintSubmission.objects.create(
        id=options.idempotency_key,
        job=job,
        document=document,
        printer_id=printer_id,
        copies=options.copies,
        pages=options.pages,
        color_mode=options.color_mode,
        sides=options.sides,
        paper_size=options.paper_size,
        status=status,
        error_detail=error_detail,
    )


def _expire_stale(job, document) -> None:
    cutoff = timezone.now() - _STALE_AFTER
    PrintSubmission.objects.filter(
        job=job,
        document=document,
        status=PrintSubmissionStatus.ACCEPTED,
        created_at__lt=cutoff,
    ).update(
        status=PrintSubmissionStatus.FAILED,
        error_detail="timeout",
        updated_at=timezone.now(),
    )


def _fail(submission: PrintSubmission, code: str) -> PrintSubmission:
    submission.status = PrintSubmissionStatus.FAILED
    submission.error_detail = code
    submission.save(update_fields=["status", "error_detail", "updated_at"])
    return submission


def _uuid_field(value) -> UUID:
    if not isinstance(value, str):
        raise PrintRequestError(_BAD_REQUEST)
    try:
        return UUID(value)
    except ValueError:
        raise PrintRequestError(_BAD_REQUEST) from None
