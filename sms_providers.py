# sms_providers.py
from __future__ import annotations
import os, sys
from typing import Protocol
from brand_config import env_for
from sms_providers import get_sms_client

SMS = get_sms_client()

class SMSClient(Protocol):
    def send(self, to_e164: str, body: str) -> None: ...

class MockSMS(SMSClient):
    def send(self, to_e164: str, body: str) -> None:
        print(f"[SMS:MOCK] to={to_e164} body={body!r}")

class TwilioSMS(SMSClient):
    def __init__(self, from_e164: str):
        try:
            from twilio.rest import Client  # pip install twilio
        except Exception as e:
            print("[SMS] Twilio not installed:", e, file=sys.stderr)
            raise
        sid   = os.getenv("TWILIO_SID") or os.getenv("TWILIO_ACCOUNT_SID")
        token = os.getenv("TWILIO_TOKEN") or os.getenv("TWILIO_AUTH_TOKEN")
        if not sid or not token:
            raise RuntimeError("[SMS] Missing Twilio SID/TOKEN")
        self._client = Client(sid, token)
        self._from   = from_e164

    def send(self, to_e164: str, body: str) -> None:
        self._client.messages.create(to=to_e164, from_=self._from, body=body)

_clients: dict[tuple[str,str], SMSClient] = {}

def get_sms_client(brand: str) -> SMSClient:
    provider = (os.getenv("SMS_PROVIDER") or "mock").lower().strip()
    key = (provider, brand)
    if key in _clients:
        return _clients[key]
    if provider == "twilio":
        from_e164 = env_for(brand, "TWILIO_FROM") or os.getenv("TWILIO_PHONE_NUMBER")
        if not from_e164:
            raise RuntimeError(f"[SMS] No FROM number for brand '{brand}'")
        client = TwilioSMS(from_e164)
    else:
        client = MockSMS()
    _clients[key] = client
    return client