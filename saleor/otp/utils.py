import random
from django.http import JsonResponse
import requests
import os
from ..account.models import User
from twilio.rest import Client

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID") 
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN") 
TWILIO_MESSAGING_SERVICE_SID = os.getenv("TWILIO_MESSAGING_SERVICE_SID") 


def generate_otp():
    return random.randint(100000, 999999)  # 6-digit OTP


def send_otp_twilio(phone_number):
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    try:
        verification = client.verify \
            .v2 \
            .services('VAc4edc2cb9b480dcfa7c3941f31ea7448') \
            .verifications \
            .create(to=phone_number, channel='sms')
        return verification.sid
    except Exception as e:
        return {"error": str(e)}



def check_user_exists_in_saleor(phone_number):
    email = f"{phone_number}@autocoq.com"  

    return User.objects.filter(email=email).exists()
