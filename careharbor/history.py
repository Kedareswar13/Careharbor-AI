"""Persistent chat history (server side).

Why: conversations must survive a page refresh, a server restart, and "logging in again" (re-verifying),
even on another device. The browser only keeps a list of signed session ids; the transcript lives here.

Privacy rules:
- User text is PII-masked before it is stored; OTP codes are never stored.
- Possession of a signed, server-issued session id is required to read a conversation.
- After a patient signs out, their conversations are hidden until they verify again ("re-open").
- A patient's conversations are linked by the opaque HMAC memory key, never by the patient id.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from careharbor.db import VERIFIED_SQL, audit, connect, now_iso, transaction, verified_cutoff
from careharbor.logging_setup import redact

PERSIST_KEYS = ("turn_id", "kind", "intent", "route_source", "citations", "evidence", "stages", "guardrails", "llm_usage", "tool_calls",
                "awaiting_confirmation", "model", "total_ms")
_OTP_ONLY = re.compile(r"^\D{0,40}\d{3}[\s-]?\d{3}\D{0,40}$")
_OTP_IN_REPLY = re.compile(r"(?i)(code is )\d{6}")


def _mask_user(text: str) -> str:
    return "[one-time code]" if _OTP_ONLY.match(text.strip()) else redact(text)


def _title(text: str) -> str:
    t = re.sub(r"\s+", " ", _mask_user(text)).strip()
    return (t[:57] + "...") if len(t) > 60 else (t or "Conversation")


def save_turn(db_path: Path | str | None, session_id: str, user_text: str, result: dict, memory_key: str | None) -> None:
    card = dict(result.get("card") or {})
    card.pop("demo_otp", None)
    payload = {k: result.get(k) for k in PERSIST_KEYS}
    payload["card"] = card or None
    reply = _OTP_IN_REPLY.sub(r"\1shown on screen", result.get("reply") or "")
    ts = now_iso()
    with closing(connect(db_path)) as conn:
        with transaction(conn):
            row = conn.execute("SELECT title FROM conversations WHERE session_id=?", (session_id,)).fetchone()
            meaningful = user_text and not _OTP_ONLY.match(user_text.strip()) and not user_text.startswith("[")
            if row is None:
                conn.execute("INSERT INTO conversations (session_id, title, patient_key, created_at, updated_at) VALUES (?,?,?,?,?)",
                             (session_id, _title(user_text) if meaningful else "New conversation", memory_key, ts, ts))
            elif row["title"] == "New conversation" and meaningful:
                conn.execute("UPDATE conversations SET title=? WHERE session_id=?", (_title(user_text), session_id))
            conn.execute("INSERT INTO chat_messages (session_id, role, text, payload, ts) VALUES (?,?,?,?,?)", (session_id, "user", _mask_user(user_text), "{}", ts))
            conn.execute("INSERT INTO chat_messages (session_id, role, text, payload, ts) VALUES (?,?,?,?,?)",
                         (session_id, "assistant", reply, json.dumps(payload, default=str, ensure_ascii=False), ts))
            conn.execute("UPDATE conversations SET updated_at=?, patient_key=COALESCE(?, patient_key) WHERE session_id=?", (ts, memory_key or None, session_id))


def _verified_patient(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT i.patient_id, i.verified_at, p.name FROM identity_sessions i JOIN patients p ON p.id=i.patient_id WHERE i.session_id=? AND i.{VERIFIED_SQL}",
        (session_id, verified_cutoff())).fetchone()


def verified_first_name(db_path: Path | str | None, session_id: str) -> str | None:
    with closing(connect(db_path)) as conn:
        who = _verified_patient(conn, session_id)
    return who["name"].split()[0] if who else None


def is_ended(db_path: Path | str | None, session_id: str) -> bool:
    with closing(connect(db_path)) as conn:
        row = conn.execute("SELECT ended FROM conversations WHERE session_id=?", (session_id,)).fetchone()
        return bool(row and row["ended"])


def load(db_path: Path | str | None, session_id: str) -> dict[str, Any]:
    with closing(connect(db_path)) as conn:
        conv = conn.execute("SELECT * FROM conversations WHERE session_id=?", (session_id,)).fetchone()
        if conv is None:
            return {"session_id": session_id, "found": False, "messages": []}
        if conv["ended"]:
            return {"session_id": session_id, "found": True, "ended": True, "messages": []}
        who = _verified_patient(conn, session_id)
        if conv["patient_key"] and not who:
            # Contains a patient's records but the verification expired: hide it until they verify again (then "re-open").
            return {"session_id": session_id, "found": True, "ended": False, "locked": True, "title": conv["title"], "messages": []}
        rows = conn.execute("SELECT id, role, text, payload, ts FROM chat_messages WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
    return {
        "session_id": session_id, "found": True, "ended": False, "locked": False, "title": conv["title"], "updated_at": conv["updated_at"],
        "verified_name": who["name"].split()[0] if who else None,
        "messages": [{"id": r["id"], "role": r["role"], "text": r["text"], "ts": r["ts"], "result": json.loads(r["payload"] or "{}")} for r in rows],
    }


def summaries(db_path: Path | str | None, session_ids: list[str]) -> list[dict]:
    if not session_ids:
        return []
    marks = ",".join("?" * len(session_ids))
    with closing(connect(db_path)) as conn:
        rows = conn.execute(
            f"SELECT c.session_id, c.title, c.updated_at, c.ended, c.patient_key, (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id=c.session_id AND m.role='user') AS n "
            f"FROM conversations c WHERE c.session_id IN ({marks}) ORDER BY c.updated_at DESC", session_ids).fetchall()
        # Record-bearing conversations whose verification expired are hidden too (shared-device protection).
        visible = [r for r in rows if not r["ended"] and (not r["patient_key"] or _verified_patient(conn, r["session_id"]))]
    return [{k: r[k] for k in ("session_id", "title", "updated_at", "n")} for r in visible]


def linked(db_path: Path | str | None, session_id: str) -> list[dict]:
    """Other conversations of the patient verified in this session (any device), newest first."""
    with closing(connect(db_path)) as conn:
        conv = conn.execute("SELECT patient_key FROM conversations WHERE session_id=?", (session_id,)).fetchone()
        if not conv or not conv["patient_key"] or not _verified_patient(conn, session_id):
            return []
        rows = conn.execute(
            "SELECT c.session_id, c.title, c.updated_at, (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id=c.session_id AND m.role='user') AS n "
            "FROM conversations c WHERE c.patient_key=? AND c.session_id != ? ORDER BY c.updated_at DESC LIMIT 20", (conv["patient_key"], session_id)).fetchall()
    return [dict(r) for r in rows]


def reopen(db_path: Path | str | None, session_id: str, via: str) -> bool:
    """Re-open an older conversation after verifying in `via`: same patient key required. Carries the verification over."""
    with closing(connect(db_path)) as conn:
        a = conn.execute("SELECT patient_key FROM conversations WHERE session_id=?", (session_id,)).fetchone()
        b = conn.execute("SELECT patient_key FROM conversations WHERE session_id=?", (via,)).fetchone()
        who = _verified_patient(conn, via)
        if not (a and b and who and a["patient_key"] and a["patient_key"] == b["patient_key"]):
            return False
        ts = now_iso()
        with transaction(conn):
            conn.execute(
                "INSERT INTO identity_sessions (session_id, patient_id, status, attempts, verified_at, updated_at) VALUES (?,?, 'verified', 0, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET patient_id=excluded.patient_id, status='verified', otp_code=NULL, verified_at=excluded.verified_at, updated_at=excluded.updated_at",
                (session_id, who["patient_id"], who["verified_at"], ts))  # inherits the OTP time: re-opening never extends the TTL
            conn.execute("UPDATE conversations SET ended=0 WHERE session_id=?", (session_id,))
            audit(conn, "patient", "conversation.reopened", via, reopened=session_id[:10])
    return True


def sign_out(db_path: Path | str | None, session_ids: list[str]) -> int:
    """End verification for these sessions and hide their transcripts until the patient verifies again."""
    if not session_ids:
        return 0
    marks = ",".join("?" * len(session_ids))
    with closing(connect(db_path)) as conn:
        with transaction(conn):
            conn.execute(f"DELETE FROM identity_sessions WHERE session_id IN ({marks})", session_ids)
            n = conn.execute(f"UPDATE conversations SET ended=1 WHERE session_id IN ({marks})", session_ids).rowcount
            audit(conn, "patient", "signed_out", session_ids[0], conversations=n)
    return n


def delete(db_path: Path | str | None, session_id: str) -> None:
    with closing(connect(db_path)) as conn:
        with transaction(conn):
            conn.execute("DELETE FROM chat_messages WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM conversations WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM identity_sessions WHERE session_id=?", (session_id,))
            audit(conn, "patient", "conversation.deleted", session_id)


def purge_older_than(db_path: Path | str | None, days: int, now: datetime | None = None) -> list[str]:
    """Retention policy: delete conversations not updated for `days` days. Returns their ids (to drop agent checkpoints too)."""
    if days <= 0:
        return []
    cutoff = ((now or datetime.fromisoformat(now_iso())) - timedelta(days=days)).isoformat(timespec="seconds")
    with closing(connect(db_path)) as conn:
        ids = [r[0] for r in conn.execute("SELECT session_id FROM conversations WHERE updated_at < ?", (cutoff,))]
        if ids:
            marks = ",".join("?" * len(ids))
            with transaction(conn):
                conn.execute(f"DELETE FROM chat_messages WHERE session_id IN ({marks})", ids)
                conn.execute(f"DELETE FROM conversations WHERE session_id IN ({marks})", ids)
                conn.execute(f"DELETE FROM identity_sessions WHERE session_id IN ({marks})", ids)
                audit(conn, "system", "retention.purged", None, conversations=len(ids), days=days)
    return ids
