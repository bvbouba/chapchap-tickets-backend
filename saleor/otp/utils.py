import random
import os
from ..account.models import User
from twilio.rest import Client

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_MESSAGING_SERVICE_SID = os.getenv("TWILIO_MESSAGING_SERVICE_SID")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")


def generate_otp():
    return random.randint(100000, 999999)  # 6-digit OTP


def send_otp_twilio(phone_number):
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    try:
        verification = (
            client.verify.v2.services("VAc4edc2cb9b480dcfa7c3941f31ea7448")
            .verifications.create(to=phone_number, channel="sms")
        )
        return verification.sid
    except Exception as e:
        return {"error": str(e)}


def send_sms_message(phone_number: str, body: str) -> dict:
    """Send a free-form SMS via Twilio Messages (same Twilio account as OTP)."""
    if not phone_number or not body:
        return {"error": "Phone number and message body are required."}
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        return {"error": "Twilio credentials are not configured."}

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    try:
        kwargs: dict = {"to": phone_number, "body": body}
        if TWILIO_MESSAGING_SERVICE_SID:
            kwargs["messaging_service_sid"] = TWILIO_MESSAGING_SERVICE_SID
        elif TWILIO_FROM_NUMBER:
            kwargs["from_"] = TWILIO_FROM_NUMBER
        else:
            return {
                "error": "TWILIO_FROM_NUMBER or TWILIO_MESSAGING_SERVICE_SID required."
            }

        message = client.messages.create(**kwargs)
        if getattr(message, "sid", None):
            return {"message": "SMS sent successfully.", "sid": message.sid}
        return {"error": "Failed to send SMS."}
    except Exception as e:
        return {"error": str(e)}


def check_user_exists_in_saleor(phone_number):
    email = f"{phone_number}@autocoq.com"

    return User.objects.filter(email=email).exists()
