import graphene

from .mutations import StaffPushTokenRegister, StaffPushTokenUnregister


class StaffNotificationMutations(graphene.ObjectType):
    staff_push_token_register = StaffPushTokenRegister.Field()
    staff_push_token_unregister = StaffPushTokenUnregister.Field()
