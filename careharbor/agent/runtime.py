"""Conversation runtime: owns the compiled graph, its SQLite checkpointer/store and the tool client.

One call to ChatService.turn() handles one patient message:
- If the thread is paused on a confirmation interrupt, typed replies ("yes", "no", "another time")
  are mapped to the card's buttons and the graph is resumed with Command(resume=...).
- Otherwise the message runs through the graph from the safety gate.
Every turn writes one JSON line to data/traces/<date>.jsonl (stages, latency, tokens, tool calls) and is saved to the
chat history tables (careharbor/history.py) so the conversation survives refreshes, restarts and re-verification.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.sqlite.aio import AsyncSqliteStore
from langgraph.types import Command

from careharbor import history
from careharbor.agent import parsing as P
from careharbor.agent.graph import CareHarborGraph
from careharbor.config import get_settings
from careharbor.db import connect, init_db
from careharbor.llm import BaseLLM, get_llm
from careharbor.logging_setup import redact
from careharbor.tools.client import ToolClient

log = logging.getLogger("careharbor.runtime")

PER_TURN_RESET = {
    "reply": "", "reply_kind": "", "card": None, "citations": [], "intent": "", "route_source": "", "grounding": "",
    "prefix": "", "resume": None, "confirm": None, "decision": "",
    "evidence": [],
    "stages": ["__reset__"], "guardrails": ["__reset__"], "llm_usage": ["__reset__"],
}


def confirm_prompt(card: dict) -> str:
    if card.get("action") == "cancel":
        return f"Please confirm: cancel your appointment with {card['doctor']} on {card['when']}?"
    if card.get("action") == "reschedule":
        return f"Please confirm: move your appointment from {card.get('previous')} to {card['when']} with {card['doctor']}?"
    return f"Please confirm: book {card['when']} with {card['doctor']} ({card.get('specialty')}) for \"{card.get('reason', 'Consultation')}\"?"


def decision_from_text(text: str) -> str | None:
    if P.CHANGE.search(text):
        return "change"
    yn = P.yes_no(text)
    if yn == "yes":
        return "confirm"
    if yn == "no":
        return "cancel"
    return None


class ChatService:
    def __init__(self, tools: ToolClient | None = None, *, llm_factory: Callable[[str | None], BaseLLM] = get_llm, clock: Callable[[], datetime] | None = None,
                 db_path: Path | None = None, checkpoint_path: Path | None = None, trace_dir: Path | None = None):
        s = get_settings()
        self.tools = tools or ToolClient(transport="memory")
        self.llm_factory = llm_factory
        self.clock = clock
        self.db_path = db_path or s.db_file
        self.checkpoint_path = checkpoint_path or s.checkpoint_file
        self.trace_dir = trace_dir or s.trace_path
        self.graph: Any = None
        self._conns: list[aiosqlite.Connection] = []

    async def start(self) -> "ChatService":
        if self.graph is not None:
            return self
        conn = connect(self.db_path)  # create chat-history tables on databases seeded by older versions
        try:
            init_db(conn)
        finally:
            conn.close()
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        cp_conn = await aiosqlite.connect(str(self.checkpoint_path))
        # Separate file for the long-term store: two writers on one SQLite file lock each other.
        st_conn = await aiosqlite.connect(str(self.checkpoint_path.with_name(self.checkpoint_path.stem + "-memory.db")), isolation_level=None)  # store requires autocommit
        for c in (cp_conn, st_conn):
            await c.execute("PRAGMA journal_mode=WAL")
            await c.execute("PRAGMA busy_timeout=5000")
        self._conns = [cp_conn, st_conn]
        saver = AsyncSqliteSaver(cp_conn)
        await saver.setup()
        store = AsyncSqliteStore(st_conn)
        await store.setup()
        await self.tools.start()
        self.builder = CareHarborGraph(self.tools, llm_factory=self.llm_factory, clock=self.clock, store=store, db_path=self.db_path)
        self.graph = self.builder.build(checkpointer=saver)
        return self

    async def stop(self) -> None:
        await self.tools.stop()
        for c in self._conns:
            await c.close()
        self._conns = []
        self.graph = None

    async def delete_thread(self, session_id: str) -> None:
        """Remove the LangGraph checkpoint for a conversation (used when a patient deletes it)."""
        saver = getattr(self.graph, "checkpointer", None)
        if saver is not None and hasattr(saver, "adelete_thread"):
            await saver.adelete_thread(session_id)

    @staticmethod
    def _config(session_id: str) -> dict:
        return {"configurable": {"thread_id": session_id}}

    async def pending_confirmation(self, session_id: str) -> dict | None:
        snap = await self.graph.aget_state(self._config(session_id))
        for intr in getattr(snap, "interrupts", ()) or ():
            if isinstance(intr.value, dict) and intr.value.get("type") == "confirm":
                return intr.value
        return None

    async def turn(self, session_id: str, text: str, model: str | None = None) -> dict:
        result: dict = {}
        async for event in self.stream_turn(session_id, text, model):
            if event["type"] == "result":
                result = event["data"]
        return result

    async def stream_turn(self, session_id: str, text: str, model: str | None = None):
        """Async generator: yields {"type": "stage", ...} as graph nodes finish, then {"type": "result", "data": ...}."""
        await self.start()
        text = (text or "").strip()[:2000]
        if await self.pending_confirmation(session_id):
            decision = decision_from_text(text)
            if decision:
                async for ev in self._run(session_id, Command(resume={"decision": decision}), model, text, resumed=True):
                    yield ev
                return
            await self.resume(session_id, "cancel", model, typed=text, silent=True)  # unrelated message: drop the pending card
        inp = {**PER_TURN_RESET, "session_id": session_id, "text": text, "model": model or "", "messages": [{"role": "user", "content": redact(text)}]}
        async for ev in self._run(session_id, inp, model, text):
            yield ev

    async def _run(self, session_id: str, payload: Any, model: str | None, text: str, resumed: bool = False):
        t0 = time.perf_counter()
        n_calls = self.tools.mark()
        base = None
        if resumed:
            snap = await self.graph.aget_state(self._config(session_id))
            base = {k: len(snap.values.get(k) or []) for k in ("stages", "guardrails", "llm_usage")}
        async for chunk in self.graph.astream(payload, self._config(session_id), stream_mode="updates", durability="exit"):
            for node, update in chunk.items():
                if node.startswith("__") or not isinstance(update, dict):
                    continue
                for st in update.get("stages") or []:
                    if isinstance(st, dict):
                        yield {"type": "stage", "node": node, **st}
        yield {"type": "result", "data": await self._package(session_id, t0, n_calls, model, text, base)}

    async def resume(self, session_id: str, decision: str, model: str | None = None, *, typed: str = "", silent: bool = False) -> dict:
        await self.start()
        t0 = time.perf_counter()
        n_calls = self.tools.mark()
        snap = await self.graph.aget_state(self._config(session_id))
        base = {k: len(snap.values.get(k) or []) for k in ("stages", "guardrails", "llm_usage")}
        await self.graph.ainvoke(Command(resume={"decision": decision}), self._config(session_id), durability="exit")
        if silent:
            return {}
        label = {"confirm": "Confirm", "change": "Show other times", "cancel": "Cancel"}.get(decision, decision)
        return await self._package(session_id, t0, n_calls, model, typed or label, base)

    async def _package(self, session_id: str, t0: float, n_calls: int, model: str | None, text: str, base: dict | None = None) -> dict:
        snap = await self.graph.aget_state(self._config(session_id))
        v = snap.values
        base = base or {}
        stages = (v.get("stages") or [])[base.get("stages", 0):]
        card = v.get("card")
        reply = v.get("reply") or ""
        awaiting = None
        for intr in getattr(snap, "interrupts", ()) or ():
            if isinstance(intr.value, dict) and intr.value.get("type") == "confirm":
                awaiting = intr.value
        if awaiting:
            card, reply = awaiting, confirm_prompt(awaiting)
        total_ms = round((time.perf_counter() - t0) * 1000, 1)
        result = {
            "turn_id": uuid.uuid4().hex[:12],
            "session_id": session_id,
            "reply": reply,
            "kind": "emergency" if (card or {}).get("type") == "emergency" else "confirm" if awaiting else v.get("reply_kind") or "template",
            "card": card,
            "citations": v.get("citations") or [],
            "evidence": v.get("evidence") or [],
            "intent": v.get("intent"),
            "route_source": v.get("route_source"),
            "stages": stages,
            "guardrails": (v.get("guardrails") or [])[base.get("guardrails", 0):],
            "llm_usage": (v.get("llm_usage") or [])[base.get("llm_usage", 0):],
            "tool_calls": self.tools.calls_since(n_calls, session_id),
            "awaiting_confirmation": bool(awaiting),
            "verified_name": await asyncio.to_thread(history.verified_first_name, self.db_path, session_id),
            "model": model or get_settings().llm_model,
            "total_ms": total_ms,
        }
        self._trace(result, text)
        try:
            await asyncio.to_thread(history.save_turn, self.db_path, session_id, text, result, v.get("memory_key"))
        except sqlite3.Error:
            log.exception("could not save chat history")
        return result

    def _trace(self, result: dict, text: str) -> None:
        try:
            self.trace_dir.mkdir(parents=True, exist_ok=True)
            row = {"ts": datetime.now().isoformat(timespec="seconds"), "text": redact(text[:120]), **{k: result[k] for k in (
                "turn_id", "session_id", "intent", "route_source", "kind", "model", "total_ms", "stages", "llm_usage", "tool_calls", "guardrails", "awaiting_confirmation")}}
            row["card_type"] = (result.get("card") or {}).get("type")
            with (self.trace_dir / f"{datetime.now():%Y-%m-%d}.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass
