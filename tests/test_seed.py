"""P0 checks: seed is deterministic, counts match the spec, constraints hold."""

from __future__ import annotations

import re
import sqlite3
from datetime import date

import pytest

from careharbor.config import CONTENT_DIR
from careharbor.db import connect, transaction
from careharbor.logging_setup import redact
from scripts.seed import seed

ANCHOR = date(2026, 11, 2)


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "seed.db"
    counts = seed(path, ANCHOR)
    conn = connect(path)
    yield conn, counts, path
    conn.close()


def test_counts_match_spec(db):
    _, counts, _ = db
    assert counts["patients"] == 30
    assert counts["doctors"] == 8
    assert counts["appointments"] == 20
    assert counts["prescriptions"] == 40
    assert counts["slots_upcoming"] > 0
    assert counts["care_leaflets"] == 15
    assert counts["policy_docs"] == 8
    assert counts["medlineplus_docs"] >= 20


def test_most_records_come_from_synthea(db):
    _, counts, _ = db
    assert counts["synthea_patients"] == 26
    assert counts["synthea_rx"] == 32


def test_synthea_import_is_safe(db):
    conn, _, _ = db
    drugs = {r[0].lower() for r in conn.execute("SELECT drug FROM prescriptions")}
    for banned in ("oxycodone", "hydrocodone", "tramadol", "fentanyl", "insulin", "estradiol", "enoxaparin", "codeine"):
        assert not any(banned in d for d in drugs), banned
    # No US identifiers leak in: Synthea names carry digits ("Abe604"); ours never do.
    assert not conn.execute("SELECT COUNT(*) FROM patients WHERE name GLOB '*[0-9]*'").fetchone()[0]
    # Allergy safety: nobody with a penicillin allergy has a penicillin-class prescription.
    clash = conn.execute(
        "SELECT COUNT(*) FROM prescriptions r JOIN patients p ON p.id=r.patient_id "
        "WHERE lower(p.allergies) LIKE '%penicillin%' AND (lower(r.drug) LIKE '%amoxicillin%' OR lower(r.drug) LIKE '%penicillin%')"
    ).fetchone()[0]
    assert clash == 0
    # Every prescription row has a full schedule the PrescriptionAgent can read back verbatim.
    assert not conn.execute("SELECT COUNT(*) FROM prescriptions WHERE dose='' OR frequency='' OR times='' OR food='' OR strength=''").fetchone()[0]


def test_seed_is_deterministic(tmp_path):
    def dump(p):
        c = connect(p)
        rows = {t: c.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in ("patients", "appointments", "prescriptions")}
        c.close()
        return {t: [tuple(r)[:-2] if t == "appointments" else tuple(r) for r in v] for t, v in rows.items()}

    seed(tmp_path / "a.db", ANCHOR)
    seed(tmp_path / "b.db", ANCHOR)
    seed(tmp_path / "b.db", ANCHOR)  # re-seeding an existing DB must work (reset with FKs)
    assert dump(tmp_path / "a.db") == dump(tmp_path / "b.db")


def test_demo_journey_data(db):
    conn, _, _ = db
    p1 = conn.execute("SELECT * FROM patients WHERE id='P001'").fetchone()
    assert p1["phone"] == "9876500001" and p1["dob"] == "1990-05-14"
    past_rao = conn.execute("SELECT COUNT(*) FROM appointments WHERE patient_id='P001' AND doctor_id='D01' AND status='completed'").fetchone()[0]
    assert past_rao == 1
    amox = conn.execute("SELECT * FROM prescriptions WHERE patient_id='P001' AND drug='Amoxicillin'").fetchone()
    assert amox["frequency"] == "three times a day" and amox["food"] == "after food" and amox["duration_days"] == 5
    assert conn.execute("SELECT COUNT(*) FROM appointments WHERE patient_id='P002' AND status='booked'").fetchone()[0] == 1


def test_prescriptions_are_current(db):
    conn, _, _ = db
    from datetime import timedelta

    dates = [date.fromisoformat(r[0]) for r in conn.execute("SELECT visit_date FROM prescriptions")]
    assert all(ANCHOR - timedelta(days=45) <= d <= ANCHOR for d in dates), sorted(dates)[:3]


def test_no_double_booking_constraint(db):
    conn, _, _ = db
    booked = conn.execute("SELECT slot_id, doctor_id FROM appointments WHERE status='booked' LIMIT 1").fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(conn):
            conn.execute(
                "INSERT INTO appointments (patient_id, doctor_id, slot_id, reason, status, created_at, updated_at) VALUES ('P010', ?, ?, 'x', 'booked', 'now', 'now')",
                (booked["doctor_id"], booked["slot_id"]),
            )


def test_appointments_fall_in_doctor_working_days(db):
    conn, _, _ = db
    rows = conn.execute("SELECT s.start_at, d.working_days FROM appointments a JOIN slots s ON s.id=a.slot_id JOIN doctors d ON d.id=a.doctor_id").fetchall()
    from datetime import datetime

    for r in rows:
        assert datetime.fromisoformat(r["start_at"]).strftime("%a") in r["working_days"].split(",")


@pytest.mark.parametrize("folder", ["leaflets", "policies", "medlineplus"])
def test_content_docs_have_front_matter(folder):
    files = list((CONTENT_DIR / folder).glob("*.md"))
    assert files
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert text.startswith("---\n"), f
        assert re.search(r"^title: .+", text, re.M), f
        if folder == "medlineplus":
            assert re.search(r"^source_url: https://medlineplus\.gov/", text, re.M), f
        if folder == "leaflets":
            headings = re.findall(r"^## (.+)$", text, re.M)
            for section in ("Self-care", "See a doctor if", "Emergency signs - call 112 / 108"):
                assert section in headings, f"{f.name} missing '## {section}'"


def test_future_slots_top_up_keeps_existing_data(db):
    from datetime import timedelta

    from careharbor.db import ensure_future_slots

    conn, _, _ = db
    before = conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
    assert ensure_future_slots(conn, today=ANCHOR) == 0  # seed already covers the next 14 days: idempotent
    added = ensure_future_slots(conn, today=ANCHOR + timedelta(days=30))
    assert added > 0 and conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == before
    last = conn.execute("SELECT MAX(start_at) FROM slots").fetchone()[0]
    assert last > str(ANCHOR + timedelta(days=40))


def test_log_redaction():
    out = redact("Patient 9876500001 dob 1990-05-14 otp 123456 slot 2026-11-02T10:00")
    assert "9876500001" not in out and "1990-05-14" not in out and "123456" not in out
    assert "2026-11-02T10:00" in out
