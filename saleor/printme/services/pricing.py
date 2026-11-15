"""Price a kiosk print from the Saleor channel listing.

The browser never supplies the amount. Duplex printing still charges one
printed side for every selected document page. When the PrintMe channel is
not configured, the quote says pricing is not yet available.
"""

from dataclasses import dataclass
from decimal import Decimal

from ..models import PrintDocument
from .pages import pages_label
from .variant_prices import resolve_side_rate

_UNAVAILABLE = "Pricing is not yet available."


@dataclass(frozen=True)
class PriceQuote:
    reference: str
    copies: int
    pages_label: str
    color_label: str
    sides_label: str
    paper_label: str
    available: bool
    currency: str
    quantity: int
    unit_amount: Decimal | None
    total: Decimal | None
    variant_id: int | None

    @property
    def price_text(self) -> str:
        if not self.available or self.unit_amount is None:
            return _UNAVAILABLE
        return format_money(self.unit_amount, self.currency)

    @property
    def total_text(self) -> str:
        if not self.available or self.total is None:
            return _UNAVAILABLE
        return format_money(self.total, self.currency)


def build_quote(
    document: PrintDocument,
    *,
    copies: int,
    pages: str,
    color_mode: str,
    sides: str,
    paper_size: str,
    page_total: int | None,
) -> PriceQuote:
    """Price one printed side for every selected page, then multiply by copies.

    Two-sided printing does not reduce the number of billed sides. The unit
    price comes from the Saleor variant for ``paper_size`` and ``color_mode``.
    """

    rate = resolve_side_rate(paper_size=paper_size, color_mode=color_mode)
    available = (
        rate is not None
        and rate.unit_amount is not None
        and page_total is not None
        and page_total > 0
    )
    quantity = copies
    unit_amount = None
    total = None
    currency = ""
    variant_id = None
    if available and rate is not None and page_total is not None:
        quantity = page_total * copies
        unit_amount = rate.unit_amount
        total = (rate.unit_amount * quantity).quantize(Decimal("0.01"))
        currency = rate.currency
        variant_id = rate.variant_id
    return PriceQuote(
        reference=f"Document {document.position}",
        copies=copies,
        pages_label=pages_label(pages),
        color_label=_color_label(color_mode),
        sides_label=_sides_label(sides),
        paper_label=paper_size.upper(),
        available=bool(available),
        currency=currency,
        quantity=quantity,
        unit_amount=unit_amount,
        total=total,
        variant_id=variant_id,
    )


def public_quote(quote: PriceQuote) -> dict[str, object]:
    """Return the price fields the kiosk may show."""

    return {
        "ok": True,
        "reference": quote.reference,
        "copies": quote.copies,
        "pages_label": quote.pages_label,
        "color_label": quote.color_label,
        "sides_label": quote.sides_label,
        "paper_label": quote.paper_label,
        "pricing_available": quote.available,
        "currency": quote.currency,
        "price": quote.price_text,
        "count": quote.copies,
        "total": quote.total_text,
        "job_total": quote.total_text,
    }


def format_money(amount: Decimal, currency: str) -> str:
    quantized = amount.quantize(Decimal("0.01"))
    if quantized == quantized.to_integral():
        text = str(int(quantized))
    else:
        text = f"{quantized:.2f}"
    code = str(currency or "").strip()
    if not code:
        return text
    return f"{text} {code}"


def _color_label(value: str) -> str:
    if value == "color":
        return "Color"
    return "Black & White"


def _sides_label(value: str) -> str:
    if value == "two-sided":
        return "Two-sided"
    return "One-sided"
