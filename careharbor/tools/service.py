"""CareHarbor AI tool implementations (transport-agnostic).

The FastMCP server (careharbor/tools/mcp_server.py) exposes these functions as
MCP tools. Key rules enforced HERE, server-side, not in prompts:

- PHI and write tools never take a patient_id argument. They take the chat
  session id (bound by the host from verified graph state) and resolve the
  patient from the identity_sessions table. Unverified -> "identity_required".
- Every booking/reschedule/cancel runs in a BEGIN IMMEDIATE transaction, is
  idempotent and re-reads the stored row so callers can only confirm what the
  database actually holds.
- Every tool call is written to the audit log (PII masked).
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from careharbor.config import get_settings
from careharbor.db import VERIFIED_SQL, audit, clinic_now, connect, iso, now_iso, transaction, verified_cutoff
from careharbor.logging_setup import redact
from careharbor.rag import get_index
from careharbor.security import memory_key, otp_digest, phone_key

OTP_TTL_MIN = 5
MAX_OTP_ATTEMPTS = 3
MAX_IDENTITY_ATTEMPTS = 5
# Across ALL sessions: a new session can't reset these, so guessing a date of birth for a known number is blocked.
MAX_PHONE_FAILURES = 5
PHONE_LOCK_WINDOW = timedelta(minutes=15)
MAX_UPCOMING = 3
CHANGE_CUTOFF = timedelta(hours=2)
BOOKING_HORIZON_DAYS = 14
CACHE_TTL_S = 30.0

SPECIALTY_ALIASES = {
    "General Medicine": ["general", "physician", "gp", "family", "medicine", "fever", "cold", "cough"],
    "Paediatrics": ["paediatric", "pediatric", "child", "children", "kid", "baby", "infant"],
    "Dermatology": ["derma", "skin", "rash", "acne", "hair"],
    "ENT": ["ent", "ear", "nose", "throat", "sinus"],
    "Orthopaedics": ["ortho", "bone", "joint", "knee", "back", "fracture", "sprain"],
    "Gynaecology": ["gynae", "gyne", "gyn", "women", "pregnan", "period", "obstet"],
    "Cardiology": ["cardio", "heart", "bp", "blood pressure", "hypertension"],
    "Physiotherapy": ["physio", "exercise", "rehab"],
}


def ok(**data: Any) -> dict[str, Any]:
    return {"ok": True, **data}


def fail(error: str, message: str, **data: Any) -> dict[str, Any]:
    return {"ok": False, "error": error, "message": message, **data}


def normalise_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits


_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def parse_dob(value: str) -> str | None:
    """Accept 1990-05-14, 14/05/1990, 14-05-1990, 14.05.1990, 14 May 1990, May 14 1990."""
    v = (value or "").strip().lower().replace(",", " ")
    try:
        if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", v):
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        if m := re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})", v):
            return date(int(m[3]), int(m[2]), int(m[1])).isoformat()
        if m := re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3})[a-z]*\s+(\d{4})", v):
            return date(int(m[3]), _MONTHS[m[2]], int(m[1])).isoformat()
        if m := re.fullmatch(r"([a-z]{3})[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?\s+(\d{4})", v):
            return date(int(m[3]), _MONTHS[m[1]], int(m[2])).isoformat()
    except (ValueError, KeyError):
        return None
    return None


def mask_phone(phone: str) -> str:
    return "******" + phone[-4:] if len(phone) >= 4 else "****"


class _TTLCache:
    def __init__(self, ttl: float):
        self.ttl, self._data, self._lock = ttl, {}, threading.Lock()

    def get(self, key: Any) -> Any:
        with self._lock:
            hit = self._data.get(key)
            if hit and time.monotonic() - hit[0] < self.ttl:
                return hit[1]
            return None

    def put(self, key: Any, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class ClinicTools:
    """All 12 CareHarbor AI tools. One instance per process; a fresh SQLite connection per call."""

    def __init__(self, db_path: Path | str | None = None, clock: Callable[[], datetime] = clinic_now, demo_mode: bool | None = None):
        self.db_path = Path(db_path) if db_path else get_settings().db_file
        self.clock = clock
        self.demo_mode = get_settings().demo_mode if demo_mode is None else demo_mode
        self.read_cache = _TTLCache(CACHE_TTL_S)

    # ------------------------------------------------------------------ helpers
    def _conn(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def _audit(self, conn: sqlite3.Connection, event: str, session_id: str | None, **detail: Any) -> None:
        safe = {k: redact(str(v)) if isinstance(v, str) else v for k, v in detail.items()}
        audit(conn, "tool", event, session_id, **safe)

    def _phone_locked(self, conn: sqlite3.Connection, phone: str) -> bool:
        row = conn.execute("SELECT failures, window_start FROM identity_throttle WHERE key=?", (phone_key(phone),)).fetchone()
        if not row:
            return False
        fresh = datetime.fromisoformat(row["window_start"]) > datetime.fromisoformat(now_iso()) - PHONE_LOCK_WINDOW
        return fresh and row["failures"] >= MAX_PHONE_FAILURES

    def _phone_failed(self, conn: sqlite3.Connection, phone: str) -> None:
        now = now_iso()
        start = (datetime.fromisoformat(now) - PHONE_LOCK_WINDOW).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO identity_throttle (key, failures, window_start) VALUES (?, 1, ?) ON CONFLICT(key) DO UPDATE SET "
            "failures = CASE WHEN identity_throttle.window_start > ? THEN identity_throttle.failures + 1 ELSE 1 END, "
            "window_start = CASE WHEN identity_throttle.window_start > ? THEN identity_throttle.window_start ELSE excluded.window_start END",
            (phone_key(phone), now, start, start))

    def session_patient(self, session_id: str, conn: sqlite3.Connection | None = None) -> str | None:
        """patient_id bound to a VERIFIED session, else None. The only way tools learn who the patient is."""
        own = conn is None
        conn = conn or self._conn()
        try:
            row = conn.execute(f"SELECT patient_id FROM identity_sessions WHERE session_id=? AND {VERIFIED_SQL}", (session_id, verified_cutoff())).fetchone()
            return row["patient_id"] if row else None
        finally:
            if own:
                conn.close()

    def _slot_view(self, conn: sqlite3.Connection, slot_id: int) -> dict | None:
        row = conn.execute(
            "SELECT s.id AS slot_id, s.start_at, s.end_at, d.id AS doctor_id, d.name AS doctor, d.specialty, d.room "
            "FROM slots s JOIN doctors d ON d.id=s.doctor_id WHERE s.id=?",
            (slot_id,),
        ).fetchone()
        return dict(row) if row else None

    def _appointment_view(self, conn: sqlite3.Connection, appointment_id: int) -> dict | None:
        row = conn.execute(
            "SELECT a.id AS appointment_id, a.patient_id, a.status, a.reason, s.id AS slot_id, s.start_at, s.end_at, "
            "d.id AS doctor_id, d.name AS doctor, d.specialty, d.room "
            "FROM appointments a JOIN slots s ON s.id=a.slot_id JOIN doctors d ON d.id=a.doctor_id WHERE a.id=?",
            (appointment_id,),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _public(appt: dict) -> dict:
        return {k: v for k, v in appt.items() if k != "patient_id"}

    # --------------------------------------------------------------- 1 find_doctors
    def find_doctors(self, specialty: str | None = None, name: str | None = None) -> dict:
        key = ("find_doctors", (specialty or "").lower(), (name or "").lower())
        if (cached := self.read_cache.get(key)) is not None:
            return cached
        with closing(self._conn()) as conn:
            rows = [dict(r) for r in conn.execute("SELECT id AS doctor_id, name, specialty, working_days, start_time, end_time, slot_minutes, room FROM doctors ORDER BY id")]
        if specialty:
            s = specialty.lower().strip()
            rows = [r for r in rows if s in r["specialty"].lower() or any(a in s for a in SPECIALTY_ALIASES.get(r["specialty"], []))]
        if name:
            parts = [p for p in re.findall(r"[a-z]+", name.lower()) if p not in {"dr", "doctor", "ms", "mr", "mrs"}]
            rows = [r for r in rows if parts and all(p in r["name"].lower() for p in parts)]
        result = ok(doctors=rows, count=len(rows))
        self.read_cache.put(key, result)
        return result

    # ------------------------------------------------------------------ 2 get_slots
    def get_slots(self, doctor_id: str, date_from: str, date_to: str | None = None, limit: int = 12) -> dict:
        try:
            d_from = date.fromisoformat(date_from)
            d_to = date.fromisoformat(date_to) if date_to else d_from
        except (TypeError, ValueError):
            return fail("bad_date", "Dates must be YYYY-MM-DD.")
        now = self.clock()
        horizon = now.date() + timedelta(days=BOOKING_HORIZON_DAYS)
        d_from, d_to = max(d_from, now.date()), min(d_to, horizon)
        if d_from > d_to:
            return ok(doctor_id=doctor_id, slots=[], count=0, note=f"Bookings open up to {BOOKING_HORIZON_DAYS} days ahead.")
        key = ("get_slots", doctor_id, d_from, d_to, limit, iso(now))
        if (cached := self.read_cache.get(key)) is not None:
            return cached
        with closing(self._conn()) as conn:
            doc = conn.execute("SELECT id, name, specialty FROM doctors WHERE id=?", (doctor_id,)).fetchone()
            if not doc:
                return fail("unknown_doctor", f"No doctor with id {doctor_id}.")
            rows = conn.execute(
                "SELECT s.id AS slot_id, s.start_at, s.end_at FROM slots s "
                "WHERE s.doctor_id=? AND s.start_at >= ? AND substr(s.start_at,1,10) BETWEEN ? AND ? "
                "AND NOT EXISTS (SELECT 1 FROM appointments a WHERE a.slot_id=s.id AND a.status='booked') "
                "ORDER BY s.start_at LIMIT ?",
                (doctor_id, iso(now + timedelta(minutes=30)), d_from.isoformat(), d_to.isoformat(), limit),
            ).fetchall()
        result = ok(doctor_id=doctor_id, doctor=doc["name"], specialty=doc["specialty"], slots=[dict(r) for r in rows], count=len(rows))
        self.read_cache.put(key, result)
        return result

    # ------------------------------------------------------------ 3 book_appointment
    def book_appointment(self, session_id: str, slot_id: int, reason: str = "", idempotency_key: str | None = None) -> dict:
        key = idempotency_key or f"{session_id}:book:{slot_id}"
        with closing(self._conn()) as conn:
            patient_id = self.session_patient(session_id, conn)
            if not patient_id:
                self._audit(conn, "book_appointment.denied", session_id, slot_id=slot_id, reason="identity_required")
                return fail("identity_required", "Please verify your identity before booking.")
            existing = conn.execute("SELECT id FROM appointments WHERE idempotency_key=? AND patient_id=?", (key, patient_id)).fetchone()
            if existing:
                appt = self._appointment_view(conn, existing["id"])
                return ok(appointment=self._public(appt), idempotent_replay=True)
            slot = self._slot_view(conn, slot_id)
            now = self.clock()
            if not slot:
                return fail("unknown_slot", "That slot does not exist.")
            start = datetime.fromisoformat(slot["start_at"])
            if start < now + timedelta(minutes=30) or start.date() > now.date() + timedelta(days=BOOKING_HORIZON_DAYS):
                return fail("slot_unavailable", "That time can no longer be booked.")
            try:
                with transaction(conn):
                    if conn.execute("SELECT 1 FROM appointments WHERE slot_id=? AND status='booked'", (slot_id,)).fetchone():
                        raise _SlotTaken
                    upcoming = conn.execute(
                        "SELECT COUNT(*) FROM appointments a JOIN slots s ON s.id=a.slot_id WHERE a.patient_id=? AND a.status='booked' AND s.start_at>=?",
                        (patient_id, iso(now)),
                    ).fetchone()[0]
                    if upcoming >= MAX_UPCOMING:
                        raise _Limit
                    clash = conn.execute(
                        "SELECT 1 FROM appointments a JOIN slots s ON s.id=a.slot_id WHERE a.patient_id=? AND a.status='booked' AND s.start_at<? AND s.end_at>?",
                        (patient_id, slot["end_at"], slot["start_at"]),
                    ).fetchone()
                    if clash:
                        raise _Clash
                    ts = now_iso()
                    cur = conn.execute(
                        "INSERT INTO appointments (patient_id, doctor_id, slot_id, reason, status, idempotency_key, created_at, updated_at) VALUES (?,?,?,?, 'booked', ?,?,?)",
                        (patient_id, slot["doctor_id"], slot_id, reason[:200], key, ts, ts),
                    )
                    new_id = cur.lastrowid
            except (_SlotTaken, sqlite3.IntegrityError):
                self._audit(conn, "book_appointment.conflict", session_id, slot_id=slot_id)
                return fail("slot_taken", "Sorry, that slot was just taken. Please choose another time.")
            except _Limit:
                return fail("limit_reached", f"You already have {MAX_UPCOMING} upcoming appointments, the maximum allowed.")
            except _Clash:
                return fail("time_clash", "You already have an appointment at that time.")
            self.read_cache.clear()
            appt = self._appointment_view(conn, new_id)  # re-read: confirm only what is stored
            self._audit(conn, "book_appointment", session_id, appointment_id=new_id, slot_id=slot_id, doctor_id=slot["doctor_id"])
            return ok(appointment=self._public(appt))

    def _owned_booked(self, conn: sqlite3.Connection, session_id: str, appointment_id: int) -> tuple[str | None, dict | None, dict | None]:
        patient_id = self.session_patient(session_id, conn)
        if not patient_id:
            return None, None, fail("identity_required", "Please verify your identity first.")
        appt = self._appointment_view(conn, appointment_id)
        if not appt or appt["patient_id"] != patient_id:
            # Same message whether it doesn't exist or belongs to someone else: no enumeration.
            return patient_id, None, fail("not_found", "I couldn't find that appointment in your records.")
        return patient_id, appt, None

    # ------------------------------------------------------- 4 reschedule_appointment
    def reschedule_appointment(self, session_id: str, appointment_id: int, new_slot_id: int) -> dict:
        with closing(self._conn()) as conn:
            patient_id, appt, err = self._owned_booked(conn, session_id, appointment_id)
            if err:
                self._audit(conn, "reschedule_appointment.denied", session_id, appointment_id=appointment_id, error=err["error"])
                return err
            assert appt is not None
            if appt["status"] == "booked" and appt["slot_id"] == new_slot_id:
                return ok(appointment=self._public(appt), idempotent_replay=True)
            if appt["status"] != "booked":
                return fail("not_active", f"That appointment is {appt['status']}, so it can't be rescheduled.")
            now = self.clock()
            if datetime.fromisoformat(appt["start_at"]) - now < CHANGE_CUTOFF:
                return fail("too_late", "Appointments can only be changed up to 2 hours before. Please call the front desk.")
            slot = self._slot_view(conn, new_slot_id)
            if not slot:
                return fail("unknown_slot", "That slot does not exist.")
            start = datetime.fromisoformat(slot["start_at"])
            if start < now + timedelta(minutes=30) or start.date() > now.date() + timedelta(days=BOOKING_HORIZON_DAYS):
                return fail("slot_unavailable", "That time can no longer be booked.")
            try:
                with transaction(conn):
                    if conn.execute("SELECT 1 FROM appointments WHERE slot_id=? AND status='booked'", (new_slot_id,)).fetchone():
                        raise _SlotTaken
                    conn.execute(
                        "UPDATE appointments SET slot_id=?, doctor_id=?, idempotency_key=NULL, updated_at=? WHERE id=? AND status='booked'",
                        (new_slot_id, slot["doctor_id"], now_iso(), appointment_id),
                    )
            except (_SlotTaken, sqlite3.IntegrityError):
                return fail("slot_taken", "Sorry, that slot was just taken. Please choose another time.")
            self.read_cache.clear()
            stored = self._appointment_view(conn, appointment_id)
            self._audit(conn, "reschedule_appointment", session_id, appointment_id=appointment_id, from_slot=appt["slot_id"], to_slot=new_slot_id)
            return ok(appointment=self._public(stored), previous={"start_at": appt["start_at"], "doctor": appt["doctor"]})

    # ----------------------------------------------------------- 5 cancel_appointment
    def cancel_appointment(self, session_id: str, appointment_id: int) -> dict:
        with closing(self._conn()) as conn:
            _pid, appt, err = self._owned_booked(conn, session_id, appointment_id)
            if err:
                self._audit(conn, "cancel_appointment.denied", session_id, appointment_id=appointment_id, error=err["error"])
                return err
            assert appt is not None
            if appt["status"] == "cancelled":
                return ok(appointment=self._public(appt), idempotent_replay=True)
            if appt["status"] != "booked":
                return fail("not_active", f"That appointment is {appt['status']}, so it can't be cancelled.")
            if datetime.fromisoformat(appt["start_at"]) - self.clock() < CHANGE_CUTOFF:
                return fail("too_late", "Appointments can only be cancelled up to 2 hours before. Please call the front desk.")
            with transaction(conn):
                conn.execute("UPDATE appointments SET status='cancelled', idempotency_key=NULL, updated_at=? WHERE id=? AND status='booked'", (now_iso(), appointment_id))
            self.read_cache.clear()
            stored = self._appointment_view(conn, appointment_id)
            self._audit(conn, "cancel_appointment", session_id, appointment_id=appointment_id)
            return ok(appointment=self._public(stored))

    # -------------------------------------------------------- 6 list_my_appointments
    def list_my_appointments(self, session_id: str) -> dict:
        with closing(self._conn()) as conn:
            patient_id = self.session_patient(session_id, conn)
            if not patient_id:
                self._audit(conn, "list_my_appointments.denied", session_id)
                return fail("identity_required", "Please verify your identity first.")
            now = iso(self.clock())
            rows = conn.execute(
                "SELECT a.id AS appointment_id, a.status, a.reason, s.id AS slot_id, s.start_at, s.end_at, d.id AS doctor_id, d.name AS doctor, d.specialty, d.room "
                "FROM appointments a JOIN slots s ON s.id=a.slot_id JOIN doctors d ON d.id=a.doctor_id WHERE a.patient_id=? ORDER BY s.start_at",
                (patient_id,),
            ).fetchall()
            self._audit(conn, "list_my_appointments", session_id, count=len(rows))
        appts = [dict(r) for r in rows]
        return ok(upcoming=[a for a in appts if a["start_at"] >= now and a["status"] == "booked"], past=[a for a in appts if a["start_at"] < now or a["status"] != "booked"])

    # ----------------------------------------------------------- 7 get_prescriptions
    def get_prescriptions(self, session_id: str) -> dict:
        with closing(self._conn()) as conn:
            patient_id = self.session_patient(session_id, conn)
            if not patient_id:
                self._audit(conn, "get_prescriptions.denied", session_id)
                return fail("identity_required", "Please verify your identity first.")
            rows = conn.execute(
                "SELECT r.id AS prescription_id, r.drug, r.strength, r.form, r.dose, r.frequency, r.times, r.food, r.duration_days, r.start_date, "
                "r.visit_date, r.reason, r.notes, d.name AS doctor FROM prescriptions r JOIN doctors d ON d.id=r.doctor_id "
                "WHERE r.patient_id=? ORDER BY r.visit_date DESC, r.id",
                (patient_id,),
            ).fetchall()
            allergies = conn.execute("SELECT allergies FROM patients WHERE id=?", (patient_id,)).fetchone()["allergies"]
            self._audit(conn, "get_prescriptions", session_id, count=len(rows))
        today = self.clock().date()
        out = []
        for r in rows:
            item = dict(r)
            end = date.fromisoformat(item["start_date"]) + timedelta(days=item["duration_days"] - 1)
            item["end_date"] = end.isoformat()
            item["active"] = end >= today
            out.append(item)
        return ok(prescriptions=out, allergies=allergies)

    # -------------------------------------------------- 8/9 search_care_leaflets / search_clinic_faq
    def search_care_leaflets(self, query: str, k: int = 3) -> dict:
        hits = get_index().search(query, "care", k=k)
        return ok(results=[h.as_dict() for h in hits], citations=[h.citation() for h in hits])

    def search_clinic_faq(self, query: str, k: int = 3) -> dict:
        hits = get_index().search(query, "faq", k=k)
        return ok(results=[h.as_dict() for h in hits], citations=[h.citation() for h in hits])

    # -------------------------------------------------------------- 10 create_handoff
    def create_handoff(self, session_id: str, summary: str, priority: str = "normal", kind: str = "handoff") -> dict:
        priority = priority if priority in {"low", "normal", "high", "urgent"} else "normal"
        kind = kind if kind in {"handoff", "emergency", "pharmacist"} else "handoff"
        lines = [ln.strip() for ln in (summary or "").strip().splitlines() if ln.strip()][:3]
        text = "\n".join(lines)[:600] or "Patient asked to speak to a person."
        with closing(self._conn()) as conn:
            patient_id = self.session_patient(session_id, conn)
            open_dup = conn.execute("SELECT id FROM handoffs WHERE session_id=? AND kind=? AND status='open' AND summary=?", (session_id, kind, text)).fetchone()
            if open_dup:
                return ok(ticket_id=open_dup["id"], priority=priority, kind=kind, idempotent_replay=True)
            ts = now_iso()
            cur = conn.execute(
                "INSERT INTO handoffs (session_id, patient_id, kind, priority, summary, status, created_at, updated_at) VALUES (?,?,?,?,?, 'open', ?, ?)",
                (session_id, patient_id, kind, priority, text, ts, ts),
            )
            self._audit(conn, "create_handoff", session_id, ticket_id=cur.lastrowid, kind=kind, priority=priority)
            return ok(ticket_id=cur.lastrowid, priority=priority, kind=kind)

    # ------------------------------------------------------------- 11 verify_identity
    def verify_identity(self, session_id: str, phone: str, dob: str) -> dict:
        phone_n, dob_n = normalise_phone(phone), parse_dob(dob)
        now = self.clock()
        with closing(self._conn()) as conn:
            sess = conn.execute("SELECT * FROM identity_sessions WHERE session_id=?", (session_id,)).fetchone()
            if sess and sess["status"] == "verified" and (sess["verified_at"] or "") >= verified_cutoff():
                return ok(status="verified", message="You're already verified in this chat.")
            if sess and sess["status"] == "failed" and sess["attempts"] >= MAX_IDENTITY_ATTEMPTS:
                return fail("locked", "Too many attempts. Please call the front desk to verify your identity.")
            if len(phone_n) == 10 and self._phone_locked(conn, phone_n):
                self._audit(conn, "verify_identity.phone_locked", session_id)
                return fail("locked", "Too many attempts for this number. Please wait 15 minutes or call the front desk.")
            patient = None
            if len(phone_n) == 10 and dob_n:
                patient = conn.execute("SELECT id, phone FROM patients WHERE phone=? AND dob=?", (phone_n, dob_n)).fetchone()
            ts = now_iso()
            if not patient:
                attempts = (sess["attempts"] if sess else 0) + 1
                if len(phone_n) == 10:
                    self._phone_failed(conn, phone_n)
                conn.execute(
                    "INSERT INTO identity_sessions (session_id, status, attempts, updated_at) VALUES (?, 'failed', ?, ?) "
                    "ON CONFLICT(session_id) DO UPDATE SET status='failed', patient_id=NULL, otp_code=NULL, attempts=excluded.attempts, updated_at=excluded.updated_at",
                    (session_id, attempts, ts),
                )
                self._audit(conn, "verify_identity.failed", session_id, attempts=attempts)
                # Generic message: never reveal whether the phone or the DOB was wrong.
                return fail("not_matched", "I couldn't match those details. Please check your phone number and date of birth.", attempts_left=max(0, MAX_IDENTITY_ATTEMPTS - attempts))
            code = f"{secrets.randbelow(1_000_000):06d}"
            expires = iso(now + timedelta(minutes=OTP_TTL_MIN))
            conn.execute(
                "INSERT INTO identity_sessions (session_id, patient_id, status, otp_code, otp_expires_at, attempts, updated_at) VALUES (?,?, 'pending_otp', ?, ?, 0, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET patient_id=excluded.patient_id, status='pending_otp', otp_code=excluded.otp_code, "
                "otp_expires_at=excluded.otp_expires_at, attempts=0, updated_at=excluded.updated_at",
                (session_id, patient["id"], otp_digest(session_id, code), expires, ts),
            )
            self._audit(conn, "verify_identity.otp_sent", session_id)
        result = ok(status="otp_sent", sent_to=mask_phone(patient["phone"]), expires_in_minutes=OTP_TTL_MIN)
        if self.demo_mode:
            result["demo_otp"] = code  # demo only: shown in the UI instead of sending an SMS
        return result

    # ----------------------------------------------------------------- 12 confirm_otp
    def confirm_otp(self, session_id: str, code: str) -> dict:
        code = re.sub(r"\D", "", code or "")
        with closing(self._conn()) as conn:
            sess = conn.execute("SELECT * FROM identity_sessions WHERE session_id=?", (session_id,)).fetchone()
            if sess and sess["status"] == "verified" and (sess["verified_at"] or "") >= verified_cutoff():
                return ok(status="verified")
            if not sess or sess["status"] != "pending_otp":
                return fail("no_pending_otp", "Please share your phone number and date of birth first.")
            if sess["otp_expires_at"] < iso(self.clock()):
                return fail("otp_expired", "That code has expired. I can send a new one.")
            if sess["attempts"] >= MAX_OTP_ATTEMPTS:
                return fail("otp_locked", "Too many wrong codes. Please request a new code.")
            if not secrets.compare_digest(otp_digest(session_id, code), sess["otp_code"] or ""):
                conn.execute("UPDATE identity_sessions SET attempts=attempts+1, updated_at=? WHERE session_id=?", (now_iso(), session_id))
                self._audit(conn, "confirm_otp.failed", session_id)
                return fail("otp_wrong", "That code doesn't match.", attempts_left=max(0, MAX_OTP_ATTEMPTS - sess["attempts"] - 1))
            ts = now_iso()
            conn.execute("UPDATE identity_sessions SET status='verified', otp_code=NULL, verified_at=?, updated_at=? WHERE session_id=?", (ts, ts, session_id))
            first = conn.execute("SELECT name FROM patients WHERE id=?", (sess["patient_id"],)).fetchone()["name"].split()[0]
            self._audit(conn, "confirm_otp.verified", session_id)
        return ok(status="verified", first_name=first, memory_key=memory_key(sess["patient_id"]))


class _SlotTaken(Exception):
    pass


class _Limit(Exception):
    pass


class _Clash(Exception):
    pass


_default: ClinicTools | None = None


def get_tools() -> ClinicTools:
    global _default
    if _default is None:
        _default = ClinicTools()
    return _default
