"""P1: the 12 MCP tools - behaviour, PHI isolation, idempotency, double-booking, allow-lists."""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timedelta

import pytest

from careharbor.db import connect
from careharbor.tools.client import ToolClient, ToolNotAllowed
from careharbor.tools.mcp_server import TOOL_NAMES, build_server
from careharbor.tools.service import ClinicTools, parse_dob
from scripts.seed import seed

ANCHOR = date(2026, 11, 2)  # a Monday
NOW = datetime(2026, 11, 2, 8, 0)
S1, S2 = "sess-aarav-0001", "sess-ananya-0002"


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "tools.db"
    seed(path, ANCHOR)
    return path


@pytest.fixture()
def tools(db_path):
    return ClinicTools(db_path, clock=lambda: NOW, demo_mode=True)


def verify(tools: ClinicTools, session: str, phone: str, dob: str) -> None:
    r = tools.verify_identity(session, phone, dob)
    assert r["ok"] and r["status"] == "otp_sent", r
    assert tools.confirm_otp(session, r["demo_otp"])["status"] == "verified"


def a_free_slot(tools: ClinicTools, doctor: str = "D01", day: str = "2026-11-06") -> dict:
    return tools.get_slots(doctor, day)["slots"][0]


# ---------------------------------------------------------------- schema / server
async def test_server_exposes_exactly_12_tools_without_identity_args(tools):
    client = ToolClient(build_server(tools))
    try:
        assert await client.list_tools() == sorted(TOOL_NAMES)
        assert len(TOOL_NAMES) == 12
        from fastmcp import Client

        async with Client(build_server(tools)) as c:
            for t in await c.list_tools():
                props = set((t.input_schema or {}).get("properties", {}))
                assert not props & {"patient_id", "session_id", "ctx"}, (t.name, props)
    finally:
        await client.stop()


# ------------------------------------------------------------------- read tools
def test_find_doctors_by_name_and_plain_language_specialty(tools):
    assert [d["doctor_id"] for d in tools.find_doctors(name="Dr. Rao")["doctors"]] == ["D01"]
    assert [d["doctor_id"] for d in tools.find_doctors(specialty="skin")["doctors"]] == ["D03"]
    assert [d["doctor_id"] for d in tools.find_doctors(specialty="my child")["doctors"]] == ["D02"]
    assert tools.find_doctors(name="Nobody")["count"] == 0


def test_get_slots_returns_only_free_future_slots(tools, db_path):
    fri = tools.get_slots("D01", "2026-11-06")
    assert fri["ok"] and fri["count"] > 0
    assert all(s["start_at"].startswith("2026-11-06") for s in fri["slots"])
    conn = connect(db_path)
    booked = {r[0] for r in conn.execute("SELECT slot_id FROM appointments WHERE status='booked'")}
    assert not booked & {s["slot_id"] for s in tools.get_slots("D06", "2026-11-02", "2026-11-14", limit=200)["slots"]}
    assert tools.get_slots("D01", "2026-10-01")["count"] == 0  # past dates are clipped
    assert tools.get_slots("D01", "next friday")["error"] == "bad_date"
    assert tools.get_slots("D99", "2026-11-06")["error"] == "unknown_doctor"


def test_search_tools_ground_in_the_right_documents(tools):
    care = tools.search_care_leaflets("I've had a headache since morning")
    assert care["results"][0]["doc_id"] == "leaflet-headache"
    assert care["citations"][0]["source"] == "Clinic care leaflet"
    faq = tools.search_clinic_faq("Do you accept Star Health insurance?")
    assert faq["results"][0]["doc_id"] == "policy-insurance"


# --------------------------------------------------------------- identity gate
@pytest.mark.parametrize("call", [
    lambda t: t.get_prescriptions(S1),
    lambda t: t.list_my_appointments(S1),
    lambda t: t.book_appointment(S1, 1, "x"),
    lambda t: t.cancel_appointment(S1, 2),
    lambda t: t.reschedule_appointment(S1, 2, 3),
])
def test_phi_and_write_tools_require_verification(tools, call):
    r = call(tools)
    assert r == {"ok": False, "error": "identity_required", "message": r["message"]}


def test_identity_flow_and_generic_failure(tools):
    bad = tools.verify_identity(S1, "9876500001", "1990-05-15")
    assert bad["error"] == "not_matched" and "phone" in bad["message"] and "demo_otp" not in bad
    r = tools.verify_identity(S1, "+91 98765 00001", "14/05/1990")
    assert r["status"] == "otp_sent" and r["sent_to"] == "******0001" and len(r["demo_otp"]) == 6
    assert tools.confirm_otp(S1, "000000" if r["demo_otp"] != "000000" else "111111")["error"] == "otp_wrong"
    assert tools.session_patient(S1) is None  # still not verified
    ok = tools.confirm_otp(S1, r["demo_otp"])
    assert ok["status"] == "verified" and ok["first_name"] == "Aarav"
    assert tools.session_patient(S1) == "P001"


def test_identity_lockout_after_repeated_failures(tools):
    for _ in range(5):
        tools.verify_identity(S1, "9876500001", "2000-01-01")
    assert tools.verify_identity(S1, "9876500001", "1990-05-14")["error"] == "locked"


def test_otp_expires(db_path):
    clock = {"now": NOW}
    t = ClinicTools(db_path, clock=lambda: clock["now"], demo_mode=True)
    code = t.verify_identity(S1, "9876500001", "1990-05-14")["demo_otp"]
    clock["now"] = NOW + timedelta(minutes=6)
    assert t.confirm_otp(S1, code)["error"] == "otp_expired"


@pytest.mark.parametrize("raw,expected", [("1990-05-14", "1990-05-14"), ("14/05/1990", "1990-05-14"), ("14-5-1990", "1990-05-14"), ("14 May 1990", "1990-05-14"), ("May 14, 1990", "1990-05-14"), ("31/02/1990", None), ("yesterday", None)])
def test_parse_dob(raw, expected):
    assert parse_dob(raw) == expected


# -------------------------------------------------------------- prescriptions
def test_prescriptions_are_only_the_verified_patients(tools):
    verify(tools, S1, "9876500001", "1990-05-14")
    r = tools.get_prescriptions(S1)
    drugs = [p["drug"] for p in r["prescriptions"]]
    assert sorted(drugs) == ["Amoxicillin", "Pantoprazole", "Paracetamol"]
    amox = next(p for p in r["prescriptions"] if p["drug"] == "Amoxicillin")
    assert (amox["dose"], amox["frequency"], amox["times"], amox["food"], amox["duration_days"]) == ("1 capsule", "three times a day", "8 AM, 2 PM and 9 PM", "after food", 5)
    assert all("patient_id" not in p for p in r["prescriptions"])


# ------------------------------------------------------------------- bookings
def test_book_rereads_db_and_is_idempotent(tools, db_path):
    verify(tools, S1, "9876500001", "1990-05-14")
    slot = a_free_slot(tools)
    first = tools.book_appointment(S1, slot["slot_id"], "Follow-up for sore throat")
    assert first["ok"] and first["appointment"]["status"] == "booked" and first["appointment"]["doctor"] == "Dr. Anil Rao"
    again = tools.book_appointment(S1, slot["slot_id"], "Follow-up for sore throat")
    assert again["idempotent_replay"] and again["appointment"]["appointment_id"] == first["appointment"]["appointment_id"]
    row = connect(db_path).execute("SELECT patient_id, status FROM appointments WHERE id=?", (first["appointment"]["appointment_id"],)).fetchone()
    assert tuple(row) == ("P001", "booked")
    assert slot["slot_id"] not in {s["slot_id"] for s in tools.get_slots("D01", "2026-11-06")["slots"]}


def test_no_double_booking_across_patients(tools, db_path):
    verify(tools, S1, "9876500001", "1990-05-14")
    verify(tools, S2, "9876500002", "1985-11-02")
    slot = a_free_slot(tools)
    assert tools.book_appointment(S1, slot["slot_id"])["ok"]
    assert tools.book_appointment(S2, slot["slot_id"])["error"] == "slot_taken"
    n = connect(db_path).execute("SELECT COUNT(*) FROM appointments WHERE slot_id=? AND status='booked'", (slot["slot_id"],)).fetchone()[0]
    assert n == 1


def test_concurrent_booking_race_has_one_winner(tools, db_path):
    verify(tools, S1, "9876500001", "1990-05-14")
    verify(tools, S2, "9876500002", "1985-11-02")
    slot = a_free_slot(tools, "D03", "2026-11-04")
    results: list[dict] = []
    barrier = threading.Barrier(2)

    def go(sess):
        barrier.wait()
        results.append(ClinicTools(db_path, clock=lambda: NOW).book_appointment(sess, slot["slot_id"]))

    threads = [threading.Thread(target=go, args=(s,)) for s in (S1, S2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(r["ok"] for r in results) == [False, True]


def test_cannot_touch_another_patients_appointment(tools, db_path):
    verify(tools, S1, "9876500001", "1990-05-14")
    ananya_appt = connect(db_path).execute("SELECT id FROM appointments WHERE patient_id='P002' AND status='booked'").fetchone()[0]
    assert tools.cancel_appointment(S1, ananya_appt)["error"] == "not_found"
    assert tools.reschedule_appointment(S1, ananya_appt, a_free_slot(tools)["slot_id"])["error"] == "not_found"
    assert connect(db_path).execute("SELECT status FROM appointments WHERE id=?", (ananya_appt,)).fetchone()[0] == "booked"


def test_reschedule_and_cancel_own_appointment(tools, db_path):
    verify(tools, S2, "9876500002", "1985-11-02")
    appt = tools.list_my_appointments(S2)["upcoming"][0]
    new = a_free_slot(tools, "D06", "2026-11-13")
    r = tools.reschedule_appointment(S2, appt["appointment_id"], new["slot_id"])
    assert r["ok"] and r["appointment"]["slot_id"] == new["slot_id"] and r["previous"]["start_at"] == appt["start_at"]
    freed = {s["slot_id"] for s in tools.get_slots("D06", appt["start_at"][:10], limit=100)["slots"]}
    assert appt["slot_id"] in freed
    c = tools.cancel_appointment(S2, appt["appointment_id"])
    assert c["ok"] and c["appointment"]["status"] == "cancelled"
    assert tools.cancel_appointment(S2, appt["appointment_id"])["idempotent_replay"]
    assert tools.list_my_appointments(S2)["upcoming"] == []


def test_changes_blocked_within_two_hours(db_path):
    t0 = ClinicTools(db_path, clock=lambda: NOW, demo_mode=True)
    verify(t0, S2, "9876500002", "1985-11-02")
    appt = t0.list_my_appointments(S2)["upcoming"][0]
    late = ClinicTools(db_path, clock=lambda: datetime.fromisoformat(appt["start_at"]) - timedelta(minutes=90))
    assert late.cancel_appointment(S2, appt["appointment_id"])["error"] == "too_late"


def test_max_three_upcoming_appointments(tools):
    verify(tools, S1, "9876500001", "1990-05-14")
    slots = tools.get_slots("D08", "2026-11-03", "2026-11-07", limit=100)["slots"]
    day_seen, picked = set(), []
    for s in slots:
        if s["start_at"][:10] not in day_seen:
            day_seen.add(s["start_at"][:10])
            picked.append(s)
    results = [tools.book_appointment(S1, s["slot_id"])["ok"] for s in picked[:4]]
    assert results == [True, True, True, False]


# -------------------------------------------------------------------- handoff
def test_create_handoff_binds_patient_and_dedupes(tools, db_path):
    verify(tools, S1, "9876500001", "1990-05-14")
    r = tools.create_handoff(S1, "Wants to talk to a person\nAbout bill\nPrefers call", "high")
    assert r["ok"] and r["priority"] == "high"
    assert tools.create_handoff(S1, "Wants to talk to a person\nAbout bill\nPrefers call", "high")["idempotent_replay"]
    row = connect(db_path).execute("SELECT patient_id, kind, status FROM handoffs WHERE id=?", (r["ticket_id"],)).fetchone()
    assert tuple(row) == ("P001", "handoff", "open")


# ------------------------------------------------------- MCP layer + allow-lists
async def test_mcp_session_binding_and_allowlist(tools):
    client = ToolClient(build_server(tools))
    try:
        assert (await client.call("prescription", "get_prescriptions", session_id=S1))["error"] == "identity_required"
        r = await client.call("identity", "verify_identity", {"phone": "9876500001", "dob": "1990-05-14"}, session_id=S1)
        await client.call("identity", "confirm_otp", {"code": r["demo_otp"]}, session_id=S1)
        rx = await client.call("prescription", "get_prescriptions", session_id=S1)
        assert rx["ok"] and len(rx["prescriptions"]) == 3
        # Same tool, different (unverified) session -> no data. Identity is bound to the session, not arguments.
        assert (await client.call("prescription", "get_prescriptions", session_id=S2))["error"] == "identity_required"
        missing = await client.call("prescription", "get_prescriptions")
        assert missing["ok"] is False and "session" in missing["message"].lower()
        with pytest.raises(ToolNotAllowed):
            await client.call("faq", "book_appointment", {"slot_id": 1}, session_id=S1)
        with pytest.raises(ToolNotAllowed):
            await client.call("triage", "get_prescriptions", session_id=S1)
    finally:
        await client.stop()


def test_audit_log_records_calls_without_raw_pii(tools, db_path):
    tools.verify_identity(S1, "9876500001", "1990-05-15")
    verify(tools, S1, "9876500001", "1990-05-14")
    tools.get_prescriptions(S1)
    rows = connect(db_path).execute("SELECT event, detail FROM audit_log").fetchall()
    events = [r[0] for r in rows]
    assert {"verify_identity.failed", "verify_identity.otp_sent", "confirm_otp.verified", "get_prescriptions"} <= set(events)
    assert not any("9876500001" in r[1] or "1990-05-14" in r[1] for r in rows)


async def test_stdio_transport_lists_tools():
    client = ToolClient(transport="stdio")
    try:
        assert await client.list_tools() == sorted(TOOL_NAMES)
    finally:
        await client.stop()


def test_db_rejects_double_booking_even_if_code_is_bypassed(db_path):
    conn = connect(db_path)
    row = conn.execute("SELECT slot_id, doctor_id FROM appointments WHERE status='booked' LIMIT 1").fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO appointments (patient_id, doctor_id, slot_id, status, created_at, updated_at) VALUES ('P005', ?, ?, 'booked', 'x', 'x')", (row[1], row[0]))
