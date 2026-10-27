"""Auto-cancel paid online bookings if the guichet never confirms in time.

MoMo is charged at checkout. If staff do not confirm/fulfill within
``GUICHET_CONFIRM_TIMEOUT_MINUTES``, refund via Paystack (Saleor transaction
action) and cancel the order so the seat is released.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from ..celeryconf import app
from ..core.db.connection import allow_writer
from ..giftcard.utils import deactivate_order_gift_cards
from ..payment import PaymentError, TransactionEventType
from ..payment.gateway import request_refund_action
from ..payment.models import TransactionEvent, TransactionItem
from ..plugins.manager import get_plugins_manager
from . import OrderStatus
from .actions import cancel_order
from .models import Order

logger = logging.getLogger(__name__)

TICKET_STATUS_KEY = "ticket_status"
GUICHET_DEADLINE_AT_KEY = "guichet_deadline_at"
CANCEL_REASON_KEY = "cancel_reason"
AWAITING_SEAT = "AWAITING_SEAT_ALLOCATION"
CANCELLED_AND_REFUNDED = "CANCELLED_AND_REFUNDED"
AUTO_EXPIRED_REASON = "AUTO_EXPIRED_UNCONFIRMED_BY_GUICHET"

GUICHET_TIMEOUT_BATCH_SIZE = 50


def _timeout_minutes() -> int:
    try:
        minutes = int(getattr(settings, "GUICHET_CONFIRM_TIMEOUT_MINUTES", 15))
    except (TypeError, ValueError):
        minutes = 15
    return minutes if minutes > 0 else 15


def _parse_deadline(raw: str | None) -> datetime | None:
    if not raw:
        return None
    value = parse_datetime(raw.strip())
    if value is None:
        return None
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    return value


def order_guichet_deadline(order: Order) -> datetime:
    """Return the guichet confirmation deadline.

    Prefer ``created_at + GUICHET_CONFIRM_TIMEOUT_MINUTES`` so ops can tune the
    SLA via env without redeploying the customer apps. If metadata
    ``guichet_deadline_at`` is earlier (e.g. client wrote a shorter window),
    use that instead.
    """
    fallback = order.created_at + timedelta(minutes=_timeout_minutes())
    raw = order.get_value_from_metadata(GUICHET_DEADLINE_AT_KEY)
    parsed = _parse_deadline(raw)
    if parsed:
        return min(parsed, fallback)
    return fallback


def is_guichet_timeout_candidate(order: Order, now: datetime | None = None) -> bool:
    now = now or timezone.now()
    if order.status in {
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
        OrderStatus.DRAFT,
        OrderStatus.FULFILLED,
        OrderStatus.PARTIALLY_FULFILLED,
        OrderStatus.RETURNED,
    }:
        return False
    ticket_status = (order.get_value_from_metadata(TICKET_STATUS_KEY) or "").strip()
    if ticket_status != AWAITING_SEAT:
        return False
    return order_guichet_deadline(order) <= now


def _refundable_transactions(order: Order) -> list[TransactionItem]:
    txs = list(
        TransactionItem.objects.filter(order_id=order.pk).order_by("-modified_at")
    )
    out: list[TransactionItem] = []
    for tx in txs:
        remaining = (tx.charged_value or Decimal(0)) - (tx.refunded_value or Decimal(0))
        if remaining > 0:
            out.append(tx)
    return out


def _request_refunds(order: Order, manager) -> Decimal:
    """Request Paystack refunds for charged remainder. Returns refunded amount requested."""
    total = Decimal(0)
    for tx in _refundable_transactions(order):
        amount = (tx.charged_value or Decimal(0)) - (tx.refunded_value or Decimal(0))
        if amount <= 0:
            continue
        event = TransactionEvent(
            amount_value=amount,
            currency=tx.currency,
            type=TransactionEventType.REFUND_REQUEST,
            transaction_id=tx.id,
            idempotency_key=str(uuid.uuid4()),
            message=AUTO_EXPIRED_REASON,
        )
        TransactionEvent.objects.bulk_create([event])
        try:
            request_refund_action(
                request_event=event,
                refund_value=amount,
                channel_slug=order.channel.slug,
                user=None,
                app=None,
                transaction=tx,
                manager=manager,
            )
            total += amount
            logger.info(
                "Guichet timeout: refund requested order=%s tx=%s amount=%s",
                order.pk,
                tx.token,
                amount,
            )
        except PaymentError as exc:
            logger.warning(
                "Guichet timeout: refund failed order=%s tx=%s: %s",
                order.pk,
                tx.token,
                exc,
            )
    return total


def expire_single_guichet_order(order_id: int) -> bool:
    """Refund + cancel one order if still awaiting guichet past deadline."""
    from ..staff_notifications.presence import send_customer_guichet_expired_sms

    manager = get_plugins_manager(allow_replica=False)

    with transaction.atomic():
        try:
            # Do not select_related nullable FKs with select_for_update —
            # Postgres rejects FOR UPDATE on the nullable side of an outer join.
            order = Order.objects.select_for_update().select_related("channel").get(
                pk=order_id
            )
        except Order.DoesNotExist:
            return False

        if not is_guichet_timeout_candidate(order):
            return False

        # Already fully processed.
        if (order.get_value_from_metadata(TICKET_STATUS_KEY) or "").strip() == CANCELLED_AND_REFUNDED:
            return False

        # Claim under lock (idempotent if already claimed).
        already_claimed = (
            order.get_value_from_metadata(CANCEL_REASON_KEY) or ""
        ).strip() == AUTO_EXPIRED_REASON
        if not already_claimed:
            order.store_value_in_metadata(
                {
                    CANCEL_REASON_KEY: AUTO_EXPIRED_REASON,
                    "refund_reason": AUTO_EXPIRED_REASON,
                }
            )
            order.save(update_fields=["metadata", "updated_at"])
        order_pk = order.pk

    # Paystack sync webhook — outside the row lock.
    try:
        order = Order.objects.select_related("channel").get(pk=order_pk)
    except Order.DoesNotExist:
        return False

    # Re-check staff didn't issue the ticket while we claimed.
    ticket_status = (order.get_value_from_metadata(TICKET_STATUS_KEY) or "").strip()
    if ticket_status == "ISSUED":
        logger.info(
            "Guichet timeout: skip refund, order %s already ISSUED", order_pk
        )
        return False

    refund_amount = _request_refunds(order, manager)

    with transaction.atomic():
        order = (
            Order.objects.select_for_update()
            .select_related("channel")
            .get(pk=order_pk)
        )
        ticket_status = (order.get_value_from_metadata(TICKET_STATUS_KEY) or "").strip()
        if ticket_status == "ISSUED":
            logger.info(
                "Guichet timeout: abort cancel, order %s issued during refund",
                order_pk,
            )
            return False

        now = timezone.now()
        order.store_value_in_metadata(
            {
                TICKET_STATUS_KEY: CANCELLED_AND_REFUNDED,
                CANCEL_REASON_KEY: AUTO_EXPIRED_REASON,
                "refund_reason": AUTO_EXPIRED_REASON,
                "refunded_at": now.isoformat(),
                "refund_amount": str(refund_amount),
                "refund_method": "MOBILE_MONEY",
            }
        )
        order.save(update_fields=["metadata", "updated_at"])

        if order.can_cancel():
            cancel_order(order=order, user=None, app=None, manager=manager)
            deactivate_order_gift_cards(order.id, user=None, app=None)
        else:
            logger.warning(
                "Guichet timeout: order %s cannot cancel (status=%s)",
                order.pk,
                order.status,
            )

    try:
        order = Order.objects.select_related("billing_address").get(pk=order_id)
        send_customer_guichet_expired_sms(order)
    except Exception:
        logger.exception("Guichet timeout SMS failed for order %s", order_id)

    return True


@app.task
@allow_writer()
def expire_unconfirmed_guichet_orders_task():
    """Periodic: auto-refund MoMo bookings the guichet never confirmed.

    Disabled unless ``GUICHET_AUTO_EXPIRE_ENABLED=true``.
    """
    if not getattr(settings, "GUICHET_AUTO_EXPIRE_ENABLED", False):
        return

    now = timezone.now()
    # Broad filter; deadline checked in Python (metadata JSON shape varies).
    candidates = (
        Order.objects.filter(metadata__contains={TICKET_STATUS_KEY: AWAITING_SEAT})
        .exclude(
            status__in=[
                OrderStatus.CANCELED,
                OrderStatus.EXPIRED,
                OrderStatus.DRAFT,
                OrderStatus.FULFILLED,
            ]
        )
        .order_by("created_at")[: GUICHET_TIMEOUT_BATCH_SIZE * 3]
    )

    expired_ids: list[int] = []
    for order in candidates:
        if is_guichet_timeout_candidate(order, now=now):
            expired_ids.append(order.pk)
        if len(expired_ids) >= GUICHET_TIMEOUT_BATCH_SIZE:
            break

    if not expired_ids:
        return

    logger.info(
        "Guichet timeout: processing %s order(s): %s",
        len(expired_ids),
        expired_ids,
    )
    for order_id in expired_ids:
        try:
            expire_single_guichet_order(order_id)
        except Exception:
            logger.exception("Guichet timeout failed for order %s", order_id)
