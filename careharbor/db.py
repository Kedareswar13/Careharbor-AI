"""SQLite storage: schema, connection helper and clinic-time utilities."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from careharbor.config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    phone         TEXT NOT NULL UNIQUE,
    dob           TEXT NOT NULL,
    gender        TEXT NOT NULL,
    language      TEXT NOT NULL DEFAULT 'en',
    allergies     TEXT NOT NULL DEFAULT 'None known',
    preferred_doctor_id TEXT REFERENCES doctors(id),
    conditions    TEXT NOT NULL DEFAULT '',   -- active chronic conditions (Synthea-derived)
    source_ref    TEXT NOT NULL DEFAULT ''    -- provenance, e.g. "synthea:166840bf" or "demo-persona"
);

CREATE TABLE IF NOT EXISTS doctors (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    specialty     TEXT NOT NULL,
    working_days  TEXT NOT NULL,          -- e.g. "Mon,Wed,Fri"
    start_time    TEXT NOT NULL,          -- "09:00"
    end_time      TEXT NOT NULL,          -- "13:00"
    slot_minutes  INTEGER NOT NULL,
    room          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS slots (
    id            INTEGER PRIMARY KEY,
    doctor_id     TEXT NOT NULL REFERENCES doctors(id),
    start_at      TEXT NOT NULL,          -- clinic local time "YYYY-MM-DDTHH:MM"
    end_at        TEXT NOT NULL,
    UNIQUE (doctor_id, start_at)
);

CREATE TABLE IF NOT EXISTS appointments (
    id            INTEGER PRIMARY KEY,
    patient_id    TEXT NOT NULL REFERENCES patients(id),
    doctor_id     TEXT NOT NULL REFERENCES doctors(id),
    slot_id       INTEGER NOT NULL REFERENCES slots(id),
    reason        TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL CHECK (status IN ('booked','cancelled','completed','rescheduled')),
    idempotency_key TEXT UNIQUE,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
-- A slot can hold at most one live booking: the database itself prevents double-booking.
CREATE UNIQUE INDEX IF NOT EXISTS ux_appointments_live_slot ON appointments(slot_id) WHERE status = 'booked';
CREATE INDEX IF NOT EXISTS ix_appointments_patient ON appointments(patient_id);

CREATE TABLE IF NOT EXISTS prescriptions (
    id            INTEGER PRIMARY KEY,
    patient_id    TEXT NOT NULL REFERENCES patients(id),
    doctor_id     TEXT NOT NULL REFERENCES doctors(id),
    visit_date    TEXT NOT NULL,
    drug          TEXT NOT NULL,
    strength      TEXT NOT NULL,
    form          TEXT NOT NULL,
    dose          TEXT NOT NULL,          -- e.g. "1 tablet"
    frequency     TEXT NOT NULL,          -- e.g. "twice a day"
    times         TEXT NOT NULL,          -- e.g. "8 AM and 8 PM"
    food          TEXT NOT NULL,          -- e.g. "after food"
    duration_days INTEGER NOT NULL,
    start_date    TEXT NOT NULL,
    reason        TEXT NOT NULL DEFAULT '',
    notes         TEXT NOT NULL DEFAULT '',
    source_ref    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_prescriptions_patient ON prescriptions(patient_id);

-- Identity: a chat session becomes "verified" only after phone + DOB + OTP.
CREATE TABLE IF NOT EXISTS identity_sessions (
    session_id    TEXT PRIMARY KEY,
    patient_id    TEXT REFERENCES patients(id),
    status        TEXT NOT NULL CHECK (status IN ('pending_otp','verified','failed')),
    otp_code      TEXT,
    otp_expires_at TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    verified_at   TEXT,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS handoffs (
    id            INTEGER PRIMARY KEY,
    session_id    TEXT NOT NULL,
    patient_id    TEXT,
    kind          TEXT NOT NULL CHECK (kind IN ('handoff','emergency','pharmacist')),
    priority      TEXT NOT NULL CHECK (priority IN ('low','normal','high','urgent')),
    summary       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','claimed','closed')),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL,
    session_id    TEXT,
    actor         TEXT NOT NULL,          -- tool | guardrail | agent | staff | system
    event         TEXT NOT NULL,
    detail        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_log(ts);

CREATE TABLE IF NOT EXISTS feedback (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    message_id    TEXT NOT NULL,
    rating        TEXT NOT NULL CHECK (rating IN ('up','down','report')),
    comment       TEXT NOT NULL DEFAULT ''
);

-- Chat history: every conversation survives refreshes, restarts and re-verification ("logging in again").
-- User text is PII-masked and OTP codes are never stored. patient_key is the opaque HMAC memory key, never a patient id.
CREATE TABLE IF NOT EXISTS conversations (
    session_id    TEXT PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT 'New conversation',
    patient_key   TEXT,
    ended         INTEGER NOT NULL DEFAULT 0,   -- 1 after the patient signs out on this device
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_conversations_patient ON conversations(patient_key, updated_at);

CREATE TABLE IF NOT EXISTS chat_messages (
    id            INTEGER PRIMARY KEY,
    session_id    TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('user','assistant')),
    text          TEXT NOT NULL,
    payload       TEXT NOT NULL DEFAULT '{}',   -- cards, citations, evidence, stages (JSON) for the assistant turn
    ts            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chat_messages_session ON chat_messages(session_id, id);

-- Identity brute-force protection across sessions: failed phone+DOB matches per phone number (keyed HMAC, never the number).
CREATE TABLE IF NOT EXISTS identity_throttle (
    key           TEXT PRIMARY KEY,
    failures      INTEGER NOT NULL DEFAULT 0,
    window_start  TEXT NOT NULL
);
"""

TABLES = ["patients", "doctors", "slots", "appointments", "prescriptions", "identity_sessions", "handoffs", "audit_log", "feedback", "conversations", "chat_messages",
          "identity_throttle"]


def clinic_now() -> datetime:
    """Current clinic-local time as a naive datetime (minutes precision)."""
    tz = ZoneInfo(get_settings().clinic_tz)
    return datetime.now(tz).replace(tzinfo=None, second=0, microsecond=0)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def now_iso() -> str:
    return datetime.now(ZoneInfo(get_settings().clinic_tz)).replace(tzinfo=None).isoformat(timespec="seconds")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    db_file = Path(path) if path else get_settings().db_file
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_file), timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit BEGIN IMMEDIATE ... COMMIT so check-then-write is atomic."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def verified_cutoff() -> str:
    """Oldest `verified_at` that still counts as verified (VERIFIED_TTL_MINUTES; 0 = never expires).
    After it, records need phone + DOB + OTP again, e.g. on a shared clinic kiosk the next person can't see them."""
    ttl = get_settings().verified_ttl_minutes
    if ttl <= 0:
        return ""
    return (datetime.fromisoformat(now_iso()) - timedelta(minutes=ttl)).isoformat(timespec="seconds")


VERIFIED_SQL = "status='verified' AND COALESCE(verified_at, '') >= ?"


def ensure_future_slots(conn: sqlite3.Connection, days: int = 14, today: date | None = None) -> int:
    """Top up bookable slots for the next `days` days from each doctor's schedule, without touching any other data.
    Lets a long-running deployment keep its bookings and chats while the calendar stays current."""
    today = today or clinic_now().date()
    docs = conn.execute("SELECT id, working_days, start_time, end_time, slot_minutes FROM doctors").fetchall()
    rows = []
    for offset in range(days):
        day = today + timedelta(days=offset)
        for d in docs:
            if day.strftime("%a") not in d["working_days"].split(","):
                continue
            t = datetime.combine(day, datetime.strptime(d["start_time"], "%H:%M").time())
            stop = datetime.combine(day, datetime.strptime(d["end_time"], "%H:%M").time())
            step = timedelta(minutes=d["slot_minutes"])
            while t + step <= stop:
                rows.append((d["id"], iso(t), iso(t + step)))
                t += step
    before = conn.total_changes
    with transaction(conn):
        conn.executemany("INSERT OR IGNORE INTO slots (doctor_id, start_at, end_at) VALUES (?,?,?)", rows)
    return conn.total_changes - before


def reset_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = OFF")
    for table in reversed(TABLES):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}


def audit(conn: sqlite3.Connection, actor: str, event: str, session_id: str | None = None, **detail: Any) -> None:
    conn.execute(
        "INSERT INTO audit_log (ts, session_id, actor, event, detail) VALUES (?,?,?,?,?)",
        (now_iso(), session_id, actor, event, json.dumps(detail, default=str, ensure_ascii=False)),
    )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
