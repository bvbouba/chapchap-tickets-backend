import graphene
from ...otp.models import PasswordResetToken
from ...account.utils import retrieve_user_by_email
from ...graphql.core.mutations import BaseMutation
from ...graphql.core.types.common import AccountError
from .resolvers import resolve_send_otp, resolve_verify_otp
from django.utils import timezone
from django.conf import settings
from ...core.tokens import token_generator
from ...account.error_codes import AccountErrorCode
from django.core.exceptions import ValidationError


class SendOTPMutation(graphene.Mutation):
    class Arguments:
        phone_number = graphene.String(required=True)
    
    message = graphene.String()
    error = graphene.String()

    def mutate(root, info, phone_number):
        result = resolve_send_otp(phone_number)
        if "error" in result:
            return SendOTPMutation(error=result["error"])
        return SendOTPMutation(message=result["message"])

class VerifyOTPMutation(graphene.Mutation):
    class Arguments:
        phone_number = graphene.String(required=True)
        otp = graphene.String(required=True)

    status = graphene.String()
    message = graphene.String()
    error = graphene.String()

    def mutate(root, info, phone_number, otp):
        result = resolve_verify_otp(phone_number, otp)
        if "error" in result:
            return VerifyOTPMutation(error=result["error"])
        return VerifyOTPMutation(status=result.get("status"), message=result.get("message"))


class RequestPasswordResetOTP(BaseMutation):
    class Arguments:
        phone_number = graphene.String(
            required=True,
            description="The phone number of the user (e.g., +22501020304).",
        )

    class Meta:
        description = "Triggers an SMS OTP and prepares a password reset token."
        error_type_class = AccountError
        error_type_field = "account_errors"

    @classmethod
    def perform_mutation(cls, _root, info, phone_number):
        email = f"{phone_number}@chapchaptickets.com"
        user = retrieve_user_by_email(email)
        
        # We always return "Success" to prevent user enumeration (security)
        if user and user.is_active:
            # Check rate limiting
            if user.last_password_reset_request:
                delta = timezone.now() - user.last_password_reset_request
                if delta.total_seconds() < settings.RESET_PASSWORD_LOCK_TIME:
                    return RequestPasswordResetOTP()

            # 1. Generate Saleor Token
            token = token_generator.make_token(user)

            # 2. Save to our custom PasswordResetToken model
            PasswordResetToken.objects.update_or_create(
                phone_number=phone_number,
                defaults={"token": token, "is_used": False, "created_at": timezone.now()}
            )

            # 3. Trigger Twilio SMS
            resolve_send_otp(phone_number)
            
            # 4. Update user request time
            user.last_password_reset_request = timezone.now()
            user.save(update_fields=["last_password_reset_request"])

        return RequestPasswordResetOTP()

class VerifyPasswordResetOTP(BaseMutation):
    class Arguments:
        phone_number = graphene.String(required=True)
        otp = graphene.String(required=True)

    token = graphene.String(description="The Saleor password reset token.")

    class Meta:
        description = "Verifies Twilio OTP and returns the Saleor reset token."
        error_type_class = AccountError
        error_type_field = "account_errors"

    @classmethod
    def perform_mutation(cls, _root, info, phone_number, otp):
        # 1. Verify code with Twilio
        result = resolve_verify_otp(phone_number, otp)
        
        if "error" in result or result.get("status") == "unapproved":
            raise ValidationError(
                {"otp": ValidationError("Code invalide ou expiré.", code=AccountErrorCode.INVALID.value)}
            )

        # 2. Retrieve the Saleor Token from our model
        try:
            reset_obj = PasswordResetToken.objects.filter(
                phone_number=phone_number, 
                is_used=False
            ).latest('created_at')
            
            # Note: You can add logic here to check if created_at is > 1 hour ago
            
            return VerifyPasswordResetOTP(token=reset_obj.token)
        except PasswordResetToken.DoesNotExist:
            raise ValidationError(
                {"phone_number": ValidationError("Aucune demande de réinitialisation trouvée.", code=AccountErrorCode.NOT_FOUND.value)}
            )

