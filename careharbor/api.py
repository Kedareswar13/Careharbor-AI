"""FastAPI gateway for CareHarbor AI (port 8010).

Patient API : /api/chat, /api/chat/confirm, /api/feedback, /api/models, /api/config, /api/health
Staff API   : /api/staff/* (signed bearer token from /api/staff/login with STAFF_USERNAME / STAFF_PASSWORD)
Debug       : /mcp (the same FastMCP tool server over streamable HTTP; needs an X-CareHarbor-Session header)
UI          : in production the built React app (frontend/dist) is served from "/".
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from careharbor import history
from careharbor.agent.runtime import ChatService
from careharbor.config import ROOT_DIR, get_settings
from careharbor.db import audit, clinic_now, connect, ensure_future_slots, now_iso, table_counts
from careharbor.llm import list_models, start_keep_alive, warm_up
from careharbor.logging_setup import redact, setup_logging
from careharbor.rag.index import get_index
from careharbor.security import issue_session_id, principal_for_staff, valid_session_id
from careharbor.tools.client import ToolClient

log = logging.getLogger("careharbor.api")
CLINIC_NAME = "Sunrise Family Clinic"
DIST = ROOT_DIR / "frontend" / "dist"
EVAL_REPORT_DIR = ROOT_DIR / "evals" / "reports"


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{8,80}$")
    model: str | None = Field(default=None, max_length=60)


class ConfirmIn(BaseModel):
    session_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{8,80}$")
    decision: Literal["confirm", "change", "cancel"]
    model: str | None = Field(default=None, max_length=60)


class FeedbackIn(BaseModel):
    session_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{8,80}$")
    turn_id: str = Field(max_length=40)
    rating: Literal["up", "down", "report"]
    comment: str = Field(default="", max_length=500)


class LoginIn(BaseModel):
    username: str = Field(max_length=60)
    password: str = Field(max_length=120)


class HandoffUpdate(BaseModel):
    status: Literal["open", "claimed", "closed"]


SID_PATTERN = r"^[A-Za-z0-9_.-]{8,80}$"


class SessionIn(BaseModel):
    session_id: str = Field(pattern=SID_PATTERN)


class SessionsIn(BaseModel):
    session_ids: list[str] = Field(default_factory=list, max_length=50)


class ReopenIn(BaseModel):
    session_id: str = Field(pattern=SID_PATTERN)
    via: str = Field(pattern=SID_PATTERN)


# ------------------------------------------------------------------ staff sessions (HMAC-signed, HttpOnly cookie, 8 h)
STAFF_COOKIE = "careharbor_staff"
CSRF_HEADER = "x-careharbor-csrf"


def _sign(payload: str) -> str:
    return base64.urlsafe_b64encode(hmac.new(get_settings().secret, payload.encode(), hashlib.sha256).digest()).decode().rstrip("=")


def make_token(username: str, ttl_s: int = 8 * 3600) -> str:
    payload = f"v2|{username}|{int(time.time()) + ttl_s}"
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=") + "." + _sign(payload)


def verify_token(token: str) -> str | None:
    try:
        body, sig = token.split(".", 1)
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
        if not hmac.compare_digest(sig, _sign(payload)):
            return None
        version, user, exp = payload.split("|")
        return user if version == "v2" and int(exp) > time.time() else None
    except (ValueError, UnicodeDecodeError):
        return None


def staff_user(request: Request) -> str:
    """Accepts the HttpOnly cookie (browser) or a bearer token (scripts/MCP clients).
    Cookie-authenticated state changes must carry the CSRF header (a cross-site form can't set it)."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        user = verify_token(auth[7:])
    else:
        user = verify_token(request.cookies.get(STAFF_COOKIE, ""))
        if user and request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get(CSRF_HEADER) != "1":
            raise HTTPException(403, "Missing CSRF header")
    if not user:
        raise HTTPException(401, "Staff login required")
    return user


# ------------------------------------------------------------------ simple per-client rate limit
class RateLimiter:
    def __init__(self, limit: int, window_s: float):
        self.limit, self.window = limit, window_s
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        if len(self.hits) > 5000:  # bounded memory: forget clients idle for a full window
            for k in [k for k, q in self.hits.items() if not q or now - q[-1] > self.window]:
                del self.hits[k]
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


chat_limiter = RateLimiter(30, 60)
login_limiter = RateLimiter(5, 60)


def create_app(service: ChatService | None = None, *, warm: bool = True) -> FastAPI:
    setup_logging()
    s = get_settings()
    from careharbor.tools.mcp_server import mcp as mcp_server

    mcp_http = mcp_server.http_app(path="/")
    state: dict[str, Any] = {"svc": service, "eval_proc": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if state["svc"] is None:
            transport = "stdio" if s.app_env != "test" else "memory"
            state["svc"] = ChatService(ToolClient(transport=transport))
        get_index()
        await state["svc"].start()
        conn = connect(state["svc"].db_path)
        try:
            added = ensure_future_slots(conn) if s.app_env != "test" else 0  # tests use a fixed clock and seed
            if added:
                log.info("Added %s future appointment slots", added)
        finally:
            conn.close()
        purged = await asyncio.to_thread(history.purge_older_than, state["svc"].db_path, s.chat_retention_days)
        for sid in purged:
            await state["svc"].delete_thread(sid)
        if purged:
            log.info("Retention: deleted %s conversations older than %s days", len(purged), s.chat_retention_days)
        if warm and s.llm_provider != "mock":
            threading.Thread(target=warm_up, name="warm-up", daemon=True).start()
            start_keep_alive()
        tg_task = None
        if s.telegram_bot_token:
            from careharbor.channels.telegram import run_telegram

            tg_task = asyncio.create_task(run_telegram(state["svc"], s.telegram_bot_token))
        async with mcp_http.lifespan(app):
            if not s.session_secret or s.session_secret == "change-me":
                log.warning("SESSION_SECRET is not set: a temporary secret is used, so saved chats become unreachable after a restart. Set SESSION_SECRET in .env.")
            log.info("CareHarbor AI API ready on port %s", s.api_port)
            yield
        if tg_task:
            tg_task.cancel()
        await state["svc"].stop()

    app = FastAPI(title="CareHarbor AI API", version="0.2.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=[f"http://localhost:{s.ui_port}", f"http://127.0.0.1:{s.ui_port}"], allow_methods=["*"], allow_headers=["*"], allow_credentials=True)

    @app.middleware("http")
    async def protect_mcp(request: Request, call_next):
        """The raw MCP tool server bypasses the chat guardrails, so only signed-in staff may use it."""
        if request.url.path.startswith("/mcp"):
            try:
                staff_user(request)
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return await call_next(request)

    app.mount("/mcp", mcp_http)

    def svc() -> ChatService:
        return state["svc"]

    # ------------------------------------------------------------ patient endpoints
    @app.get("/api/health")
    async def health() -> dict:
        conn = connect()
        try:
            counts = table_counts(conn)
        finally:
            conn.close()
        idx = get_index()
        folders: dict[str, int] = defaultdict(int)
        for d in idx.docs.values():
            folders[d["folder"].split("/")[0]] += 1
        return {"status": "ok", "model": s.llm_model, "provider": s.llm_provider, "rag_mode": s.rag_mode, "rag_pipeline": s.rag_pipeline, "tenant": s.tenant_id,
                "documents": len(idx.docs), "sections": len(idx.nodes), "collections": dict(folders), "patients": counts["patients"], "time": now_iso()}

    @app.get("/api/config")
    async def config() -> dict:
        return {"clinic": CLINIC_NAME, "demo_mode": s.demo_mode, "default_model": s.llm_model, "emergency_numbers": ["112", "108"],
                "disclaimer": "Demo, not medical advice. In an emergency call 112 / 108."}

    @app.get("/api/models")
    async def models() -> dict:
        return {"models": await asyncio.to_thread(list_models)}

    def chat_session(sid: str | None) -> str:
        """Only server-issued, signed session ids are accepted; anything else starts a fresh session (anti-spoofing).
        A conversation the patient signed out of is closed: new messages start a new conversation."""
        if valid_session_id(sid) and not history.is_ended(svc().db_path, sid):
            return sid
        return issue_session_id()

    @app.post("/api/chat")
    async def chat(body: ChatIn, request: Request) -> dict:
        sid = chat_session(body.session_id)
        if not chat_limiter.allow(sid) or not chat_limiter.allow(request.client.host if request.client else "local"):
            raise HTTPException(429, "Too many messages - please wait a moment.")
        return await svc().turn(sid, body.message, body.model)

    @app.post("/api/chat/stream")
    async def chat_stream(body: ChatIn, request: Request) -> StreamingResponse:
        """Server-Sent Events: real pipeline stages as they finish (thinking panel), then the final result."""
        sid = chat_session(body.session_id)
        if not chat_limiter.allow(sid) or not chat_limiter.allow(request.client.host if request.client else "local"):
            raise HTTPException(429, "Too many messages - please wait a moment.")

        async def events():
            yield f"data: {json.dumps({'type': 'start', 'session_id': sid})}\n\n"
            try:
                async for ev in svc().stream_turn(sid, body.message, body.model):
                    yield f"data: {json.dumps(ev, default=str)}\n\n"
            except Exception:  # noqa: BLE001
                log.exception("stream failed")
                yield f"data: {json.dumps({'type': 'error', 'message': 'Something went wrong. Please try again.'})}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/chat/confirm")
    async def confirm(body: ConfirmIn, request: Request) -> dict:
        if not chat_limiter.allow(body.session_id) or not chat_limiter.allow(request.client.host if request.client else "local"):
            raise HTTPException(429, "Too many messages - please wait a moment.")
        if (not valid_session_id(body.session_id) or await asyncio.to_thread(history.is_ended, svc().db_path, body.session_id)
                or not await svc().pending_confirmation(body.session_id)):
            raise HTTPException(409, "Nothing is waiting for confirmation in this chat.")
        return await svc().resume(body.session_id, body.decision, body.model)

    # ------------------------------------------------------------ chat history (survives refresh, restart and re-login)
    def _sid(session_id: str) -> str:
        if not valid_session_id(session_id):
            raise HTTPException(404, "Unknown conversation")
        return session_id

    @app.get("/api/chat/history")
    async def chat_history(session_id: str) -> dict:
        sid = _sid(session_id)
        data = await asyncio.to_thread(history.load, svc().db_path, sid)
        pending = await svc().pending_confirmation(sid) if data.get("messages") else None
        data["pending"] = pending
        return data

    @app.post("/api/chat/conversations")
    async def chat_conversations(body: SessionsIn) -> dict:
        ids = [x for x in body.session_ids if valid_session_id(x)]
        return {"conversations": await asyncio.to_thread(history.summaries, svc().db_path, ids)}

    @app.get("/api/chat/linked")
    async def chat_linked(session_id: str) -> dict:
        """After the patient verifies (OTP) in this conversation, list their earlier conversations, from any device."""
        return {"conversations": await asyncio.to_thread(history.linked, svc().db_path, _sid(session_id))}

    @app.post("/api/chat/reopen")
    async def chat_reopen(body: ReopenIn) -> dict:
        if not (valid_session_id(body.session_id) and valid_session_id(body.via)):
            raise HTTPException(404, "Unknown conversation")
        if not await asyncio.to_thread(history.reopen, svc().db_path, body.session_id, body.via):
            raise HTTPException(403, "Verify your identity in this conversation first.")
        return await chat_history(body.session_id)

    @app.post("/api/chat/signout")
    async def chat_signout(body: SessionsIn) -> dict:
        ids = [x for x in body.session_ids if valid_session_id(x)]
        return {"ended": await asyncio.to_thread(history.sign_out, svc().db_path, ids)}

    @app.post("/api/chat/delete")
    async def chat_delete(body: SessionIn) -> dict:
        sid = _sid(body.session_id)
        await asyncio.to_thread(history.delete, svc().db_path, sid)
        await svc().delete_thread(sid)
        return {"ok": True}

    @app.post("/api/feedback")
    async def feedback(body: FeedbackIn) -> dict:
        conn = connect()
        try:
            conn.execute("INSERT INTO feedback (ts, session_id, message_id, rating, comment) VALUES (?,?,?,?,?)", (now_iso(), body.session_id, body.turn_id, body.rating, redact(body.comment)))
            audit(conn, "patient", f"feedback.{body.rating}", body.session_id, turn_id=body.turn_id)
        finally:
            conn.close()
        return {"ok": True}

    # ------------------------------------------------------------ staff endpoints
    @app.post("/api/staff/login")
    async def login(body: LoginIn, request: Request) -> dict:
        if not login_limiter.allow(request.client.host if request.client else "local"):
            raise HTTPException(429, "Too many attempts - wait a minute.")
        if s.app_env == "prod" and s.staff_password in {"", "change-me"}:
            raise HTTPException(503, "Staff login is disabled until STAFF_PASSWORD is changed from the default.")
        ok = secrets.compare_digest(body.username, s.staff_username) and secrets.compare_digest(body.password, s.staff_password)
        conn = connect()
        try:
            audit(conn, "staff", "login.ok" if ok else "login.failed", None, username=body.username[:30])
        finally:
            conn.close()
        if not ok:
            raise HTTPException(401, "Wrong username or password")
        token = make_token(body.username)
        resp = JSONResponse({"username": body.username, "token": token})
        resp.set_cookie(STAFF_COOKIE, token, max_age=8 * 3600, httponly=True, samesite="strict", secure=request.url.scheme == "https", path="/")
        return resp

    @app.post("/api/staff/logout")
    async def logout() -> JSONResponse:
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(STAFF_COOKIE, path="/")
        return resp

    @app.get("/api/staff/me")
    async def me(user: str = Depends(staff_user)) -> dict:
        return {"username": user}

    def rows(sql: str, args: tuple = ()) -> list[dict]:
        conn = connect()
        try:
            return [dict(r) for r in conn.execute(sql, args)]
        finally:
            conn.close()

    @app.get("/api/staff/overview")
    async def overview(user: str = Depends(staff_user)) -> dict:
        today = clinic_now().date().isoformat()
        q = lambda sql, a=(): rows(sql, a)[0]["n"]  # noqa: E731
        return {
            "open_handoffs": q("SELECT COUNT(*) n FROM handoffs WHERE status!='closed'"),
            "urgent_open": q("SELECT COUNT(*) n FROM handoffs WHERE status!='closed' AND priority='urgent'"),
            "appointments_today": q("SELECT COUNT(*) n FROM appointments a JOIN slots s ON s.id=a.slot_id WHERE a.status='booked' AND substr(s.start_at,1,10)=?", (today,)),
            "guardrail_events_24h": q("SELECT COUNT(*) n FROM audit_log WHERE actor='guardrail' AND ts >= datetime('now','-1 day')"),
            "feedback_down": q("SELECT COUNT(*) n FROM feedback WHERE rating!='up'"),
        }

    @app.get("/api/staff/handoffs")
    async def handoffs(user: str = Depends(staff_user)) -> dict:
        items = rows("SELECT h.id, h.kind, h.priority, h.summary, h.status, h.created_at, h.patient_id, p.name AS patient_name FROM handoffs h LEFT JOIN patients p ON p.id=h.patient_id "
                     "ORDER BY CASE h.status WHEN 'open' THEN 0 WHEN 'claimed' THEN 1 ELSE 2 END, CASE h.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, h.id DESC LIMIT 200")
        return {"items": items}

    @app.post("/api/staff/handoffs/{ticket_id}")
    async def update_handoff(ticket_id: int, body: HandoffUpdate, user: str = Depends(staff_user)) -> dict:
        conn = connect()
        try:
            cur = conn.execute("UPDATE handoffs SET status=?, updated_at=? WHERE id=?", (body.status, now_iso(), ticket_id))
            if not cur.rowcount:
                raise HTTPException(404, "No such ticket")
            audit(conn, "staff", f"handoff.{body.status}", None, ticket_id=ticket_id, username=user)
        finally:
            conn.close()
        return {"ok": True}

    @app.get("/api/staff/appointments")
    async def appointments(day: str | None = None, user: str = Depends(staff_user)) -> dict:
        d = day if day and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) else clinic_now().date().isoformat()
        items = rows("SELECT a.id, s.start_at, a.status, a.reason, d.name AS doctor, d.specialty, d.room, p.name AS patient_name FROM appointments a "
                     "JOIN slots s ON s.id=a.slot_id JOIN doctors d ON d.id=a.doctor_id JOIN patients p ON p.id=a.patient_id WHERE substr(s.start_at,1,10)=? ORDER BY s.start_at", (d,))
        upcoming = rows("SELECT a.id, s.start_at, a.status, d.name AS doctor, p.name AS patient_name FROM appointments a JOIN slots s ON s.id=a.slot_id "
                        "JOIN doctors d ON d.id=a.doctor_id JOIN patients p ON p.id=a.patient_id WHERE a.status='booked' AND s.start_at >= ? ORDER BY s.start_at LIMIT 30", (d,))
        return {"day": d, "items": items, "upcoming": upcoming}

    @app.get("/api/staff/audit")
    async def audit_log(limit: int = 200, actor: str | None = None, user: str = Depends(staff_user)) -> dict:
        limit = max(1, min(limit, 1000))
        if actor:
            items = rows("SELECT id, ts, session_id, actor, event, detail FROM audit_log WHERE actor=? ORDER BY id DESC LIMIT ?", (actor, limit))
        else:
            items = rows("SELECT id, ts, session_id, actor, event, detail FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
        for it in items:
            it["detail"] = json.loads(it["detail"] or "{}")
            it["session_id"] = (it["session_id"] or "")[:10]
        return {"items": items}

    @app.get("/api/staff/traces")
    async def traces(limit: int = 100, user: str = Depends(staff_user)) -> dict:
        files = sorted(s.trace_path.glob("*.jsonl"))[-3:]
        out: list[dict] = []
        for f in reversed(files):
            for line in reversed(f.read_text(encoding="utf-8").splitlines()):
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
                if len(out) >= limit:
                    break
            if len(out) >= limit:
                break
        return {"items": out}

    @app.get("/api/staff/sops")
    async def sops(q: str, user: str = Depends(staff_user)) -> dict:
        who = principal_for_staff(user)
        hits = get_index().search(q[:200], "staff", who=who)
        from careharbor.security import log_retrieval

        log_retrieval(None, who, q, hits, "staff_search")
        return {"results": [h.as_dict() for h in hits]}

    @app.get("/api/staff/security")
    async def security_status(user: str = Depends(staff_user)) -> dict:
        """The four RAG security layers with live evidence from the audit log, plus session/identity controls."""
        from careharbor.logging_setup import redact as _redact
        from careharbor.security import TEMPLATES

        def count(sql: str, args: tuple = ()) -> int:
            return rows(sql, args)[0]["n"]

        idx = get_index()
        blocked = rows("SELECT json_extract(detail,'$.reason') AS reason, COUNT(*) AS n FROM audit_log WHERE event='query_blocked' GROUP BY reason ORDER BY n DESC")
        by_role = rows("SELECT json_extract(detail,'$.role') AS role, COUNT(*) AS n FROM audit_log WHERE actor='retriever' GROUP BY role ORDER BY n DESC")
        recent = rows("SELECT detail FROM audit_log ORDER BY id DESC LIMIT 500")
        raw_pii = sum(1 for r in recent if _redact(r["detail"] or "") != (r["detail"] or ""))
        latest = sorted(EVAL_REPORT_DIR.glob("eval_summary_*.json"))
        sec_eval = None
        if latest:
            sec_eval = next((x for x in json.loads(latest[-1].read_text(encoding="utf-8")).get("suites", []) if x["name"].startswith("Security")), None)
        return {
            "layers": [
                {"id": 1, "title": "Query validation before the pipeline", "controls": ["Prompt-injection, smuggling and exfiltration patterns", "Out-of-scope refusal", "Length limit"],
                 "evidence": {"blocked_total": sum(b["n"] for b in blocked), "by_reason": blocked}},
                {"id": 2, "title": "Authorisation before retrieval", "controls": [f"Tenant isolation ({s.tenant_id})", "Role filter: anonymous / patient / staff", "Records only after phone + DOB + OTP"],
                 "evidence": {"retrievals_by_role": by_role, "staff_only_docs": sum(1 for d in idx.docs.values() if d["audience"] == "staff"),
                              "other_tenant_docs": sum(1 for d in idx.docs.values() if d["tenant"] not in {s.tenant_id, "*"})}},
                {"id": 3, "title": "Fixed prompt templates", "controls": ["User and document text only fill neutralised slots", "Injection lines stripped from retrieved text"],
                 "evidence": {"templates": [{"name": t.name, "slots": dict(t.limits)} for t in TEMPLATES.values()]}},
                {"id": 4, "title": "Observability without a new leak", "controls": ["Document ids only in retrieval logs", "PII masked in logs, audit, checkpoints and saved chats"],
                 "evidence": {"audit_rows": count("SELECT COUNT(*) AS n FROM audit_log"), "recent_rows_scanned": len(recent), "raw_pii_found": raw_pii}},
            ],
            "guardrails": {"red_flags": count("SELECT COUNT(*) AS n FROM audit_log WHERE event='red_flag'"),
                           "output_blocked": count("SELECT COUNT(*) AS n FROM audit_log WHERE event IN ('output_blocked','final_output_blocked')"),
                           "identity_lockouts": count("SELECT COUNT(*) AS n FROM identity_throttle WHERE failures >= 5")},
            "settings": {"session_secret_set": bool(s.session_secret and s.session_secret != "change-me"), "staff_password_changed": s.staff_password not in {"", "change-me"},
                         "verified_ttl_minutes": s.verified_ttl_minutes, "chat_retention_days": s.chat_retention_days, "demo_mode": s.demo_mode, "app_env": s.app_env},
            "latest_security_eval": sec_eval,
        }

    @app.get("/api/staff/evals")
    async def evals(user: str = Depends(staff_user)) -> dict:
        latest = sorted(EVAL_REPORT_DIR.glob("eval_report_*.md"))
        summary = sorted(EVAL_REPORT_DIR.glob("eval_summary_*.json"))
        proc = state["eval_proc"]
        running = proc is not None and proc.poll() is None
        return {"running": running, "report": latest[-1].read_text(encoding="utf-8") if latest else None, "report_name": latest[-1].name if latest else None,
                "summary": json.loads(summary[-1].read_text(encoding="utf-8")) if summary else None}

    @app.post("/api/staff/evals/run")
    async def run_evals(user: str = Depends(staff_user)) -> dict:
        proc = state["eval_proc"]
        if proc is not None and proc.poll() is None:
            return {"started": False, "message": "An eval run is already in progress."}
        script = ROOT_DIR / "scripts" / "run_evals.py"
        if not script.exists():
            raise HTTPException(404, "Eval runner not installed yet")
        state["eval_proc"] = subprocess.Popen([sys.executable, str(script), "--quiet"], cwd=str(ROOT_DIR), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        conn = connect()
        try:
            audit(conn, "staff", "evals.run", None, username=user)
        finally:
            conn.close()
        return {"started": True}

    # ------------------------------------------------------------ built UI (production / HF Spaces)
    if DIST.exists():
        app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            target = (DIST / path).resolve()
            if path and target.is_file() and DIST.resolve() in target.parents:
                return FileResponse(target)
            return FileResponse(DIST / "index.html")

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):  # never leak stack traces to clients
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse({"detail": "Something went wrong. Please try again."}, status_code=500)

    return app


app = create_app()
