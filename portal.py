# portal.py
import os, sqlite3, smtplib, ssl, requests
import threading
from datetime import datetime
from flask import Blueprint, request, jsonify
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

bp = Blueprint("portal", __name__)

PORTAL_API_KEY = os.getenv("PORTAL_API_KEY", "change-me")
DB_PATH        = os.getenv("BOOKINGS_DB_PATH", "bookings.db")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
REPLY_TO       = os.getenv("REPLY_TO", "")

# --- DB helpers ---
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with _db() as cx:
        cx.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clinic TEXT NOT NULL,
            appointment_id TEXT,
            name TEXT,
            email TEXT,
            phone TEXT,
            date TEXT,
            time TEXT,
            treatment TEXT,
            status TEXT DEFAULT 'pending',
            notes TEXT,
            created_at TEXT NOT NULL
        )
        """)
        cx.commit()
    print("✅ bookings.db ready at", DB_PATH)

def store_booking(clinic: str, data: dict) -> int:
    """
    Insert booking and immediately persist a 4-digit appointment_id (e.g. '0007').
    Returns the new booking id.
    """
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with _db() as cx:
        cur = cx.execute("""
            INSERT INTO bookings (
                clinic, appointment_id, name, email, phone, date, time, treatment, status, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            clinic,
            data.get("appointment_id"),           # allow override if provided
            data.get("name"),
            data.get("email"),
            data.get("phone"),
            data.get("date"),
            data.get("time"),
            data.get("treatment"),
            data.get("status", "pending"),
            data.get("notes"),
            now,
        ))
        booking_id = cur.lastrowid

        # Ensure a 4-digit public code is stored
        code = f"{booking_id:04d}"
        cx.execute("UPDATE bookings SET appointment_id=? WHERE id=?", (code, booking_id))
        cx.commit()
        return booking_id

def list_bookings(clinic: str, status: str | None = None, limit: int = 100, offset: int = 0):
    q = "SELECT * FROM bookings WHERE LOWER(clinic)=LOWER(?)"
    args = [clinic]
    if status:
        q += " AND status=?"
        args.append(status)
    q += " ORDER BY id DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    with _db() as cx:
        rows = cx.execute(q, args).fetchall()
        return [dict(r) for r in rows]

def get_booking(booking_id: int):
    with _db() as cx:
        row = cx.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        return dict(row) if row else None

def update_booking_status(booking_id: int, status: str):
    with _db() as cx:
        cx.execute("UPDATE bookings SET status=? WHERE id=?", (status, booking_id))
        cx.commit()

def reschedule_booking(booking_id: int, date: str, time: str):
    with _db() as cx:
        cx.execute("UPDATE bookings SET date=?, time=? WHERE id=?", (date, time, booking_id))
        cx.commit()

# --- Email helper (HTML) ---
def send_email_html(to: str, subject: str, html: str, reply_to: str | None = None) -> bool:
    """
    Sends HTML email via Resend if RESEND_API_KEY is set.
    Falls back to Gmail SMTP if EMAIL_USER/EMAIL_PASS are set.
    Returns True on success, False otherwise.
    """
    to = (to or "").strip()
    if "@" not in to:
        print(f"[EMAIL] invalid recipient: {to!r}")
        return False

    RESEND_API_KEY  = os.getenv("RESEND_API_KEY", "")
    EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "Tandläkarkliniken")
    EMAIL_USER      = os.getenv("EMAIL_USER", "onboarding@resend.dev")
    EMAIL_PASS      = os.getenv("EMAIL_PASS", "")

    # Try Resend first
    if RESEND_API_KEY:
        try:
            payload = {
                "from":   f"{EMAIL_FROM_NAME} <{EMAIL_USER}>",
                "to":     [to],
                "subject": subject,
                "html":    html,
            }
            if reply_to:
                payload["reply_to"] = reply_to

            r = requests.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=20,
            )
            print(f"[EMAIL/RESEND] status={r.status_code} body={r.text[:300]}")
            r.raise_for_status()
            return True
        except Exception as e:
            print(f"[EMAIL/RESEND] failed: {e}")

    # SMTP fallback
    if EMAIL_USER and EMAIL_PASS:
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = formataddr((EMAIL_FROM_NAME, EMAIL_USER))
            msg["To"]      = to
            if reply_to:
                msg["Reply-To"] = reply_to
            msg.attach(MIMEText(html, "html", "utf-8"))

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(EMAIL_USER, EMAIL_PASS)
                server.sendmail(EMAIL_USER, [to], msg.as_string())

            print("[EMAIL/SMTP] sent via SMTP fallback")
            return True
        except Exception as e:
            print(f"[EMAIL/SMTP] failed: {e}")

    print("[EMAIL] no provider succeeded (Resend missing/failed and SMTP missing/failed).")
    return False

# --- availability helpers ---
def _to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":"); return int(h)*60 + int(m)

def is_slot_free(clinic: str, date: str, time: str, duration_min: int = 30) -> bool:
    """True if no overlapping non-cancelled bookings in [time, time+duration)."""
    start = _to_minutes(time); end = start + duration_min
    with _db() as cx:
        rows = cx.execute("""
            SELECT time, treatment, status FROM bookings
            WHERE LOWER(clinic)=LOWER(?) AND date=? AND status!='cancelled'
        """, (clinic, date)).fetchall()
    for r in rows:
        s = _to_minutes(r["time"]); e = s + 30  # assume 30-min blocks for existing rows
        # overlap if start < e and s < end
        if start < e and s < end:
            return False
    return True

# --- Send confirmation in background ---
def _send_confirmation_async(to_email: str, clinic: str, booking_id: int, data: dict):
    try:
        code = f"{booking_id:04d}"
        html = f"""
        <!doctype html><html><body style="font-family:-apple-system, Segoe UI, Roboto, Arial; background:#f8fafc; padding:24px;">
          <table width="100%" cellpadding="0" cellspacing="0" style="max-width:560px; margin:auto; background:white; border-radius:12px; box-shadow:0 1px 6px rgba(0,0,0,0.06);">
            <tr><td style="padding:24px 28px;">
              <h2 style="margin:0 0 12px 0; font-size:20px; color:#0f172a;">Bokningsbekräftelse</h2>
              <p style="margin:0 0 16px 0; color:#334155;">Hej <strong>{data.get('name','')}</strong>! Din tid är bokad.</p>
              <table cellpadding="0" cellspacing="0" style="width:100%; background:#f1f5f9; border-radius:8px; padding:12px;">
                <tr><td><strong>Klinik:</strong> {clinic.capitalize()}</td></tr>
                <tr><td><strong>Behandling:</strong> {data.get('treatment') or '—'}</td></tr>
                <tr><td><strong>Datum:</strong> {data.get('date')}</td></tr>
                <tr><td><strong>Tid:</strong> {data.get('time')}</td></tr>
                <tr><td><strong>Boknings-ID:</strong> {code}</td></tr>
              </table>
            </td></tr>
          </table>
        </body></html>
        """
        print(f"[EMAIL] sending to {to_email} for booking {booking_id}…")
        send_email_html(to_email, "Bokningsbekräftelse", html)
        print(f"[EMAIL] sent ok for booking {booking_id}")
    except Exception as e:
        print(f"[EMAIL] failed for booking {booking_id}: {e}")

def require_portal_key(req) -> bool:
    key = req.headers.get("X-Portal-Key", "")
    return key and key == PORTAL_API_KEY

# --- Routes ---
# --- availability helpers ---
def _to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":"); return int(h)*60 + int(m)

def is_slot_free(clinic: str, date: str, time: str, duration_min: int = 30) -> bool:
    """True if no overlapping non-cancelled bookings in [time, time+duration)."""
    start = _to_minutes(time); end = start + duration_min
    with _db() as cx:
        rows = cx.execute("""
            SELECT time, treatment, status FROM bookings
            WHERE LOWER(clinic)=LOWER(?) AND date=? AND status!='cancelled'
        """, (clinic, date)).fetchall()
    for r in rows:
        s = _to_minutes(r["time"]); e = s + 30  # assume 30-min blocks for existing rows
        # overlap if start < e and s < end
        if start < e and s < end:
            return False
    return True
def list_free_slots(clinic: str, date: str, open_time="09:00", close_time="17:00",
                    step_min=30, duration_min=30, limit=8):
    open_m  = _to_minutes(open_time)
    close_m = _to_minutes(close_time)
    slots = []
    for m in range(open_m, close_m, step_min):
        hh = f"{m//60:02d}:{m%60:02d}"
        if is_slot_free(clinic, date, hh, duration_min):
            slots.append(hh)
            if len(slots) >= limit:
                break
    return slots

@bp.route("/portal/api/availability/check", methods=["GET"])
def availability_check():
    clinic = (request.args.get("clinic") or os.getenv("CLINIC","default")).strip().lower()
    date   = request.args.get("date") or ""
    time   = request.args.get("time") or ""
    dur    = int(request.args.get("duration") or 30)
    if not (date and time):
        return jsonify({"ok": False, "error":"missing date/time"}), 400
    free = is_slot_free(clinic, date, time, dur)
    return jsonify({"ok": True, "free": free, "clinic": clinic, "date": date, "time": time, "duration": dur})

@bp.route("/portal/api/availability/suggest", methods=["GET"])
def availability_suggest():
    clinic = (request.args.get("clinic") or os.getenv("CLINIC","default")).strip().lower()
    date   = request.args.get("date") or ""
    treat  = (request.args.get("treatment") or "").strip().lower()
    # basic duration mapping per treatment (adjust to your clinic)
    dur_map = {"undersökning": 30, "akut": 30, "hygienist": 45, "blekning": 60}
    duration = dur_map.get(treat, 30)
    # optionally specialist mapping later (see below)
    slots = list_free_slots(clinic, date, duration_min=duration, limit=8)
    return jsonify({"ok": True, "clinic": clinic, "date": date, "treatment": treat, "duration": duration, "slots": slots})

@bp.get("/portal/api/bookings")
def portal_list_bookings():
    if not require_portal_key(request):
        return jsonify({"error": "forbidden"}), 403

    # normalize to lowercase so UI 'mathias' / 'Mathias' works the same
    clinic = (request.args.get("clinic") or os.getenv("CLINIC", "default")).strip().lower()
    status = request.args.get("status")

    try:
        limit = int(request.args.get("limit", "100"))
        offset = int(request.args.get("offset", "0"))
    except ValueError:
        limit, offset = 100, 0

    items = list_bookings(clinic, status=status, limit=limit, offset=offset)
    return jsonify({"clinic": clinic, "items": items})

@bp.get("/health")
def health():
    return jsonify({"status": "ok"})

@bp.post("/portal/api/bookings/resend")
def portal_resend():
    if not require_portal_key(request):
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json(force=True) or {}
    clinic = (request.args.get("clinic") or os.getenv("CLINIC", "default")).strip().lower()

    # Allow either booking_id (preferred) OR name+date+time
    booking_id = data.get("booking_id") or data.get("id") or data.get("appointment_id")
    b = None

    if booking_id:
        try:
            b = get_booking(int(str(booking_id)))
        except Exception:
            return jsonify({"error": "invalid booking_id"}), 400
        if not b:
            return jsonify({"error": "not_found"}), 404
    else:
        name = (data.get("name") or "").strip()
        date = (data.get("date") or "").strip()
        time = (data.get("time") or "").strip()
        if not (name and date and time):
            return jsonify({"error": "need booking_id OR name+date+time"}), 400
        with _db() as cx:
            rows = cx.execute("""
                SELECT * FROM bookings
                WHERE LOWER(clinic)=LOWER(?) AND LOWER(name)=LOWER(?) AND date=? AND time=?
                ORDER BY id DESC
            """, (clinic, name, date, time)).fetchall()
            if not rows:
                return jsonify({"error": "not_found"}), 404
            if len(rows) > 1:
                return jsonify({"error": "multiple_matches_provide_id"}), 409
            b = dict(rows[0])

    to_email = (b.get("email") or "").strip()
    if not to_email:
        return jsonify({"error": "no_email_on_booking"}), 422

    _send_confirmation_async(to_email, clinic, b["id"], b)
    return jsonify({"ok": True, "id": b["id"], "email": to_email})

@bp.get("/portal/api/bookings/<int:booking_id>")
def portal_get_booking(booking_id: int):
    if not require_portal_key(request):
        return jsonify({"error": "forbidden"}), 403
    b = get_booking(booking_id)
    if not b:
        return jsonify({"error": "not_found"}), 404
    return jsonify(b)

@bp.post("/portal/api/bookings/<int:booking_id>/status")
def portal_set_status(booking_id: int):
    if not require_portal_key(request):
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(force=True) or {}
    status = (data.get("status") or "").lower().strip()
    if status not in {"pending", "confirmed", "cancelled"}:
        return jsonify({"error": "invalid_status"}), 400
    if not get_booking(booking_id):
        return jsonify({"error": "not_found"}), 404
    update_booking_status(booking_id, status)
    return jsonify({"ok": True, "id": booking_id, "status": status})

@bp.post("/portal/api/bookings/<int:booking_id>/reschedule")
def portal_reschedule(booking_id: int):
    if not require_portal_key(request):
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(force=True) or {}
    date = (data.get("date") or "").strip()
    time = (data.get("time") or "").strip()
    if not (date and time):
        return jsonify({"error": "missing date/time"}), 400
    if not get_booking(booking_id):
        return jsonify({"error": "not_found"}), 404
    reschedule_booking(booking_id, date, time)
    return jsonify({"ok": True, "id": booking_id, "date": date, "time": time})

@bp.post("/portal/api/bookings/new")
def portal_create_booking():
    if not require_portal_key(request):
        return jsonify({"error": "forbidden"}), 403
    
    # Require 'X-Verified' header for safety (set to 'true' only after BankID)
    verified_hdr = (request.headers.get("X-Verified") or "").lower().strip()
    if verified_hdr != "true":
        return jsonify({"error": "verification_required"}), 403

    data = request.get_json(force=True) or {}
    clinic = (request.args.get("clinic") or os.getenv("CLINIC", "default")).strip().lower()

    print(f"[PORTAL] /bookings/new hit. clinic={clinic}")
    print(f"[PORTAL] payload={data}")

    # --- validation (email REQUIRED, phone optional) ---
    if not data.get("name"):
        return jsonify({"error": "missing name"}), 400
    if not data.get("email"):
        return jsonify({"error": "missing email"}), 400
    if "@" not in data["email"]:
        return jsonify({"error": "invalid email"}), 400
    if not (data.get("date") and data.get("time")):
        return jsonify({"error": "missing date/time"}), 400

    if not is_slot_free(clinic, data["date"], data["time"], 30):
        return jsonify({"ok": False, "error": "slot_taken"}), 409
    # Store booking
    booking_id = store_booking(clinic, data)
    print(f"[PORTAL] stored booking_id={booking_id}")

    # persist a 4-digit public code
    code = f"{booking_id:04d}"
    with _db() as cx:
        cx.execute("UPDATE bookings SET appointment_id=? WHERE id=?", (code, booking_id))
        cx.commit()
    print(f"[PORTAL] appointment_id={code} persisted")

    # queue confirmation email (non-blocking)
    to_email = (data.get("email") or "").strip()
    email_queued = False
    if to_email:
        threading.Thread(
            target=_send_confirmation_async,
            args=(to_email, clinic, booking_id, data),
            daemon=True
        ).start()
        email_queued = True

    return jsonify({"ok": True, "id": booking_id, "clinic": clinic, "email_queued": email_queued})

# expose entrypoint for app.py
def init_portal(app):
    init_db()
    app.register_blueprint(bp)
