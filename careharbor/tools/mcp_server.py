"""FastMCP server exposing the 12 CareHarbor AI tools.

Transports:
- stdio  : the agent host spawns `python -m careharbor.tools.mcp_server` and keeps one session open.
- http   : the same server is mounted on the FastAPI app at /mcp for debugging (port 8010).
- memory : tests and the in-process mode connect to the FastMCP object directly.

Patient identity never appears in a tool's input schema. The host passes the
chat session id out-of-band in the MCP request `_meta` (key SESSION_META_KEY),
or the `X-CareHarbor-Session` header over HTTP. The tool layer resolves the
VERIFIED patient for that session from the database.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Any

if __package__ in (None, ""):  # launched as a script by the stdio transport
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastmcp import Context, FastMCP  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402
from pydantic import Field  # noqa: E402

from careharbor.tools.service import ClinicTools, get_tools  # noqa: E402

SESSION_META_KEY = "careharbor/session_id"
SESSION_HEADER = "x-careharbor-session"

READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


def _session_id(ctx: Context) -> str:
    rc = ctx.request_context
    meta = getattr(rc, "meta", None) or {}
    sid = meta.get(SESSION_META_KEY) if isinstance(meta, dict) else getattr(meta, SESSION_META_KEY, None)
    if not sid:
        try:
            from fastmcp.server.dependencies import get_http_headers

            sid = get_http_headers().get(SESSION_HEADER)
        except Exception:  # noqa: BLE001 - not an HTTP request
            sid = None
    if not sid or not isinstance(sid, str) or len(sid) < 8:
        raise ToolError("No chat session bound to this call.")
    return sid


def build_server(tools: ClinicTools | None = None) -> FastMCP:
    svc = tools or get_tools()
    mcp = FastMCP(
        "careharbor-clinic",
        instructions="Clinic front-desk tools. Patient identity comes from the verified chat session, never from arguments.",
    )

    @mcp.tool(annotations=READ_ONLY)
    def find_doctors(
        specialty: Annotated[str | None, Field(description="Department or plain-language need, e.g. 'skin', 'ENT', 'child'")] = None,
        name: Annotated[str | None, Field(description="Doctor name or surname, e.g. 'Rao'")] = None,
    ) -> dict[str, Any]:
        """Find clinic doctors by specialty and/or name."""
        return svc.find_doctors(specialty, name)

    @mcp.tool(annotations=READ_ONLY)
    def get_slots(
        doctor_id: Annotated[str, Field(description="Doctor id, e.g. 'D01'")],
        date_from: Annotated[str, Field(description="First date YYYY-MM-DD")],
        date_to: Annotated[str | None, Field(description="Last date YYYY-MM-DD (default: same day)")] = None,
    ) -> dict[str, Any]:
        """List free appointment slots for a doctor (up to 14 days ahead)."""
        return svc.get_slots(doctor_id, date_from, date_to)

    @mcp.tool(annotations=WRITE)
    def book_appointment(
        slot_id: Annotated[int, Field(description="Slot id from get_slots")],
        reason: Annotated[str, Field(description="Short reason for the visit")] = "",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Book a slot for the verified patient of this chat. Call only after the patient explicitly confirmed."""
        return svc.book_appointment(_session_id(ctx), slot_id, reason)

    @mcp.tool(annotations=WRITE)
    def reschedule_appointment(
        appointment_id: Annotated[int, Field(description="Existing appointment id")],
        new_slot_id: Annotated[int, Field(description="New slot id from get_slots")],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Move one of the verified patient's appointments to a new slot. Call only after explicit confirmation."""
        return svc.reschedule_appointment(_session_id(ctx), appointment_id, new_slot_id)

    @mcp.tool(annotations={**WRITE, "destructiveHint": True})
    def cancel_appointment(
        appointment_id: Annotated[int, Field(description="Existing appointment id")],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Cancel one of the verified patient's appointments. Call only after explicit confirmation."""
        return svc.cancel_appointment(_session_id(ctx), appointment_id)

    @mcp.tool(annotations=READ_ONLY)
    def list_my_appointments(ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """List the verified patient's upcoming and past appointments."""
        return svc.list_my_appointments(_session_id(ctx))

    @mcp.tool(annotations=READ_ONLY)
    def get_prescriptions(ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Read the verified patient's own prescription records, exactly as stored."""
        return svc.get_prescriptions(_session_id(ctx))

    @mcp.tool(annotations=READ_ONLY)
    def search_care_leaflets(query: Annotated[str, Field(description="Symptom or self-care question")]) -> dict[str, Any]:
        """Search the clinic's care leaflets and MedlinePlus reference topics (vectorless BM25 + tree)."""
        return svc.search_care_leaflets(query)

    @mcp.tool(annotations=READ_ONLY)
    def search_clinic_faq(query: Annotated[str, Field(description="Question about the clinic")]) -> dict[str, Any]:
        """Search clinic policies: timings, departments, insurance, fees, visiting, cancellation, lab, privacy."""
        return svc.search_clinic_faq(query)

    @mcp.tool(annotations=WRITE)
    def create_handoff(
        summary: Annotated[str, Field(description="Up to 3 short lines for the staff member")],
        priority: Annotated[str, Field(description="low | normal | high | urgent")] = "normal",
        kind: Annotated[str, Field(description="handoff | emergency | pharmacist")] = "handoff",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Create a ticket in the staff queue so a person follows up."""
        return svc.create_handoff(_session_id(ctx), summary, priority, kind)

    @mcp.tool(annotations=WRITE)
    def verify_identity(
        phone: Annotated[str, Field(description="Registered mobile number")],
        dob: Annotated[str, Field(description="Date of birth, e.g. 1990-05-14 or 14/05/1990")],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Start identity verification; sends a one-time code (mocked in demo mode)."""
        return svc.verify_identity(_session_id(ctx), phone, dob)

    @mcp.tool(annotations=WRITE)
    def confirm_otp(code: Annotated[str, Field(description="6-digit one-time code")], ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Complete identity verification with the one-time code."""
        return svc.confirm_otp(_session_id(ctx), code)

    return mcp


TOOL_NAMES = [
    "find_doctors", "get_slots", "book_appointment", "reschedule_appointment", "cancel_appointment", "list_my_appointments",
    "get_prescriptions", "search_care_leaflets", "search_clinic_faq", "create_handoff", "verify_identity", "confirm_otp",
]

mcp = build_server()


def main() -> None:
    from careharbor.logging_setup import force_utf8_stdio

    force_utf8_stdio()
    # stdio: stdout is the protocol channel, so never print anything else to it.
    mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    sys.exit(main())
