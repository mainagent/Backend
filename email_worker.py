# email_worker.py
import time, traceback
from portal import send_email_html
from email_outbox import fetch_and_mark_sending, _backoff_sec, _conn

def worker_loop():
    print("[EMAIL_WORKER] started")
    while True:
        try:
            job = fetch_and_mark_sending()
            if not job:
                time.sleep(3)
                continue

            row_id, to_email, subject, html, attempt, idem_key = job
            print(f"[EMAIL_WORKER] sending id={row_id} to={to_email} attempt={attempt+1}")

            try:
                send_email_html(to_email, subject, html)
                with _conn() as c:
                    c.execute(
                        "UPDATE email_outbox SET status='sent', updated_at=? WHERE id=?",
                        (int(time.time()), row_id),
                    )
                print(f"[EMAIL_WORKER] success id={row_id}")

            except Exception as e:
                print(f"[EMAIL_WORKER] failed id={row_id}: {e}")
                tb = traceback.format_exc()
                delay = _backoff_sec(attempt + 1)
                next_time = int(time.time() + delay)
                with _conn() as c:
                    c.execute(
                        "UPDATE email_outbox SET status='error', attempt=attempt+1, "
                        "last_error=?, next_attempt_at=?, updated_at=? WHERE id=?",
                        (tb, next_time, int(time.time()), row_id),
                    )

        except Exception as loop_err:
            print(f"[EMAIL_WORKER] loop error: {loop_err}")
            time.sleep(5)

if __name__ == "__main__":
    worker_loop()