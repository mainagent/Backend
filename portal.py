# portal.py
import os, sqlite3, smtplib
from datetime import datetime
from flask import Blueprint, request, jsonify
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

bp = Blueprint("portal", __name__)

PORTAL_API_KEY = os.getenv("PORTAL_API_KEY", "change-me")
DB_PATH = os.getenv("BOOKINGS_DB_PATH", "bookings.db")

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
    with _db() as cx:
        cur = cx.execute("""
        INSERT INTO bookings (clinic, appointment_id, name, email, phone, date, time, treatment, status, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            clinic,
            data.get("appointment_id"),
            data.get("name"),
            data.get("email"),
            data.get("phone"),
            data.get("date"),
            data.get("time"),
            data.get("treatment"),
            data.get("status", "pending"),
            data.get("notes"),
            datetime.utcnow().isoformat(timespec="seconds") + "Z",
        ))
        cx.commit()
        return cur.lastrowid

def list_bookings(clinic: str, status: str | None = None, limit: int = 100, offset: int = 0):
    q = "SELECT * FROM bookings WHERE clinic=?"
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

# --- Email helper (HTML) — moved here ---
def send_email_html(to: str, subject: str, html: str):
    sender_email = os.getenv("EMAIL_USER")
    sender_name  = os.getenv("EMAIL_FROM_NAME", "Tandläkarkliniken")
    if not (sender_email and to):
        raise ValueError("Missing EMAIL_USER or recipient")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = formataddr((sender_name, sender_email))
    msg["To"]      = to
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(os.getenv("EMAIL_USER"), os.getenv("EMAIL_PASS"))
        server.sendmail(sender_email, [to], msg.as_string())

def require_portal_key(req) -> bool:
    key = req.headers.get("X-Portal-Key", "")
    return key and key == PORTAL_API_KEY

# --- Routes ---
@bp.get("/portal/api/bookings")
def portal_list_bookings():
    if not require_portal_key(request):
        return jsonify({"error": "forbidden"}), 403
    clinic = (request.args.get("clinic") or os.getenv("CLINIC", "default")).strip()
    status = request.args.get("status")
    try:
        limit = int(request.args.get("limit", "100"))
        offset = int(request.args.get("offset", "0"))
    except ValueError:
        limit, offset = 100, 0
    return jsonify({
        "clinic": clinic,
        "items": list_bookings(clinic, status=status, limit=limit, offset=offset)
    })

@bp.get("/health")
def health():
    return jsonify({"status": "ok"})

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
    data = request.get_json(force=True) or {}
    clinic = (request.args.get("clinic") or os.getenv("CLINIC", "default")).strip()

    # --- validation ---
    if not data.get("name"):
        return jsonify({"error": "missing name"}), 400
    if not (data.get("email") or data.get("phone")):
        return jsonify({"error": "need at least email or phone"}), 400
    if not (data.get("date") and data.get("time")):
        return jsonify({"error": "missing date/time"}), 400
    
    # Store booking
    booking_id = store_booking(clinic, data)

    # Send confirmation email if email present
    email_sent = False
    to_email = (data.get("email") or "").strip()
    if to_email:
        try:
            html = f"""
            <!doctype html>
            <html>
              <body style="font-family:-apple-system, Segoe UI, Roboto, Arial; background:#f8fafc; padding:24px;">
                <table width="100%" cellpadding="0" cellspacing="0" style="max-width:560px; margin:auto; background:white; border-radius:12px; box-shadow:0 1px 6px rgba(0,0,0,0.06);">
                  <tr>
                    <td style="padding:24px 28px;">
                      <h2 style="margin:0 0 12px 0; font-size:20px; color:#0f172a;">Bokningsbekräftelse</h2>
                      <p style="margin:0 0 16px 0; color:#334155;">
                        Hej <strong>{data.get('name','')}</strong>! Din tid är bokad.
                      </p>
                      <table cellpadding="0" cellspacing="0" style="width:100%; background:#f1f5f9; border-radius:8px; padding:12px;">
                        <tr><td><strong>Klinik:</strong> {clinic.capitalize()}</td></tr>
                        <tr><td><strong>Behandling:</strong> {data.get('treatment') or '—'}</td></tr>
                        <tr><td><strong>Datum:</strong> {data.get('date')}</td></tr>
                        <tr><td><strong>Tid:</strong> {data.get('time')}</td></tr>
                        <tr><td><strong>Boknings-ID:</strong> {booking_id}</td></tr>
                      </table>
                      <p style="margin:16px 0 0 0; color:#475569;">Behöver du omboka? Svara på detta mail.</p>
                    </td>
                  </tr>
                </table>
              </body>
            </html>
            """
            send_email_html(to_email, "Bokningsbekräftelse", html)
            email_sent = True
        except Exception as e:
            print(f"[EMAIL] failed to send confirmation: {e}")

    return jsonify({"ok": True, "id": booking_id, "clinic": clinic, "email_sent": email_sent})

# expose entrypoint for app.py
def init_portal(app):
    init_db()
    app.register_blueprint(bp)