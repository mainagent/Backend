import os
import requests

# Prefer env; fall back to your deployed URL (HTTPS!)
BACKEND_URL = os.getenv(
    "BACKEND_URL",
    "https://postnord-backend-production.up.railway.app"
).rstrip("/")

def _url(path: str) -> str:
    return f"{BACKEND_URL}{path}"

# ---------- Audio (returns bytes) ----------
def generate_audio(text: str) -> bytes:
    """Request MP3 TTS from backend. Returns raw bytes (mp3)."""
    r = requests.post(
        _url("/generate-audio"),
        json={"text": text},
        timeout=60,
    )
    r.raise_for_status()
    return r.content

# ---------- JSON tool endpoints ----------
def track_package(tracking_number: str) -> dict:
    r = requests.post(_url("/track"), json={"tracking_number": tracking_number}, timeout=30)
    r.raise_for_status()
    return r.json()

def recheck_sms(tracking_number: str) -> dict:
    r = requests.post(_url("/recheck_sms"), json={"tracking_number": tracking_number}, timeout=30)
    r.raise_for_status()
    return r.json()

def verify_customs_docs_needed(tracking_number: str) -> dict:
    r = requests.post(_url("/verify_customs_docs_needed"), json={"tracking_number": tracking_number}, timeout=30)
    r.raise_for_status()
    return r.json()

def resend_notification(tracking_number: str) -> dict:
    r = requests.post(_url("/resend_notification"), json={"tracking_number": tracking_number}, timeout=30)
    r.raise_for_status()
    return r.json()

def provide_est_delivery_window(tracking_number: str) -> dict:
    r = requests.post(_url("/provide_est_delivery_window"), json={"tracking_number": tracking_number}, timeout=30)
    r.raise_for_status()
    return r.json()

# Handy health check
def ping() -> str:
    r = requests.get(_url("/ping"), timeout=10)
    r.raise_for_status()
    return r.text