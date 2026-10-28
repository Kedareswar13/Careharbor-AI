"""Build the CareHarbor AI clinic database.

Clinical histories come from the open Synthea dataset (sources/synthea/,
realistic synthetic EHR records - no real patients). Clinic operations data
(doctors, schedules, slots, upcoming bookings) and three demo personas are
defined here. Deterministic: fixed random seed; dates are re-anchored to
"today" in clinic time so upcoming slots stay bookable (use --today to pin).

    uv run python scripts/fetch_sources.py   # once, downloads Synthea
    uv run python scripts/seed.py
    uv run python scripts/seed.py --today 2026-11-02 --db data/test.db
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from careharbor.config import CONTENT_DIR  # noqa: E402
from careharbor.db import clinic_now, connect, ensure_future_slots, init_db, iso, now_iso, reset_db, table_counts, transaction  # noqa: E402
from careharbor.logging_setup import setup_logging  # noqa: E402
from careharbor.synthea import SYNTHEA_ZIP, SyntheaPatient, current_prescriptions, department_for, load_patients, prescriber_for  # noqa: E402

SEED = 20261027
UPCOMING_DAYS = 14
PAST_DAYS = 45
N_PATIENTS = 30
N_APPOINTMENTS = 20
N_PRESCRIPTIONS = 40

DOCTORS = [
    # id, name, specialty, working days, start, end, slot minutes, room
    ("D01", "Dr. Anil Rao", "General Medicine", "Mon,Tue,Wed,Thu,Fri,Sat", "09:00", "13:00", 15, "101"),
    ("D02", "Dr. Meera Iyer", "Paediatrics", "Mon,Tue,Wed,Thu,Fri", "10:00", "14:00", 15, "102"),
    ("D03", "Dr. Farhan Siddiqui", "Dermatology", "Mon,Wed,Fri", "11:00", "15:00", 20, "201"),
    ("D04", "Dr. Kavya Reddy", "ENT", "Tue,Thu,Sat", "09:30", "12:30", 20, "202"),
    ("D05", "Dr. Rohan Kulkarni", "Orthopaedics", "Mon,Tue,Thu,Fri", "14:00", "17:00", 20, "203"),
    ("D06", "Dr. Lakshmi Narayanan", "Gynaecology", "Mon,Wed,Fri,Sat", "10:00", "13:00", 20, "204"),
    ("D07", "Dr. Vikram Mehta", "Cardiology", "Tue,Thu", "15:00", "18:00", 30, "301"),
    ("D08", "Ms. Priya Desai (Physiotherapist)", "Physiotherapy", "Mon,Tue,Wed,Thu,Fri,Sat", "08:00", "12:00", 45, "Physio Bay"),
]

FIRST_NAMES = {
    "M": ["Vihaan", "Arjun", "Karthik", "Siddharth", "Imran", "Manoj", "Suresh", "Nikhil", "Pranav", "Joseph", "Harish", "Gaurav", "Venkat", "Rohit", "Aditya", "Sameer"],
    "F": ["Priya", "Sneha", "Kavitha", "Fatima", "Deepa", "Neha", "Lakshmi", "Pooja", "Divya", "Meenakshi", "Ritu", "Shalini", "Asha", "Nandini", "Swathi", "Rekha"],
}
LAST_NAMES = ["Patel", "Nair", "Reddy", "Khan", "Gupta", "Menon", "Das", "Joshi", "Pillai", "Verma", "Bhat", "Kulkarni", "Fernandes", "Singh", "Hegde", "Shetty", "Chopra", "Mishra"]
LANGUAGES = ["en", "en", "en", "hi", "hi", "kn", "ta", "te"]

# Fixed demo personas so the three journeys are easy to show (clinic-authored records).
DEMO_PATIENTS = [
    # id, name, phone, dob, gender, language, allergies, preferred doctor, conditions, source
    ("P001", "Aarav Sharma", "9876500001", "1990-05-14", "M", "en", "None known", "D01", "", "demo-persona"),
    ("P002", "Ananya Iyer", "9876500002", "1985-11-02", "F", "en", "Penicillin", "D06", "Vitamin D deficiency", "demo-persona"),
    ("P003", "Rahul Verma", "9876500003", "1972-03-28", "M", "hi", "None known", "D07", "Essential hypertension, Hyperlipidemia", "demo-persona"),
    # QA / regression test account: stable identity + records used by the golden dataset after every build.
    ("P004", "QA Test Patient", "9000000001", "2000-01-01", "F", "en", "Sulfa drugs", "D01", "", "qa-test-account"),
]
DEMO_RX = [
    # patient, doctor, days ago, drug, strength, form, dose, frequency, times, food, days, reason, notes
    ("P001", "D01", 2, "Amoxicillin", "500 mg", "capsule", "1 capsule", "three times a day", "8 AM, 2 PM and 9 PM", "after food", 5, "Acute bacterial pharyngitis", "Complete the full course even if you feel better."),
    ("P001", "D01", 2, "Paracetamol", "650 mg", "tablet", "1 tablet", "up to three times a day only if fever or pain", "at least 6 hours apart", "after food", 3, "Fever", "Only when needed for fever above 100 F or pain."),
    ("P001", "D01", 2, "Pantoprazole", "40 mg", "tablet", "1 tablet", "once a day", "7:30 AM", "30 minutes before breakfast", 14, "Acid reflux", "Take on an empty stomach."),
    ("P002", "D06", 20, "Vitamin D3 (cholecalciferol)", "60000 IU", "sachet", "1 sachet mixed in a glass of milk", "once a week", "Sunday morning", "after breakfast", 56, "Vitamin D deficiency", "8 weekly doses."),
    ("P003", "D07", 18, "Amlodipine", "5 mg", "tablet", "1 tablet", "once a day", "8 AM", "with or without food", 90, "Essential hypertension", "Long-term medicine. Do not stop or change it without speaking to your doctor."),
    ("P003", "D07", 18, "Atorvastatin", "10 mg", "tablet", "1 tablet", "once a day", "9 PM", "after dinner", 90, "Hyperlipidemia", "Long-term medicine. Do not stop or change it without speaking to your doctor."),
    ("P004", "D01", 1, "Cetirizine", "10 mg", "tablet", "1 tablet", "once a day", "9 PM", "with or without food", 5, "Seasonal allergy", "May cause drowsiness."),
    ("P004", "D01", 1, "Ibuprofen", "400 mg", "tablet", "1 tablet", "twice a day", "8 AM and 8 PM", "after food", 3, "Sprained ankle", "Stop and contact the clinic if you get stomach pain or black stools."),
]


def build_slots(today: date) -> list[tuple]:
    slots: list[tuple] = []
    sid = 0
    for offset in range(-PAST_DAYS, UPCOMING_DAYS):
        day = today + timedelta(days=offset)
        for doc_id, _n, _s, days, start, end, minutes, _r in DOCTORS:
            if day.strftime("%a") not in days.split(","):
                continue
            t = datetime.combine(day, datetime.strptime(start, "%H:%M").time())
            stop = datetime.combine(day, datetime.strptime(end, "%H:%M").time())
            while t + timedelta(minutes=minutes) <= stop:
                sid += 1
                slots.append((sid, doc_id, iso(t), iso(t + timedelta(minutes=minutes))))
                t += timedelta(minutes=minutes)
    return slots


class SlotPicker:
    def __init__(self, slots: list[tuple], rng: random.Random):
        self.slots, self.rng, self.taken = slots, rng, set()

    def near(self, doctor_id: str, target: datetime, lo: datetime, hi: datetime) -> tuple | None:
        """Free slot for the doctor closest to the target time, within [lo, hi)."""
        options = [s for s in self.slots if s[1] == doctor_id and s[0] not in self.taken and lo <= datetime.fromisoformat(s[2]) < hi]
        if not options:
            return None
        options.sort(key=lambda s: (abs((datetime.fromisoformat(s[2]) - target).total_seconds()), s[0]))
        slot = self.rng.choice(options[:4])
        self.taken.add(slot[0])
        return slot


def choose_synthea_patients(pool: list[SyntheaPatient], today: date, rng: random.Random) -> list[SyntheaPatient]:
    usable = [p for p in pool if current_prescriptions(p) and p.encounters and p.age_on(today) <= 85]
    children = [p for p in usable if p.age_on(today) < 14]
    adults = [p for p in usable if p.age_on(today) >= 14]
    picked = rng.sample(children, min(3, len(children)))
    picked += rng.sample(adults, N_PATIENTS - len(DEMO_PATIENTS) - len(picked))
    return picked


def seed(db_path: Path | None, today: date | None, synthea_zip: Path = SYNTHEA_ZIP) -> dict[str, int]:
    rng = random.Random(SEED)
    now = clinic_now()
    today = today or now.date()
    anchor = datetime.combine(today, now.time()) if today == now.date() else datetime.combine(today, datetime.min.time().replace(hour=8))

    conn = connect(db_path)
    reset_db(conn)

    slots = build_slots(today)
    picker = SlotPicker(slots, rng)
    created = now_iso()
    past_lo = anchor - timedelta(days=PAST_DAYS)

    patients: list[tuple] = list(DEMO_PATIENTS)
    appointments: list[tuple] = []
    prescriptions: list[tuple] = []

    def add_appt(pid: str, slot: tuple | None, status: str, reason: str) -> None:
        if slot is not None:
            appointments.append((len(appointments) + 1, pid, slot[1], slot[0], reason, status, None, created, created))

    def add_rx(pid: str, doc: str, visit: date, drug: str, strength: str, form: str, dose: str, freq: str, times: str, food: str, days: int, reason: str, notes: str, source: str) -> None:
        prescriptions.append((len(prescriptions) + 1, pid, doc, visit.isoformat(), drug, strength, form, dose, freq, times, food, days, visit.isoformat(), reason, notes, source))

    # --- Demo personas ---------------------------------------------------
    add_appt("P001", picker.near("D01", anchor - timedelta(days=30), past_lo, anchor - timedelta(days=21)), "completed", "Fever and sore throat")
    add_appt("P002", picker.near("D06", anchor + timedelta(days=4), anchor + timedelta(hours=4), anchor + timedelta(days=UPCOMING_DAYS)), "booked", "Routine check-up")
    add_appt("P003", picker.near("D07", anchor - timedelta(days=18), past_lo, anchor - timedelta(days=1)), "completed", "Blood pressure review")
    for pid, doc, ago, *rest in DEMO_RX:
        add_rx(pid, doc, today - timedelta(days=ago), *rest, source="qa-test-account" if pid == "P004" else "demo-persona")

    # --- Synthea-derived patients ----------------------------------------
    chosen = choose_synthea_patients(load_patients(synthea_zip), today, rng)
    used_names: set[str] = {p[1] for p in patients}
    records: list[dict] = []
    for idx, sp in enumerate(chosen, start=len(DEMO_PATIENTS) + 1):
        pid = f"P{idx:03d}"
        while True:
            name = f"{rng.choice(FIRST_NAMES[sp.gender])} {rng.choice(LAST_NAMES)}"
            if name not in used_names:
                used_names.add(name)
                break
        age = sp.age_on(today)
        # Index visit = when the newest medicine was prescribed. Re-anchor the timeline so it was 3-40 days ago.
        all_rx = current_prescriptions(sp)
        index_dt = all_rx[0]["start"]
        shift = (anchor - timedelta(days=rng.randint(3, 40))) - index_dt
        last_visit = (index_dt + shift).date()
        # Current medicines only: courses from the index visit (within 30 days) plus long-term medicines.
        rxs = [rx for rx in all_rx if rx["sig"].days is None or (index_dt - rx["start"]).days <= 30]
        enc = min(sp.encounters, key=lambda e: abs((e["start"] - index_dt).total_seconds()))
        visit_reason = rxs[0]["reason"] or enc["reason"]
        if visit_reason == "Allergic disposition":
            visit_reason = "Allergy review"
        patients.append((
            pid, name, f"98765{idx:05d}", sp.birthdate.isoformat(), sp.gender, rng.choice(LANGUAGES),
            ", ".join(sp.allergies) or "None known", None, ", ".join(sp.conditions[:4]), f"synthea:{sp.synthea_id[:8]}",
        ))
        records.append({"pid": pid, "sp": sp, "age": age, "rxs": rxs, "last_visit": last_visit, "visit_reason": visit_reason, "visit_at": index_dt + shift})

    # Prescriptions: one per patient first, then extra medicines until the target count.
    for round_ in range(3):
        for rec in records:
            if len(prescriptions) >= N_PRESCRIPTIONS or round_ >= len(rec["rxs"]):
                continue
            rx = rec["rxs"][round_]
            sig = rx["sig"]
            add_rx(rec["pid"], prescriber_for(sig, rec["age"]), rec["last_visit"], rx["drug"], rx["strength"], rx["form"], sig.dose, sig.frequency, sig.times, sig.food,
                   sig.days or 90, rx["reason"], sig.notes, f"synthea:medications.csv:{rx['code']}")

    # Appointments: past visits = the Synthea index visit, then cancelled and upcoming bookings.
    for rec in records:
        if sum(1 for a in appointments if a[5] == "completed") >= 11:
            break
        doc = department_for(rec["visit_reason"], rec["age"])
        add_appt(rec["pid"], picker.near(doc, rec["visit_at"], past_lo, anchor - timedelta(hours=1)), "completed", rec["visit_reason"])
    rng.shuffle(records)
    for rec in records:
        if len(appointments) >= N_APPOINTMENTS:
            break
        cancelled = sum(1 for a in appointments if a[5] == "cancelled") < 2
        cond = rec["sp"].conditions[0] if rec["sp"].conditions else ""
        reason = f"{cond} review" if cond else "Follow-up visit"
        doc = department_for(reason, rec["age"])
        target = anchor + timedelta(days=rng.randint(1, UPCOMING_DAYS - 2), hours=rng.randint(0, 6))
        add_appt(rec["pid"], picker.near(doc, target, anchor + timedelta(hours=2), anchor + timedelta(days=UPCOMING_DAYS)), "cancelled" if cancelled else "booked", reason)

    with transaction(conn):
        conn.executemany("INSERT INTO doctors VALUES (?,?,?,?,?,?,?,?)", DOCTORS)
        conn.executemany("INSERT INTO patients VALUES (?,?,?,?,?,?,?,?,?,?)", patients)
        conn.executemany("INSERT INTO slots VALUES (?,?,?,?)", slots)
        conn.executemany("INSERT INTO appointments VALUES (?,?,?,?,?,?,?,?,?)", appointments)
        conn.executemany("INSERT INTO prescriptions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", prescriptions)
    counts = table_counts(conn)
    counts["slots_upcoming"] = conn.execute("SELECT COUNT(*) FROM slots WHERE start_at >= ?", (iso(anchor),)).fetchone()[0]
    counts["synthea_patients"] = conn.execute("SELECT COUNT(*) FROM patients WHERE source_ref LIKE 'synthea:%'").fetchone()[0]
    counts["synthea_rx"] = conn.execute("SELECT COUNT(*) FROM prescriptions WHERE source_ref LIKE 'synthea:%'").fetchone()[0]
    conn.close()
    counts["care_leaflets"] = len(list((CONTENT_DIR / "leaflets").glob("*.md")))
    counts["policy_docs"] = len(list((CONTENT_DIR / "policies").glob("*.md")))
    counts["medlineplus_docs"] = len(list((CONTENT_DIR / "medlineplus").glob("*.md")))
    return counts


def main() -> int:
    setup_logging()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=None, help="SQLite file (default: DB_PATH from .env)")
    ap.add_argument("--today", type=date.fromisoformat, default=None, help="Anchor date YYYY-MM-DD (default: today in clinic time)")
    ap.add_argument("--if-missing", action="store_true", help="Keep an existing database (bookings, chat history) and only top up future slots")
    args = ap.parse_args()
    if args.if_missing:
        conn = connect(args.db)
        try:
            has_data = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='patients'").fetchone() and conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0] > 0
            if has_data:
                init_db(conn)
                added = ensure_future_slots(conn)
                print(f"CareHarbor AI database kept (chats and bookings preserved); {added} new future slots added. Use -Reseed / without --if-missing to reset.")
                return 0
        finally:
            conn.close()
    counts = seed(args.db, args.today)
    print("CareHarbor AI clinic data seeded ✅  (clinical histories: Synthea open dataset)")
    for k, v in counts.items():
        print(f"  {k:<18} {v:>5}")
    print("Demo identities: P001 Aarav Sharma 9876500001 / 1990-05-14 · P002 Ananya Iyer 9876500002 / 1985-11-02 · P003 Rahul Verma 9876500003 / 1972-03-28")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
