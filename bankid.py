# bankid.py
from flask import Blueprint, request, jsonify, current_app
import os, re, time, threading
from datetime import datetime
import requests

bp = Blueprint("bankid", __name__)

# --- config via env ---
BANKID_MODE = os.getenv("BANKID_MODE", "DEMO").upper()  # DEMO or REAL
# BankID RP API (TEST base). Keep these empty if you don't have certs yet.
BANKID_BASE = os.getenv("BANKID_BASE", "https://appapi2.test.bankid.com/rp/v6.0")
BANKID_CLIENT_CERT = os.getenv("BANKID_CLIENT_CERT", "")  # path to .pem/.crt (client cert)
BANKID_CLIENT_KEY  = os.getenv("BANKID_CLIENT_KEY", "")   # path to .key (client key)
BANKID_CA_CERT     = os.getenv("BANKID_CA_CERT", "")      # path to BankID test CA bundle (optional; can use verify=False in TEST only)

_SESS = {}  # orderRef -> dict

def _clean_pnr(s: str) -> str:
    digits = re.sub(r"\D", "", s or "")
    if len(digits) == 10:
        # crude century guess: 19 for YY starting 6-9 else 20
        digits = ("19" if digits[0] in "6789" else "20") + digits
    return digits

def _real_session():
    # requests kwargs for mTLS
    kw = {}
    if BANKID_CLIENT_CERT and BANKID_CLIENT_KEY:
        kw["cert"] = (BANKID_CLIENT_CERT, BANKID_CLIENT_KEY)
    # In TEST you can set verify=False; better: point to BankID test CA if you have it
    if BANKID_CA_CERT:
        kw["verify"] = BANKID_CA_CERT
    else:
        kw["verify"] = False
    return kw

@bp.route("/portal/api/bankid/start", methods=["POST"])
def bankid_start():
    data = request.get_json(silent=True) or {}
    pnr = _clean_pnr(data.get("personal_number", ""))
    if len(pnr) != 12:
        return jsonify({"ok": False, "error": "invalid_personal_number"}), 400

    if BANKID_MODE == "REAL":
        # BankID /auth – same device is typical when caller has the app on the same phone.
        # endUserIp MUST be the end-user device IP (BankID v6 still requires it).
        end_user_ip = data.get("endUserIp") or request.headers.get("X-EndUser-IP") or request.remote_addr
        payload = {"personalNumber": pnr, "endUserIp": end_user_ip}
        # Optional: "requirement": {...}
        kw = _real_session()
        r = requests.post(f"{BANKID_BASE}/auth", json=payload, timeout=10, **kw)
        if r.status_code != 200:
            return jsonify({"ok": False, "error": "bankid_auth_failed", "details": r.text}), 502
        resp = r.json()  # contains orderRef, autoStartToken, qrStartSecret etc.
        order_ref = resp["orderRef"]
        _SESS[order_ref] = {"pnr": pnr, "status": "pending", "started_at": datetime.utcnow().isoformat()+"Z"}
        # For same-device voice flow: instruct caller to open their BankID app manually,
        # OR if you have a visual device, you can use autostart URL:
        # autostart = f"bankid:///?autostarttoken={resp['autoStartToken']}&redirect=null"
        return jsonify({"ok": True, "orderRef": order_ref, "autoStartToken": resp.get("autoStartToken")})
    else:
        # DEMO mode – pretend it was accepted and will complete soon.
        order_ref = f"demo-{int(time.time()*1000)}"
        _SESS[order_ref] = {"pnr": pnr, "status": "pending", "started_at": datetime.utcnow().isoformat()+"Z"}
        # flip to complete after 6 seconds in a background thread
        def _complete_later(ref):
            time.sleep(6)
            if ref in _SESS and _SESS[ref]["status"] == "pending":
                _SESS[ref]["status"] = "complete"
                _SESS[ref]["name"] = "Test Person"
        threading.Thread(target=_complete_later, args=(order_ref,), daemon=True).start()
        return jsonify({"ok": True, "orderRef": order_ref})

@bp.route("/portal/api/bankid/status", methods=["GET"])
def bankid_status():
    order_ref = request.args.get("orderRef", "")
    if not order_ref:
        return jsonify({"ok": False, "error": "missing_orderRef"}), 400

    if BANKID_MODE == "REAL":
        kw = _real_session()
        r = requests.post(f"{BANKID_BASE}/collect", json={"orderRef": order_ref}, timeout=10, **kw)
        if r.status_code != 200:
            return jsonify({"ok": False, "error": "bankid_collect_failed", "details": r.text}), 502
        j = r.json()  # {status: pending|failed|complete, hintCode, completionData?}
        # normalize shape for the agent
        out = {"ok": True, "orderRef": order_ref, "status": j["status"]}
        if j["status"] == "complete":
            comp = j.get("completionData", {})
            # pnr is in comp["user"]["personalNumber"]
            out["personal_number"] = comp.get("user", {}).get("personalNumber")
            out["name"] = comp.get("user", {}).get("name")
        else:
            out["hintCode"] = j.get("hintCode")
        return jsonify(out)
    else:
        s = _SESS.get(order_ref)
        if not s:
            return jsonify({"ok": False, "error": "not_found"}), 404
        out = {"ok": True, "orderRef": order_ref, "status": s["status"]}
        if s["status"] == "complete":
            out["personal_number"] = s["pnr"]
            out["name"] = s.get("name", "Test Person")
        return jsonify(out)

@bp.route("/portal/api/bankid/cancel", methods=["POST"])
def bankid_cancel():
    data = request.get_json(silent=True) or {}
    order_ref = data.get("orderRef", "")
    if not order_ref:
        return jsonify({"ok": False, "error": "missing_orderRef"}), 400

    if BANKID_MODE == "REAL":
        kw = _real_session()
        r = requests.post(f"{BANKID_BASE}/cancel", json={"orderRef": order_ref}, timeout=10, **kw)
        if r.status_code not in (200, 404):
            return jsonify({"ok": False, "error": "bankid_cancel_failed", "details": r.text}), 502
        return jsonify({"ok": True})
    else:
        if order_ref in _SESS:
            _SESS[order_ref]["status"] = "failed"
        return jsonify({"ok": True})