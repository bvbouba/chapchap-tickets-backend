"""Issue ChapChap avoir (store credit gift card) when cancelling a ticket.

Mirrors Saleor dashboard `giftCardCreate` (same GiftCard model, tags, events),
but is callable with MANAGE_ORDERS so counters can emit the full code for SMS/print
without needing MANAGE_GIFT_CARD.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal

import graphene
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from ..account.error_codes import AccountErrorCode
from ..core.db.connection import allow_writer
from ..core.utils.promo_code import generate_promo_code
from ..giftcard import events as gift_card_events
from ..giftcard.models import GiftCard, GiftCardTag
from ..giftcard.search import update_gift_cards_search_vector
from ..graphql.core.mutations import BaseMutation
from ..graphql.core.types.common import AccountError
from ..graphql.order.types import Order as OrderType
from ..order import FulfillmentStatus
from ..order.actions import cancel_fulfillment
from ..order.models import Order
from ..otp.utils import send_sms_message
from ..permission.enums import OrderPermissions
from ..plugins.manager import get_plugins_manager
from ..warehouse.models import Warehouse
from .presence import phone_from_chapchap_email

logger = logging.getLogger(__name__)

TICKET_STATUS_KEY = "ticket_status"
TICKET_STATUS_CANCELLED_AND_REFUNDED = "CANCELLED_AND_REFUNDED"
REFUND_AMOUNT_KEY = "refund_amount"
REFUND_METHOD_KEY = "refund_method"
REFUNDED_AT_KEY = "refunded_at"
REFUNDED_BY_USER_KEY = "refunded_by_user"
REFUND_REASON_KEY = "refund_reason"
AVOIR_GIFT_CARD_ID_KEY = "avoir_gift_card_id"
AVOIR_CODE_KEY = "avoir_code"
AVOIR_EXPIRES_AT_KEY = "avoir_expires_at"
AVOIR_FEE_KEY = "avoir_cancellation_fee"


def _customer_phone_from_order(order: Order) -> str:
    billing = order.billing_address
    if billing and billing.phone:
        phone = str(billing.phone).strip()
        if phone:
            if phone.startswith("+"):
                return phone
            digits = "".join(c for c in phone if c.isdigit())
            return f"+{digits}" if digits else phone
    return phone_from_chapchap_email(order.user_email)


def _resolve_warehouse_id(order: Order, fulfillment) -> int | None:
    # Fulfillment has no warehouse_id FK — warehouse lives on fulfillment line stocks
    # (same as GraphQL Fulfillment.warehouse resolver).
    for fl in fulfillment.lines.select_related("stock").all():
        if fl.stock_id and fl.stock and fl.stock.warehouse_id:
            return fl.stock.warehouse_id

    for line in order.lines.all():
        if line.variant_id:
            stock = line.variant.stocks.first()
            if stock and stock.warehouse_id:
                return stock.warehouse_id
        if hasattr(line, "allocations"):
            alloc = line.allocations.select_related("stock").first()
            if alloc and getattr(alloc, "stock", None) and alloc.stock.warehouse_id:
                return alloc.stock.warehouse_id
    return None


def _order_ticket_gross(order: Order) -> Decimal:
    charged = getattr(order, "total_charged_amount", None)
    if charged is not None and Decimal(charged) > 0:
        return Decimal(charged)
    return Decimal(order.total_gross_amount or 0)


def _create_avoir_gift_card(
    *,
    credit: Decimal,
    currency: str,
    expiry: date,
    user,
    customer,
    customer_email: str,
    order: Order,
    fee: Decimal,
    note: str,
) -> GiftCard:
    """Same shape as dashboard giftCardCreate (code, balances, tag, events)."""
    code = generate_promo_code()
    gift_card = GiftCard.objects.create(
        code=code,
        is_active=True,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        created_by_email=getattr(user, "email", None) if user else None,
        # me.giftCards resolves via used_by — assign passenger so it shows in app.
        used_by=customer,
        used_by_email=customer_email or None,
        expiry_date=expiry,
        currency=currency,
        initial_balance_amount=credit,
        current_balance_amount=credit,
    )
    tag, _ = GiftCardTag.objects.get_or_create(name="avoir")
    gift_card.tags.add(tag)
    gift_card.store_value_in_metadata(
        {
            "origin_order_id": graphene.Node.to_global_id("Order", order.id),
            "origin_order_number": str(order.number),
            "type": "avoir",
            "cancellation_fee": str(int(fee)),
            "note": note[:200] if note else "",
        }
    )
    gift_card.save(update_fields=["metadata"])
    update_gift_cards_search_vector([gift_card])

    gift_card_events.gift_card_issued_event(
        gift_card=gift_card, user=user, app=None
    )
    if note:
        gift_card_events.gift_card_note_added_event(
            gift_card=gift_card, user=user, app=None, message=note
        )
    return gift_card


class StaffIssueOrderAvoir(BaseMutation):
    gift_card_id = graphene.ID(description="Created gift card global id.")
    gift_card_code = graphene.String(description="Full gift card code (for SMS / print).")
    gift_card_display_code = graphene.String(description="Last 4 chars of the code.")
    credit_amount = graphene.Int(description="Avoir amount credited (whole XOF).")
    currency = graphene.String()
    expiry_date = graphene.String(description="ISO date YYYY-MM-DD.")
    sms_sent = graphene.Boolean()
    order_id = graphene.ID()

    class Arguments:
        order_id = graphene.ID(required=True, description="Order to cancel and credit.")
        cancellation_fee = graphene.Int(
            required=False,
            description="Fee deducted from ticket price (whole XOF).",
        )
        send_sms = graphene.Boolean(
            required=False,
            description="Send the avoir code by SMS to the passenger phone.",
        )
        note = graphene.String(required=False, description="Optional staff note.")

    class Meta:
        description = (
            "Cancel an active ticket reservation, restock the seat, and issue a "
            "ChapChap avoir (Saleor gift card) for the net amount — same card type "
            "as dashboard giftCardCreate."
        )
        permissions = (OrderPermissions.MANAGE_ORDERS,)
        error_type_class = AccountError
        error_type_field = "account_errors"

    @classmethod
    @allow_writer()
    def perform_mutation(cls, _root, info, /, **data):
        try:
            return cls._perform(info, **data)
        except ValidationError:
            raise
        except Exception:
            logger.exception("staffIssueOrderAvoir failed")
            raise ValidationError(
                {
                    "orderId": ValidationError(
                        "Échec de l'émission de l'avoir. Réessayez ou contactez le support.",
                        code=AccountErrorCode.GRAPHQL_ERROR.value,
                    )
                }
            )

    @classmethod
    def _perform(cls, info, **data):
        user = info.context.user
        order = cls.get_node_or_error(
            info, data["order_id"], only_type=OrderType, field="order_id"
        )
        assert isinstance(order, Order)

        fee = Decimal(int(data.get("cancellation_fee") or 0))
        if fee < 0:
            raise ValidationError(
                {
                    "cancellationFee": ValidationError(
                        "Les frais d'annulation ne peuvent pas être négatifs.",
                        code=AccountErrorCode.INVALID.value,
                    )
                }
            )

        meta = order.metadata or {}
        if meta.get(TICKET_STATUS_KEY) == TICKET_STATUS_CANCELLED_AND_REFUNDED:
            raise ValidationError(
                {
                    "orderId": ValidationError(
                        "Cette commande est déjà remboursée ou créditée (avoir).",
                        code=AccountErrorCode.INVALID.value,
                    )
                }
            )
        if meta.get(TICKET_STATUS_KEY) == "CANCELLED_AND_TRANSFERRED":
            raise ValidationError(
                {
                    "orderId": ValidationError(
                        "Ce voyage a déjà été transféré.",
                        code=AccountErrorCode.INVALID.value,
                    )
                }
            )

        ticket_gross = _order_ticket_gross(order)
        credit = ticket_gross - fee
        if credit <= 0:
            raise ValidationError(
                {
                    "cancellationFee": ValidationError(
                        "Le montant de l'avoir doit être supérieur à 0.",
                        code=AccountErrorCode.INVALID.value,
                    )
                }
            )

        currency = (order.currency or "XOF").upper()
        expiry = date.today() + timedelta(days=365)
        send_sms = bool(data["send_sms"]) if "send_sms" in data and data["send_sms"] is not None else True
        note = (data.get("note") or "").strip()

        active_fulfillments = list(
            order.fulfillments.exclude(status=FulfillmentStatus.CANCELED)
        )
        if not active_fulfillments:
            raise ValidationError(
                {
                    "orderId": ValidationError(
                        "Aucune réservation active à annuler sur cette commande.",
                        code=AccountErrorCode.INVALID.value,
                    )
                }
            )

        manager = get_plugins_manager(allow_replica=False)
        customer = order.user
        customer_email = (order.user_email or "").strip() or (
            customer.email if customer else ""
        )

        with transaction.atomic():
            for fulfillment in active_fulfillments:
                warehouse_id = _resolve_warehouse_id(order, fulfillment)
                warehouse = (
                    Warehouse.objects.filter(id=warehouse_id).first()
                    if warehouse_id
                    else None
                )
                if not warehouse:
                    raise ValidationError(
                        {
                            "orderId": ValidationError(
                                "Aucun entrepôt trouvé pour restocker la place.",
                                code=AccountErrorCode.INVALID.value,
                            )
                        }
                    )
                cancel_fulfillment(fulfillment, user, None, warehouse, manager)

            gift_card = _create_avoir_gift_card(
                credit=credit,
                currency=currency,
                expiry=expiry,
                user=user,
                customer=customer,
                customer_email=customer_email,
                order=order,
                fee=fee,
                note=note,
            )

            staff_label = ""
            if user and getattr(user, "is_authenticated", False):
                staff_label = (
                    f"{user.first_name} {user.last_name}".strip() or user.email or ""
                )

            order.store_value_in_metadata(
                {
                    TICKET_STATUS_KEY: TICKET_STATUS_CANCELLED_AND_REFUNDED,
                    REFUND_AMOUNT_KEY: str(int(credit)),
                    REFUND_METHOD_KEY: "AVOIR",
                    REFUNDED_AT_KEY: timezone.now().isoformat(),
                    REFUNDED_BY_USER_KEY: staff_label,
                    REFUND_REASON_KEY: note or "Bon d'achat (avoir) émis",
                    AVOIR_GIFT_CARD_ID_KEY: graphene.Node.to_global_id(
                        "GiftCard", gift_card.id
                    ),
                    AVOIR_CODE_KEY: gift_card.code,
                    AVOIR_EXPIRES_AT_KEY: expiry.isoformat(),
                    AVOIR_FEE_KEY: str(int(fee)),
                }
            )
            order.save(update_fields=["metadata", "updated_at"])

        sms_sent = False
        if send_sms:
            phone = _customer_phone_from_order(order)
            if phone:
                body = (
                    f"ChapChap: bon d'achat {gift_card.code} de {int(credit)} {currency} "
                    f"valable jusqu'au {expiry.strftime('%d/%m/%Y')} "
                    f"(annulation billet #{order.number}). "
                    f"Utilisez-le au prochain achat."
                )
                result = send_sms_message(phone, body)
                if "error" in result:
                    logger.warning(
                        "Avoir SMS failed order=%s: %s", order.pk, result.get("error")
                    )
                else:
                    sms_sent = True

        return StaffIssueOrderAvoir(
            gift_card_id=graphene.Node.to_global_id("GiftCard", gift_card.id),
            gift_card_code=gift_card.code,
            gift_card_display_code=gift_card.display_code,
            credit_amount=int(credit),
            currency=currency,
            expiry_date=expiry.isoformat(),
            sms_sent=sms_sent,
            order_id=graphene.Node.to_global_id("Order", order.id),
        )
