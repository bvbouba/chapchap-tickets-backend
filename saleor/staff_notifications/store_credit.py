"""Add prepaid store credit to a station manager / operator admin.

Saleor has no native `storeCreditAdd`. Company deposits are stored as a tagged
gift card on the manager (`used_by`) so the balance is spendable like an avoir,
and proof is kept on the gift card plus the user's private metadata history.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from decimal import Decimal

import graphene
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from prices import Money as PricesMoney

from ..account.error_codes import AccountErrorCode
from ..channel.models import Channel
from ..core.db.connection import allow_writer
from ..core.utils.promo_code import generate_promo_code
from ..giftcard import events as gift_card_events
from ..giftcard.models import GiftCard, GiftCardTag
from ..giftcard.search import update_gift_cards_search_vector
from ..graphql.account.types import User as UserType
from ..graphql.channel.types import Channel as ChannelType
from ..graphql.core.mutations import BaseMutation
from ..graphql.core.scalars import Decimal as DecimalScalar
from ..graphql.core.types.common import AccountError
from ..graphql.core.types.money import Money
from ..permission.enums import AccountPermissions, ChannelPermissions
from .advance_ledger import (
    STORE_CREDIT_TAG,
    TYPE_CREDIT,
    append_ledger_entry,
    infer_manager_channel,
)

logger = logging.getLogger(__name__)


class StoreCredit(graphene.ObjectType):
    id = graphene.ID(required=True, description="Gift card id backing this credit.")
    balance = graphene.Field(Money, required=True, description="Current store credit.")


class StoreCreditAddInput(graphene.InputObjectType):
    user = graphene.ID(
        required=True, description="Station manager / operator admin user id."
    )
    channel = graphene.ID(
        required=False,
        description="Operator channel whose advance ledger should be credited.",
    )
    amount = DecimalScalar(required=True, description="Deposit amount to add.")
    currency = graphene.String(required=True, description="ISO currency code (e.g. XOF).")
    payment_method = graphene.String(
        required=False,
        description="WAVE or CASH.",
    )
    wave_transaction_id = graphene.String(
        required=False,
        description="Wave / Mobile Money transaction id.",
    )
    receipt_url = graphene.String(
        required=False,
        description="Hosted cash receipt image URL.",
    )
    note = graphene.String(required=False, description="Optional staff note.")


def _normalize_method(raw: str | None) -> str:
    value = (raw or "").strip().upper()
    if value in {"WAVE", "MOMO", "MOBILE_MONEY", "MOBILE MONEY", "WAVE / MOBILE MONEY"}:
        return "WAVE"
    if value in {"CASH", "ESPECES", "ESPÈCES", "ESPÈCES (CASH)", "ESPECES (CASH)"}:
        return "CASH"
    return value


class StoreCreditAdd(BaseMutation):
    store_credit = graphene.Field(StoreCredit, description="Updated store credit.")

    class Arguments:
        input = StoreCreditAddInput(
            required=True,
            description="Deposit to record on the manager's store credit.",
        )

    class Meta:
        description = (
            "Record a company deposit and add it to the station manager's "
            "Saleor store credit (gift card tagged store-credit)."
        )
        permissions = (
            AccountPermissions.MANAGE_STAFF,
            ChannelPermissions.MANAGE_CHANNELS,
        )
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
            logger.exception("storeCreditAdd failed")
            raise ValidationError(
                {
                    "input": ValidationError(
                        "Échec de l'enregistrement du dépôt. Réessayez.",
                        code=AccountErrorCode.GRAPHQL_ERROR.value,
                    )
                }
            )

    @classmethod
    def _input_value(cls, payload, key):
        if isinstance(payload, dict):
            return payload.get(key)
        return getattr(payload, key, None)

    @classmethod
    def _perform(cls, info, **data):
        payload = data.get("input") or {}
        user = info.context.user

        target = cls.get_node_or_error(
            info,
            cls._input_value(payload, "user"),
            only_type=UserType,
            field="user",
        )

        amount = cls._input_value(payload, "amount")
        if amount is None:
            raise ValidationError(
                {
                    "amount": ValidationError(
                        "Le montant est requis.",
                        code=AccountErrorCode.REQUIRED.value,
                    )
                }
            )
        amount = Decimal(str(amount))
        if amount <= 0:
            raise ValidationError(
                {
                    "amount": ValidationError(
                        "Le montant du dépôt doit être supérieur à 0.",
                        code=AccountErrorCode.INVALID.value,
                    )
                }
            )

        currency = (
            cls._input_value(payload, "currency") or "XOF"
        ).strip().upper() or "XOF"
        method = _normalize_method(cls._input_value(payload, "payment_method"))
        wave_tx = (cls._input_value(payload, "wave_transaction_id") or "").strip()
        receipt_url = (cls._input_value(payload, "receipt_url") or "").strip()
        note = (cls._input_value(payload, "note") or "").strip()

        if method == "WAVE" and not wave_tx:
            raise ValidationError(
                {
                    "waveTransactionId": ValidationError(
                        "L'ID de transaction Wave est requis.",
                        code=AccountErrorCode.REQUIRED.value,
                    )
                }
            )
        if method == "CASH" and not receipt_url:
            raise ValidationError(
                {
                    "receiptUrl": ValidationError(
                        "Le reçu / preuve de paiement est requis.",
                        code=AccountErrorCode.REQUIRED.value,
                    )
                }
            )

        channel = None
        channel_gid = cls._input_value(payload, "channel")
        if channel_gid:
            channel = cls.get_node_or_error(
                info,
                channel_gid,
                only_type=ChannelType,
                field="channel",
            )
        if channel is None:
            channel = infer_manager_channel(target)

        with transaction.atomic():
            if channel is not None:
                channel = (
                    Channel.objects.select_for_update()
                    .filter(pk=channel.pk)
                    .first()
                )
            existing_ids = list(
                GiftCard.objects.filter(
                    used_by=target,
                    is_active=True,
                    tags__name=STORE_CREDIT_TAG,
                    currency=currency,
                ).values_list("pk", flat=True)
            )
            gift_card = (
                GiftCard.objects.select_for_update()
                .filter(pk__in=existing_ids)
                .order_by("pk")
                .first()
                if existing_ids
                else None
            )

            if gift_card is None:
                gift_card = GiftCard.objects.create(
                    code=generate_promo_code(),
                    is_active=True,
                    created_by=user if getattr(user, "is_authenticated", False) else None,
                    created_by_email=getattr(user, "email", None) if user else None,
                    used_by=target,
                    used_by_email=(target.email or None),
                    expiry_date=None,
                    currency=currency,
                    initial_balance_amount=amount,
                    current_balance_amount=amount,
                )
                tag, _ = GiftCardTag.objects.get_or_create(name=STORE_CREDIT_TAG)
                gift_card.tags.add(tag)
                gift_card_events.gift_card_issued_event(
                    gift_card=gift_card, user=user, app=None
                )
            else:
                old = deepcopy(gift_card)
                gift_card.current_balance_amount = (
                    Decimal(gift_card.current_balance_amount) + amount
                )
                if gift_card.current_balance_amount > gift_card.initial_balance_amount:
                    gift_card.initial_balance_amount = gift_card.current_balance_amount
                gift_card.save(
                    update_fields=[
                        "current_balance_amount",
                        "initial_balance_amount",
                    ]
                )
                gift_card_events.gift_card_balance_reset_event(
                    gift_card, old, user, app=None
                )

            proof_note = note or (
                f"Dépôt {method or 'N/A'} {int(amount)} {currency}"
                + (f" · Wave {wave_tx}" if wave_tx else "")
            )
            gift_card.store_value_in_metadata(
                {
                    "type": STORE_CREDIT_TAG,
                    "last_deposit_amount": str(amount),
                    "last_payment_method": method,
                    "last_wave_transaction_id": wave_tx,
                    "last_receipt_url": receipt_url,
                    "last_note": proof_note[:200],
                    "last_recorded_at": timezone.now().isoformat(),
                }
            )
            gift_card.save(update_fields=["metadata"])
            update_gift_cards_search_vector([gift_card])

            if proof_note:
                gift_card_events.gift_card_note_added_event(
                    gift_card=gift_card, user=user, app=None, message=proof_note[:250]
                )

            staff_label = ""
            if user and getattr(user, "is_authenticated", False):
                staff_label = (
                    f"{user.first_name} {user.last_name}".strip() or user.email or ""
                )
            if channel is not None:
                append_ledger_entry(
                    channel=channel,
                    manager=target,
                    entry_type=TYPE_CREDIT,
                    amount=amount,
                    currency=currency,
                    method=method,
                    wave_transaction_id=wave_tx,
                    receipt_url=receipt_url,
                    note=note or proof_note,
                    recorded_by=staff_label,
                    gift_card_id=graphene.Node.to_global_id(
                        "GiftCard", gift_card.id
                    ),
                )
            else:
                private = dict(target.private_metadata or {})
                private["store_credit_balance"] = str(
                    int(gift_card.current_balance_amount)
                )
                target.private_metadata = private
                target.save(update_fields=["private_metadata", "updated_at"])

        return StoreCreditAdd(
            store_credit=StoreCredit(
                id=graphene.Node.to_global_id("GiftCard", gift_card.id),
                balance=PricesMoney(
                    amount=gift_card.current_balance_amount,
                    currency=gift_card.currency,
                ),
            )
        )
