# email_outbox.py
import os, sqlite3, time, random, threading, traceback
from typing import Optional, Tuple, Any, Dict
from portal import send_email_html

DB_PATH = os.getenv("DB_PATH", "bookings.db")
MAX_ATTEMPTS = int(os.getenv("EMAIL_MAX_ATTEMPTS", "10"))
POLL_SEC = float(os.getenv("EMAIL_WORKER_POLL_SEC", "2.0"))

DDL = """
CREATE TABLE IF NOT EXISTS email_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  to_email TEXT NOT NULL,
  subject TEXT NOT NULL,
  html TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',            -- pending|sending|sent|error
  attempt INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  next_attempt_at INTEGER NOT NULL DEFAULT 0,
  conv_id TEXT,
  idem_key TEXT,                                     -- used to avoid duplicates
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_next ON email_outbox(status, next_attempt_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_outbox_idem ON email_outbox(idem_key) WHERE idem_key IS NOT NULL;
"""

def _conn():
    # sqlite is fine; Railway already uses bookings.db in your app
    return sqlite3.connect(DB_PATH, timeout=30)

def ensure_outbox_schema():
    with _conn() as c:
        c.executescript(DDL)

def enqueue_email(to_email: str, subject: str, html: str, conv_id: str, idem_key: str | None = None) -> int:
    """Insert a pending email (or no-op if same idem_key already exists)."""
    now = int(time.time())
    with _conn() as c:
        try:
            c.execute(
                "INSERT INTO email_outbox(to_email,subject,html,status,attempt,next_attempt_at,conv_id,idem_key,created_at,updated_at) "
                "VALUES(?,?,?,?,0,?,?,?, ?, ?)",
                (to_email, subject, html, 'pending', now, conv_id, idem_key, now, now)
            )
            row_id = c.lastrowid
        except sqlite3.IntegrityError:
            # duplicate idem_key → fetch existing row id
            row = c.execute("SELECT id FROM email_outbox WHERE idem_key=?", (idem_key,)).fetchone()
            row_id = row[0] if row else -1
    print(f"[OUTBOX] queued id={row_id} to={to_email} idem={idem_key}", flush=True)
    return row_id

def _backoff_sec(attempt: int) -> float:
    base = min(300, 2 ** max(0, attempt - 1))   # cap 5 min
    return base + random.uniform(0, 0.3 * base) # jitter

def fetch_and_mark_sending() -> Optional[Tuple[int, str, str, str, int, str | None]]:
    """Pick one due job and mark it 'sending' so only one thread handles it."""
    now = int(time.time())
    with _conn() as c:
        row = c.execute(
            "SELECT id,to_email,subject,html,attempt,idem_key FROM email_outbox "
            "WHERE status IN ('pending','error') AND next_attempt_at <= ? "
            "ORDER BY id LIMIT 1",
            (now,)
        ).fetchone()
        if not row:
            return None
        row_id = row[0]
        cur = c.execute(
            "UPDATE email_outbox SET status='sending', updated_at=? "
            "WHERE id=? AND status IN ('pending','error')",
            (now, row_id)
        )
        if cur.rowcount != 1:
            return None
        return row  # (id, to, subject, html, attempt, idem_key)

def mark_sent(row_id: int):
    now = int(time.time())
    with _conn() as c:
        c.execute(
            "UPDATE email_outbox SET status='sent', updated_at=? WHERE id=?",
            (now, row_id)
        )

def mark_error(row_id: int, attempt: int, err: str):
    now = int(time.time())
    next_at = now + int(_backoff_sec(attempt))
    with _conn() as c:
        c.execute(
            "UPDATE email_outbox SET status='error', attempt=?, last_error=?, next_attempt_at=?, updated_at=? WHERE id=?",
            (attempt, err[:1000], next_at, now, row_id)
        )

def worker_loop(stop_event: threading.Event):
    print("[OUTBOX] worker started", flush=True)
    while not stop_event.is_set():
        try:
            job = fetch_and_mark_sending()
            if not job:
                time.sleep(POLL_SEC)
                continue
            row_id, to_email, subject, html, attempt, idem_key = job
            try:
                send_email_html(to_email, subject, html)  # your existing sender
                mark_sent(row_id)
                print(f"[OUTBOX] sent id={row_id} to={to_email}", flush=True)
            except Exception as e:
                attempt += 1
                if attempt >= MAX_ATTEMPTS:
                    # stop retrying
                    now = int(time.time())
                    err = f"{type(e).__name__}: {e}"
                    with _conn() as c:
                        c.execute(
                            "UPDATE email_outbox SET status='error', attempt=?, last_error=?, updated_at=? WHERE id=?",
                            (attempt, err[:1000], now, row_id)
                        )
                    print(f"[OUTBOX] permanent error id={row_id}: {err}", flush=True)
                else:
                    err = f"{type(e).__name__}: {e}"
                    mark_error(row_id, attempt, err)
                    print(f"[OUTBOX] retry scheduled id={row_id} attempt={attempt} err={err}", flush=True)
        except Exception as e:
            print(f"[OUTBOX] loop error: {e}\n{traceback.format_exc()}", flush=True)
            time.sleep(1.0)

_worker_stop = None
_worker_thread = None

def start_outbox_worker():
    global _worker_stop, _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _worker_stop = threading.Event()
    _worker_thread = threading.Thread(target=worker_loop, args=(_worker_stop,), daemon=True)
    _worker_thread.start()