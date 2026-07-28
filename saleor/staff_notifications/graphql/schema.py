import graphene

from .mutations import (
    StaffPresenceHeartbeat,
    StaffPushTokenRegister,
    StaffPushTokenUnregister,
)


class StaffNotificationMutations(graphene.ObjectType):
    staff_push_token_register = StaffPushTokenRegister.Field()
    staff_push_token_unregister = StaffPushTokenUnregister.Field()
    staff_presence_heartbeat = StaffPresenceHeartbeat.Field()
