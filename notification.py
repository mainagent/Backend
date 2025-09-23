# notifications.py  (new)  --- or put this right next to your existing send_email_html
from brand_config import env_for
import os
import requests  # or your existing Resend client

def send_email_html(to_email: str, subject: str, html: str, brand: str = "dental") -> None:
    from_name  = env_for(brand, "EMAIL_FROM_NAME", "Kundservice")
    reply_to   = env_for(brand, "REPLY_TO", "noreply@example.com")
    # your existing Resend call; keep as-is, only swap sender fields
    sender = f"{from_name} <noreply@your-domain.example>"  # or whatever you already use
    # ... send via Resend
    requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {os.getenv('RESEND_API_KEY')}"},
        json={
            "from": sender,
            "to": [to_email],
            "subject": subject,
            "html": html,
            "reply_to": reply_to
        },
        timeout=10
    )