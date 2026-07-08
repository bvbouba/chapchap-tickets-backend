import logging

import graphene
import requests
from django.utils import timezone

from ..celeryconf import app
from ..core.db.connection import allow_writer
from ..order.models import Order
from .models import StaffDeviceToken

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


def _build_message(order: Order, token: str) -> dict:
    passenger_name = _first_passenger_name(order)
    trip_route = _trip_route(order)
    order_global_id = graphene.Node.to_global_id("Order", order.id)
    return {
        "to": token,
        "title": "Nouvelle commande",
        "body": f"{passenger_name} - {trip_route}",
        "priority": "high",
        "sound": "default",
        "data": {
            "type": "ORDER_CREATED",
            "orderId": order_global_id,
            "orderNumber": order.number,
            "passengerName": passenger_name,
            "tripRoute": trip_route,
        },
    }


@app.task
@allow_writer()
def send_incoming_order_push(order_id: int):
    order = (
        Order.objects.select_related("billing_address")
        .prefetch_related("lines")
        .filter(id=order_id)
        .first()
    )
    if not order:
        return

    tokens = list(
        StaffDeviceToken.objects.filter(
            is_active=True,
            user__is_active=True,
            user__is_staff=True,
        ).values_list("token", flat=True)
    )
    if not tokens:
        return

    messages = [_build_message(order, token) for token in tokens]
    try:
        response = requests.post(EXPO_PUSH_URL, json=messages, timeout=10)
        response.raise_for_status()
    except requests.RequestException:
        logger.exception("Failed to send incoming order push for order %s", order_id)
        return

    now = timezone.now()
    StaffDeviceToken.objects.filter(token__in=tokens).update(last_seen_at=now)

    try:
        results = response.json().get("data", [])
    except ValueError:
        return

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
