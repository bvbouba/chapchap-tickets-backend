import logging

import graphene
import requests
from django.utils import timezone

from ..celeryconf import app
from ..core.db.connection import allow_writer
from ..order.models import Order
from .models import StaffDeviceToken
from .presence import send_cashier_offline_sms

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


def _first_passenger_name(order: Order) -> str:
    passenger = (order.customer_note or "").split(";")[0].strip()
    if passenger:
        return passenger.replace(",", " ")
    billing = order.billing_address
    if billing:
        name = f"{billing.first_name} {billing.last_name}".strip()
        if name:
            return name
    return order.user_email or "Client"


def _trip_route(order: Order) -> str:
    line = order.lines.first()
    return line.product_name if line else "Nouveau trajet"


def _passenger_count(order: Order) -> int:
    total = sum(line.quantity or 0 for line in order.lines.all())
    return total if total > 0 else 1


def _build_message(order: Order, token: str) -> dict:
    passenger_name = _first_passenger_name(order)
    trip_route = _trip_route(order)
    order_global_id = graphene.Node.to_global_id("Order", order.id)
    seats = _passenger_count(order)
    seat_label = "1 passager" if seats == 1 else f"{seats} passagers"
    return {
        "to": token,
        "title": f"Nouvelle commande #{order.number}",
        "body": f"{passenger_name} ({seat_label}) — {trip_route}",
        "priority": "high",
        "sound": "default",
        "channelId": "default",
        "data": {
            "type": "ORDER_CREATED",
            "orderId": order_global_id,
            "orderNumber": order.number,
            "passengerName": passenger_name,
            "tripRoute": trip_route,
            "passengerCount": seats,
        },
    }


@app.task
@allow_writer()
def send_incoming_order_push(order_id: int):
    order = (
        Order.objects.select_related("billing_address", "channel")
        .prefetch_related("lines")
        .filter(id=order_id)
        .first()
    )
    if not order:
        return

    try:
        from .advance_ledger import debit_online_order

        debit_online_order(order)
    except Exception:
        logger.exception("advance ledger debit failed for order %s", order.id)

    tokens = list(
        StaffDeviceToken.objects.filter(
            is_active=True,
            user__is_active=True,
            user__is_staff=True,
        )
        .exclude(token__startswith="presence:")
        .values_list("token", flat=True)
    )

    if tokens:
        messages = [_build_message(order, token) for token in tokens]
        try:
            response = requests.post(EXPO_PUSH_URL, json=messages, timeout=10)
            response.raise_for_status()
        except requests.RequestException:
            logger.exception("Failed to send incoming order push for order %s", order_id)
            # Still attempt SMS fallback below.
            response = None
        else:
            now = timezone.now()
            # Do NOT bump last_seen_at here — that would fake "online" presence.
            # Only deactivate dead tokens.
            try:
                results = response.json().get("data", [])
            except ValueError:
                results = []

            invalid_tokens = [
                token
                for token, result in zip(tokens, results)
                if result.get("status") == "error"
                and result.get("details", {}).get("error") == "DeviceNotRegistered"
            ]
            if invalid_tokens:
                StaffDeviceToken.objects.filter(token__in=invalid_tokens).update(
                    is_active=False,
                    last_seen_at=now,
                )

    try:
        sent = send_cashier_offline_sms(order)
        if sent:
            logger.info(
                "Sent %s cashier offline SMS alert(s) for order %s",
                sent,
                order_id,
            )
    except Exception:
        logger.exception(
            "Cashier offline SMS fallback failed for order %s", order_id
        )
