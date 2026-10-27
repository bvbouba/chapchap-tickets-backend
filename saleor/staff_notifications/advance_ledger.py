"""ChapChap advance deposit ledger on the operator Channel.

Credits: ChapChap pays the bus company in advance (Wave / cash).
Debits: an online (checkout) ticket sale recoups that advance.

Canonical store is Channel public metadata so cashiers can read the
balance without MANAGE_STAFF. The manager gift card stays in sync so
the prepaid amount remains a real Saleor store-credit balance.
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from decimal import Decimal

import graphene
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..account.models import User
from ..channel.models import Channel
from ..giftcard import events as gift_card_events
from ..giftcard.models import GiftCard
from ..giftcard.search import update_gift_cards_search_vector
from ..order import OrderOrigin
from ..order.models import Order

logger = logging.getLogger(__name__)

STORE_CREDIT_TAG = "store-credit"
STORE_CREDIT_BALANCE_KEY = "store_credit_balance"
STORE_CREDIT_HISTORY_KEY = "store_credit_history"

ADVANCE_BALANCE_KEY = "advance_balance"
ADVANCE_LEDGER_KEY = "advance_ledger"
ADVANCE_MANAGER_USER_ID_KEY = "advance_manager_user_id"

ORDER_DEBITED_KEY = "advance_ledger_debited"

LEDGER_LIMIT = 500
HISTORY_LIMIT = 500

TYPE_CREDIT = "CREDIT"
TYPE_DEBIT = "DEBIT"

OPERATOR_GROUP_NAMES = {"operators", "gestionnaires"}
ROLE_GROUP_NAMES = {
    "operators",
    "gestionnaires",
    "counters",
    "guichets",
    "drivers",
    "chauffeurs",
    "schedulers",
    "planificateurs",
    "finance",
    "finances",
    "manage_operators",
    "manage-operators",
    "gestion_operateurs",
    "gestion-operateurs",
}
PLATFORM_CHANNEL_SLUGS = {"ci", "default-channel", "default"}

FEE_NEEDLES = (
    "platform-fee",
    "platform fee",
    "platform_fee",
    "service fee",
    "service-fee",
    "service_fee",
    "frais de plateforme",
    "frais plateforme",
    "frais de service",
    "chapticket fee",
    "chapchap fee",
)


def parse_json_list(raw) -> list:
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _decimal_amount(value) -> Decimal:
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except Exception:
        return Decimal("0")


def _int_amount(value) -> int:
    return int(_decimal_amount(value).quantize(Decimal("1")))


def _is_platform_fee_line(line) -> bool:
    parts = [
        getattr(line, "product_name", None),
        getattr(line, "product_sku", None),
        getattr(line, "variant_name", None),
    ]
    variant = getattr(line, "variant", None)
    product = getattr(variant, "product", None) if variant else None
    if product is not None:
        parts.extend([getattr(product, "slug", None), getattr(product, "name", None)])
    hay = " ".join(str(p) for p in parts if p).lower()
    return any(needle in hay for needle in FEE_NEEDLES)


def online_ticket_amount(order: Order) -> Decimal:
    """Company ticket share: order gross minus platform-fee lines."""
    gross = Decimal(order.total_gross_amount or 0)
    fees = Decimal("0")
    for line in order.lines.all():
        if _is_platform_fee_line(line):
            fees += Decimal(line.total_price_gross_amount or 0)
    amount = gross - fees
    return amount if amount > 0 else Decimal("0")


CONFIRMATION_CODE_KEY = "confirmation_code"


def _js_int32(n: int) -> int:
    n &= 0xFFFFFFFF
    if n >= 0x80000000:
        n -= 0x100000000
    return n


def generate_short_code(saleor_id: str) -> str:
    """Match staff/customer `generateShortCode` (5-char base36 of the Order gid)."""
    hashed = 0
    for ch in saleor_id:
        hashed = _js_int32((hashed << 5) - hashed + ord(ch))
    n = abs(hashed)
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n == 0:
        return "0".upper()[:5]
    digits: list[str] = []
    while n:
        n, rem = divmod(n, 36)
        digits.append(alphabet[rem])
    return "".join(reversed(digits)).upper()[:5]


def order_confirmation_code(order: Order) -> str:
    raw = (order.get_value_from_metadata(CONFIRMATION_CODE_KEY) or "").strip()
    if raw:
        return raw.upper()
    return generate_short_code(graphene.Node.to_global_id("Order", order.pk))


def format_order_reference(order: Order) -> str:
    return order_confirmation_code(order)


def _channel_ledger(channel: Channel) -> list[dict]:
    return parse_json_list(
        channel.get_value_from_private_metadata(ADVANCE_LEDGER_KEY, "[]")
    )


def _channel_balance(channel: Channel) -> int:
    raw = channel.get_value_from_private_metadata(ADVANCE_BALANCE_KEY, None)
    if raw is None or raw == "":
        return 0
    return _int_amount(raw)


def _save_channel_ledger(channel: Channel, ledger: list[dict], balance: int) -> None:
    trimmed = ledger[-LEDGER_LIMIT:]
    private = dict(channel.private_metadata or {})
    private[ADVANCE_LEDGER_KEY] = json.dumps(trimmed, ensure_ascii=False)
    private[ADVANCE_BALANCE_KEY] = str(int(balance))
    channel.private_metadata = private
    channel.save(update_fields=["private_metadata"])


def _append_user_history(user: User, entry: dict, balance: int) -> None:
    history = parse_json_list(
        user.get_value_from_private_metadata(STORE_CREDIT_HISTORY_KEY, "[]")
    )
    history.append(entry)
    history = history[-HISTORY_LIMIT:]
    private = dict(user.private_metadata or {})
    private[STORE_CREDIT_HISTORY_KEY] = json.dumps(history, ensure_ascii=False)
    private[STORE_CREDIT_BALANCE_KEY] = str(int(balance))
    user.private_metadata = private
    user.save(update_fields=["private_metadata", "updated_at"])


def _history_item_to_ledger_entry(item: dict, running: int) -> tuple[dict, int]:
    amount = _int_amount(item.get("amount") or 0)
    entry_type = str(item.get("type") or "").upper()
    if entry_type not in {TYPE_CREDIT, TYPE_DEBIT}:
        entry_type = TYPE_DEBIT if amount < 0 else TYPE_CREDIT
    signed = -abs(amount) if entry_type == TYPE_DEBIT else abs(amount)
    running += signed
    return (
        {
            "at": item.get("at") or timezone.now().isoformat(),
            "type": entry_type,
            "amount": str(signed),
            "currency": item.get("currency") or "XOF",
            "balanceAfter": str(running),
            "method": item.get("method") or "",
            "waveTransactionId": item.get("waveTransactionId")
            or item.get("wave_transaction_id")
            or "",
            "receiptUrl": item.get("receiptUrl") or item.get("receipt_url") or "",
            "orderNumber": item.get("orderNumber") or "",
            "orderId": item.get("orderId") or "",
            "confirmationCode": item.get("confirmationCode") or "",
            "note": item.get("note") or "",
            "recordedBy": item.get("recordedBy") or "",
            "giftCardId": item.get("giftCardId") or "",
        },
        running,
    )


def seed_ledger_from_user_history(channel: Channel, manager: User) -> list[dict]:
    """One-time copy of manager store-credit history onto the channel ledger."""
    history = parse_json_list(
        manager.get_value_from_private_metadata(STORE_CREDIT_HISTORY_KEY, "[]")
    )
    currency = (channel.currency_code or "XOF").upper()
    if history:
        chronological = sorted(history, key=lambda row: str(row.get("at") or ""))
        ledger: list[dict] = []
        running = 0
        for item in chronological:
            if not isinstance(item, dict):
                continue
            entry, running = _history_item_to_ledger_entry(item, running)
            ledger.append(entry)
        if running == 0:
            running = peek_store_credit_amount(manager, currency)
        _store_manager_on_channel(channel, manager)
        _save_channel_ledger(channel, ledger, running)
        return ledger

    balance = peek_store_credit_amount(manager, currency)
    if balance:
        _store_manager_on_channel(channel, manager)
        _save_channel_ledger(channel, [], balance)
    return []


def peek_store_credit_amount(user: User, currency: str | None = None) -> int:
    raw = user.get_value_from_private_metadata(STORE_CREDIT_BALANCE_KEY)
    if raw not in (None, ""):
        amount = _int_amount(raw)
        if amount:
            return amount
    qs = GiftCard.objects.filter(
        used_by=user,
        is_active=True,
        tags__name=STORE_CREDIT_TAG,
    )
    if currency:
        qs = qs.filter(currency=currency)
    card = qs.order_by("pk").first()
    if card is None:
        return 0
    return _int_amount(card.current_balance_amount)


def ensure_channel_ledger(channel: Channel, manager: User | None) -> list[dict]:
    ledger = _channel_ledger(channel)
    if ledger:
        return ledger
    if manager is not None:
        return seed_ledger_from_user_history(channel, manager)
    return []


def _user_belongs_to_channel(user: User, channel: Channel) -> bool:
    slug = (channel.slug or "").strip().lower()
    name = (channel.name or "").strip().lower()
    if not slug and not name:
        return False
    groups = [
        (g or "").strip().lower()
        for g in user.groups.values_list("name", flat=True)
    ]
    if slug and slug in groups:
        return True
    if name and name in groups:
        return True
    meta_slug = (
        user.get_value_from_private_metadata("assigned_channel_slug") or ""
    ).strip().lower()
    return bool(slug) and meta_slug == slug


def find_channel_manager(channel: Channel) -> User | None:
    stored = channel.get_value_from_private_metadata(ADVANCE_MANAGER_USER_ID_KEY, "") or ""
    if stored:
        try:
            _type, pk = graphene.Node.from_global_id(stored)
            user = User.objects.filter(pk=pk, is_staff=True, is_active=True).first()
            if user:
                return user
        except (ValueError, TypeError, OverflowError):
            pass

    qs = User.objects.filter(is_staff=True, is_active=True)
    by_groups = qs.filter(
        groups__name__iexact=channel.slug,
        groups__name__in=list(OPERATOR_GROUP_NAMES),
    ).distinct()
    manager = by_groups.order_by("pk").first()
    if manager:
        return manager

    by_meta = qs.filter(
        Q(private_metadata__assigned_channel_slug=channel.slug)
        | Q(private_metadata__staff_role="OPERATOR")
    ).filter(
        Q(groups__name__iexact=channel.slug)
        | Q(private_metadata__assigned_channel_slug=channel.slug)
    ).distinct()
    manager = by_meta.order_by("pk").first()
    if manager:
        return manager

    card_user_ids = (
        GiftCard.objects.filter(
            is_active=True,
            tags__name=STORE_CREDIT_TAG,
            used_by__is_staff=True,
            used_by__is_active=True,
        )
        .exclude(current_balance_amount=0)
        .values_list("used_by_id", flat=True)
        .distinct()
    )
    for user in qs.filter(pk__in=list(card_user_ids)).prefetch_related("groups"):
        if _user_belongs_to_channel(user, channel):
            return user
    return None


def infer_manager_channel(user: User) -> Channel | None:
    names = [
        (name or "").strip().lower()
        for name in user.groups.values_list("name", flat=True)
    ]
    slugs = [name for name in names if name and name not in ROLE_GROUP_NAMES]
    if slugs:
        channel = Channel.objects.filter(slug__in=slugs).first()
        if channel:
            return channel
    meta_slug = (user.get_value_from_private_metadata("assigned_channel_slug", "") or "").strip()
    if meta_slug:
        return Channel.objects.filter(slug__iexact=meta_slug).first()
    return None


def _store_manager_on_channel(channel: Channel, manager: User) -> None:
    private = dict(channel.private_metadata or {})
    private[ADVANCE_MANAGER_USER_ID_KEY] = graphene.Node.to_global_id(
        "User", manager.pk
    )
    channel.private_metadata = private


def get_store_credit_gift_card(user: User, currency: str) -> GiftCard | None:
    existing_ids = list(
        GiftCard.objects.filter(
            used_by=user,
            is_active=True,
            tags__name=STORE_CREDIT_TAG,
            currency=currency,
        ).values_list("pk", flat=True)
    )
    if not existing_ids:
        return None
    return (
        GiftCard.objects.select_for_update()
        .filter(pk__in=existing_ids)
        .order_by("pk")
        .first()
    )


def debit_store_credit_gift_card(
    manager: User,
    amount: Decimal,
    currency: str,
    note: str,
) -> GiftCard | None:
    gift_card = get_store_credit_gift_card(manager, currency)
    if gift_card is None:
        return None
    old = deepcopy(gift_card)
    next_balance = Decimal(gift_card.current_balance_amount) - amount
    if next_balance < 0:
        next_balance = Decimal("0")
    gift_card.current_balance_amount = next_balance
    gift_card.save(update_fields=["current_balance_amount"])
    gift_card_events.gift_card_balance_reset_event(gift_card, old, user=None, app=None)
    if note:
        gift_card_events.gift_card_note_added_event(
            gift_card=gift_card, user=None, app=None, message=note[:250]
        )
    update_gift_cards_search_vector([gift_card])
    return gift_card


def append_ledger_entry(
    *,
    channel: Channel,
    manager: User | None,
    entry_type: str,
    amount: Decimal,
    currency: str,
    method: str = "",
    wave_transaction_id: str = "",
    receipt_url: str = "",
    order: Order | None = None,
    note: str = "",
    recorded_by: str = "",
    gift_card_id: str = "",
) -> dict:
    ledger = ensure_channel_ledger(channel, manager)
    signed = _int_amount(amount)
    if entry_type == TYPE_DEBIT:
        signed = -abs(signed)
    else:
        signed = abs(signed)

    if ledger:
        try:
            running = _int_amount(ledger[-1].get("balanceAfter"))
        except Exception:
            running = _channel_balance(channel)
    else:
        running = _channel_balance(channel)

    running += signed
    now_iso = timezone.now().isoformat()
    order_number = ""
    order_id = ""
    confirmation_code = ""
    if order is not None:
        order_number = str(order.number or "")
        order_id = graphene.Node.to_global_id("Order", order.pk)
        confirmation_code = order_confirmation_code(order)

    entry = {
        "at": now_iso,
        "type": entry_type,
        "amount": str(signed),
        "currency": currency,
        "balanceAfter": str(running),
        "method": method,
        "waveTransactionId": wave_transaction_id,
        "receiptUrl": receipt_url,
        "orderNumber": order_number,
        "orderId": order_id,
        "confirmationCode": confirmation_code,
        "note": note,
        "recordedBy": recorded_by,
        "giftCardId": gift_card_id,
        "reference": (
            confirmation_code
            if order is not None
            else (wave_transaction_id or receipt_url or "")
        ),
    }
    ledger.append(entry)
    if manager is not None:
        _store_manager_on_channel(channel, manager)
    _save_channel_ledger(channel, ledger, running)
    if manager is not None:
        _append_user_history(manager, entry, running)
    return entry


def ledger_has_order(channel: Channel, order: Order) -> bool:
    order_id = graphene.Node.to_global_id("Order", order.pk)
    number = str(order.number or "")
    for item in _channel_ledger(channel):
        if not isinstance(item, dict):
            continue
        if item.get("orderId") == order_id:
            return True
        if number and str(item.get("orderNumber") or "") == number:
            return True
    return False


def read_channel_advance(channel: Channel, manager: User | None = None) -> dict:
    raw = channel.get_value_from_private_metadata(ADVANCE_LEDGER_KEY, "[]")
    if isinstance(raw, list):
        ledger_json = json.dumps(raw, ensure_ascii=False)
    else:
        ledger_json = raw or "[]"
    balance = _channel_balance(channel)
    if balance == 0:
        owner = manager or find_channel_manager(channel)
        if owner is not None:
            balance = peek_store_credit_amount(owner, channel.currency_code)
    return {
        "balance": float(balance),
        "currency": (channel.currency_code or "XOF").upper(),
        "ledger_json": ledger_json,
    }


def debit_online_order(order: Order) -> None:
    """Deduct an online ticket sale from the channel advance. Idempotent."""
    if not order or order.origin != OrderOrigin.CHECKOUT:
        return
    channel = getattr(order, "channel", None)
    if channel is None:
        return
    slug = (channel.slug or "").strip().lower()
    if slug in PLATFORM_CHANNEL_SLUGS:
        return
    if order.get_value_from_private_metadata(ORDER_DEBITED_KEY) == "true":
        return

    amount = online_ticket_amount(order)
    if amount <= 0:
        return

    currency = (order.currency or channel.currency_code or "XOF").upper()
    note = f"Vente en ligne {format_order_reference(order)}"

    with transaction.atomic():
        locked = Channel.objects.select_for_update().filter(pk=channel.pk).first()
        if locked is None:
            return
        if ledger_has_order(locked, order):
            return
        manager = find_channel_manager(locked)
        gift_card = None
        if manager is not None:
            gift_card = debit_store_credit_gift_card(manager, amount, currency, note)
        gift_card_id = (
            graphene.Node.to_global_id("GiftCard", gift_card.id) if gift_card else ""
        )
        append_ledger_entry(
            channel=locked,
            manager=manager,
            entry_type=TYPE_DEBIT,
            amount=amount,
            currency=currency,
            order=order,
            note=note,
            gift_card_id=gift_card_id,
        )
        private = dict(order.private_metadata or {})
        private[ORDER_DEBITED_KEY] = "true"
        order.private_metadata = private
        order.save(update_fields=["private_metadata", "updated_at"])
