import graphene
import secrets
import string
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone
from django.conf import settings

from ...otp.models import PasswordResetToken
from ...otp.utils import send_sms_message
from ...account.utils import retrieve_user_by_email
from ...account.error_codes import AccountErrorCode
from ...core.db.connection import allow_writer
from ...core.exceptions import PermissionDenied
from ...core.tokens import token_generator
from ...graphql.core.mutations import BaseMutation
from ...graphql.core.types.common import AccountError
from ...order.models import Order
from ...order.utils import match_orders_with_new_user
from ...permission.auth_filters import AuthorizationFilters
from ...graphql.account.types import User as UserType
from ...permission.enums import AccountPermissions
from .resolvers import resolve_send_otp, resolve_verify_otp

MUST_CHANGE_PASSWORD_KEY = "must_change_password"


def _identity_variants_from_email(email: str) -> tuple[set[str], set[str]]:
    """Build email/phone variants for virtual ChapChap accounts."""
    email = (email or "").strip().lower()
    email_variants: set[str] = {email} if email else set()
    phone_variants: set[str] = set()
    if not email.endswith("@chapchapticket.com"):
        return email_variants, phone_variants

    local = email[: -len("@chapchapticket.com")]
    if not local:
        return email_variants, phone_variants

    digits = local.lstrip("+")
    for candidate in {local, digits, f"+{digits}"}:
        if not candidate:
            continue
        phone_variants.add(candidate)
        email_variants.add(f"{candidate}@chapchapticket.com")
    return email_variants, phone_variants


def _phone_from_staff_email(email: str) -> str:
    email = (email or "").strip()
    if not email.endswith("@chapchapticket.com"):
        return ""
    local = email[: -len("@chapchapticket.com")].strip()
    if not local:
        return ""
    if local.startswith("+"):
        return local
    digits = "".join(c for c in local if c.isdigit())
    return f"+{digits}" if digits else local


def _generate_temporary_password(length: int = 8) -> str:
    alphabet = string.ascii_uppercase + string.digits
    # Avoid ambiguous characters.
    alphabet = alphabet.replace("O", "").replace("0", "").replace("I", "").replace("1", "")
    return "".join(secrets.choice(alphabet) for _ in range(length))


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
        # 1. Check if user exists first (Saves SMS cost)
        # Using your virtual email logic:
        email = f"{phone_number}@chapchapticket.com"
        user = retrieve_user_by_email(email)

        # If user doesn't exist or isn't active, we exit early
        # Note: We don't raise an error here to prevent "User Enumeration"
        # (Hackers checking which numbers are registered)
        if not user or not user.is_active:
            return RequestPasswordResetOTP()

        # 2. Check Rate Limiting (Prevent spamming the same user)
        if user.last_password_reset_request:
            delta = timezone.now() - user.last_password_reset_request
            if delta.total_seconds() < settings.RESET_PASSWORD_LOCK_TIME:
                return RequestPasswordResetOTP()

        # 3. Generate Saleor Token
        token = token_generator.make_token(user)

        # 4. Save to our custom model
        PasswordResetToken.objects.update_or_create(
            phone_number=phone_number,
            defaults={
                "token": token,
                "is_used": False,
                "created_at": timezone.now(),
            },
        )

        # 5. ONLY NOW trigger Twilio (The cost-incurring step)
        # We only reach this if the user is valid and not rate-limited
        resolve_send_otp(phone_number)

        # 6. Update user metadata
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
        # 1. Check if a valid, unused reset request exists BEFORE calling Twilio
        try:
            reset_obj = PasswordResetToken.objects.filter(
                phone_number=phone_number,
                is_used=False,
            ).latest("created_at")
        except PasswordResetToken.DoesNotExist:
            # If empty, the user either never started the process or already used the code
            raise ValidationError(
                {
                    "phone_number": ValidationError(
                        "Aucune demande de réinitialisation active trouvée. Veuillez recommencer le processus.",
                        code=AccountErrorCode.NOT_FOUND.value,
                    )
                }
            )

        # 2. Verify code with Twilio
        # We only get here if reset_obj exists
        result = resolve_verify_otp(phone_number, otp)

        if "error" in result or result.get("status") == "unapproved":
            raise ValidationError(
                {
                    "otp": ValidationError(
                        "Code invalide ou expiré.",
                        code=AccountErrorCode.INVALID.value,
                    )
                }
            )

        # 3. Mark the token as used and return it
        # This prevents the user from using the same OTP verification twice
        reset_obj.is_used = True
        reset_obj.save(update_fields=["is_used"])

        return VerifyPasswordResetOTP(token=reset_obj.token)


class SyncMyOrders(BaseMutation):
    """Claim counter/guest orders that match the logged-in user's phone/email."""

    linked_count = graphene.Int(
        description="Number of previously unassigned orders linked in this call."
    )
    order_ids = graphene.List(
        graphene.NonNull(graphene.ID),
        description="Global IDs of the authenticated user's non-draft orders after sync.",
    )

    class Meta:
        description = (
            "Link orders that share the authenticated user's phone number or "
            "virtual email (e.g. counter sales) to their customer account."
        )
        error_type_class = AccountError
        error_type_field = "account_errors"
        permissions = (AuthorizationFilters.AUTHENTICATED_USER,)

    @classmethod
    @allow_writer()
    def perform_mutation(cls, _root, info):
        user = getattr(info.context, "user", None)
        if not user or getattr(user, "is_anonymous", True):
            raise PermissionDenied(
                permissions=[AuthorizationFilters.AUTHENTICATED_USER]
            )

        # Saleor built-in: exact user_email match with user=None
        match_orders_with_new_user(user)

        email_variants, phone_variants = _identity_variants_from_email(user.email)
        q = Q()
        for email in email_variants:
            q |= Q(user_email__iexact=email)
        if phone_variants:
            q |= Q(billing_address__phone__in=list(phone_variants))
            q |= Q(shipping_address__phone__in=list(phone_variants))

        linked_ids: list[int] = []
        if q != Q():
            to_link = (
                Order.objects.non_draft()
                .filter(user__isnull=True)
                .filter(q)
                .distinct()
            )
            linked_ids = list(to_link.values_list("id", flat=True))
            if linked_ids:
                Order.objects.filter(id__in=linked_ids).update(user=user)

        user_order_ids = list(
            Order.objects.non_draft()
            .filter(user=user)
            .order_by("-created_at")
            .values_list("id", flat=True)[:100]
        )
        global_ids = [
            graphene.Node.to_global_id("Order", pk) for pk in user_order_ids
        ]
        return SyncMyOrders(linked_count=len(linked_ids), order_ids=global_ids)


class StaffIssueTemporaryPassword(BaseMutation):
    """Set a temporary password on a staff user and SMS it to their phone."""

    message = graphene.String()

    class Arguments:
        id = graphene.ID(
            required=True,
            description="ID of the staff user to issue a temporary password for.",
        )

    class Meta:
        description = (
            "Generate a temporary password for a staff member, store a "
            "must-change-password flag, and send the password by SMS."
        )
        permissions = (AccountPermissions.MANAGE_STAFF,)
        error_type_class = AccountError
        error_type_field = "account_errors"

    @classmethod
    @allow_writer()
    def perform_mutation(cls, _root, info, /, **data):
        user = cls.get_node_or_error(info, data["id"], only_type=UserType)
        if not user.is_staff:
            raise ValidationError(
                {
                    "id": ValidationError(
                        "User is not a staff member.",
                        code=AccountErrorCode.INVALID.value,
                    )
                }
            )

        phone = _phone_from_staff_email(user.email)
        if not phone:
            raise ValidationError(
                {
                    "id": ValidationError(
                        "Staff email must be a ChapChap phone virtual email "
                        "(e.g. +225…@chapchapticket.com).",
                        code=AccountErrorCode.INVALID.value,
                    )
                }
            )

        temp_password = _generate_temporary_password()
        user.set_password(temp_password)
        user.store_value_in_metadata({MUST_CHANGE_PASSWORD_KEY: "true"})
        user.save(update_fields=["password", "metadata", "updated_at"])

        first = (user.first_name or "").strip()
        greeting = f"Bonjour {first}, " if first else ""
        body = (
            f"{greeting}ChapChap Staff — votre mot de passe temporaire est : "
            f"{temp_password}. Connectez-vous et changez-le immédiatement."
        )
        sms_result = send_sms_message(phone, body)
        if "error" in sms_result:
            raise ValidationError(
                {
                    "id": ValidationError(
                        f"Password set but SMS failed: {sms_result['error']}",
                        code=AccountErrorCode.INVALID.value,
                    )
                }
            )

        return StaffIssueTemporaryPassword(
            message="Temporary password sent by SMS."
        )
