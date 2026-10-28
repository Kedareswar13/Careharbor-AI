"""Text normalisation, tokenisation and query expansion for BM25."""

from __future__ import annotations

import re

STOPWORDS = set(
    """a an and are as at be been but by can could do does did for from had has have how i if in into is it its me my of on or our please
    should so than that the their them then there these they this to was we were what when where which who why will with would you your
    am im i'm ive i've get got any some just also very really much more most about after before since till until up down out over again
    hai hain ka ki ke ko se me mein mujhe mera meri kya kab kaise aur bhi tha thi raha rahi""".split()
)

# Phrase-level expansions applied before tokenising (Hinglish, lay terms, abbreviations).
PHRASES: dict[str, str] = {
    "sir dard": "headache",
    "sar dard": "headache",
    "sirdard": "headache",
    "pet dard": "stomach pain abdominal",
    "kamar dard": "back pain",
    "gale me dard": "sore throat",
    "gala kharab": "sore throat",
    "loose motion": "diarrhoea",
    "runny nose": "cold nose",
    "blocked nose": "cold nose congestion",
    "stuffy nose": "cold nose congestion",
    "high bp": "high blood pressure hypertension",
    "low bp": "low blood pressure",
    "bp machine": "blood pressure monitor",
    "sugar patient": "diabetes",
    "opd timing": "opd timings hours",
    "visiting hours": "visiting rules visitors",
    "health card": "insurance card",
    "can't sleep": "sleep insomnia",
    "cannot sleep": "sleep insomnia",
    "heart attack": "heart attack chest pain emergency",
    "twisted ankle": "sprain ankle",
    "itchy eyes": "allergy hay fever",
    "watery eyes": "allergy hay fever",
    "every spring": "seasonal allergy pollen",
    "every winter": "seasonal allergy",
}

# Token-level synonyms (expansion only adds terms; originals are kept).
SYNONYMS: dict[str, list[str]] = {
    "bukhar": ["fever"],
    "bukhaar": ["fever"],
    "khansi": ["cough"],
    "zukam": ["cold"],
    "jukam": ["cold"],
    "dast": ["diarrhoea"],
    "ulti": ["vomiting"],
    "khujli": ["itching", "rash"],
    "neend": ["sleep"],
    "chot": ["injury", "wound"],
    "temperature": ["fever"],
    "pyrexia": ["fever"],
    "migraine": ["headache"],
    "diarrhea": ["diarrhoea"],
    "diarrhoea": ["diarrhea"],
    "dehydrated": ["dehydration"],
    "ors": ["rehydration", "dehydration"],
    "acidity": ["acid", "reflux", "heartburn"],
    "heartburn": ["acidity", "reflux"],
    "gerd": ["reflux", "acidity"],
    "gas": ["acidity", "indigestion"],
    "itchy": ["itching"],
    "itching": ["rash"],
    "hives": ["rash", "allergy"],
    "eczema": ["rash", "skin"],
    "pollen": ["allergy"],
    "sneezing": ["allergy", "cold"],
    "insomnia": ["sleep"],
    "bp": ["blood", "pressure"],
    "hypertension": ["blood", "pressure"],
    "sugar": ["diabetes"],
    "diabetic": ["diabetes"],
    "sprained": ["sprain"],
    "cut": ["wound"],
    "wound": ["cut"],
    "scald": ["burn"],
    "scalded": ["burn", "scald"],
    "scalding": ["burn", "scald"],
    "burnt": ["burn"],
    "burned": ["burn"],
    "burning": ["burn"],
    "flu": ["cold", "fever"],
    "timings": ["hours", "opd"],
    "timing": ["hours", "opd"],
    "open": ["hours", "timings"],
    "closed": ["hours", "sunday", "holidays"],
    "fee": ["fees", "consultation"],
    "fees": ["consultation", "charges"],
    "cost": ["fees", "charges"],
    "charges": ["fees"],
    "price": ["fees"],
    "pay": ["payment"],
    "upi": ["payment"],
    "insurance": ["insurer", "cashless"],
    "insurer": ["insurance"],
    "tpa": ["insurance"],
    "cancel": ["cancellation"],
    "reschedule": ["rescheduling"],
    "report": ["reports", "lab"],
    "reports": ["lab"],
    "fasting": ["lab"],
    "visitor": ["visiting", "companion"],
    "visitors": ["visiting", "companion"],
    "attendant": ["companion", "visiting"],
    "parking": ["visiting"],
    "wheelchair": ["accessibility"],
    "privacy": ["data"],
    "paediatrics": ["pediatrics", "children"],
    "pediatrics": ["paediatrics", "children"],
    "child": ["children", "paediatrics"],
    "kid": ["children", "paediatrics"],
    "skin": ["dermatology"],
    "dermatologist": ["dermatology"],
    "cardiologist": ["cardiology"],
    "bones": ["orthopaedics"],
    "ortho": ["orthopaedics"],
    "ent": ["ear", "nose", "throat"],
    "gynaecologist": ["gynaecology"],
    "gynecologist": ["gynaecology"],
    "physio": ["physiotherapy"],
    "specialist": ["departments", "doctors"],
}


def normalise(text: str) -> str:
    t = text.lower().replace("\u2019", "'")
    for phrase, expansion in PHRASES.items():
        if phrase in t:
            t = f"{t} {expansion}"
    return t


def stem(tok: str) -> str:
    """Tiny suffix stripper: enough for 'fevers'/'fever', 'sprains'/'sprain'."""
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 4 and tok.endswith("es") and tok[-3] in "sxz":
        return tok[:-2]
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return [stem(t) for t in _TOKEN.findall(text.lower()) if t not in STOPWORDS and len(t) > 1]


def expand_query(query: str) -> list[str]:
    out: list[str] = []
    for tok in _TOKEN.findall(normalise(query)):
        if tok in STOPWORDS or len(tok) < 2:
            continue
        out.append(stem(tok))
        out.extend(stem(s) for s in SYNONYMS.get(tok, []))
    return out
