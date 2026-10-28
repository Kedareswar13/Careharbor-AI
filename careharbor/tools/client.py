"""Host-side MCP client with per-agent tool allow-lists.

Agents never talk to the MCP server directly. They go through ToolClient,
which (1) refuses any tool outside the calling agent's allow-list, (2) binds
the chat session id out-of-band in `_meta`, and (3) keeps one MCP session open
for the life of the app (spawning a process per call would be slow on Windows).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

from fastmcp import Client, FastMCP
from fastmcp.client.transports import PythonStdioTransport

from careharbor.config import ROOT_DIR
from careharbor.tools.mcp_server import SESSION_META_KEY

log = logging.getLogger("careharbor.tools")

ALLOWLIST: dict[str, set[str]] = {
    "triage": {"search_care_leaflets", "create_handoff"},
    "booking": {"find_doctors", "get_slots", "book_appointment", "reschedule_appointment", "cancel_appointment", "list_my_appointments", "verify_identity", "confirm_otp"},
    "prescription": {"verify_identity", "confirm_otp", "get_prescriptions", "create_handoff"},
    "faq": {"search_clinic_faq", "find_doctors"},
    "handoff": {"create_handoff"},
    "safety": {"create_handoff"},
    "identity": {"verify_identity", "confirm_otp"},
}


class ToolNotAllowed(PermissionError):
    pass


class ToolClient:
    def __init__(self, server: FastMCP | None = None, transport: str = "memory"):
        """transport: 'memory' (in-process FastMCP object) or 'stdio' (subprocess)."""
        self._server = server
        self.transport = transport
        self._client: Client | None = None
        self._lock = asyncio.Lock()
        self.calls: list[dict[str, Any]] = []  # recent tool calls (seq, session, agent, tool, ms, ok); see calls_since()
        self._seq = 0

    async def start(self) -> "ToolClient":
        if self._client is not None:
            return self
        if self.transport == "stdio":
            env = {**os.environ, "PYTHONUTF8": "1"}
            target: Any = PythonStdioTransport(script_path=str(ROOT_DIR / "careharbor" / "tools" / "mcp_server.py"), python_cmd=sys.executable, env=env, cwd=str(ROOT_DIR))
        else:
            if self._server is None:
                from careharbor.tools.mcp_server import mcp

                self._server = mcp
            target = self._server
        self._client = Client(target, timeout=60)
        await self._client.__aenter__()
        return self

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None

    async def list_tools(self) -> list[str]:
        await self.start()
        assert self._client is not None
        return sorted(t.name for t in await self._client.list_tools())

    async def call(self, agent: str, tool: str, args: dict[str, Any] | None = None, session_id: str | None = None) -> dict[str, Any]:
        if tool not in ALLOWLIST.get(agent, set()):
            log.warning("blocked tool %s for agent %s", tool, agent)
            raise ToolNotAllowed(f"Agent '{agent}' may not call '{tool}'.")
        await self.start()
        assert self._client is not None
        t0 = time.perf_counter()
        meta = {SESSION_META_KEY: session_id} if session_id else None
        async with self._lock:  # one in-flight request per stdio pipe
            result = await self._client.call_tool(tool, args or {}, meta=meta, raise_on_error=False)
        ms = round((time.perf_counter() - t0) * 1000, 1)
        if result.is_error:
            text = " ".join(getattr(c, "text", "") for c in result.content)
            data: dict[str, Any] = {"ok": False, "error": "tool_error", "message": text}
        else:
            data = result.structured_content or {}
            if "result" in data and len(data) == 1 and isinstance(data["result"], dict):
                data = data["result"]
            if not data and result.content:
                data = json.loads(getattr(result.content[0], "text", "{}") or "{}")
        self._seq += 1
        self.calls.append({"seq": self._seq, "sid": session_id, "agent": agent, "tool": tool, "ms": ms, "ok": bool(data.get("ok"))})
        if len(self.calls) > 4000:  # bounded memory for a long-running server
            del self.calls[:2000]
        return data

    def mark(self) -> int:
        """Position marker taken at the start of a turn."""
        return self._seq

    def calls_since(self, mark: int, session_id: str) -> list[dict[str, Any]]:
        """Tool calls made for THIS conversation since `mark`. Concurrent turns of other patients are excluded."""
        return [{k: c[k] for k in ("agent", "tool", "ms", "ok")} for c in self.calls if c["seq"] > mark and c["sid"] == session_id]
