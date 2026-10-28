"""Triage-lite helpers: safe follow-up questions and a cautious, rule-based disposition.

The disposition is deterministic so it can be evaluated (30 labelled stories) and
so that any doubt resolves to the MORE cautious level.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from careharbor.rag.index import SearchHit

LEVELS = ["self_care", "book", "urgent", "emergency"]  # increasing caution

DEPARTMENT_FOR_DOC = {
    "leaflet-skin-rash": "Dermatology",
    "leaflet-cuts-burns": "General Medicine",
    "leaflet-sprains": "Orthopaedics",
    "leaflet-back-pain": "Orthopaedics",
    "leaflet-seasonal-allergy": "ENT",
    "leaflet-sore-throat": "ENT",
}
CHILD = re.compile(r"(?i)\b(my|our)\W+(son|daughter|child|kid|baby|toddler|boy|girl)\b|\b(child|kid|toddler|infant)\b|\b\d{1,2}\W*(year|yr)s?\W*old\W+(son|daughter|boy|girl|child)")
PREGNANT = re.compile(r"(?i)\bpregnan\w*")
NUM_WORDS = {"one": 1, "a": 1, "an": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "ten": 10, "few": 3, "couple": 2, "do": 2, "teen": 3, "char": 4, "paanch": 5}

URGENT_PATTERNS = [
    ("high_fever", r"\b(10[3-9](\.\d)?|11\d)\s*(°|deg\w*)?\s*f?\b|\b(39\.[4-9]|4[0-2](\.\d)?)\s*(°|deg\w*)?\s*c\b|\bvery high (fever|temperature)"),
    ("worsening", r"\b(getting|got|becoming|became)\W+(much\W+)?worse\b|\bworsening\b|\bspreading\b"),
    ("severe_pain", r"\b(severe|unbearable|excruciating|very bad|terrible|intense)\W+(\w+\W+)?(pain|ache|headache|cramps?)\b|\bpain\W+is\W+(severe|unbearable|very bad)"),
    ("infection_signs", r"\bpus\b|\b(red|hot|swollen)\W+and\W+(swollen|hot|red|painful)\b|\bred streaks?\b|\bbad smell\b"),
    ("cannot_keep_fluids", r"\b(can't|cannot|unable to)\W+(keep|hold)\W+(\w+\W+)?(down|fluids|water)|\bvomit\w*\W+(everything|every time|after drinking)"),
    ("dehydration", r"\b(no|not passed|haven't passed|very little)\W+(urine|pee)\b|\bvery dry mouth\b|\bsunken eyes\b"),
    ("cannot_bear_weight", r"\b(can't|cannot|unable to)\W+(walk|stand|put (any )?weight|move (my|the) (leg|arm|hand|foot))"),
    ("deep_wound", r"\b(deep|gaping|large)\W+(cut|wound)\b|\bdog bite|\banimal bite|\bbitten by\b|\brusty\b"),
    ("diabetic_wound", r"\bdiabet\w*\b.*\b(wound|cut|blister|ulcer|sore)\b|\b(wound|cut|blister|ulcer|sore)\b.*\bdiabet\w*"),
    ("very_high_bp", r"\b(1[6-9]\d|2\d\d)\s*/\s*(1[0-4]\d|9\d)\b"),
    ("ear_or_throat_severe", r"\b(ear\W+pain|ear\W+discharge|white patches|swollen (glands|tonsils))\b"),
    ("blood_in_urine", r"\bblood\W+(in\W+)?(my\W+)?(urine|pee)\b"),
]
BOOK_PATTERNS = [
    ("recurring", r"\b(keeps?|keep) (coming back|happening|recurring)\b|\bevery (week|day|month|night)\b|\bagain and again\b|\boff and on\b|\brecurr\w*"),
    ("not_improving", r"\bnot (getting )?(better|improving)\b|\bno improvement\b|\bstill (there|the same)\b"),
    ("affects_life", r"\b(can't|cannot|unable to) (work|study|concentrate)\b|\baffect\w* (my )?(work|studies|studying)\b"),
    ("medication_overuse", r"\bpain\W*killers?\W+(every|daily)|\bevery day\W+(\w+\W+)?(painkiller|tablet)"),
]


@dataclass
class Disposition:
    level: str
    reasons: list[str] = field(default_factory=list)
    days: float | None = None

    @property
    def advice(self) -> str:
        return {
            "self_care": "Self-care at home should help. See a doctor if it lasts longer than expected, gets worse, or any warning sign appears.",
            "book": "It would be sensible to see a doctor in the next few days.",
            "urgent": "Please see a doctor today. I can look for the earliest available slot.",
            "emergency": "This needs emergency care now: call 112 / 108 or go to the nearest emergency department.",
        }[self.level]


def duration_days(text: str) -> float | None:
    t = text.lower()
    if re.search(r"\bsince (this )?morning\b|\bfew hours\b|\b\d+ hours?\b|\btoday\b|\baaj\b|\bsince (an hour|last hour)", t):
        return 0.5
    if re.search(r"\bsince (yesterday|last night)\b|\bkal se\b", t):
        return 1
    if re.search(r"\bsince last week\b|\bpichle hafte\b", t):
        return 7
    if m := re.search(r"\b(\d+|one|a|an|two|three|four|five|six|seven|ten|few|couple|do|teen|char|paanch)\s*(?:of\s+)?(day|days|din|week|weeks|hafte|hafta|month|months|mahine)\b", t):
        n = float(m.group(1)) if m.group(1).isdigit() else NUM_WORDS.get(m.group(1), 1)
        unit = m.group(2)
        return n * (30 if unit.startswith(("month", "mahin")) else 7 if unit.startswith(("week", "haft")) else 1)
    return None


# Topic-specific "see a doctor if it lasts longer than N days" thresholds (from the leaflets).
LONG_AFTER_DAYS = {
    "leaflet-fever": 3, "leaflet-cold-cough": 10, "leaflet-sore-throat": 7, "leaflet-diarrhoea": 2, "leaflet-skin-rash": 7,
    "leaflet-acid-reflux": 14, "leaflet-sprains": 7, "leaflet-back-pain": 14, "leaflet-sleep-hygiene": 21, "leaflet-headache": 7,
}


def disposition(text: str, doc_id: str | None = None) -> Disposition:
    reasons = [name for name, p in URGENT_PATTERNS if re.search(p, text, re.I)]
    days = duration_days(text)
    child = bool(CHILD.search(text))
    if PREGNANT.search(text):
        reasons.append("pregnancy")
    if child and re.search(r"(?i)\b(fever|vomit\w*|diarrh\w*|loose motion|rash|not eating|not drinking)\b", text):
        reasons.append("child_with_symptoms")
    if reasons:
        return Disposition("urgent", reasons, days)
    book = [name for name, p in BOOK_PATTERNS if re.search(p, text, re.I)]
    limit = LONG_AFTER_DAYS.get(doc_id or "", 7)
    if days is not None and days > limit:
        book.append(f"longer_than_{limit}_days")
    if book:
        return Disposition("book", book, days)
    return Disposition("self_care", [], days)


def short_sign(text: str, limit: int = 60) -> str:
    s = re.sub(r"\*\*|\(.*?\)", "", text).strip().rstrip(".")
    return s if len(s) <= limit else s[:limit].rsplit(" ", 1)[0] + "..."


def followup_questions(first_message: str, sections: list[SearchHit]) -> list[str]:
    """1-3 safe questions: duration/trend (if unknown), a yes/no emergency screen from the leaflet, who it's for."""
    qs: list[str] = []
    if duration_days(first_message) is None:
        qs.append("How long have you had this, and is it getting better or worse?")
    emergency = next((h for h in sections if h.node_id.split("#", 1)[1].startswith("emergency-signs")), None)
    if emergency:
        signs = [short_sign(b) for b in re.findall(r"^- (.+)$", emergency.text, re.M)[:3]]
        qs.append("Do you have any of these right now: " + "; ".join(signs) + "? (yes / no)")
    else:
        qs.append("Do you have chest pain, trouble breathing, confusion or fainting? (yes / no)")
    if not CHILD.search(first_message) and not re.search(r"(?i)\b(i|i'm|i've|me|my|mujhe|mera|meri)\b", first_message):
        qs.append("Is this for you, a child, or someone who is pregnant?")
    return qs[:3]


def guidance_bullets(sections: list[SearchHit], max_self: int = 5, max_doctor: int = 3) -> tuple[list[str], list[str]]:
    self_care = next((h for h in sections if h.node_id.split("#", 1)[1].startswith("self-care")), None)
    doctor = next((h for h in sections if h.node_id.split("#", 1)[1].startswith("see-a-doctor")), None)

    def bullets(h: SearchHit | None, n: int) -> list[str]:
        return [re.sub(r"\*\*", "", b).strip() for b in re.findall(r"^- (.+)$", h.text, re.M)][:n] if h else []

    return bullets(self_care, max_self), bullets(doctor, max_doctor)
