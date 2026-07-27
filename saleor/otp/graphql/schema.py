import graphene
from .mutations import (
    RequestPasswordResetOTP,
    SendOTPMutation,
    StaffIssueTemporaryPassword,
    SyncMyOrders,
    VerifyOTPMutation,
    VerifyPasswordResetOTP,
)
from .resolvers import resolve_check_phone_exists
from .types import PhoneExistenceType


class OTPMutations(graphene.ObjectType):
    send_otp = SendOTPMutation.Field()
    verify_otp = VerifyOTPMutation.Field()
    request_password_reset_otp = RequestPasswordResetOTP.Field()
    verify_password_reset_otp = VerifyPasswordResetOTP.Field()
    sync_my_orders = SyncMyOrders.Field()
    staff_issue_temporary_password = StaffIssueTemporaryPassword.Field()


class OTPQueries(graphene.ObjectType):
    check_phone_exists = graphene.Field(
        PhoneExistenceType,
        phone_number=graphene.String(required=True),
        description="Check if a phone number exists in the system.",
    )

    def resolve_check_phone_exists(self, info, phone_number):
        return resolve_check_phone_exists(phone_number)


schema = graphene.Schema(mutation=OTPMutations)
