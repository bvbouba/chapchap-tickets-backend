"""Validate a kiosk page selection and count pages in a stored PDF.

PrintNode accepts ``options.pages`` as a string such as ``1-3,6,8-10``.
``all`` means the whole document and is sent without a page list. Custom
ranges are checked against the page count in the PDF before anything is sent.
"""

import re

_PAGE_MARK = re.compile(rb"/Type\s*/Page(?!s)")
_TOKEN = re.compile(r"^(\d+)(?:-(\d+))?$")
_MAX_LENGTH = 128

PAGE_MESSAGES = {
    "pages_invalid": "That page range is not valid.",
    "pages_empty": "Enter a page range.",
    "pages_order": "That page range is out of order.",
    "pages_missing": "Choose pages that are in this document.",
    "pages_unknown": "That page range cannot be checked for this document.",
}


class PageSelectionError(Exception):
    """The requested pages cannot be printed."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(PAGE_MESSAGES[code])


def pdf_page_count(content: bytes) -> int | None:
    """Return the number of pages written as page objects in ``content``.

    Compressed page trees that hide ``/Type /Page`` return None. Callers
    refuse a custom range when the count is unknown.
    """

    if not isinstance(content, bytes) or not content.startswith(b"%PDF-"):
        return None
    found = len(_PAGE_MARK.findall(content))
    if found < 1:
        return None
    return found


def selection_syntax(value) -> str:
    """Return ``all`` or a cleaned range string.

    This checks the shape of the text only. It does not know how many pages
    the document has.
    """

    if not isinstance(value, str):
        raise PageSelectionError("pages_invalid")
    text = "".join(value.split())
    if text == "all":
        return "all"
    if not text:
        raise PageSelectionError("pages_empty")
    if len(text) > _MAX_LENGTH:
        raise PageSelectionError("pages_invalid")
    for part in text.split(","):
        match = _TOKEN.fullmatch(part)
        if match is None:
            raise PageSelectionError("pages_invalid")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1:
            raise PageSelectionError("pages_invalid")
        if end < start:
            raise PageSelectionError("pages_order")
    return text


def normalize_page_selection(value: str, content: bytes) -> str:
    """Return the PrintNode page string for ``value``.

    ``all`` stays ``all``. Any other selection becomes a compressed range
    that only contains pages present in ``content``.
    """

    text = selection_syntax(value)
    if text == "all":
        return "all"
    count = pdf_page_count(content)
    if count is None:
        raise PageSelectionError("pages_unknown")
    chosen = _expand(text, count)
    return _compress(chosen)


def selected_page_total(value: str, content: bytes) -> int | None:
    """Return how many document pages ``value`` includes.

    None means the PDF page count could not be read. ``all`` still needs that
    count before a price can be calculated.
    """

    text = selection_syntax(value)
    count = pdf_page_count(content)
    if count is None:
        return None
    if text == "all":
        return count
    return len(_expand(text, count))


def pages_label(value: str) -> str:
    if value == "all":
        return "All pages"
    return value


def _expand(text: str, page_count: int) -> list[int]:
    chosen: list[int] = []
    seen: set[int] = set()
    for part in text.split(","):
        match = _TOKEN.fullmatch(part)
        if match is None:
            raise PageSelectionError("pages_invalid")
        start = int(match.group(1))
        end = int(match.group(2) or match.group(1))
        if end < start:
            raise PageSelectionError("pages_order")
        if start < 1 or end > page_count:
            raise PageSelectionError("pages_missing")
        for number in range(start, end + 1):
            if number not in seen:
                seen.add(number)
                chosen.append(number)
    if not chosen:
        raise PageSelectionError("pages_empty")
    return chosen


def _compress(pages: list[int]) -> str:
    if not pages:
        raise PageSelectionError("pages_empty")
    parts: list[str] = []
    start = pages[0]
    previous = pages[0]
    for number in pages[1:]:
        if number == previous + 1:
            previous = number
            continue
        parts.append(_span(start, previous))
        start = previous = number
    parts.append(_span(start, previous))
    text = ",".join(parts)
    if len(text) > _MAX_LENGTH:
        raise PageSelectionError("pages_invalid")
    return text


def _span(start: int, end: int) -> str:
    if start == end:
        return str(start)
    return f"{start}-{end}"
