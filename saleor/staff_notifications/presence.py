"""Cashier presence helpers for push + SMS fallback on new orders."""

from __future__ import annotations

import json
import logging
import re
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from ..account.models import User
from ..attribute.models import AssignedVariantAttribute
from ..order.models import Order
from ..otp.utils import send_sms_message
from .models import StaffDeviceToken

logger = logging.getLogger(__name__)

# Cashier considered offline if no heartbeat within this window.
ONLINE_THRESHOLD = timedelta(minutes=3)

CASHIER_SMS_META_KEY = "cashier_alert_sms_sent"
CUSTOMER_FULFILLMENT_SMS_META_KEY = "customer_fulfillment_sms_sent"

_STATION_META_KEYS = (
    "assigned_station_slug",
    "assigned_station_name",
    "active_station_slug",
    "active_station_name",
)


def phone_from_chapchap_email(email: str | None) -> str:
    email = (email or "").strip()
    if not email.lower().endswith("@chapchapticket.com"):
        return ""
    local = email[: -len("@chapchapticket.com")].strip()
    if not local:
        return ""
    if local.startswith("+"):
        return local
    digits = "".join(c for c in local if c.isdigit())
    return f"+{digits}" if digits else ""


def normalize_station_key(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    # Drop city suffix often present in attribute names: "Gare d'Adjamé, Abidjan"
    if "," in raw:
        raw = raw.split(",", 1)[0].strip()
    return re.sub(r"[^a-z0-9]+", "", raw)


def order_departure_station_keys(order: Order) -> set[str]:
    """Return normalized departure-station slug/name keys for an order."""
    keys: set[str] = set()
    for line in order.lines.all():
        variant = getattr(line, "variant", None)
        if not variant:
            continue
        assignments = AssignedVariantAttribute.objects.filter(
            variant_id=variant.id,
            assignment__attribute__slug="departure-station",
        ).prefetch_related("values")
        for assignment in assignments:
            for value in assignment.values.all():
                for candidate in (value.slug, value.name):
                    key = normalize_station_key(candidate)
                    if key:
                        keys.add(key)
    return keys


def user_assigned_station_keys(user: User) -> set[str]:
    meta = user.metadata or {}
    keys: set[str] = set()

    raw_json = meta.get("assigned_stations")
    if raw_json:
        try:
            stations = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
            if isinstance(stations, list):
                for station in stations:
                    if not isinstance(station, dict):
                        continue
                    for field in ("stationSlug", "stationName", "station_slug", "station_name"):
                        key = normalize_station_key(station.get(field))
                        if key:
                            keys.add(key)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

    for meta_key in _STATION_META_KEYS:
        key = normalize_station_key(meta.get(meta_key))
        if key:
            keys.add(key)
    return keys


def user_matches_departure(user: User, departure_keys: set[str]) -> bool:
    if not departure_keys:
        # Unknown departure → notify all counters (safe fallback).
        return True
    assigned = user_assigned_station_keys(user)
    if not assigned:
        # Unassigned counters still receive alerts (same as push today).
        return True
    return bool(assigned & departure_keys)


def counter_staff_queryset():
    return (
        User.objects.filter(is_active=True, is_staff=True)
        .filter(
            Q(groups__name__iexact="counters") | Q(groups__name__iexact="guichets")
        )
        .distinct()
    )


def is_user_online(user: User, *, now=None) -> bool:
    now = now or timezone.now()
    threshold = now - ONLINE_THRESHOLD
    if StaffDeviceToken.objects.filter(
        user=user,
        is_active=True,
        last_seen_at__gte=threshold,
    ).exists():
        return True
    # Fallback when the cashier is in-app but push token registration failed.
    raw = (user.get_value_from_metadata("staff_last_seen_at") or "").strip()
    if not raw:
        return False
    try:
        from django.utils.dateparse import parse_datetime

        seen = parse_datetime(raw)
        if seen is None:
            return False
        if timezone.is_naive(seen):
            seen = timezone.make_aware(seen, timezone.utc)
        return seen >= threshold
    except (TypeError, ValueError):
        return False


def bump_user_presence(user: User) -> int:
    """Refresh last_seen_at for active device tokens + user metadata heartbeat."""
    now = timezone.now()
    updated = StaffDeviceToken.objects.filter(user=user, is_active=True).update(
        last_seen_at=now,
        updated_at=now,
    )
    user.store_value_in_metadata({"staff_last_seen_at": now.isoformat()})
    user.save(update_fields=["metadata", "updated_at"])
    return max(updated, 1)


def _sms_already_sent(order: Order) -> bool:
    return (order.get_value_from_metadata(CASHIER_SMS_META_KEY) or "").strip() == "true"


def _mark_sms_sent(order: Order) -> None:
    order.store_value_in_metadata({CASHIER_SMS_META_KEY: "true"})
    order.save(update_fields=["metadata", "updated_at"])


def send_cashier_offline_sms(order: Order) -> int:
    """
    SMS counters assigned to the order departure when none of them look online.
    Returns number of SMS successfully handed to Twilio.
    """
    if _sms_already_sent(order):
        return 0

    departure_keys = order_departure_station_keys(order)
    counters = list(counter_staff_queryset())
    if not counters:
        logger.info(
            "No counter staff found for offline SMS on order %s", order.pk
        )
        return 0

    matching = [u for u in counters if user_matches_departure(u, departure_keys)]
    if not matching:
        return 0

    now = timezone.now()
    offline = [u for u in matching if not is_user_online(u, now=now)]
    # If at least one matching cashier is online on the app, skip SMS.
    if len(offline) < len(matching):
        return 0
    if not offline:
        return 0

    passenger = (order.customer_note or "").split(";")[0].strip().replace(",", " ")
    if not passenger:
        billing = order.billing_address
        if billing:
            passenger = f"{billing.first_name} {billing.last_name}".strip()
    passenger = passenger or "Client"
    line = order.lines.first()
    trip = line.product_name if line else "Nouveau trajet"
    body = (
        f"ChapChap: nouvelle commande #{order.number} — {passenger} — {trip}. "
        f"Ouvrez l'app Staff pour confirmer."
    )

    sent = 0
    for user in offline:
        phone = phone_from_chapchap_email(user.email)
        if not phone:
            continue
        result = send_sms_message(phone, body)
        if "error" in result:
            logger.warning(
                "Cashier offline SMS failed for user=%s order=%s: %s",
                user.pk,
                order.pk,
                result.get("error"),
            )
            continue
        sent += 1

    if sent > 0:
        _mark_sms_sent(order)
    return sent


def customer_phone_from_order(order: Order) -> str:
    billing = order.billing_address
    if billing and billing.phone:
        phone = str(billing.phone).strip()
        if phone:
            if phone.startswith("+"):
                return phone
            digits = "".join(c for c in phone if c.isdigit())
            return f"+{digits}" if digits else phone
    return phone_from_chapchap_email(order.user_email)


def is_cash_or_counter_order(order: Order) -> bool:
    """Cash / guichet / transfer — no customer fulfillment SMS."""
    method = (order.get_value_from_metadata("payment_method") or "").strip().lower()
    if method in {"cash", "transfer"}:
        return True
    channel = (order.get_value_from_metadata("sale_channel") or "").strip().lower()
    return channel in {"counter", "guichet", "cash"}


def send_customer_fulfillment_sms(order: Order) -> bool:
    """Notify the customer by SMS that their reservation was confirmed.

    Only for online payments — cash/guichet orders never send this SMS.
    """
    if is_cash_or_counter_order(order):
        logger.info(
            "Skip customer fulfillment SMS — cash/counter order %s", order.pk
        )
        return False

    if (order.get_value_from_metadata(CUSTOMER_FULFILLMENT_SMS_META_KEY) or "").strip() == "true":
        return False

    phone = customer_phone_from_order(order)
    if not phone:
        logger.info("Skip customer fulfillment SMS — no phone on order %s", order.pk)
        return False

    body = (
        f"ChapChap: votre réservation #{order.number} est confirmée. "
        f"Présentez votre billet à l'embarquement. Bon voyage !"
    )
    result = send_sms_message(phone, body)
    if "error" in result:
        logger.warning(
            "Customer fulfillment SMS failed for order=%s: %s",
            order.pk,
            result.get("error"),
        )
        return False

    order.store_value_in_metadata({CUSTOMER_FULFILLMENT_SMS_META_KEY: "true"})
    order.save(update_fields=["metadata", "updated_at"])
    return True


CUSTOMER_GUICHET_EXPIRED_SMS_META_KEY = "customer_guichet_expired_sms_sent"


def send_customer_guichet_expired_sms(order: Order) -> bool:
    """Notify customer that guichet missed the SLA and payment was refunded."""
    if is_cash_or_counter_order(order):
        return False

    if (
        order.get_value_from_metadata(CUSTOMER_GUICHET_EXPIRED_SMS_META_KEY) or ""
    ).strip() == "true":
        return False

    phone = customer_phone_from_order(order)
    if not phone:
        logger.info(
            "Skip guichet-expired SMS — no phone on order %s", order.pk
        )
        return False

    amount = order.total_gross_amount
    currency = (order.currency or "XOF").upper()
    money = (
        f"{int(amount):,} {('FCFA' if currency == 'XOF' else currency)}".replace(",", " ")
        if amount is not None
        else ""
    )
    body = (
        f"ChapChap: désolé, la gare n'a pas validé votre réservation #{order.number} à temps. "
        + (
            f"Votre paiement de {money} a été automatiquement remboursé sur Mobile Money."
            if money
            else "Votre paiement a été automatiquement remboursé sur Mobile Money."
        )
    )
    result = send_sms_message(phone, body)
    if "error" in result:
        logger.warning(
            "Guichet-expired SMS failed for order=%s: %s",
            order.pk,
            result.get("error"),
        )
        return False

    order.store_value_in_metadata({CUSTOMER_GUICHET_EXPIRED_SMS_META_KEY: "true"})
    order.save(update_fields=["metadata", "updated_at"])
    return True
