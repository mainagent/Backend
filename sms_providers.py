# sms_providers.py
from __future__ import annotations
import os, sys
from typing import Protocol, runtime_checkable
from brand_config import env_for

try:
    # Only needed if SMS_PROVIDER=twilio
    from twilio.rest import Client  # pip install twilio
except Exception:
    Client = None  # allow mock mode without twilio installed


@runtime_checkable
class SMSClient(Protocol):
    def send(self, to_e164: str, body: str) -> None: ...


class MockSMS:
    def send(self, to_e164: str, body: str) -> None:
        print(f"[SMS:MOCK] to={to_e164} body={body!r}")


class TwilioSMS:
    def __init__(self, from_e164: str):
        if Client is None:
            raise RuntimeError("[SMS] Twilio client not installed")
        # Support both env naming styles
        sid   = os.getenv("TWILIO_SID") or os.getenv("TWILIO_ACCOUNT_SID")
        token = os.getenv("TWILIO_TOKEN") or os.getenv("TWILIO_AUTH_TOKEN")
        if not sid or not token:
            raise RuntimeError("[SMS] Missing Twilio SID/TOKEN")
        if not from_e164:
            raise RuntimeError("[SMS] Missing Twilio FROM number")
        self._client = Client(sid, token)
        self._from   = from_e164

    def send(self, to_e164: str, body: str) -> None:
        self._client.messages.create(to=to_e164, from_=self._from, body=body)


# Cached clients per (provider, brand) so we don’t rebuild every call
_clients: dict[tuple[str, str], SMSClient] = {}


def get_sms_client(brand: str) -> SMSClient:
    """
    Factory returning an SMS client for a given brand namespace.
    Env:
      SMS_PROVIDER = "twilio" | "mock"
      {BRAND}_TWILIO_FROM or TWILIO_PHONE_NUMBER  (e.g. HAIR_TWILIO_FROM)
      TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN      (or TWILIO_SID / TWILIO_TOKEN)
    """
    provider = (os.getenv("SMS_PROVIDER") or "mock").lower().strip()
    key = (provider, brand)
    if key in _clients:
        return _clients[key]

    if provider == "twilio":
        from_e164 = env_for(brand, "TWILIO_FROM") or os.getenv("TWILIO_PHONE_NUMBER")
        if not from_e164:
            raise RuntimeError(f"[SMS] No FROM number configured for brand '{brand}'")
        client: SMSClient = TwilioSMS(from_e164)
    else:
        client = MockSMS()

    _clients[key] = client
    return client