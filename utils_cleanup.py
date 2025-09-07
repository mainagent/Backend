# utils_cleanup.py
import re

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