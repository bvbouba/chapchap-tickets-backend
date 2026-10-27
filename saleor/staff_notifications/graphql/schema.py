import graphene

from ...channel.models import Channel
from ...core.db.connection import allow_writer
from ...graphql.core.fields import PermissionsField
from ...permission.auth_filters import AuthorizationFilters
from .mutations import (
    StaffPresenceHeartbeat,
    StaffPushTokenRegister,
    StaffPushTokenUnregister,
)
from ..advance_ledger import ensure_channel_ledger, find_channel_manager, read_channel_advance
from ..avoir import StaffIssueOrderAvoir
from ..store_credit import StoreCreditAdd


class ChannelAdvanceLedger(graphene.ObjectType):
    balance = graphene.Float(required=True, description="Remaining advance balance.")
    currency = graphene.String(required=True)
    ledger_json = graphene.String(
        required=True,
        description="JSON array of ledger entries (credits and debits).",
    )


class StaffNotificationQueries(graphene.ObjectType):
    channel_advance_ledger = PermissionsField(
        ChannelAdvanceLedger,
        id=graphene.Argument(
            graphene.ID, required=True, description="Operator channel id."
        ),
        description="ChapChap advance deposit ledger for a bus company channel.",
        permissions=[AuthorizationFilters.AUTHENTICATED_STAFF_USER],
    )

    @staticmethod
    def resolve_channel_advance_ledger(_root, info, *, id):
        try:
            _type, pk = graphene.Node.from_global_id(id)
        except (ValueError, TypeError):
            return None
        channel = Channel.objects.filter(pk=pk).first()
        if channel is None:
            return None
        with allow_writer():
            manager = find_channel_manager(channel)
            ensure_channel_ledger(channel, manager)
            channel.refresh_from_db()
        data = read_channel_advance(channel, manager)
        return ChannelAdvanceLedger(
            balance=data["balance"],
            currency=data["currency"],
            ledger_json=data["ledger_json"],
        )


class StaffNotificationMutations(graphene.ObjectType):
    staff_push_token_register = StaffPushTokenRegister.Field()
    staff_push_token_unregister = StaffPushTokenUnregister.Field()
    staff_presence_heartbeat = StaffPresenceHeartbeat.Field()
    staff_issue_order_avoir = StaffIssueOrderAvoir.Field()
    store_credit_add = StoreCreditAdd.Field()
