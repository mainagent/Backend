from __future__ import annotations
from flask import Flask, request, jsonify, send_file
from resend_notification import handle_resend_notification
from elevenlabs.client import ElevenLabs
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from io import BytesIO
from dotenv import load_dotenv
import base64
import io
import os, json
import smtplib
import random
import string
import datetime as dt
import re
import hmac, hashlib
import requests

# --- Google Calendar imports ---
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()
load_dotenv(override=True)
XI_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_WEBHOOK_SECRET = os.getenv("ELEVENLABS_WEBHOOK_SECRET", "")

# >>> NEW: portal/env for bookings + clinic tag
PORTAL_BASE = os.getenv("PORTAL_BASE", "http://127.0.0.1:5000")
PORTAL_KEY  = os.getenv("PORTAL_API_KEY", "")
CLINIC      = os.getenv("CLINIC", "mathias")

from openai import OpenAI
client_oa = OpenAI()

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

def _make_short_id(k=4):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=k))

def _get_gcal_service():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())  # type: ignore
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as token:
            token.write(creds.to_json())
    return build("calendar", "v3", credentials=creds)

def _compose_event(data: dict):
    tz = os.getenv("GCAL_TZ", "Europe/Stockholm")
    date = data.get("date", "")
    time = data.get("time", "")
    name = data.get("name", "")
    treatment = data.get("treatment", "Behandling")

    start_dt = dt.datetime.fromisoformat(f"{date}T{time}:00")
    end_dt = start_dt + dt.timedelta(minutes=30)

    attendees = []
    if data.get("email"):
        attendees.append({"email": data["email"]})

    return {
        "summary": f"Tandläkartid: {treatment} – {name}",
        "description": f"Bokad via röstagenten.\nNamn: {name}\nBehandling: {treatment}",
        "start": {"dateTime": start_dt.isoformat(), "timeZone": tz},
        "end":   {"dateTime": end_dt.isoformat(), "timeZone": tz},
        "attendees": attendees,
    }

def repair_text_with_gpt(text: str, lang: str = "sv-SE") -> str:
    system_prompt = (
        "Du är ett transkript-reparationsfilter för svenska/engelska. "
        "Korrigera fel från tal-till-text utan att ändra betydelsen.\n"
        "Regler:\n"
        "1) Korrigera uppenbara stavfel via kontext (t.ex. 'buka'->'boka', 'imorlon'->'imorgon').\n"
        "2) Siffror: skriv som siffror. Telefonnummer skrivs utan mellanslag (t.ex. 'noll sju tre...' -> '073...').\n"
        "3) E-post: ersätt ' at ' och 'snabela' med '@'; ' dot '/'punkt'/'prick' med '.'; ta bort mellanslag runt '@' och '.'.\n"
        "   Vanliga domäner: '.kom' -> '.com'.\n"
        "4) Tider: '10 00' -> '10:00'.\n"
        "5) Lägg inte till information. Behåll språket och innebörd. Returnera endast den korrigerade texten."
    )
    resp = client_oa.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text}
        ],
        temperature=0.0,
    )
    return (resp.choices[0].message.content or "").strip()

_SW_TO_SYMBOL = {
    " snabela ": " @ ",
    " at ": " @ ",
    " punkt ": " . ",
    " dot ": " . ",
    " prick ": " . ",
}
_WORD_TO_DIGIT = {
    "noll":"0","zero":"0",
    "ett":"1","en":"1","one":"1",
    "två":"2","tva":"2","two":"2",
    "tre":"3","three":"3",
    "fyra":"4","four":"4",
    "fem":"5","five":"5",
    "sex":"6","six":"6",
    "sju":"7","seven":"7",
    "åtta":"8","atta":"8","eight":"8",
    "nio":"9","nine":"9",
}
def normalize_contacts(s: str) -> str:
    t = f" {s} "
    low = t.lower()
    for k, v in _SW_TO_SYMBOL.items():
        low = low.replace(k, v)
    low = re.sub(r"\s*@\s*", "@", low)
    low = re.sub(r"\s*\.\s*", ".", low)
    low = re.sub(r"\.kom\b", ".com", low)

    tokens = low.split()
    out = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _WORD_TO_DIGIT or re.fullmatch(r"\d", tok):
            digits = []
            while i < len(tokens) and (tokens[i] in _WORD_TO_DIGIT or re.fullmatch(r"\d", tokens[i])):
                digits.append(_WORD_TO_DIGIT.get(tokens[i], tokens[i]))
                i += 1
            if len(digits) >= 6:
                out.append("".join(digits))
            else:
                out.extend(digits)
        else:
            out.append(tok)
            i += 1
    low = " ".join(out)
    low = re.sub(r"\b(\d{1,2})[ ](\d{2})\b", r"\1:\2", low)
    return low.strip()

app = Flask(__name__)

# Temporary in-memory "database"
appointments = {}

def generate_short_id(length=4):
    chars = string.ascii_uppercase + string.digits
    while True:
        short_id = ''.join(random.choices(chars, k=length))
        if short_id not in appointments:
            return short_id

from routes.generate_audio import tts_bp
app.register_blueprint(tts_bp)

def transcribe_with_whisper(audio_bytes: bytes, mime: str = "audio/wav") -> str:
    file_like = BytesIO(audio_bytes)
    file_like.name = f"input.{ 'mp3' if mime=='audio/mpeg' else 'wav' }"
    resp = client_oa.audio.transcriptions.create(
        model="whisper-1",
        file=("audio", file_like, mime)
    )
    return resp.text

def _extract_final_text(evt: dict) -> str:
    return (
        evt.get("text")
        or evt.get("transcript")
        or (evt.get("item") or {}).get("transcript")
        or ""
    ).strip()

def _post_eleven_response(conversation_id: str, text: str) -> requests.Response:
    url = f"https://api.elevenlabs.io/v1/convai/conversations/{conversation_id}/responses"
    headers = {
        "xi-api-key": XI_API_KEY,
        "Content-Type": "application/json",
        "X-Requested-With": "python",
    }
    return requests.post(url, headers=headers, json={"response": {"text": text}}, timeout=15)

# --- ElevenLabs webhook receiver ---
@app.post("/webhooks/elevenlabs")
def elevenlabs_webhook():
    raw = request.get_data()
    provided_sig = request.headers.get("X-ElevenLabs-Signature", "")
    print(f"[11L] webhook hit. len={len(raw)} provided_sig={provided_sig[:12]}")

    if ELEVENLABS_WEBHOOK_SECRET:
        computed = hmac.new(ELEVENLABS_WEBHOOK_SECRET.encode("utf-8"),
                            raw, hashlib.sha256).hexdigest()
        print(f"[11L] computed_sig={computed[:12]}...")
        if not hmac.compare_digest(provided_sig, computed):
            print("[11L] bad signature MISMATCH -> returning 400")
            return ("bad signature", 400)

    try:
        evt = request.get_json(force=True)
    except Exception as e:
        print(f"[11L] JSON parse error: {e} raw[:200]={raw[:200]!r}")
        return ("bad json", 400)

    etype = evt.get("type")
    print(f"[11L] event type={etype} keys={list(evt.keys())[:10]}")
    return jsonify(ok=True)

@app.route("/ping")
def ping():
    return {"message": "✅ Backend is alive!"}, 200

@app.route("/track", methods=["POST"])
def track_package():
    data = request.get_json()
    tracking_number = data.get("tracking_number")
    return jsonify({
        "status": "Package is at terminal",
        "last_location": "Gothenburg, Sweden",
        "expected_delivery": "2025-08-10"
    })

@app.route("/recheck_sms", methods=["POST"])
def recheck_sms():
    data = request.get_json()
    tracking_number = data.get("tracking_number", "")
    return jsonify({
        "action": "recheck_sms",
        "status": "SMS notification resent",
        "tracking_number": tracking_number
    })

# ---------------- EMAIL HELPER ----------------
def send_email_helper(to, subject, body):
    if not to or "@" not in to:
        raise ValueError("Invalid or missing 'to' address")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = os.getenv("EMAIL_USER")
    msg["To"] = to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(os.getenv("EMAIL_USER"), os.getenv("EMAIL_PASS"))
        server.sendmail(os.getenv("EMAIL_USER"), [to], msg.as_string())

# ---------------- BOOK APPOINTMENT ----------------
def send_email_html(to: str, subject: str, html: str):
    sender_email = os.getenv("EMAIL_USER")
    sender_name  = os.getenv("EMAIL_FROM_NAME", "Tandläkarkliniken")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = formataddr((sender_name, sender_email))
    msg["To"]      = to
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(os.getenv("EMAIL_USER"), os.getenv("EMAIL_PASS"))
        server.sendmail(sender_email, [to], msg.as_string())

@app.route("/book", methods=["POST"])
def book():
    try:
        data = request.get_json(force=True) or {}
        for f in ("name", "date", "time"):
            if not data.get(f):
                return jsonify({"status": "error", "error": f"Missing field: {f}"}), 400
        service = _get_gcal_service()
        event = _compose_event(data)
        created = service.events().insert(calendarId="primary", body=event, sendUpdates="all").execute()
        appt_id = _make_short_id()
        return jsonify({
            "status": "success",
            "method": "google_calendar",
            "appointment_id": appt_id,
            "patient_name": data.get("name",""),
            "date": data.get("date",""),
            "time": data.get("time",""),
            "calendar_event_id": created.get("id"),
            "htmlLink": created.get("htmlLink"),
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/resend_confirmation", methods=["POST"])
def resend_confirmation():
    data = request.get_json() or {}
    appointment_id = (data.get("appointment_id") or "").strip().upper()
    appt = None
    target_id = None
    if appointment_id:
        appt = appointments.get(appointment_id)
        if appt:
            target_id = appointment_id
        else:
            appointment_id = ""
    if not appt:
        name = (data.get("name") or "").strip().lower()
        date = (data.get("date") or "").strip()
        time = (data.get("time") or "").strip()
        if not (name and date and time):
            return jsonify({
                "error": "Provide either a valid appointment_id OR name+date+time"
            }), 400
        matches = [
            (aid, a) for aid, a in appointments.items()
            if a.get("name","").strip().lower() == name
            and a.get("date","").strip() == date
            and a.get("time","").strip() == time
        ]
        if not matches:
            return jsonify({"error": "No matching appointment found"}), 404
        if len(matches) > 1:
            return jsonify({
                "error": "Multiple matches found; please provide appointment_id"
            }), 409
        target_id, appt = matches[0]

    html = f"""
    <!doctype html>
    <html>
      <body style="font-family: -apple-system, Segoe UI, Roboto, Arial; background:#f8fafc; padding:24px;">
        <table width="100%" cellpadding="0" cellspacing="0" style="max-width:560px; margin:auto; background:white; border-radius:12px; box-shadow:0 1px 6px rgba(0,0,0,0.06);">
          <tr>
            <td style="padding:24px 28px;">
              <h2 style="margin:0 0 12px 0; font-size:20px; color:#0f172a;">Bokningsbekräftelse (igen)</h2>
              <table cellpadding="0" cellspacing="0" style="width:100%; background:#f1f5f9; border-radius:8px; padding:12px;">
                <tr><td><strong>Behandling:</strong> {appt['treatment']}</td></tr>
                <tr><td><strong>Datum:</strong> {appt['date']}</td></tr>
                <tr><td><strong>Tid:</strong> {appt['time']}</td></tr>
                <tr><td><strong>Boknings-ID:</strong> {target_id}</td></tr>
              </table>
              <p style="margin:8px 0 0 0; color:#64748b; font-size:12px;">Vänliga hälsningar,<br/>Tandläkarkliniken</p>
            </td>
          </tr>
        </table>
      </body>
    </html>
    """
    send_email_html(
        to=appt["email"],
        subject="Din tandläkartid (påminnelse)",
        html=html
    )
    return jsonify({
        "status": "resent",
        "appointment_id": target_id,
        "email": appt["email"]
    }), 200

@app.route("/send_email", methods=["POST"])
def send_email():
    try:
        data = request.get_json(force=True) or {}
        to = (data.get("to") or "").strip()
        subject = (data.get("subject") or "").strip()
        body = (data.get("body") or "").strip()
        send_email_helper(to, subject, body)
        return jsonify({"status": "sent", "to": to, "subject": subject}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/flag_human_support_request", methods=["POST"])
def flag_human_support_request():
    data = request.get_json()
    tracking_number = data.get("tracking_number")
    reason = data.get("reason", "Unclear issue")
    return jsonify({
        "action": "flag_human_support_request",
        "status": "Human support flagged for follow-up",
        "tracking_number": tracking_number,
        "reason": reason
    })

@app.route("/resend_notification", methods=["POST"])
def resend_notification():
    return handle_resend_notification()

@app.route("/verify_customs_docs_needed", methods=["POST"])
def verify_customs_docs_needed():
    data = request.get_json()
    tracking_number = data.get("tracking_number")
    return jsonify({
        "action": "verify_customs_docs_needed",
        "status": "Customs documents required",
        "instructions": "Please upload your ID and invoice at postnord.se/tull within 24 hours to avoid return.",
        "tracking_number": tracking_number
    })

@app.route("/provide_est_delivery_window", methods=["POST"])
def provide_est_delivery_window():
    data = request.get_json()
    tracking_number = data.get("tracking_number")
    return jsonify({
        "action": "provide_est_delivery_window",
        "tracking_number": tracking_number,
        "estimated_window": "Between 14:00 - 18:00 on 2025-08-10",
        "status": "Delivery window provided"
    })

# ====== NEW: booking intent + portal client ======
DATE_RE = r"(20\d{2}-\d{2}-\d{2})"
TIME_RE = r"\b([01]?\d|2[0-3]):([0-5]\d)\b"

def parse_booking(text: str) -> dict | None:
    t = text.strip().lower()
    if not any(k in t for k in ["book", "boka", "appointment", "tid"]):
        return None
    d = re.search(DATE_RE, t)
    tm = re.search(TIME_RE, t)
    if not (d and tm):
        return None
    name = None
    m = re.search(r"(my name is|jag heter)\s+([a-zåäöé\- ]{2,})", t)
    if m: name = m.group(2).title().strip()
    return {
        "clinic": CLINIC,
        "name": name or "Okänd",
        "email": None,
        "phone": None,
        "date": d.group(1),
        "time": f"{tm.group(1)}:{tm.group(2)}",
        "treatment": None,
    }

def create_booking_via_portal(payload: dict) -> tuple[bool, str]:
    try:
        r = requests.post(
            f"{PORTAL_BASE}/portal/api/bookings/new",
            headers={
                "Content-Type": "application/json",
                "X-Portal-Key": PORTAL_KEY,
            },
            params={"clinic": payload.get("clinic", CLINIC)},
            json=payload,
            timeout=10,
        )
        if r.status_code == 200 and (r.json() or {}).get("ok"):
            return True, str((r.json() or {}).get("id"))
        return False, f"portal error {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"portal exception: {e}"
# ================================================

@app.post("/process_input")
def process_input():
    """
    Accepts:
      { "session_id":"...", "text":"...", "is_final": true/false, "lang":"sv-SE",
        "audio_base64": "...", "audio_mime":"audio/wav" }
    Returns:
      { "response":"...", "end_turn": bool }
    """
    data = request.get_json() or {}
    is_final = bool(data.get("is_final"))
    lang = (data.get("lang") or "sv-SE").strip()

    text_in = (data.get("text") or "").strip()
    audio_b64 = data.get("audio_base64")
    audio_mime = (data.get("audio_mime") or "audio/wav").strip()

    print(f"[IN] text_in='{text_in}' lang={lang} is_final={is_final}")

    corrected_text = text_in

    # --- Whisper block (optional) ---
    # if is_final and audio_b64:
    #     try:
    #         audio_bytes = base64.b64decode(audio_b64)
    #         whisper_text = transcribe_with_whisper(audio_bytes, mime=audio_mime)
    #         corrected_text = whisper_text.strip() or text_in
    #     except Exception as e:
    #         print(f"[WARN] Whisper failed, falling back to frontend text: {e}")

    if not corrected_text:
        return jsonify({"error": "No text"}), 400

    # GPT repair + normalization
    try:
        print("[GPT] calling repair_text_with_gpt...")
        corrected_text = repair_text_with_gpt(corrected_text, lang=lang)
        print(f"[GPT] result='{corrected_text}'")
    except Exception as e:
        print(f"[WARN] GPT repair failed: {e}")
    corrected_text = normalize_contacts(corrected_text)
    print(f"[NORM] after normalize='{corrected_text}'")

    # >>> Booking intent first
    booking = parse_booking(corrected_text)
    if booking:
        ok, info = create_booking_via_portal(booking)
        if ok:
            reply = (
                f"Toppen! Jag bokade in en tid för {booking['date']} klockan {booking['time']} "
                f"hos {CLINIC.capitalize()}. Bokningsnummer {info}. Behöver du något mer?"
            )
        else:
            reply = (
                "Jag försökte boka men något gick fel. "
                "Vill du att jag försöker igen eller vill du ge ett annat datum och tid?"
            )
        print(f"[BOOK] {('ok id='+info) if ok else info}")
        return jsonify({"response": reply, "end_turn": is_final})

    # Fallback echo
    reply = f"Jag hörde: {corrected_text}"
    print(f"[OUT] reply='{reply}'")
    return jsonify({"response": reply, "end_turn": is_final})

from portal import init_portal
init_portal(app)

if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=PORT)