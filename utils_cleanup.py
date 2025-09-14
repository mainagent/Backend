# utils_cleanup.py
import re, datetime as dt

# --- Rule groups ---

# Symbols (spoken → written)
SYMBOL_RULES = {
    "snabel-a": "@", "snabela": "@", "snabel": "@", "at": "@",
    "punkt": ".", "dot": ".", "prick": ".",
    "bindestreck": "-", "minus": "-", "dash": "-",
    "understreck": "_", "underscore": "_",
    "plus": "+",
}

# Domain corrections
DOMAIN_RULES = {
    "gmail com": "gmail.com",
    "hotmail com": "hotmail.com",
    "outlook com": "outlook.com",
    "icloud com": "icloud.com",
    "yahoo com": "yahoo.com",
}

# Letters (phonetic mapping — extendable with A=Adam, B=Bertil, etc.)
LETTER_RULES = {
    "a": "a", "å": "å", "ä": "ä", "ö": "ö",
    "b": "b", "c": "c", "d": "d", "e": "e",
    "f": "f", "g": "g", "h": "h", "i": "i",
    "j": "j", "k": "k", "l": "l", "m": "m",
    "n": "n", "o": "o", "p": "p", "q": "q",
    "r": "r", "s": "s", "t": "t", "u": "u",
    "v": "v", "w": "w", "x": "x", "y": "y",
    "z": "z",
}

# Phonetics / common Swedish NATO-style
PHONETIC_RULES = {
    "adam": "a", "anders": "a",
    "bertil": "b",
    "cesar": "c",
    "david": "d",
    "erik": "e",
    "filip": "f",
    "gustav": "g",
    "helge": "h",
    "ivar": "i",
    "johan": "j",
    "kalle": "k",
    "ludvig": "l",
    "martin": "m",
    "niklas": "n",
    "olof": "o", "oscar": "o",
    "pelle": "p",
    "quintus": "q",
    "ragnar": "r",
    "sigurd": "s",
    "tore": "t",
    "urban": "u",
    "vikto": "v", "victor": "v",
    "wilhelm": "w",
    "x-ray": "x",
    "yngve": "y",
    "zäta": "z",
}

# --- Helpers ---

EMAIL_REGEX = re.compile(r"^[A-Za-z0-9._%+\-åäöÅÄÖ]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

def normalize_spelled_email(text: str) -> str:
    """Normalize a spoken email string into a valid email address if possible."""
    s = text.strip().lower()
    s = re.sub(r"\s+", " ", s)

    tokens = [t.strip("’'\",;:") for t in s.split(" ") if t.strip()]
    mapped = []

    for tok in tokens:
        if tok in SYMBOL_RULES:
            mapped.append(SYMBOL_RULES[tok])
        elif tok in DOMAIN_RULES:
            mapped.append(DOMAIN_RULES[tok])
        elif tok in PHONETIC_RULES:
            mapped.append(PHONETIC_RULES[tok])
        elif tok in LETTER_RULES:
            mapped.append(LETTER_RULES[tok])
        else:
            mapped.append(tok)

    s = " ".join(mapped)

    # Fix spacing around symbols
    s = s.replace(" @ ", "@").replace(" . ", ".").replace(" - ", "-").replace(" _ ", "_").replace(" + ", "+")
    
    # Apply domain fixes
    for bad, good in DOMAIN_RULES.items():
        s = s.replace(bad, good)

    return s.replace(" ", "")

def validate_email(s: str) -> bool:
    return bool(EMAIL_REGEX.match(s or ""))

WD = {"mån":0,"tis":1,"ons":2,"tors":3,"fre":4,"lör":5,"sön":6}

def parse_sv_date_time(t: str, base: dt.date|None=None) -> tuple[str|None,str|None]:
    t0 = (t or "").lower().strip()
    base = base or dt.date.today()

# ----- DATE -----
    date = None
    if "idag" in t0: date = base
    elif "imorgon" in t0: date = base + dt.timedelta(days=1)
    elif "i övermorgon" in t0 or "i över morgon" in t0: date = base + dt.timedelta(days=2)
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
            if m.group(1) == "halv": # halv tre = 2:30
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