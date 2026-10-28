"""Deterministic safety layers that run without any LLM.

1. red_flags()        - emergency detection BEFORE any model call (English, Hinglish, common typos).
2. injection_check()  - prompt-injection / data-exfiltration attempts in user text or retrieved text.
3. check_output()     - blocks diagnosis, dosing, new-medicine advice and ungrounded numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

EMERGENCY_REPLY = (
    "This could be a medical emergency. Please call 112 or 108 now, or go to the nearest emergency department. "
    "Don't wait for an appointment, and don't drive yourself. I've alerted our clinic staff."
)
CRISIS_LINE = " If you are thinking about harming yourself, you can also call Tele-MANAS on 14416 (free, 24x7). You are not alone."
SAFE_FALLBACK = "I'm not able to advise on that. Please speak to your doctor or pharmacist. Would you like me to connect you with our staff?"

# ---------------------------------------------------------------- normalisation
TYPOS = {
    "pian": "pain", "pain": "pain", "chets": "chest", "chst": "chest", "cheast": "chest", "brethe": "breathe", "breth": "breath", "brething": "breathing",
    "breating": "breathing", "breathin": "breathing", "headace": "headache", "hedache": "headache", "headach": "headache", "unconcious": "unconscious",
    "siezure": "seizure", "seizer": "seizure", "faintd": "fainted", "bleding": "bleeding", "bleedin": "bleeding", "sucide": "suicide", "suiside": "suicide",
    "stiffneck": "stiff neck", "slured": "slurred", "numbnes": "numbness", "vomitting": "vomiting", "pregnent": "pregnant", "pragnant": "pregnant",
    "presure": "pressure", "pressur": "pressure", "cant": "can't", "cannt": "can't", "dont": "don't", "wont": "won't", "isnt": "isn't",
}


def normalise(text: str) -> str:
    t = text.lower().replace("\u2019", "'")
    t = re.sub(r"(.)\1{2,}", r"\1\1", t)  # "paaaain" -> "paain"
    t = re.sub(r"[a-z']+", lambda m: TYPOS.get(m.group(0), m.group(0)), t)
    return re.sub(r"\s+", " ", t)


# ------------------------------------------------------------------- red flags
W = r"(?:\W+\w+){0,4}?\W+"  # up to 4 words in between
RED_FLAGS: list[tuple[str, str]] = [
    ("chest_pain", r"\bchest" + W + r"(pain|pressure|tight\w*|heav\w*|hurt\w*|dard|squeez\w*)"),
    ("chest_pain", r"\b(pain|pressure|tightness|heaviness|dard)" + W + r"(in|on|of|me|mein)\W+(my\W+)?chest"),
    ("chest_pain", r"\b(seene|seena|chhati|chaati)\W+(me|mein|mai)?\W*(dard|dabav|jalan)"),
    ("chest_pain", r"\bheart attack\b"),
    ("breathing", r"\b(can't|cannot|can not|unable to|hard to|struggling to|difficult to|trouble)\W+(\w+\W+)?(breathe|breath|breathing)"),
    ("breathing", r"\b(difficulty|trouble|problem|struggling)\W+(in\W+)?breath\w*"),
    ("breathing", r"\b(short(ness)?\W+of\W+breath|breathless\w*|gasping|choking|suffocat\w*)"),
    ("breathing", r"\bsaa?ns\W+(\w+\W+){0,2}(nahi|nhi|nai|nahin|ruk|phool|phul|taklif|dikkat|takleef)"),
    ("breathing", r"\b(lips|face|fingers?)\W+(\w+\W+){0,3}(blue|bluish)"),
    ("stroke", r"\bface\W+(is\W+)?(droop\w*|drop\w*|tilt\w*|lopsided)|\b(droop\w*)\W+(\w+\W+)?face"),
    ("stroke", r"\bslurr\w*\W+(speech|words|talk\w*)|\bspeech\W+(is\W+)?slurr\w*|\b(can't|cannot|unable to)\W+(speak|talk)\b"),
    ("stroke", r"\b(arm|leg|hand|side)\W+(\w+\W+){0,2}(weak\w*|numb\w*|paraly\w*|can't move)|\b(weak\w*|numb\w*)\W+(\w+\W+){0,3}(one side|left side|right side|arm|leg)"),
    ("stroke", r"\bstroke\b|\bmu(n|h)h?\W+tedha"),
    ("severe_headache", r"\bworst\W+(\w+\W+){0,3}headache|\bheadache\W+(\w+\W+){0,4}worst\b"),
    ("severe_headache", r"\b(sudden|suddenly|thunderclap)\W+(\w+\W+){0,3}(severe|bad|terrible|intense|explosive)?\W*headache"),
    ("meningitis", r"\bstiff\W+neck\b.*\b(fever|bukhar|temperature)|\b(fever|bukhar|temperature)\b.*\bstiff\W+neck"),
    ("collapse", r"\b(faint\w*|passed out|pass out|unconscious|collapsed|blacked out|behosh\w*|not waking|won't wake|unresponsive)\b"),
    ("seizure", r"\b(seizure\w*|convuls\w*|fits\b|having a fit|mirgi)"),
    ("confusion", r"\b(confused|confusion|disoriented|not making sense|doesn't recognise|doesn't recognize)\b"),
    ("bleeding", r"\b(severe|heavy|heavily|lot of|uncontrolled|massive)\W+(\w+\W+)?bleed\w*|\bbleed\w*\W+(\w+\W+){0,2}(won't stop|not stopping|heavily|a lot|nonstop|non stop)"),
    ("bleeding", r"\b(vomit\w*|cough\w*|throw\w* up|spit\w*)\W+(\w+\W+){0,2}blood|\bblood\W+(in\W+)?(vomit|my vomit)|\bkhoon\W+(\w+\W+)?(ulti|aa|nikal)"),
    ("suicide", r"\b(suicid\w*|kill\w* myself|end (my|it all)|end my life|want to die|wanna die|self.?harm\w*|hurt(ing)? myself|cut(ting)? myself|no reason to live|marna chah\w*|jeena nahi|khudkushi|aatmahatya)"),
    ("pregnancy", r"\bpregnan\w*\b.*\b(bleed\w*|severe pain|cramp\w*|pain|spotting|fits|water broke)|\b(bleed\w*|spotting|severe pain)\b.*\bpregnan\w*"),
    ("infant_fever", r"\b(newborn|new born|infant|neonate|(\d|one|two|three)\W*(week|weeks|wk|month|months|mo)\W*old|baby)\b.*\b(fever|temperature|bukhar|hot)\b"),
    ("infant_fever", r"\b(fever|temperature|bukhar)\b.*\b(newborn|infant|neonate|(\d|one|two|three)\W*(week|weeks|month|months)\W*old)\b"),
    ("anaphylaxis", r"\b(throat|tongue|lips?|face)\W+(\w+\W+)?(swell\w*|swollen|closing)\b|\banaphyla\w*"),
    ("poisoning", r"\b(overdose\w*|poison\w*|swallowed (bleach|acid|kerosene|pills)|took too many (pills|tablets))\b"),
]
_RED = [(name, re.compile(p)) for name, p in RED_FLAGS]

# Only explicitly negated SYMPTOM LISTS are removed ("no stiff neck or rash", "without fever").
# A generic "negation window" is unsafe: "I don't know, my chest hurts" must still trigger.
_SYMPTOM = (r"(stiff neck|neck stiffness|(purple |red )?rash|confusion|fever|temperature|chest pain|chest pressure|breathing (problems?|difficulty|trouble)|"
            r"trouble breathing|vomiting|bleeding|fainting|fits|seizures?|headache|weakness|numbness|swelling|blood|drowsiness)")
NEGATED_LIST = re.compile(rf"\b(no|without|denies|none of|never had)\s+((any|a|the)\s+)?{_SYMPTOM}(\s*(,|or|and|nor)\s*((any|a|the|no)\s+)?{_SYMPTOM})*")


@dataclass
class RedFlagResult:
    hit: bool
    categories: list[str] = field(default_factory=list)
    matches: list[str] = field(default_factory=list)

    @property
    def crisis(self) -> bool:
        return "suicide" in self.categories


def red_flags(text: str) -> RedFlagResult:
    t = NEGATED_LIST.sub(" ", normalise(text))
    cats, matches = [], []
    for name, rx in _RED:
        m = rx.search(t)
        if m:
            cats.append(name)
            matches.append(m.group(0))
    return RedFlagResult(bool(cats), sorted(set(cats)), matches)


def emergency_reply(result: RedFlagResult) -> str:
    return EMERGENCY_REPLY + (CRISIS_LINE if result.crisis else "")


# ------------------------------------------------------------ prompt injection
# Single source of truth lives in careharbor.security (Layer 1); these wrappers keep older call sites working.
def injection_check(text: str) -> list[str]:
    from careharbor.security import validate_query

    v = validate_query(text)
    return [] if v.allowed or v.reason == "out_of_scope" else v.matches


def sanitise_context(text: str) -> str:
    from careharbor.security import sanitise_retrieved

    return sanitise_retrieved(text)


# --------------------------------------------------------------- output guard
DRUGS = (
    r"paracetamol|acetaminophen|crocin|dolo|ibuprofen|brufen|combiflam|aspirin|diclofenac|naproxen|nimesulide|amoxicillin|augmentin|azithromycin|"
    r"ciprofloxacin|doxycycline|cefuroxime|cephalexin|metronidazole|antibiotic\w*|cetirizine|levocetirizine|loratadine|fexofenadine|montelukast|"
    r"pantoprazole|omeprazole|ranitidine|antacid\w*|ondansetron|domperidone|loperamide|steroid\w*|prednisolone|hydrocortisone|insulin|metformin|"
    r"amlodipine|telmisartan|losartan|atorvastatin|salbutamol|cough syrup|sleeping (pill|tablet)s?|painkiller\w*|pain reliever\w*"
)
OUTPUT_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("diagnosis", re.compile(r"(?i)\byou\W+(probably|likely|definitely|clearly|must|might|may|could)\W+(have|be suffering|be having)\b(?!\W+(to|been|a few|some questions|an appointment)\b)")),
    ("diagnosis", re.compile(r"(?i)\byou\W+(have|are suffering from|have got)\W+(a\W+|an\W+)?(viral|bacterial|infection|migraine|flu|dengue|malaria|typhoid|covid|pneumonia|"
                             r"diabetes|hypertension|asthma|gastritis|sinusitis|tonsillitis|bronchitis|uti|ulcer|appendicitis|arthritis|eczema|psoriasis|allergic rhinitis|gerd|ibs|stroke|heart attack)")),
    ("diagnosis", re.compile(r"(?i)\b(it|this)\W+(is|sounds like|looks like|seems like|could be|might be|may be|is likely|is probably)\W+(a\W+|an\W+)?(viral|bacterial|infection|migraine|flu|dengue|malaria|typhoid|pneumonia|sinusitis|tonsillitis|gastritis|uti|ulcer|appendicitis)")),
    ("diagnosis", re.compile(r"(?i)\b(my|the)\W+diagnosis\W+is|\bi\W+(think|believe)\W+you\W+have\b")),
    ("dosing", re.compile(r"(?i)\btake\W+(\d+(\.\d+)?|one|two|three|half|a)\W*(mg|ml|g|tablets?|tabs?|capsules?|caps?|pills?|puffs?|drops?|teaspoons?|spoons?)\b")),
    ("dosing", re.compile(r"(?i)\b\d+(\.\d+)?\s?(mg|mcg|ml)\b")),
    ("dosing", re.compile(r"(?i)\b(increase|decrease|reduce|double|halve|skip|stop|change|adjust)\W+(\w+\W+){0,2}(dose|dosage|medicine|medication|tablets?|pills?)\b")),
    ("dosing", re.compile(r"(?i)\b(every|once|twice|thrice|\d+ times)\W+(a|per)\W+day\b")),
    ("new_medicine", re.compile(rf"(?i)\b(take|try|use|start|buy|have)\W+(\w+\W+){{0,2}}({DRUGS})\b")),
    ("new_medicine", re.compile(rf"(?i)\b({DRUGS})\W+(\w+\W+){{0,3}}(will help|should help|is good|works well|is safe|can help)")),
]
ALWAYS_OK_NUMBERS = {"112", "108", "14416", "24", "7"}
DEFERS_TO_CLINICIAN = re.compile(r"(?i)\b(only (as|if)|as advised|ask|consult|speak (to|with)|check with|without (asking|advice|a doctor|your doctor))\b.*\b(doctor|pharmacist|clinic)|\b(doctor|pharmacist)('s)?\b.*\b(advised|advises|advice|recommends)\b")


@dataclass
class OutputCheck:
    ok: bool
    violations: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)


def _numbers(text: str) -> set[str]:
    out = set()
    for n in re.findall(r"\d+(?:[.:]\d+)?", text):
        out.add(n if ("." in n or ":" in n) else (n.lstrip("0") or "0"))  # "04" (from 2026-11-04) == "4"
    return out


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def check_output(reply: str, *, kind: str, grounding: str = "") -> OutputCheck:
    """kind: 'llm' (triage/faq generated text) or 'record' (template filled from a DB record).

    LLM text may not diagnose, dose or suggest medicines, and every number must appear in the
    grounding text (retrieved context + user message). Record replies are built from tool output,
    so only the diagnosis rules apply (dosing text there is the patient's own prescription).
    Sentences that defer to a doctor/pharmacist ("only as advised by your doctor") are not advice."""
    violations, details = [], []
    rules = OUTPUT_RULES if kind == "llm" else [r for r in OUTPUT_RULES if r[0] == "diagnosis"]
    for sentence in _sentences(reply):
        defers = bool(DEFERS_TO_CLINICIAN.search(sentence))
        for name, rx in rules:
            if defers and name in {"new_medicine", "dosing"}:
                continue
            m = rx.search(sentence)
            if m:
                violations.append(name)
                details.append(m.group(0))
    if kind in {"llm", "record"} and grounding:
        missing = {n for n in _numbers(reply) - _numbers(grounding) if n not in ALWAYS_OK_NUMBERS}
        if missing:
            violations.append("ungrounded_number")
            details.append(", ".join(sorted(missing)))
    if kind == "llm" and grounding:
        weak = unsupported_sentences(reply, grounding)
        if weak:
            violations.append("unsupported_claim")
            details.append(weak[0][:80])
    return OutputCheck(not violations, sorted(set(violations)), details)


_STOP = set("""a an and are as at be been but by can could do does for from had has have how i if in into is it its may me might my no not of on or our
please should so than that the their them then there these they this to too us was we were what when where which who why will with would you your
also any all some more most much very just only about over under after before during while each every both other such own same few here""".split())
_GENERIC = set("""help helps helpful clinic patient patients doctor doctors please feel feeling better good well kind care take taking make keep keeping try
can could would should also like need needs want things thing time times day days way ways sure important best recommend recommended advice
include includes including provide provides available contact visit support supports recovery recover ease easing reduce relieve relief comfort
comfortable symptoms symptom condition body home simple steps step practical gentle gently regularly regular overall""".split())


def _content_words(text: str) -> set[str]:
    # 5-character prefixes act as a crude stemmer: stretch/stretching, gentle/gently, hydrate/hydrated.
    return {w[:5] for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 3 and w not in _STOP and w not in _GENERIC}


def unsupported_sentences(reply: str, grounding: str, min_support: float = 0.4) -> list[str]:
    """Sentences whose content words are mostly absent from the grounding text (likely hallucinated facts)."""
    source = _content_words(grounding)
    weak = []
    for s in _sentences(reply):
        words = _content_words(s)
        if len(words) >= 4 and len(words & source) / len(words) < min_support:
            weak.append(s)
    return weak
