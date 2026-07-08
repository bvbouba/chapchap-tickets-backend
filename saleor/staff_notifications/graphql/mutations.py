import graphene
from django.utils import timezone

from ...graphql.core.mutations import BaseMutation
from ...graphql.core.types.common import AccountError
from ...permission.enums import OrderPermissions
from ..models import StaffDeviceToken


class StaffPushTokenRegister(BaseMutation):
    token = graphene.String(description="Registered Expo push token.")
    registered = graphene.Boolean(description="Whether the token is active.")

    class Arguments:
        token = graphene.String(required=True, description="Expo push token.")
        platform = graphene.String(required=False, description="Device platform.")

    class Meta:
        description = "Registers an Expo push token for the authenticated staff user."
        permissions = (OrderPermissions.MANAGE_ORDERS,)
        error_type_class = AccountError
        error_type_field = "account_errors"

    @classmethod
    def perform_mutation(cls, _root, info, token, platform=None):
        user = info.context.user
        now = timezone.now()
        device_token, _ = StaffDeviceToken.objects.update_or_create(
            token=token,
            defaults={
                "user": user,
                "platform": platform or "",
                "is_active": True,
                "last_seen_at": now,
            },
        )
        return StaffPushTokenRegister(
            token=device_token.token,
            registered=device_token.is_active,
        )


class StaffPushTokenUnregister(BaseMutation):
    removed = graphene.Boolean(description="Whether the token was deactivated.")

    class Arguments:
        token = graphene.String(required=True, description="Expo push token.")

    class Meta:
        description = "Unregisters an Expo push token for the authenticated staff user."
        permissions = (OrderPermissions.MANAGE_ORDERS,)
        error_type_class = AccountError
        error_type_field = "account_errors"

    @classmethod
    def perform_mutation(cls, _root, info, token):
        updated = StaffDeviceToken.objects.filter(
            token=token,
            user=info.context.user,
        ).update(is_active=False, last_seen_at=timezone.now())
        return StaffPushTokenUnregister(removed=updated > 0)
