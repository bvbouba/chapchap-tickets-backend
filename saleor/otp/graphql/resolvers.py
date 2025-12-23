from django.utils.translation import gettext_lazy as _
import os
from twilio.rest import Client
from ..utils import  check_user_exists_in_saleor


TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID") 
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN") 
VERIFY_SERVICE_SID = os.getenv("VERIFY_SERVICE_SID") 

def resolve_send_otp(phone_number):
    if not phone_number:
        return {"error": "Phone number is required."}
    
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    verification = client.verify \
        .v2 \
        .services(VERIFY_SERVICE_SID) \
        .verifications \
        .create(to=phone_number, channel='sms', locale="fr")
    
    if hasattr(verification, 'sid'):  
        return {"message": "OTP sent successfully."}
    else:
        return {"error": "Failed to send code."}


def resolve_verify_otp(phone_number, code):
    if not phone_number or not code:
        return {"error": "Phone number and OTP are required."}

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    verification_check = client.verify \
        .v2 \
        .services(VERIFY_SERVICE_SID) \
        .verification_checks \
        .create(to=phone_number, code=code)

    if verification_check.status == "approved":
        user_exists = check_user_exists_in_saleor(phone_number)
        if user_exists:
            return {"status": "existing_user", "message": "User verified, proceed to login."}
        else:
            return {"status": "new_user", "message": "User verified, proceed to registration."}
    else:
        return {"error": "Invalid code."}

    


def resolve_check_phone_exists(phone_number):
    if not phone_number:
        return {"error": "Phone number is required."}
    
    user_exists = check_user_exists_in_saleor(phone_number)
    
    return {"exists": user_exists}

