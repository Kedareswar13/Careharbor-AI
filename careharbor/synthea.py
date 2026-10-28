"""Import realistic patient histories from the Synthea open dataset.

Synthea (https://synthetichealth.github.io/synthea/) generates lifelike but
fully synthetic electronic health records from real population health
statistics. We read its CSV sample zip directly (no extraction) and keep only
what a primary-care front desk needs:

- age and sex, allergies, active chronic conditions          -> patients
- recent outpatient encounter reasons                        -> past visits
- outpatient medicines mapped to a prescription "sig"        -> prescriptions

Synthea's medication table has the drug, strength and dates but no dosing
schedule, so each supported drug is paired with a conventional schedule from
SIG_TABLE. Opioids, injections, contraceptives, oncology and hospital-only
drugs are excluded. US identifiers (names, SSN, address) are never imported;
names and phone numbers are localised for the (fictional) Bengaluru clinic.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

from careharbor.config import ROOT_DIR

SYNTHEA_ZIP = ROOT_DIR / "sources" / "synthea" / "synthea_sample_data_csv_latest.zip"


@dataclass(frozen=True)
class Sig:
    pattern: str            # regex matched against the Synthea DESCRIPTION
    drug: str               # display name (Indian generic naming)
    dose: str
    frequency: str
    times: str
    food: str
    days: int | None        # None = long-term medicine (recorded as 90-day supply)
    notes: str = ""
    form: str | None = None  # override parsed form
    strength: str | None = None  # override parsed strength
    drug_class: str = "general"  # general | cardio | resp | antibiotic | pain | bone | allergy | gi
    penicillin: bool = False


LONG_TERM = "Long-term medicine. Do not stop or change it without speaking to your doctor."

SIG_TABLE: list[Sig] = [
    Sig(r"^lisinopril \d+ MG Oral Tablet", "Lisinopril", "1 tablet", "once a day", "8 AM", "with or without food", None, LONG_TERM, drug_class="cardio"),
    Sig(r"^Hydrochlorothiazide 25 MG Oral Tablet", "Hydrochlorothiazide", "1 tablet", "once a day", "8 AM", "after breakfast", None, "You may pass urine more often for a few hours after taking it. " + LONG_TERM, drug_class="cardio"),
    Sig(r"^amLODIPine [\d.]+ MG Oral Tablet", "Amlodipine", "1 tablet", "once a day", "8 AM", "with or without food", None, LONG_TERM, drug_class="cardio"),
    Sig(r"^losartan potassium \d+ MG Oral Tablet", "Losartan", "1 tablet", "once a day", "8 AM", "with or without food", None, LONG_TERM, drug_class="cardio"),
    Sig(r"^carvedilol \d+ MG Oral Tablet", "Carvedilol", "1 tablet", "twice a day", "8 AM and 8 PM", "with food", None, LONG_TERM, drug_class="cardio"),
    Sig(r"^24 HR metoprolol succinate \d+ MG", "Metoprolol succinate (extended release)", "1 tablet", "once a day", "8 AM", "with or just after breakfast", None, "Swallow whole; do not crush or chew. " + LONG_TERM, drug_class="cardio"),
    Sig(r"^Furosemide 40 MG Oral Tablet", "Furosemide", "1 tablet", "once a day", "8 AM", "with or without food", None, "You will pass urine more often for a few hours. " + LONG_TERM, drug_class="cardio"),
    Sig(r"^Clopidogrel 75 MG Oral Tablet", "Clopidogrel", "1 tablet", "once a day", "2 PM", "after lunch", None, LONG_TERM, drug_class="cardio"),
    Sig(r"^aspirin 81 MG Oral (Capsule|Tablet)", "Aspirin (low dose)", "1 tablet", "once a day", "2 PM", "after lunch", None, LONG_TERM, form="tablet", drug_class="cardio"),
    Sig(r"^[Ss]imvastatin \d+ MG Oral Tablet", "Simvastatin", "1 tablet", "once a day", "9 PM", "after dinner", None, LONG_TERM, drug_class="cardio"),
    Sig(r"^atorvastatin \d+ MG Oral Tablet", "Atorvastatin", "1 tablet", "once a day", "9 PM", "after dinner", None, LONG_TERM, drug_class="cardio"),
    Sig(r"^rosuvastatin calcium \d+ MG Oral Tablet", "Rosuvastatin", "1 tablet", "once a day", "9 PM", "after dinner", None, LONG_TERM, drug_class="cardio"),
    Sig(r"^24 HR Metformin hydrochloride 500 MG Extended Release", "Metformin (extended release)", "1 tablet", "once a day", "8 PM", "with dinner", None, "Swallow whole; do not crush or chew. " + LONG_TERM, drug_class="general"),
    Sig(r"^Levothyroxine Sodium [\d.]+ MG Oral Tablet", "Levothyroxine", "1 tablet", "once a day", "6:30 AM", "on an empty stomach, 30 minutes before breakfast", None, LONG_TERM, strength="75 mcg", drug_class="general"),
    Sig(r"^Alendronic acid 10 MG Oral Tablet", "Alendronic acid", "1 tablet", "once a day", "6:30 AM", "30 minutes before breakfast, with a full glass of plain water", None, "Stay upright (sitting or standing) for 30 minutes after taking it. " + LONG_TERM, drug_class="bone"),
    Sig(r"^ferrous sulfate 325 MG Oral Tablet", "Ferrous sulphate (iron)", "1 tablet", "once a day", "11 AM", "1 hour before lunch, with water (not with tea, coffee or milk)", 90, "Stools may turn dark; this is expected.", drug_class="general"),
    Sig(r"^60 ACTUAT Fluticasone propionate 0.25 MG/ACTUAT / salmeterol", "Fluticasone + salmeterol inhaler", "1 inhalation", "twice a day", "8 AM and 8 PM", "not applicable", None, "Rinse your mouth with water after each use. " + LONG_TERM, form="dry powder inhaler", strength="250 mcg + 50 mcg per inhalation", drug_class="resp"),
    Sig(r"^120 ACTUAT fluticasone propionate ([\d.]+) MG/ACTUAT Metered Dose Inhaler", "Fluticasone inhaler", "2 puffs", "twice a day", "8 AM and 8 PM", "not applicable", None, "Rinse your mouth with water after each use. " + LONG_TERM, form="inhaler", drug_class="resp"),
    Sig(r"^NDA02\d+ 200 ACTUAT [Aa]lbuterol 0.09 MG/ACTUAT Metered Dose Inhaler", "Salbutamol inhaler", "2 puffs", "only when needed for wheeze or breathlessness", "no more often than every 4 hours", "not applicable", None, "If you need it more often than usual, contact the clinic.", form="inhaler", strength="100 mcg per puff", drug_class="resp"),
    Sig(r"^Acetaminophen (500|325) MG Oral Tablet", "Paracetamol", "1 tablet", "up to three times a day only if fever or pain", "at least 6 hours apart", "after food", 3, "Only when needed for fever or pain.", drug_class="pain"),
    Sig(r"^Acetaminophen 160 MG Chewable Tablet", "Paracetamol chewable", "1 chewable tablet", "up to three times a day only if fever", "at least 6 hours apart", "after food", 3, "Only when needed for fever.", drug_class="pain"),
    Sig(r"^Ibuprofen (100|200|400) MG Oral Tablet", "Ibuprofen", "1 tablet", "up to three times a day only if pain", "at least 8 hours apart", "after food", 5, "Stop and contact the clinic if you get stomach pain or black stools.", drug_class="pain"),
    Sig(r"^Naproxen( sodium)? \d+ MG Oral Tablet", "Naproxen", "1 tablet", "twice a day", "8 AM and 8 PM", "after food", 5, "Stop and contact the clinic if you get stomach pain or black stools.", drug_class="pain"),
    Sig(r"^Amoxicillin (250|500) MG Oral (Tablet|Capsule)", "Amoxicillin", "1 capsule", "three times a day", "8 AM, 2 PM and 9 PM", "after food", 5, "Complete the full course even if you feel better.", form="capsule", drug_class="antibiotic", penicillin=True),
    Sig(r"^[Aa]moxicillin \d+ MG / [Cc]lavulanate 125 MG Oral Tablet", "Amoxicillin + clavulanic acid", "1 tablet", "twice a day", "8 AM and 8 PM", "after food", 7, "Complete the full course even if you feel better.", drug_class="antibiotic", penicillin=True),
    Sig(r"^Penicillin V Potassium \d+ MG Oral Tablet", "Penicillin V", "1 tablet", "four times a day", "6 AM, 12 PM, 6 PM and 10 PM", "1 hour before food", 10, "Complete the full course even if you feel better.", drug_class="antibiotic", penicillin=True),
    Sig(r"^Cefuroxime 250 MG Oral Tablet", "Cefuroxime", "1 tablet", "twice a day", "8 AM and 8 PM", "after food", 7, "Complete the full course even if you feel better.", drug_class="antibiotic"),
    Sig(r"^cephalexin 500 MG Oral Tablet", "Cephalexin", "1 tablet", "four times a day", "6 AM, 12 PM, 6 PM and 10 PM", "with or without food", 7, "Complete the full course even if you feel better.", drug_class="antibiotic"),
    Sig(r"^[Aa]zithromycin 250 MG Oral (Capsule|Tablet)", "Azithromycin", "1 tablet", "once a day", "9 AM", "1 hour before food", 5, "Complete the full course.", form="tablet", drug_class="antibiotic"),
    Sig(r"^ciprofloxacin 500 MG Oral Tablet", "Ciprofloxacin", "1 tablet", "twice a day", "8 AM and 8 PM", "with water; avoid milk or curd within 2 hours", 5, "Complete the full course.", drug_class="antibiotic"),
    Sig(r"^doxycycline hyclate 100 MG", "Doxycycline", "1 capsule", "twice a day", "8 AM and 8 PM", "after food, with a full glass of water", 7, "Stay upright for 30 minutes after each dose. Complete the full course.", form="capsule", drug_class="antibiotic"),
    Sig(r"^nitrofurantoin", "Nitrofurantoin", "1 capsule", "twice a day", "8 AM and 8 PM", "with food", 5, "Urine may turn dark yellow or brown; this is expected. Complete the full course.", strength="100 mg", form="capsule", drug_class="antibiotic"),
    Sig(r"^Loratadine (5|10) MG", "Loratadine", "1 tablet", "once a day", "9 PM", "with or without food", 5, "", drug_class="allergy"),
    Sig(r"^Fexofenadine hydrochloride \d+ MG Oral Tablet", "Fexofenadine", "1 tablet", "once a day", "9 PM", "with water (not with fruit juice)", 5, "", drug_class="allergy"),
    Sig(r"^Hydrocortisone 10 MG/ML Topical Cream", "Hydrocortisone cream", "a thin layer on the affected skin", "twice a day", "morning and night", "not applicable", 7, "For external use only. Do not use on the face unless your doctor said so.", strength="1%", form="cream", drug_class="skin"),
]

_COMPILED = [(re.compile(s.pattern), s) for s in SIG_TABLE]

EXCLUDED_REASONS = re.compile(r"(?i)abuse|neoplasm|cancer|posttraumatic|dental|teeth|gingivitis|contracept|overdose|violence|misuse|referral")

DEPARTMENT_RULES: list[tuple[str, str]] = [
    (r"(?i)pregnan|prenatal|antenatal|menstru|pelvic", "D06"),
    (r"(?i)heart|cardiac|coronary|ischemic|atrial|hypertens|angina", "D07"),
    (r"(?i)otitis|sinusitis|pharyngitis|sore throat|tonsil", "D04"),
    (r"(?i)dermatitis|eczema|rash|acne|skin", "D03"),
    (r"(?i)fracture|sprain|osteoporosis|osteoarthritis|back pain|neck pain|joint|knee|whiplash|chronic pain", "D05"),
]


def _clean_snomed(text: str) -> str:
    text = re.sub(r"\s*\((disorder|finding|procedure|situation|substance|organism|regime/therapy)\)", "", text).strip()
    return text[:1].upper() + text[1:] if text else text


def _parse_strength(desc: str) -> str:
    per_puff = re.search(r"([\d.]+) MG/ACTUAT", desc)
    if per_puff:
        return f"{float(per_puff.group(1)) * 1000:g} mcg per puff"
    parts = re.findall(r"([\d.]+) (MG|UNT|MG/ML)\b", desc)
    if not parts:
        return ""
    return " + ".join(f"{v} {u.lower()}" for v, u in parts)


def _parse_form(desc: str) -> str:
    d = desc.lower()
    for key, form in [("chewable", "chewable tablet"), ("extended release oral tablet", "extended-release tablet"), ("oral capsule", "capsule"), ("oral tablet", "tablet"), ("inhaler", "inhaler"), ("cream", "cream")]:
        if key in d:
            return form
    return "tablet"


def match_sig(description: str) -> Sig | None:
    for rx, sig in _COMPILED:
        if rx.search(description):
            return sig
    return None


@dataclass
class SyntheaPatient:
    synthea_id: str
    birthdate: date
    gender: str
    allergies: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)
    meds: list[dict] = field(default_factory=list)        # mapped, newest first
    encounters: list[dict] = field(default_factory=list)  # outpatient, newest first

    @property
    def penicillin_allergy(self) -> bool:
        return any("penicillin" in a.lower() or "amoxicillin" in a.lower() for a in self.allergies)

    def age_on(self, day: date) -> int:
        return day.year - self.birthdate.year - ((day.month, day.day) < (self.birthdate.month, self.birthdate.day))


def _rows(z: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    member = next(n for n in z.namelist() if n.endswith("/" + name) or n == name)
    with z.open(member) as fh:
        return list(csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8")))


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def load_patients(zip_path: Path = SYNTHEA_ZIP) -> list[SyntheaPatient]:
    """Parsed once per process (the zip is immutable input); callers must not mutate the result."""
    return _load_patients_cached(Path(zip_path).resolve(), zip_path.stat().st_mtime if zip_path.exists() else 0.0)


@lru_cache(maxsize=2)
def _load_patients_cached(zip_path: Path, _mtime: float) -> list[SyntheaPatient]:
    if not zip_path.exists():
        raise FileNotFoundError(f"Synthea data not found at {zip_path}. Run: uv run python scripts/fetch_sources.py")
    with zipfile.ZipFile(zip_path) as z:
        patients = {r["Id"]: SyntheaPatient(r["Id"], date.fromisoformat(r["BIRTHDATE"]), r["GENDER"]) for r in _rows(z, "patients.csv") if not r["DEATHDATE"]}
        for r in _rows(z, "allergies.csv"):
            p = patients.get(r["PATIENT"])
            desc = _clean_snomed(r["DESCRIPTION"])
            if p and not r["STOP"] and desc != "Allergic disposition" and desc not in p.allergies:
                p.allergies.append(desc)
        for r in _rows(z, "conditions.csv"):
            p = patients.get(r["PATIENT"])
            if p and not r["STOP"] and "(disorder)" in r["DESCRIPTION"] and not EXCLUDED_REASONS.search(r["DESCRIPTION"]):
                desc = _clean_snomed(r["DESCRIPTION"])
                if desc not in p.conditions:
                    p.conditions.append(desc)
        for r in _rows(z, "medications.csv"):
            p = patients.get(r["PATIENT"])
            sig = match_sig(r["DESCRIPTION"]) if p else None
            if p and sig:
                p.meds.append({"start": _ts(r["START"]), "stop": _ts(r["STOP"]) if r["STOP"] else None, "description": r["DESCRIPTION"], "code": r["CODE"], "reason": _clean_snomed(r["REASONDESCRIPTION"]), "sig": sig})
        for r in _rows(z, "encounters.csv"):
            p = patients.get(r["PATIENT"])
            if p and r["ENCOUNTERCLASS"] in ("ambulatory", "outpatient", "wellness"):
                reason = r["REASONDESCRIPTION"] or ("General check-up" if r["ENCOUNTERCLASS"] == "wellness" else "")
                if reason and not EXCLUDED_REASONS.search(reason):
                    p.encounters.append({"start": _ts(r["START"]), "reason": _clean_snomed(reason), "code": r["CODE"]})
    for p in patients.values():
        p.meds.sort(key=lambda m: m["start"], reverse=True)
        p.encounters.sort(key=lambda e: e["start"], reverse=True)
    return sorted(patients.values(), key=lambda p: p.synthea_id)


def current_prescriptions(p: SyntheaPatient, limit: int = 3) -> list[dict]:
    """Most recent distinct, allergy-safe medicines for a patient."""
    seen: set[str] = set()
    out: list[dict] = []
    for m in p.meds:
        sig: Sig = m["sig"]
        if sig.drug in seen or (sig.penicillin and p.penicillin_allergy):
            continue
        seen.add(sig.drug)
        out.append({
            **m,
            "drug": sig.drug,
            "strength": sig.strength or _parse_strength(m["description"]),
            "form": sig.form or _parse_form(m["description"]),
        })
        if len(out) >= limit:
            break
    return out


def department_for(reason: str, age: int) -> str:
    if age < 14:
        return "D02"
    for pattern, doc in DEPARTMENT_RULES:
        if re.search(pattern, reason):
            return doc
    return "D01"


def prescriber_for(sig: Sig, age: int) -> str:
    if age < 14:
        return "D02"
    return {"cardio": "D07", "bone": "D05", "skin": "D03"}.get(sig.drug_class, "D01")
