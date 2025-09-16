# FILE: utils_cleanup.py
# -*- coding: utf-8 -*-
import re
import datetime as dt
from typing import Tuple

# ===============================
# Email Normalization – Layered
# ===============================

# 1) Filler / noise tokens to drop entirely
FILLERS = {
    "eh", "öh", "aaah", "mmm", "typ", "liksom", "alltså", "ehm", "öhmm",
    "ja", "jo", "nej", "okej", "ok", "mm", "mhm", "ah", "jah", "well",
}

# 2) Symbols (spoken → written) — include many Swedish mis-hearings
SYMBOL_RULES = {
    # @
    "snabel-a": "@", "snabela": "@", "snabel": "@", "snabbel": "@",
    "snabbela": "@", "snabbel-a": "@", "snobbel": "@", "snobbel-a": "@",
    "snobell": "@", "snabble": "@", "snabell": "@", "snobbela": "@",
    "at": "@", "ett": "@",  # ASR sometimes turns "at" to "ett"
    # dot
    "punkt": ".", "punk": ".", "ponkt": ".", "pankt": ".", "dot": ".", "prick": ".",
    # dash / hyphen
    "streck": "-", "sträck": "-", "bindestreck": "-", "bindestrek": "-", "dash": "-", "minus": "-",
    # underscore
    "understreck": "_", "understräck": "_", "underscore": "_", "under score": "_",
    # plus
    "plus": "+", "pluss": "+", "plos": "+",
}

# 3) Domain phrase fixes (spoken → written)
DOMAIN_RULES = {
    "gmail com": "gmail.com", "gmail punkt com": "gmail.com", "gmail dot com": "gmail.com",
    "hotmail com": "hotmail.com", "hotmail punkt com": "hotmail.com", "hot mail punkt com": "hotmail.com",
    "outlook com": "outlook.com", "out look com": "outlook.com",
    "yahoo com": "yahoo.com", "icloud com": "icloud.com",
    # common TLD mis-hearings
    ".kom": ".com", "punkt kom": ".com", "dot com": ".com", "dotcon": ".com", "punkt con": ".com",
    ".se.": ".se", ".com.": ".com",
}

# 4) Letter + phonetic helpers (not interactive, heuristic only)
#    We mainly use this to collapse things like "j som johan" → "j"
#    If conflict is detected (e.g., "u som yngve"), we keep the first letter (best effort).
PHONETIC_MAP = {
    "adam": "a", "anders": "a",
    "bertil": "b",
    "cesar": "c", "caesar": "c",
    "david": "d",
    "erik": "e",
    "filip": "f",
    "gustav": "g",
    "helge": "h", "hilda": "h",
    "ivar": "i",
    "johan": "j",
    "kalle": "k",
    "ludvig": "l", "lovisa": "l",
    "martin": "m", "maria": "m",
    "niklas": "n",
    "olof": "o", "oscar": "o", "oskar": "o",
    "petter": "p", "pelle": "p",
    "qvintus": "q", "quintus": "q",
    "rudolf": "r",
    "sigurd": "s", "sara": "s",
    "tore": "t",
    "urban": "u",
    "viktor": "v", "victor": "v",
    "wilhelm": "w", "dubbel-v": "w", "double v": "w", "double-v": "w",
    "xerxes": "x", "x-ray": "x",
    "yngve": "y",
    "zäta": "z", "zeta": "z",
}

# 5) Confusion pairs we treat carefully (used only for light post-checks)
CONFUSION_SETS = [
    {"u", "y"},        # vowels (phone lines)
    {"b", "p", "d"},   # plosives
    {"m", "n"},        # nasals
    {"v", "w"},        # identical in sv speech
    {"c", "z"},        # both can be 's'ish
]

# 6) Allowed email regex (ASCII only, strictish)
EMAIL_REGEX = re.compile(
    r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$"
)

# 7) Utility: replace Swedish diacritics in email context (ASCII-only policy)
def _strip_diacritics(s: str) -> str:
    # Map å/ä→a, ö→o in email contexts to avoid invalid addresses
    return (
        s.replace("å", "a").replace("ä", "a").replace("ö", "o")
         .replace("Å", "A").replace("Ä", "A").replace("Ö", "O")
    )

def _preclean(text: str) -> str:
    s = (text or "").strip().lower()
    # squeeze spaces
    s = re.sub(r"\s+", " ", s)
    # remove obvious fillers
    toks = [t for t in s.split(" ") if t not in FILLERS]
    return " ".join(toks)

def _apply_symbol_map(s: str) -> str:
    # token-wise replacement
    toks = []
    for t in s.split(" "):
        t_clean = t.strip("’'\",;:()[]{}")
        if t_clean in SYMBOL_RULES:
            toks.append(SYMBOL_RULES[t_clean])
        else:
            toks.append(t_clean)
    s2 = " ".join(toks)

    # merge spaces around symbols
    s2 = s2.replace(" @ ", " @ ").replace(" . ", " . ")
    # later we’ll remove spaces around @ and .
    return s2

def _collapse_phonetics(s: str) -> str:
    """
    Collapse patterns like 'j som johan' → 'j'.
    If mismatch (e.g., 'u som yngve'), keep the first letter (best-effort).
    """
    # Replace multi-token phonetic words to their letters (standalone usage)
    toks = s.split(" ")
    toks2 = []
    for t in toks:
        toks2.append(PHONETIC_MAP.get(t, t))
    s = " ".join(toks2)

    # Handle explicit pattern "<letter> som <word>"
    pattern = re.compile(r"\b([a-zåäö])\s+som\s+([a-zåäö]+)\b")
    def _rep(m):
        letter = m.group(1)
        word   = m.group(2)
        mapped = PHONETIC_MAP.get(word, word[:1])
        # If mapped mismatches, keep spoken letter (best-effort)
        return letter
    return pattern.sub(_rep, s)

def _apply_domain_fixes(s: str) -> str:
    s2 = s
    # simple phrase replacements first
    for bad, good in DOMAIN_RULES.items():
        s2 = s2.replace(bad, good)
    # common glued phrases like "gmailpunktcom" → "gmail.com"
    s2 = re.sub(r"g\s*mail\s*punkt\s*com", "gmail.com", s2)
    s2 = re.sub(r"g\s*mail\s*dot\s*com", "gmail.com", s2)
    s2 = re.sub(r"hot\s*mail\s*punkt\s*com", "hotmail.com", s2)
    s2 = re.sub(r"out\s*look\s*punkt\s*com", "outlook.com", s2)
    s2 = re.sub(r"icloud\s*punkt\s*com", "icloud.com", s2)
    return s2

def _tighten_symbols(s: str) -> str:
    # remove spaces around @ and .
    s = re.sub(r"\s*@\s*", "@", s)
    s = re.sub(r"\s*\.\s*", ".", s)
    s = re.sub(r"\s*-\s*", "-", s)
    s = re.sub(r"\s*_\s*", "_", s)
    s = re.sub(r"\s*\+\s*", "+", s)
    # remove remaining spaces
    return s.replace(" ", "")

def _extract_best_email(s: str) -> str | None:
    """
    Find the most plausible email candidate in a messy string.
    Prefer the longest match if multiple candidates.
    """
    candidates = re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", s)
    if not candidates:
        return None
    # longest (often the real one)
    return max(candidates, key=len)

def normalize_spelled_email(text: str) -> str:
    """
    Public API (kept name/signature). Takes a messy spoken string and returns:
      - a cleaned string (email if found), OR the original string if no email-like content.
    We do NOT raise here; backend routes can decide what to do if no valid email.
    """
    if not text:
        return ""

    s = _preclean(text)
    s = _apply_symbol_map(s)
    s = _collapse_phonetics(s)
    s = _apply_domain_fixes(s)

    # Special: ensure spoken '@' variants eventually become literal '@'
    # (Already handled via SYMBOL_RULES; this is an extra guard)
    s = s.replace("snabela", "@").replace("snabel@", "@")

    # Tighten symbols & strip diacritics for emails
    s = _tighten_symbols(s)
    s = _strip_diacritics(s)

    # If there is any email candidate, return the best one;
    # otherwise, if there is an '@' but no full TLD, try to patch common mistakes
    candidate = _extract_best_email(s)
    if candidate:
        return candidate

    # Patch obvious ".kom" / missing dot before com
    s = s.replace("kom", "com").replace("@gmailcom", "@gmail.com")

    candidate = _extract_best_email(s)
    return candidate or s  # return s (could be non-email text)

def validate_email(s: str) -> bool:
    """True if s is a valid ASCII email (strictish)."""
    if not s:
        return False
    s2 = _strip_diacritics(s)
    return bool(EMAIL_REGEX.match(s2 or ""))

# ===============================
# Date/Time (unchanged below)
# ===============================

WD = {"mån":0,"tis":1,"ons":2,"tors":3,"fre":4,"lör":5,"sön":6}

def parse_sv_date_time(t: str, base: dt.date|None=None) -> Tuple[str|None, str|None]:
    t0 = (t or "").lower().strip()
    base = base or dt.date.today()

    # ----- DATE -----
    date = None
    if "idag" in t0:
        date = base
    elif "imorgon" in t0:
        date = base + dt.timedelta(days=1)
    elif "i övermorgon" in t0 or "i över morgon" in t0:
        date = base + dt.timedelta(days=2)
    elif "nästa " in t0:
        m = re.search(r"nästa\s+(mån|tis|ons|tors|fre|lör|sön)", t0)
        if m:
            target = WD[m.group(1)]
            delta = (target - base.weekday() + 7) % 7
            delta = 7 if delta == 0 else delta
            date = base + dt.timedelta(days=delta)
    else:
        m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", t0)
        if m:
            date = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # ----- TIME -----
    time_ = None
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", t0)
    if m:
        time_ = f"{int(m.group(1)):02d}:{m.group(2)}"
    else:
        m = re.search(r"(halv|kvart över|kvart i)\s*(\d{1,2})", t0)
        if m:
            h = int(m.group(2)) % 24
            if m.group(1) == "halv":  # halv tre = 2:30
                h = (h - 1) % 24
                time_ = f"{h:02d}:30"
            elif m.group(1) == "kvart över":
                time_ = f"{h:02d}:15"
            elif m.group(1) == "kvart i":
                h = (h - 1) % 24
                time_ = f"{h:02d}:45"
        elif "lunchtid" in t0 or "vid lunch" in t0:
            time_ = "12:00"
        elif "förmiddag" in t0 and not time_:
            time_ = "10:00"
        elif "eftermiddag" in t0 and not time_:
            time_ = "15:00"

    return (date.isoformat() if date else None, time_)