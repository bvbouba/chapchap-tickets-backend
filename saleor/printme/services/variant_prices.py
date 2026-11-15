"""Read PrintMe side prices from Saleor variant channel listings.

The kiosk matches paper size and color by attribute value slug, then uses the
price stored on the PrintMe channel. A price sent by the browser is never used.
"""

from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings

PRICE_MESSAGES = {
    "missing_channel": "Pricing is not configured for this print.",
    "missing_variant": "Pricing is not configured for this print.",
    "missing_listing": "Pricing is not configured for this print.",
    "null_price": "Pricing is not configured for this print.",
    "ambiguous": "Pricing is not configured for this print.",
    "wrong_currency": "That price is not in the channel currency.",
    "invalid_paper": "That paper size is not available.",
    "invalid_color": "That color option is not available.",
}

_PAPER_ATTRIBUTE = "paper-size"
_COLOR_ATTRIBUTE = "color-mode"
_COLOR_SLUGS = {
    "black-and-white": "bw",
    "color": "color",
}
_PAPER_SIZES = {"a4", "a6"}


class PriceError(Exception):
    """The selected print cannot be priced from Saleor."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(PRICE_MESSAGES[code])


@dataclass(frozen=True)
class CatalogEntry:
    variant_id: int
    paper_size: str
    color_mode: str
    listed: bool
    currency: str
    channel_currency: str
    unit_amount: Decimal | None


def load_channel_catalog() -> list[CatalogEntry] | None:
    """Return print variants for the configured channel.

    None means the channel slug is not configured, so the kiosk must not
    invent a price. A configured channel with no matching variants returns
    an empty list.
    """

    slug = str(getattr(settings, "PRINTME_CHANNEL_SLUG", "") or "").strip()
    if not slug:
        return None
    return _load_catalog(slug)


def choose_rate(
    catalog: list[CatalogEntry], *, paper_size: str, color_mode: str
) -> CatalogEntry:
    """Pick the one listing that matches the selected paper and color."""

    paper = str(paper_size or "").strip().lower()
    if paper not in _PAPER_SIZES:
        raise PriceError("invalid_paper")
    color_slug = _COLOR_SLUGS.get(color_mode)
    if color_slug is None:
        raise PriceError("invalid_color")
    matches = [
        entry
        for entry in catalog
        if entry.paper_size == paper and entry.color_mode == color_slug
    ]
    if not matches:
        raise PriceError("missing_variant")
    if len(matches) > 1:
        raise PriceError("ambiguous")
    entry = matches[0]
    if not entry.listed:
        raise PriceError("missing_listing")
    if entry.unit_amount is None:
        raise PriceError("null_price")
    if entry.currency != entry.channel_currency:
        raise PriceError("wrong_currency")
    return entry


def resolve_side_rate(*, paper_size: str, color_mode: str) -> CatalogEntry | None:
    """Return the channel price, or None when pricing is not configured."""

    catalog = load_channel_catalog()
    if catalog is None:
        return None
    return choose_rate(catalog, paper_size=paper_size, color_mode=color_mode)


def _load_catalog(slug: str) -> list[CatalogEntry]:
    from saleor.attribute.models import (
        AssignedVariantAttribute,
        AssignedVariantAttributeValue,
    )
    from saleor.channel.models import Channel
    from saleor.product.models import ProductVariantChannelListing

    channel = Channel.objects.filter(slug=slug).first()
    if channel is None:
        raise PriceError("missing_channel")
    assignments = list(
        AssignedVariantAttribute.objects.filter(
            assignment__attribute__slug__in=(_PAPER_ATTRIBUTE, _COLOR_ATTRIBUTE),
        ).select_related("assignment__attribute")
    )
    if not assignments:
        return []
    values_by_assignment: dict[int, list[str]] = {}
    value_rows = AssignedVariantAttributeValue.objects.filter(
        assignment_id__in=[row.id for row in assignments],
    ).select_related("value")
    for row in value_rows:
        values_by_assignment.setdefault(row.assignment_id, []).append(row.value.slug)
    by_variant: dict[int, dict[str, str]] = {}
    for assignment in assignments:
        slugs = values_by_assignment.get(assignment.id) or []
        if len(slugs) != 1:
            continue
        by_variant.setdefault(assignment.variant_id, {})[
            assignment.assignment.attribute.slug
        ] = slugs[0]
    variant_ids = [
        variant_id
        for variant_id, attrs in by_variant.items()
        if _PAPER_ATTRIBUTE in attrs and _COLOR_ATTRIBUTE in attrs
    ]
    listings = {
        listing.variant_id: listing
        for listing in ProductVariantChannelListing.objects.filter(
            channel=channel,
            variant_id__in=variant_ids,
        )
    }
    catalog: list[CatalogEntry] = []
    for variant_id in variant_ids:
        attrs = by_variant[variant_id]
        listing = listings.get(variant_id)
        catalog.append(
            CatalogEntry(
                variant_id=variant_id,
                paper_size=attrs[_PAPER_ATTRIBUTE],
                color_mode=attrs[_COLOR_ATTRIBUTE],
                listed=listing is not None,
                currency="" if listing is None else listing.currency,
                channel_currency=channel.currency_code,
                unit_amount=None if listing is None else listing.price_amount,
            )
        )
    return catalog
