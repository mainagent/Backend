from __future__ import annotations
from flask import Flask, request, jsonify, send_file, send_from_directory
from resend_notification import handle_resend_notification
from elevenlabs.client import ElevenLabs
from email.mime.text import MIMEText
from io import BytesIO
from dotenv import load_dotenv
from bankid import bp as bankid_bp
from utils_cleanup import normalize_spelled_email, parse_sv_date_time, validate_email
import base64
import io
import os, json
import smtplib
import random
import string
import datetime as dt
import re
import hmac, hashlib, time
import requests
from flask import Response
from urllib.parse import quote_plus

# --- Google Calendar imports ---
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()
load_dotenv(override=True)
XI_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_WEBHOOK_SECRET = os.getenv("ELEVENLABS_WEBHOOK_SECRET", "")
ELEVEN_AGENT_ID = os.getenv("ELEVEN_AGENT_ID", "")

# >>> portal/env for bookings + clinic tag
PORTAL_BASE = os.getenv("PORTAL_BASE", "http://127.0.0.1:5000")
PORTAL_KEY  = os.getenv("PORTAL_API_KEY", "")
CLINIC      = os.getenv("CLINIC", "mathias")

from openai import OpenAI
client_oa = OpenAI()

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

# -------------------- SESSION & BOOKING GATES --------------------
# One unified session store (per conversation/call)
SESSION = {}  # {conversation_id: {"slots": {...}, "verified": False, "last_tool": None, "created_booking": False}}

def session_reset(cid: str):
    SESSION[cid] = {"slots": {}, "verified": False, "last_tool": None, "created_booking": False}

def session_end(cid: str):
    SESSION.pop(cid, None)

REQUIRED_SLOTS = ("name","email","date","time","treatment")

def set_slot(cid: str, key: str, value: str):
    SESSION.setdefault(cid, {"slots": {}, "verified": False, "last_tool": None, "created_booking": False})
    if value is not None and value != "":
        SESSION[cid]["slots"][key] = value

def slots_ready(cid: str) -> bool:
    s = SESSION.get(cid, {}).get("slots", {})
    return all(s.get(k) for k in REQUIRED_SLOTS)

def booking_allowed(cid: str) -> bool:
    s = SESSION.get(cid, {})
    return (s.get("verified") is True) and (s.get("created_booking") is not True) and slots_ready(cid)

# idempotency (avoid dupes if the LLM retries)
LAST_BOOK = {}  # {hash_key: timestamp}

def _idem_key(s: dict) -> str:
    base = f"{s.get('name','')}|{s.get('email','')}|{s.get('date','')}|{s.get('time','')}|{s.get('treatment','')}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()

def safe_create_booking(payload: dict) -> tuple[bool, str]:
    k = _idem_key(payload)
    now = time.time()
    if k in LAST_BOOK and now - LAST_BOOK[k] < 60:  # 60s guard window
        return False, "duplicate_attempt"
    ok, info = create_booking_via_portal(payload)
    if ok:
        LAST_BOOK[k] = now
    return ok, info
# ----------------------------------------------------------------

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

app = Flask(__name__)

# Temporary in-memory "database" (kept)
appointments = {}

def generate_short_id(length=4):
    chars = string.ascii_uppercase + string.digits
    while True:
        short_id = ''.join(random.choices(chars, k=length))
        if short_id not in appointments:
            return short_id

from routes.generate_audio import tts_bp
app.register_blueprint(tts_bp)
app.register_blueprint(bankid_bp)

print("bankid blueprint registered")
print(app.url_map)

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
        match = hmac.compare_digest(provided_sig, computed)
        print(f"[11L] computed_sig={computed[:12]}... sig match={match}")
        if not match:
            return ("bad signature", 401)

    try:
        evt = request.get_json(force=True)
    except Exception as e:
        print(f"[11L] JSON parse error: {e}")
        return ("bad json", 400)

    etype = (evt.get("type") or "").strip()
    conv_id = (evt.get("conversation_id") or evt.get("conversationId") or "").strip()

    print(f"[11L] event type={etype} conv_id={conv_id}")

    if etype in ("conversation_started", "call_started"):
        if conv_id:
            session_reset(conv_id)
            print(f"[SESSION] reset -> {conv_id}")
    elif etype in ("conversation_ended", "call_ended"):
        if conv_id:
            session_end(conv_id)
            print(f"[SESSION] end -> {conv_id}")

    return jsonify(ok=True)

@app.route("/ping")
def ping():
    return {"message": "✅ Backend is alive!"}, 200

@app.get("/admin")
def admin_page():
    return send_from_directory("static", "admin.html")

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

# ---------------- RESEND CONFIRMATION (kept) ----------------
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
        time_s = (data.get("time") or "").strip()
        if not (name and date and time_s):
            return jsonify({
                "error": "Provide either a valid appointment_id OR name+date+time"
            }), 400
        matches = [
            (aid, a) for aid, a in appointments.items()
            if a.get("name","").strip().lower() == name
            and a.get("date","").strip() == date
            and a.get("time","").strip() == time_s
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
    from portal import send_email_html  # avoid circular on startup
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

# ====== portal client ======
DATE_RE = r"(20\d{2}-\d{2}-\d{2})"
TIME_RE = r"\b([01]?\d|2[0-3]):([0-5]\d)\b"

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
            timeout=12,
        )
        if r.status_code == 200 and (r.json() or {}).get("ok"):
            return True, str((r.json() or {}).get("id"))
        return False, f"portal error {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"portal exception: {e}"

def repair_text_with_gpt(text: str, lang: str = "sv-SE") -> str:
    """
    Light transcript repair for Swedish/English (email/times normalizations).
    Returns ONLY the corrected text.
    """
    system_prompt = (
        "Du är ett transkript-reparationsfilter för svenska/engelska. "
        "Korrigera fel från tal-till-text utan att ändra betydelsen.\n"
        "Regler:\n"
        "1) Korrigera uppenbara stavfel via kontext.\n"
        "2) Siffror som siffror; telefonnummer utan mellanslag.\n"
        "3) E-post: 'snabel-a/at'->'@'; 'punkt/dot/prick'->'.'; ta bort mellanslag runt '@' och '.'. '.kom'->'.com'.\n"
        "4) Tider: '10 00' -> '10:00'.\n"
        "5) Lägg inte till information. Returnera endast den korrigerade texten."
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

# -------------------- MAIN INPUT ROUTE --------------------
@app.post("/process_input")
def process_input():
    data = request.get_json() or {}
    is_final = bool(data.get("is_final"))
    lang = (data.get("lang") or "sv-SE").strip()

    # conversation id from payload or header (fallback to "local" for tests)
    conv_id = (data.get("conv_id")
               or data.get("conversation_id")
               or data.get("conversationId")
               or request.headers.get("X-Conversation-Id")
               or "local")
    SESSION.setdefault(conv_id, {"slots": {}, "verified": False, "last_tool": None, "created_booking": False})

    text_in = (data.get("text") or "").strip()
    audio_b64 = data.get("audio_base64")
    audio_mime = (data.get("audio_mime") or "audio/wav").strip()

    print(f"[IN] cid={conv_id} text='{text_in}' lang={lang} is_final={is_final}")

    corrected_text = text_in
    if not corrected_text:
        return jsonify({"error": "No text"}), 400

    try:
        corrected_text = repair_text_with_gpt(corrected_text, lang=lang)
    except Exception as e:
        print(f"[WARN] GPT repair failed: {e}")

    corrected_text = normalize_spelled_email(corrected_text)
    print(f"[NORM] -> '{corrected_text}'")

    # light date/time extraction from utterance (doesn't overwrite if already set)
    d, tm = parse_sv_date_time(corrected_text)
    if d and not SESSION[conv_id]["slots"].get("date"):
        set_slot(conv_id, "date", d)
    if tm and not SESSION[conv_id]["slots"].get("time"):
        set_slot(conv_id, "time", tm)

    # --- BOOKING GATE (only when verified & all slots present) ---
    if booking_allowed(conv_id):
        s = SESSION[conv_id]["slots"]
        payload = {
            "clinic": CLINIC,
            "name":  s["name"],
            "email": s["email"],
            "phone": s.get("phone"),
            "date":  s["date"],
            "time":  s["time"],
            "treatment": s["treatment"],
        }
        ok, info = safe_create_booking(payload)
        if ok:
            SESSION[conv_id]["created_booking"] = True
            reply = (f"Toppen! Jag bokade {payload['treatment']} {payload['date']} {payload['time']}. "
                     f"Bokningsnummer {info}. Behöver du något mer?")
        else:
            if info == "duplicate_attempt":
                reply = "Jag har redan registrerat den bokningen nyss. Vill du ändra något?"
            else:
                reply = ("Jag försökte boka men något gick fel. "
                         "Vill du att jag försöker igen eller vill du ge en annan tid?")
        # compact logging (redacted)
        def _redact(s_: str) -> str:
            s_ = re.sub(r"\b\d{6}[- ]?\d{4}\b", "[PNR]", s_)
            s_ = re.sub(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", "[EMAIL]", s_)
            return s_
        log_obj = {
            "cid": conv_id,
            "text_in": _redact(text_in),
            "corrected": _redact(corrected_text),
            "slots": SESSION.get(conv_id,{}).get("slots", {}),
            "verified": SESSION.get(conv_id,{}).get("verified", False),
        }
        print("[TURN]", json.dumps(log_obj, ensure_ascii=False))
        return jsonify({"response": reply, "end_turn": is_final})

    # default echo (dev)
    reply = f"Jag hörde: {corrected_text}"

    # compact logging (redacted)
    def _redact(s_: str) -> str:
        s_ = re.sub(r"\b\d{6}[- ]?\d{4}\b", "[PNR]", s_)
        s_ = re.sub(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", "[EMAIL]", s_)
        return s_
    log_obj = {
        "cid": conv_id,
        "text_in": _redact(text_in),
        "corrected": _redact(corrected_text),
        "slots": SESSION.get(conv_id,{}).get("slots", {}),
        "verified": SESSION.get(conv_id,{}).get("verified", False),
    }
    print("[TURN]", json.dumps(log_obj, ensure_ascii=False))
    return jsonify({"response": reply, "end_turn": is_final})
# -------------------------------------------------------------

from portal import init_portal  # send_email_html is imported lazily where needed
init_portal(app)

if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=PORT)
