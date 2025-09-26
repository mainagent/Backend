# hair.py
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Tuple
from difflib import get_close_matches
from flask import Blueprint, request, jsonify
from utils_cleanup import normalize_spelled_email, validate_email
from portal import send_email_html
from threading import Thread
from sms_providers import get_sms_client
import re
import os, time, random
import requests


bp_hair = Blueprint("hair", __name__, url_prefix="/hair/api")
SMS = get_sms_client("hair")
# -----------------------------
# Shared models (simple)
# -----------------------------
@dataclass
class Customer:
    id: str
    name: str
    email: str
    phone: str

# -----------------------------
# Adapter interface
# -----------------------------
class HairAdapter:
    def list_services(self, salon_id: int) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def check_availability(self, salon_id: int, service_id: int, date_iso: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def create_booking(self, salon_id: int, customer: Customer, service_id: int, time_id: int, notes: str = "") -> Dict[str, Any]:
        raise NotImplementedError

    def cancel_booking(self, salon_id: int, booking_id: int) -> Dict[str, Any]:
        raise NotImplementedError

    def get_bookings(self, customer_id: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

# -----------------------------
# Mock adapter (demo-only)
# -----------------------------
class MockHairAdapter(HairAdapter):
    def __init__(self):
        # fake service catalog
        self.services = [
            {"id": 298, "name": "Klippning rek. Långt och tjockt hår", "duration_mins": 50},
            {"id": 301, "name": "Klippning kort hår", "duration_mins": 30},
        ]
        # fake availabilities per (salon_id, service_id, date)
        self.avails = {}  # key: (salon, service, date) -> list of {time_id, start, end}
        # fake bookings
        self.bookings = {}  # booking_id -> dict

    def list_services(self, salon_id: int) -> List[Dict[str, Any]]:
        return self.services

    def _seed_avails(self, salon_id: int, service_id: int, date_iso: str):
        key = (salon_id, service_id, date_iso)
        if key in self.avails:
            return
        base_ts = int(time.time()) + 3600  # start one hour from now
        slots = []
        for i in range(6):
            start_ts = base_ts + i * 3600
            slots.append({
                "time_id": 4700000 + i,  # fake time_id
                "start": time.strftime("%Y-%m-%dT%H:%M:00", time.localtime(start_ts)),
                "end":   time.strftime("%Y-%m-%dT%H:%M:00", time.localtime(start_ts + 50*60)),
            })
        self.avails[key] = slots

    def check_availability(self, salon_id: int, service_id: int, date_iso: str) -> List[Dict[str, Any]]:
        self._seed_avails(salon_id, service_id, date_iso)
        return self.avails[(salon_id, service_id, date_iso)]

    def create_booking(self, salon_id: int, customer: Customer, service_id: int, time_id: int, notes: str = "") -> Dict[str, Any]:
        # find matching slot
        key_candidates = [k for k in self.avails.keys() if k[0]==salon_id and k[1]==service_id]
        slot = None
        for k in key_candidates:
            for s in self.avails[k]:
                if s["time_id"] == time_id:
                    slot = s; break
        if not slot:
            return {"ok": False, "error": "time_not_available"}

        booking_id = random.randint(100000, 999999)
        record = {
            "id": booking_id,
            "salon_id": salon_id,
            "customer": asdict(customer),
            "service_id": service_id,
            "service_name": next((x["name"] for x in self.services if x["id"]==service_id), "Unknown"),
            "start": slot["start"],
            "end": slot["end"],
            "notes": notes,
        }
        self.bookings[booking_id] = record
        return {"ok": True, "booking": record}

    def cancel_booking(self, salon_id: int, booking_id: int) -> Dict[str, Any]:
        if booking_id in self.bookings:
            rec = self.bookings.pop(booking_id)
            return {"ok": True, "canceled": rec}
        return {"ok": False, "error": "not_found"}

    def get_bookings(self, customer_id: str) -> List[Dict[str, Any]]:
        return [v for v in self.bookings.values() if v["customer"]["id"] == customer_id]

# -----------------------------
# Nikita adapter (skeleton)
# -----------------------------
class NikitaHairAdapter(HairAdapter):
    def __init__(self, base_url: str):
        # Example: https://nikitahair.se/timebestilling/api
        self.base = base_url.rstrip("/")
        self.session = requests.Session()

    def list_services(self, salon_id: int) -> List[Dict[str, Any]]:
        raise NotImplementedError("Wire this when partner provides API details.")

    def check_availability(self, salon_id: int, service_id: int, date_iso: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def create_booking(self, salon_id: int, customer: Customer, service_id: int, time_id: int, notes: str = "") -> Dict[str, Any]:
        raise NotImplementedError

    def cancel_booking(self, salon_id: int, booking_id: int) -> Dict[str, Any]:
        raise NotImplementedError

    def get_bookings(self, customer_id: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

# -----------------------------
# pick adapter
# -----------------------------
def _build_adapter() -> HairAdapter:
    provider = (os.getenv("HAIR_PROVIDER") or "mock").lower().strip()
    if provider == "nikita":
        base = os.getenv("NIKITA_API_BASE") or "https://nikitahair.se/timebestilling/api"
        return NikitaHairAdapter(base)
    return MockHairAdapter()

ADAPTER: HairAdapter = _build_adapter()



# -----------------------------
# Flask routes
# -----------------------------
@bp_hair.get("/services")
def hair_services():
    salon_id = int(request.args.get("salon_id", "97"))
    return jsonify({"ok": True, "services": ADAPTER.list_services(salon_id)})

@bp_hair.get("/availability")
def hair_availability():
    salon_id = int(request.args.get("salon_id", "97"))
    service_id = int(request.args.get("service_id", "298"))
    date_iso = request.args.get("date", "") or time.strftime("%Y-%m-%d")
    slots = ADAPTER.check_availability(salon_id, service_id, date_iso)
    return jsonify({"ok": True, "slots": slots})

@bp_hair.post("/book")
def hair_book():
    data = request.get_json(force=True) or {}
    salon_id   = int(data.get("salon_id", 97))
    service_id = int(data.get("service_id", 298))
    time_id    = int(data.get("time_id", 4700000))
    notes      = data.get("notes", "")

    customer = Customer(
        id   = str(data.get("customer_id", "demo-1")),
        name = data.get("name", "Demo User"),
        email= data.get("email", "demo@example.com"),
        phone= data.get("phone", "0700000000"),
    )
    result = ADAPTER.create_booking(salon_id, customer, service_id, time_id, notes)
    return jsonify(result), (200 if result.get("ok") else 400)

@bp_hair.post("/cancel")
def hair_cancel():
    data = request.get_json(force=True) or {}
    salon_id   = int(data.get("salon_id", 97))
    booking_id = int(data.get("booking_id", 0))
    result = ADAPTER.cancel_booking(salon_id, booking_id)
    return jsonify(result), (200 if result.get("ok") else 404)

@bp_hair.get("/bookings")
def hair_bookings():
    customer_id = request.args.get("customer_id", "demo-1")
    items = ADAPTER.get_bookings(customer_id)
    return jsonify({"ok": True, "items": items})

# -----------------------------
# Simple conversational slot filling for hair
# -----------------------------
SESSION: dict[str, dict] = {}  # conv_id -> {"slots": {...}}

RE_TIME = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")  # HH:MM
RE_PHONE = re.compile(r'(?:\+?46|0)\s*(?:\d[\s-]*){8,10}')
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

def _state(cid: str) -> dict:
    s = SESSION.setdefault(cid, {"slots": {}, "last_prompt": 0})
    s.setdefault("slots", {})
    return s

def _set(cid: str, k: str, v):
    _state(cid)["slots"][k] = v

def _g(cid: str, k: str, d=None):
    return _state(cid)["slots"].get(k, d)

def _pick_service_id(salon_id: int, text: str) -> tuple[int | None, str | None]:
    t = (text or "").lower()

    # quick intent rules for Swedish haircuts
    if any(w in t for w in ["lång", "långt", "tjockt", "tjock"]):
        return 298, "Klippning rek. Långt och tjockt hår"

    if "kort" in t:
        return 301, "Klippning kort hår"

    if any(w in t for w in ["klipp", "klippa", "klippa mig", "klippning"]):
        # default to the long/thick recommendation if not specified
        return 298, "Klippning rek. Långt och tjockt hår"

    # fallback: fuzzy against catalog
    services = ADAPTER.list_services(salon_id)
    names = [s["name"].lower() for s in services]
    match = get_close_matches(t, names, n=1, cutoff=0.4)
    if match:
        i = names.index(match[0])
        return services[i]["id"], services[i]["name"]
    return None, None

def _extract_name(text: str) -> str|None:
    # very simple: catch “jag heter X” or just first capitalized word(s)
    m = re.search(r"\bjag\s+heter\s+([a-öA-Ö][^\d,\.]+)$", text.strip(), re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # fallback: two capitalized tokens
    m2 = re.findall(r"\b[A-Ö][a-ö]+(?:\s+[A-Ö][a-ö]+)?", text)
    if m2:
        return m2[0].strip()
    return None

def _extract_phone(text: str) -> str | None:
    # First try a tolerant pattern anywhere in the sentence
    m = RE_PHONE.search(text)
    if not m:
        # Brutal fallback: take all digits from the utterance and infer
        digits_all = re.sub(r'\D', '', text)
        if len(digits_all) < 9:
            return None
        # Prefer last 9–10 digits (common when user says stuff around it)
        if digits_all.startswith('0046'):
            return f"+46{digits_all[4:]}"
        if digits_all.startswith('46'):
            return f"+{digits_all}"
        if digits_all.startswith('0'):
            return f"+46{digits_all[1:]}"
        # final fallback: assume last 9 digits are the local part
        return f"+46{digits_all[-9:]}"

    raw = m.group(0)
    digits = re.sub(r'\D', '', raw)

    # Normalize to E.164
    if digits.startswith('0046'):
        return f"+46{digits[4:]}"
    if digits.startswith('46'):
        return f"+{digits}"
    if digits.startswith('0'):
        return f"+46{digits[1:]}"
    if digits.startswith('46'):
        return f"+{digits}"
    return f"+46{digits[-9:]}"

def _parse_email(text: str) -> str | None:
    """
    Robust email extractor:
    - normalizes Swedish 'snabel a', 'punkt', etc.
    - finds the first email-looking token
    - strips trailing punctuation/quotes
    - lowercases and validates
    """
    raw = (text or "")

    # 1) normalize common Swedish variants (lightweight, local to hair.py)
    t = raw.lower()
    repl = {
        "snabel-a": "@", "snabel a": "@", "snabela": "@",
        "snabel_ a": "@", "snabel@": "@",
        " at ": "@",           # some people say "name at domain"
        " punkt ": ".", " dot ": ".",
        " punktcom": ".com", " punkt com": ".com",
        " punkt se": ".se", " punktse": ".se",
        " punkt nu": ".nu", " punkt nu": ".nu",
    }
    for k, v in repl.items():
        t = t.replace(k, v)

    # remove spaces around @ and dots that often appear when spoken/spelled
    t = re.sub(r"\s*@\s*", "@", t)
    t = re.sub(r"\s*\.\s*", ".", t)

    # 2) grab first candidate
    m = re.search(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", t, re.IGNORECASE)
    if not m:
        return None
    e = m.group(0)

    # 3) strip common trailing/leading punctuation accidentally caught
    e = e.strip(" ,;:!?)\"]}<>(")

    # 4) final sanity/validation with your existing EMAIL_RE
    e = e.lower()
    return e if EMAIL_RE.match(e) else None

def _extract_booking_id(text: str) -> str | None:
    """Pick the first 3-10 digit sequence (e.g. '501484') from a sentence."""
    try:
        cleaned = text or ""
        m = re.search(r"\b(\d{3,10})\b", cleaned)
        return m.group(1) if m else None
    except Exception as e:
        print(f"[BOOKING_ID] regex error on text={text!r}: {e}")
        return None

def _cancel_intent(text: str) -> bool:
    t = (text or "").lower()
    return any(p in t for p in [
        "avboka", "avbokning", "kan du avboka",
        "ta bort min tid", "ändra min bokning", "cancel"
    ])

def _format_slots_for_prompt(slots: list[dict], max_items: int = 6) -> str:
    """
    Takes a list of {start, end, ...} and formats a short Swedish line with up to N times.
    """
    if not slots:
        return "Inga lediga tider hittades."
    times = [s.get("start", "")[11:16] for s in slots if s.get("start")]
    times = [t for t in times if t]  # clean
    if not times:
        return "Inga lediga tider hittades."
    if len(times) > max_items:
        times = times[:max_items]
    if len(times) == 1:
        return f"Jag har {times[0]}."
    return "Tillgängliga tider: " + ", ".join(times[:-1]) + f" och {times[-1]}."

def _parse_time_from_text(text: str) -> str | None:
    """
    Returns HH:MM if we can parse a time from Swedish/free text.
    Examples handled: '14', '14:30', 'klockan 15', 'halv tre', 'kvart över två', 'kvart i tre',
    'vid lunch', 'förmiddag', 'eftermiddag'.
    """
    t = (text or "").lower().strip()

    # Direct HH:MM
    m = re.search(r"\b([01]?\d|2[0-3])[:\.]([0-5]\d)\b", t)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"

    # Bare hour (e.g., "klockan 14", "14", "vid 9")
    m = re.search(r"(klockan|kl\.?|vid)?\s*\b([01]?\d|2[0-3])\b", t)
    if m:
        hh = int(m.group(2))
        return f"{hh:02d}:00"

    # Swedish phrases
    m = re.search(r"\bhalv\s+([01]?\d|2[0-3])\b", t)             # halv tre = 14:30
    if m:
        hh = (int(m.group(1)) - 1) % 24
        return f"{hh:02d}:30"

    m = re.search(r"\bkvart över\s+([01]?\d|2[0-3])\b", t)       # kvart över två = 14:15
    if m:
        hh = int(m.group(1)) % 24
        return f"{hh:02d}:15"

    m = re.search(r"\bkvart i\s+([01]?\d|2[0-3])\b", t)          # kvart i tre = 14:45
    if m:
        hh = (int(m.group(1)) - 1) % 24
        return f"{hh:02d}:45"

    # Coarse words
    if "vid lunch" in t or "lunchtid" in t:
        return "12:00"
    if "förmiddag" in t:
        return "10:00"
    if "eftermiddag" in t:
        return "15:00"

    return None

def _pick_time_id(user_text: str, slots: list[dict]) -> int | None:
    """
    Map user's spoken time to one of the live slots.
    Strategy:
      1) Parse a desired HH:MM from user_text
      2) If found, pick exact match; if none, pick nearest future slot
      3) If user says 'första / andra / tredje', map to list index
      4) If user says 'vilken som helst', pick first slot
    Returns the slot's time_id or None.
    """
    if not slots:
        return None

    # 1) explicit ordinal pick: "första", "andra", ...
    txt = (user_text or "").lower()
    ord_map = {"första": 0, "andra": 1, "tredje": 2, "fjärde": 3, "femte": 4, "sjätte": 5}
    for word, idx in ord_map.items():
        if word in txt and idx < len(slots):
            return int(slots[idx].get("time_id"))

    # 2) "vilken som helst" / "spelar ingen roll"
    if "vilken som helst" in txt or "spelar ingen roll" in txt or "ta första" in txt:
        return int(slots[0].get("time_id"))

    # 3) parse a desired time
    want = _parse_time_from_text(user_text)
    if want:
        # exact match first
        for s in slots:
            st = s.get("start", "")
            if st and st[11:16] == want:
                return int(s.get("time_id"))

        # else pick the nearest future slot by minutes distance
        def _to_minutes(hhmm: str) -> int:
            h, m = hhmm.split(":"); return int(h) * 60 + int(m)

        want_m = _to_minutes(want)
        best = None
        best_diff = None
        for s in slots:
            st = s.get("start", "")
            if not st:
                continue
            slot_m = _to_minutes(st[11:16])
            diff = slot_m - want_m
            # prefer future; if all are past, take smallest absolute
            score = diff if diff >= 0 else abs(diff) + 10000
            if best is None or score < best_diff:
                best = s; best_diff = score
        if best:
            return int(best.get("time_id"))

    return None

# -----------------------------
# Confirmation helpers + booking runner
# -----------------------------
def _yes(text: str) -> bool:
    t = (text or "").lower()
    return any(p in t for p in ["ja", "japp", "okej", "ok", "kör", "stämmer", "boka", "ja tack", "yes"])

def _no(text: str) -> bool:
    t = (text or "").lower()
    return any(p in t for p in ["nej", "inte", "ändra", "avbryt", "nej tack", "no"])

def _do_booking(cid: str, salon_id: int):
    """Create booking + send email/SMS. Returns a Flask jsonify response."""
    cust = Customer(
        id=cid,
        name=_g(cid, "name"),
        email=_g(cid, "email") or "kund@example.com",
        phone=_g(cid, "phone"),
    )

    res = ADAPTER.create_booking(
        salon_id=salon_id,
        customer=cust,
        service_id=_g(cid, "service_id"),
        time_id=_g(cid, "time_id"),
        notes=_g(cid, "notes") or "",
    )

    if not res.get("ok"):
        return jsonify({"response": "Det gick inte att boka just nu. Vill du prova en annan tid?", "ok": False})

    b = res["booking"]  # expect keys: id, start, service_name, ...
    msg = f"Klart! Jag bokade {b['service_name']} {b['start'][:10]} kl {b['start'][11:16]}. Boknings-ID: {b['id']}."

    # email (best-effort)
    # email (synchronous for now so we SEE errors/success in logs)
    to_email = _g(cid, "email")
    if to_email:
        try:
            print(f"[HAIR/EMAIL] about to send to {to_email}", flush=True)
            html = f"""
            <!doctype html>
            <html>
              <body style="font-family:-apple-system,Segoe UI,Roboto,Arial; background:#f8fafc; padding:24px;">
                <table width="100%" cellpadding="0" cellspacing="0" style="max-width:560px; margin:auto; background:white; border-radius:12px; box-shadow:0 1px 6px rgba(0,0,0,0.06);">
                  <tr><td style="padding:24px 28px;">
                    <h2 style="margin:0 0 12px 0; font-size:20px; color:#0f172a;">Bokningsbekräftelse</h2>
                    <p style="margin:0 0 12px 0; color:#334155;">
                      Hej <strong>{cust.name}</strong>! Din tid är bokad.
                    </p>
                    <table cellpadding="0" cellspacing="0" style="width:100%; background:#f1f5f9; border-radius:8px; padding:12px;">
                      <tr><td><strong>Behandling:</strong> {b['service_name']}</td></tr>
                      <tr><td><strong>Datum:</strong> {b['start'][:10]}</td></tr>
                      <tr><td><strong>Tid:</strong> {b['start'][11:16]}</td></tr>
                      <tr><td><strong>Boknings-ID:</strong> {b['id']}</td></tr>
                      <tr><td><strong>Plats:</strong> Salong {_g(cid,'salon_id')}</td></tr>
                    </table>
                    <p style="margin:12px 0 0 0; color:#64748b; font-size:12px;">
                      Om du behöver avboka, svara på detta mejl eller ring salongen.
                    </p>
                  </td></tr>
                </table>
              </body>
            </html>
            """
            # keep same signature you used for dental:
            send_email_html(to_email, "Bokningsbekräftelse – din tid är bokad", html)
            print(f"[HAIR/EMAIL] success sent to {to_email}", flush=True)
            msg += f" Jag skickade en bekräftelse till {to_email}."
        except Exception as e:
            print(f"[HAIR/EMAIL] failed sending to {to_email}: {e}", flush=True)

    # sms (best-effort)
    to_phone = _g(cid, "phone")
    if to_phone:
        def _send_sms():
            try:
                SMS.send(to_phone, f"Bokningsbekräftelse: {b['service_name']} {b['start'][:10]} kl {b['start'][11:16]} (ID: {b['id']}).")
            except Exception as e:
                print(f"[HAIR/SMS] failed: {e}")
        Thread(target=_send_sms, daemon=True).start()

    _set(cid, "booking_id", str(b["id"]))  # so “avboka” works later
    _set(cid, "awaiting_confirm", False)
    _set(cid, "done", True)
    return jsonify({"response": msg, "ok": True, "booking": b})

# -----------------------------
# MAIN DIALOG
# -----------------------------
@bp_hair.post("/process_input")
def hair_process_input():
    """
    Minimal dialog manager:
    required slots: name, phone, service_id(+service_name), date, time_id
    """
    data = request.get_json(force=True) or {}
    text = (data.get("text") or "").strip()
    cid  = (data.get("conv_id") or "local").strip()
    salon_id = int(data.get("salon_id", 97))
    date_iso = data.get("date") or time.strftime("%Y-%m-%d")
    lang = (data.get("lang") or "sv-SE").lower()

    # store salon_id for later email body use
    _set(cid, "salon_id", salon_id)

    if not text:
        return jsonify({"response": "Säg något så hjälper jag dig boka."})

    # ---- FINAL CONFIRMATION BRANCH ----
    if _g(cid, "awaiting_confirm"):
        if _yes(text):
            return _do_booking(cid, salon_id)
        if _no(text):
            _set(cid, "awaiting_confirm", False)
            slots = ADAPTER.check_availability(salon_id, _g(cid, "service_id"), date_iso)
            return jsonify({"response": "Okej, vi bokar inte den. Vilken tid passar istället? " + _format_slots_for_prompt(slots)})
        # neither yes nor no: re-ask with summary
        s = _g(cid, "service_name") or ""
        slot = _g(cid, "slot") or {}
        hhmm = (slot.get("start") or "")[11:16]
        email = _g(cid, "email") or ""
        return jsonify({"response": f"Vill du att jag bokar {s} kl {hhmm} och skickar bekräftelsen till {email}? Svara ja eller nej."})
    # -----------------------------------

    # ---- CANCEL INTENT HANDLER (runs before slot prompts) ----
    bkid = _extract_booking_id(text)        # e.g. finds 501484 in "avboka 501484"
    want_cancel = _cancel_intent(text) or _g(cid, "awaiting_bkid")

    if want_cancel:
        if not bkid:
            _set(cid, "awaiting_bkid", True)
            return jsonify({"response": "Självklart! Vad är ditt boknings-ID så fixar jag avbokningen?"})
        # we have a number now → try to cancel
        _set(cid, "awaiting_bkid", False)
        try:
            salon_id_local = int(_g(cid, "salon_id") or salon_id or 97)
            res = ADAPTER.cancel_booking(salon_id_local, int(bkid))
            if res.get("ok"):
                return jsonify({
                    "response": f"Klart! Jag avbokade din tid (ID {bkid}). Vill du boka något annat?",
                    "ok": True,
                    "canceled": res.get("canceled")
                })
            else:
                return jsonify({
                   "response": f"Tyvärr, jag hittade ingen bokning med ID {bkid}. Kan du dubbelkolla numret?",
                   "ok": False
                })
        except Exception as e:
            print(f"[HAIR/CANCEL] error: {e}")
            return jsonify({"response": "Hoppsan, något gick snett när jag försökte avboka. Vill du prova igen?", "ok": False})
    # ---- end cancel handler ----

    st = _state(cid)
    # try fill from this utterance
    if not _g(cid, "name"):
        n = _extract_name(text)
        if n: _set(cid, "name", n)

    if not _g(cid, "phone"):
        p = _extract_phone(text)
        print(f"[HAIR] phone_extracted='{p}' from text='{text}'")
        if p:
            _set(cid, "phone", p)

    # --- EMAIL (robust, prefers raw parse and extracts from normalized) ---
    if not _g(cid, "email"):
        candidates = []

        # 1) Raw parse directly from what the user said
        e_raw = (_parse_email(text) or "").strip().lower()
        if e_raw:
            candidates.append(e_raw)

        # 2) Normalized (handles "snabel a" etc.), then extract the actual token from it
        e_norm_source = (normalize_spelled_email(text) or "").strip().lower()
        # strip stray trailing punctuation (speech artifacts)
        e_norm_source = e_norm_source.strip(" ,;:!?)\"]}<>(“”’‘")
        # pull the email *inside* the normalized string (avoids "minemailar" prefix)
        e_norm = (_parse_email(e_norm_source) or "").strip().lower()
        if e_norm:
            candidates.append(e_norm)

        # 3) Pick the first valid candidate; if both valid, raw wins due to ordering above
        chosen = next((c for c in candidates if EMAIL_RE.match(c)), None)
        print(f"[HAIR] email_candidates={candidates} chosen={chosen} from text={text!r}")
        if chosen:
            _set(cid, "email", chosen)

    if not _g(cid, "service_id"):
        sid, sname = _pick_service_id(salon_id, text)
        if sid:
            _set(cid, "service_id", sid)
            _set(cid, "service_name", sname)

    # If we have service but no time yet, try mapping the user's text to a real slot now
    if _g(cid, "service_id") and not _g(cid, "time_id"):
        slots = ADAPTER.check_availability(salon_id, _g(cid, "service_id"), date_iso)
        tid = _pick_time_id(text, slots)
        slot = next((s for s in slots if str(s.get("time_id")) == str(tid)), None)
        if slot:
            _set(cid, "time_id", tid)
            _set(cid, "slot", slot)
            # ask for final confirmation (don’t book yet)
            _set(cid, "awaiting_confirm", True)
            hhmm = (slot.get("start") or "")[11:16]
            sname = _g(cid, "service_name") or ""
            email = _g(cid, "email") or ""
            return jsonify({"response": f"Perfekt, jag tar den tiden. Vill du att jag bokar {sname} kl {hhmm} och skickar bekräftelsen till {email}?"})

    # ask next missing slot
    if not _g(cid, "name"):
        return jsonify({"response": "Vad heter du?"})

    if not _g(cid, "phone"):
        return jsonify({"response": "Vad är ditt telefonnummer?"})

    if not _g(cid, "email"):
        return jsonify({"response": "Alright, och vad var din e-postadress?"})

    if not _g(cid, "service_id"):
        # show top services to help user choose
        names = [s["name"] for s in ADAPTER.list_services(salon_id)][:5]
        return jsonify({"response": "Vilken behandling vill du ha? Exempel: " + " / ".join(names)})

    if not _g(cid, "time_id"):
        # show fresh availability and/or map this utterance
        slots = ADAPTER.check_availability(salon_id, _g(cid, "service_id"), date_iso)
        chosen = _pick_time_id(text, slots)
        if chosen:
            _set(cid, "time_id", chosen)
            slot = next((s for s in slots if str(s.get("time_id")) == str(chosen)), None)
            _set(cid, "slot", slot or {})
            _set(cid, "awaiting_confirm", True)
            hhmm = (slot.get("start") or "")[11:16] if slot else ""
            sname = _g(cid, "service_name") or ""
            email = _g(cid, "email") or ""
            return jsonify({"response": f"Perfekt, jag tar den tiden. Vill du att jag bokar {sname} kl {hhmm} och skickar bekräftelsen till {email}?"})
        if not slots:
            return jsonify({"response": "Jag hittar inga tider idag. Vill du prova ett annat datum?"})
        return jsonify({"response": _format_slots_for_prompt(slots)})

    # ---- all fields present → if we haven’t confirmed yet, ask; else book ----
    if not _g(cid, "awaiting_confirm"):
        _set(cid, "awaiting_confirm", True)
        slot = _g(cid, "slot") or {}
        hhmm = (slot.get("start") or "")[11:16]
        sname = _g(cid, "service_name") or ""
        email = _g(cid, "email") or ""
        return jsonify({"response": f"Vill du att jag bokar {sname} kl {hhmm} och skickar bekräftelsen till {email}? Svara ja eller nej."})

    return _do_booking(cid, salon_id)


@bp_hair.post("/reset")
def hair_reset():
    data = request.get_json(force=True) or {}
    cid = (data.get("conv_id") or "local").strip()
    SESSION.pop(cid, None)
    return jsonify({"ok": True})

# -----------------------------
# init hook (called from app.py)
# -----------------------------
def init_hair(app):
    app.register_blueprint(bp_hair)