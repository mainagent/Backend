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
SESSION = {}  # {conversation_id: {...}}

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

LAST_BOOK = {}  # {hash_key: timestamp}

def _idem_key(s: dict) -> str:
    base = f"{s.get('name','')}|{s.get('email','')}|{s.get('date','')}|{s.get('time','')}|{s.get('treatment','')}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()

def safe_create_booking(payload: dict) -> tuple[bool, str]:
    k = _idem_key(payload)
    now = time.time()
    if k in LAST_BOOK and now - LAST_BOOK[k] < 60:
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

# ====== portal client ======
DATE_RE = r"(20\d{2}-\d{2}-\d{2})"
TIME_RE = r"\b([01]?\d|2[0-3]):([0-5]\d)\b"

PNR_RE = re.compile(r"\b(\d{6}[- ]?\d{4}|\d{8}[- ]?\d{4})\b")

def _clean_personnummer(s: str) -> str | None:
    if not s: 
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) == 10:  # YYMMDDXXXX -> guess century
        digits = ("19" if digits[0] in "6789" else "20") + digits
    return digits if len(digits) == 12 else None

def _bankid_start_local(pnr: str) -> tuple[bool, str | None]:
    """Calls your own BankID Start endpoint; returns (ok, orderRef)."""
    try:
        r = requests.post(
            f"{PORTAL_BASE}/portal/api/bankid/start",
            headers={"Content-Type": "application/json"},
            json={"personal_number": pnr},
            timeout=10,
        )
        j = r.json() if r.content else {}
        if r.status_code == 200 and j.get("ok") and j.get("orderRef"):
            return True, j["orderRef"]
        return False, None
    except Exception as e:
        print(f"[BANKID] start error: {e}")
        return False, None

def _bankid_status_local(order_ref: str) -> tuple[bool, str]:
    """Returns (ok, status) where status ∈ {'pending','complete','failed'}."""
    try:
        r = requests.get(
            f"{PORTAL_BASE}/portal/api/bankid/status",
            params={"orderRef": order_ref},
            timeout=10,
        )
        j = r.json() if r.content else {}
        if r.status_code == 200 and j.get("ok"):
            return True, j.get("status", "pending")
        return False, "failed"
    except Exception as e:
        print(f"[BANKID] status error: {e}")
        return False, "failed"

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
# only call this after BankID verification  
def create_booking_via_portal_verified(payload: dict) -> tuple[bool, str]:
    try:
        r = requests.post(
            f"{PORTAL_BASE}/portal/api/bookings/new",
            headers={
                "Content-Type": "application/json",
                "X-Portal-Key": PORTAL_KEY,
                "X-Verified": "true",  # <- only use this path after BankID
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

    conv_id = (data.get("conv_id")
               or data.get("conversation_id")
               or data.get("conversationId")
               or request.headers.get("X-Conversation-Id")
               or "local")

    SESSION.setdefault(conv_id, {
        "slots": {},
        "verified": False,
        "last_tool": None,
        "created_booking": False,
        "bankid": {"orderRef": None, "asked": False, "last_prompt": 0}
    })

    text_in = (data.get("text") or "").strip()
    if not text_in:
        return jsonify({"error": "No text"}), 400

    print(f"[IN] cid={conv_id} text='{text_in}' is_final={is_final}")

    # --- light repair + email normalization (kept) ---
    corrected_text = text_in
    try:
        corrected_text = repair_text_with_gpt(corrected_text, lang=lang)
    except Exception as e:
        print(f"[WARN] GPT repair failed: {e}")
    corrected_text = normalize_spelled_email(corrected_text)
    print(f"[NORM] -> '{corrected_text}'")

    # --- extract date/time if casually mentioned (kept) ---
    d, tm = parse_sv_date_time(corrected_text)
    if d and not SESSION[conv_id]["slots"].get("date"): set_slot(conv_id, "date", d)
    if tm and not SESSION[conv_id]["slots"].get("time"): set_slot(conv_id, "time", tm)

    slots = SESSION[conv_id]["slots"]

    # ===== 1) COLLECT MANDATORY SLOTS in fixed order =====
    # name
    if not slots.get("name"):
        m = re.search(r"\b(jag heter|mitt namn är)\s+([A-Za-zÅÄÖåäö\- ]+)", corrected_text)
        if m:
            set_slot(conv_id, "name", m.group(2).strip())
            reply = f"Tack {slots.get('name','')}. Vilken e-post vill du använda?"
        else:
            reply = "Vad heter du?"
        print("[TURN]", json.dumps({"cid": conv_id, "stage": "ask_name", "slots": slots}, ensure_ascii=False))
        return jsonify({"response": reply, "end_turn": is_final})

    # email
    if not slots.get("email"):
        # try to extract an email-looking token from the normalized text
        maybe = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", corrected_text)
        if maybe:
            set_slot(conv_id, "email", maybe.group(0))
            reply = "Tack! Vilken behandling gäller det?"
        else:
            reply = "Bokstavera din e-post, säg 'snabel-a' för @ och 'punkt' för ."
        print("[TURN]", json.dumps({"cid": conv_id, "stage": "ask_email", "slots": slots}, ensure_ascii=False))
        return jsonify({"response": reply, "end_turn": is_final})

    # treatment
    if not slots.get("treatment"):
        # simple catch
        m = re.search(r"\b(undersökning|akut|hygienist|kontroll|blekning)\b", corrected_text)
        if m:
            set_slot(conv_id, "treatment", m.group(1))
            reply = "Vilket datum vill du komma? Säg t.ex. 2025-09-01."
        else:
            reply = "Vilken behandling vill du boka? (t.ex. undersökning, akut, hygienist)"
        print("[TURN]", json.dumps({"cid": conv_id, "stage": "ask_treatment", "slots": slots}, ensure_ascii=False))
        return jsonify({"response": reply, "end_turn": is_final})

    # date
    if not slots.get("date"):
        if d:
            set_slot(conv_id, "date", d)
            reply = "Vilken tid passar? (t.ex. 10:00)"
        else:
            reply = "Vilket datum vill du komma? Säg t.ex. 2025-09-01."
        print("[TURN]", json.dumps({"cid": conv_id, "stage": "ask_date", "slots": slots}, ensure_ascii=False))
        return jsonify({"response": reply, "end_turn": is_final})

    # time
    if not slots.get("time"):
        if tm:
            set_slot(conv_id, "time", tm)
            reply = "Toppen. Jag behöver verifiera dig med BankID."
        else:
            reply = "Vilken tid passar? (t.ex. 10:00)"
        print("[TURN]", json.dumps({"cid": conv_id, "stage": "ask_time", "slots": slots}, ensure_ascii=False))
        return jsonify({"response": reply, "end_turn": is_final})

    # ===== 2) BANKID GATE (mandatory before booking) =====
    if not SESSION[conv_id]["verified"]:
        bank = SESSION[conv_id]["bankid"]
        # If we don't have an orderRef yet, try to capture pnr from this turn
        if not bank.get("orderRef"):
            # ask for pnr or parse it
            pnr_match = PNR_RE.search(corrected_text)
            if pnr_match:
                pnr = _clean_personnummer(pnr_match.group(0))
                if pnr:
                    ok, order_ref = _bankid_start_local(pnr)
                    if ok and order_ref:
                        bank["orderRef"] = order_ref
                        bank["asked"] = True
                        reply = "Startar BankID. Öppna din BankID-app och godkänn – säg till när du är klar."
                    else:
                        reply = "Kunde inte starta BankID just nu. Vill du att jag försöker igen?"
                else:
                    reply = "Jag behöver hela personnumret, tolv siffror. Säg det långsamt tack."
            else:
                # first time we hit the gate: ask explicitly
                reply = "För att boka behöver jag verifiera dig med BankID. Säg ditt personnummer, tolv siffror."
            print("[TURN]", json.dumps({"cid": conv_id, "stage": "bankid_start", "slots": slots}, ensure_ascii=False))
            return jsonify({"response": reply, "end_turn": is_final})

        # We have an orderRef → check status
        ok, status = _bankid_status_local(bank["orderRef"])
        if ok and status == "complete":
            SESSION[conv_id]["verified"] = True
            reply = "Tack, du är verifierad. Jag bokar din tid nu."
        elif not ok or status == "failed":
            # reset and re-ask
            bank["orderRef"] = None
            reply = "Det blev fel med BankID. Vill du försöka igen?"
        else:
            reply = "Ett ögonblick… jag kollar din BankID-status."
        print("[TURN]", json.dumps({"cid": conv_id, "stage": "bankid_status", "status": status}, ensure_ascii=False))
        return jsonify({"response": reply, "end_turn": is_final})

    # ===== 3) BOOKING (only AFTER verified) =====
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
        ok, info = create_booking_via_portal_verified(payload)
        if ok:
            SESSION[conv_id]["created_booking"] = True
            reply = (f"Klart! Jag bokade {payload['treatment']} {payload['date']} {payload['time']}. "
                     f"Bokningsnummer {info}. Behöver du något mer?")
        else:
            if info == "duplicate_attempt":
                reply = "Jag har redan registrerat den bokningen nyss. Vill du ändra något?"
            else:
                reply = "Jag försökte boka men något gick fel. Vill du att jag försöker igen?"
        print("[TURN]", json.dumps({"cid": conv_id, "stage": "book", "ok": ok}, ensure_ascii=False))
        return jsonify({"response": reply, "end_turn": is_final})

    # Fallback (shouldn’t really happen with the flow above)
    reply = "Jag hörde: " + corrected_text
    print("[TURN]", json.dumps({"cid": conv_id, "stage": "echo"}, ensure_ascii=False))
    return jsonify({"response": reply, "end_turn": is_final})

@app.post("/conv/mark_verified")
def mark_verified():
    data = request.get_json(force=True) or {}
    cid = (data.get("conv_id") or data.get("conversation_id") or data.get("conversationId") or "").strip()
    if not cid:
        return jsonify({"ok": False, "error": "missing conv_id"}), 400
    SESSION.setdefault(cid, {"slots": {}, "verified": False, "last_tool": None, "created_booking": False})
    SESSION[cid]["verified"] = True
    return jsonify({"ok": True, "conv_id": cid, "verified": True})

@app.post("/bankid/verify_and_mark")
def bankid_verify_and_mark():
    """
    Demo-friendly helper:
    - starts BankID for a given personal_number
    - polls /portal/api/bankid/status until complete/failed
    - if complete -> marks SESSION[conv_id]['verified'] = True
    """
    data = request.get_json(force=True) or {}
    conv_id = (data.get("conv_id") or "local").strip()
    personal_number = (data.get("personal_number") or "").strip()

    if not personal_number:
        return jsonify({"ok": False, "error": "missing personal_number"}), 400

    # 1) start BankID
    try:
        r = requests.post(
            f"{PORTAL_BASE}/portal/api/bankid/start",
            json={"personal_number": personal_number},
            timeout=10
        )
        jr = r.json()
        if r.status_code != 200 or not jr.get("ok"):
            return jsonify({"ok": False, "error": "start_failed", "details": jr}), 502
        order_ref = jr["orderRef"]
    except Exception as e:
        return jsonify({"ok": False, "error": f"start_exception: {e}"}), 502

    # 2) poll status (DEMO completes in ~6s in your bankid.py)
    status = "pending"
    for _ in range(12):  # ~24s max
        time.sleep(2)
        try:
            s = requests.get(
                f"{PORTAL_BASE}/portal/api/bankid/status",
                params={"orderRef": order_ref},
                timeout=10
            ).json()
            status = s.get("status", "failed")
            if status in ("complete", "failed"):
                break
        except Exception:
            pass

    if status != "complete":
        return jsonify({"ok": False, "status": status}), 200

    # 3) mark session verified
    SESSION.setdefault(conv_id, {"slots": {}, "verified": False, "last_tool": None, "created_booking": False})
    SESSION[conv_id]["verified"] = True
    return jsonify({"ok": True, "conv_id": conv_id, "verified": True})

if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=PORT)