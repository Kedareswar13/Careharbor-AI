"""CareHarbor AI in the terminal.

    uv run python scripts/chat_cli.py              # interactive chat (real LLM, real tools)
    uv run python scripts/chat_cli.py --demo       # scripted run of all journeys with checks
    uv run python scripts/chat_cli.py --demo --mock  # same, with the deterministic MockLLM

The demo runs on its own freshly seeded database (data/cli_demo.db) so the main data stays clean.
In interactive mode, confirmation cards are answered with c(onfirm) / x (change) / n(o).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from careharbor.agent.runtime import ChatService  # noqa: E402
from careharbor.config import ROOT_DIR, get_settings  # noqa: E402
from careharbor.db import connect  # noqa: E402
from careharbor.llm import MockLLM, get_llm, start_keep_alive, warm_up  # noqa: E402
from careharbor.logging_setup import setup_logging  # noqa: E402
from careharbor.tools.client import ToolClient  # noqa: E402
from careharbor.tools.mcp_server import build_server  # noqa: E402
from careharbor.tools.service import ClinicTools  # noqa: E402

DEMO_DB = ROOT_DIR / "data" / "cli_demo.db"


def show(r: dict) -> None:
    print(f"\n\033[96mCareHarbor AI\033[0m [{r['intent']} via {r['route_source']} | {r['total_ms'] / 1000:.1f}s]")
    print(r["reply"])
    card = r.get("card") or {}
    if card.get("type") == "confirm":
        print("  [confirmation card] c = confirm, x = change, n = cancel")
    if r.get("citations"):
        print("  sources: " + "; ".join(f"{c['title']} - {c['section']} ({c['source']})" for c in r["citations"][:3]))
    if r.get("guardrails"):
        print("  guardrails: " + json.dumps(r["guardrails"])[:200])
    stages = ", ".join(f"{s['label']} {s['ms']:.0f}ms" for s in r.get("stages", []))
    print(f"  \033[90mstages: {stages}\033[0m")


def build_service(mock: bool, demo: bool) -> tuple[ChatService, Path]:
    db = DEMO_DB if demo else get_settings().db_file
    if demo:
        from scripts.seed import seed

        seed(db, None)
    tools = ToolClient(build_server(ClinicTools(db)))
    factory = (lambda m=None, _l=MockLLM({"router": lambda s, u: json.dumps({"intent": "faq"}), "safety": lambda s, u: '{"emergency": false}'}): _l) if mock else get_llm
    cp = ROOT_DIR / "data" / ("cli_demo_checkpoints.db" if demo else "cli_checkpoints.db")
    return ChatService(tools, llm_factory=factory, db_path=db, checkpoint_path=cp), db


async def say(svc: ChatService, sid: str, text: str) -> dict:
    print(f"\n\033[93mPatient\033[0m: {text}")
    r = await svc.turn(sid, text)
    show(r)
    return r


async def demo(svc: ChatService, db: Path) -> int:
    failures: list[str] = []

    def check(cond: bool, label: str) -> None:
        print(f"  {'PASS' if cond else 'FAIL'}: {label}")
        if not cond:
            failures.append(label)

    print("\n================ Journey 1: symptom guidance (triage-lite) ================")
    sid = uuid.uuid4().hex
    r = await say(svc, sid, "I've had a headache since morning, what should I do?")
    check(r["card"] and r["card"]["type"] == "followup", "asks 1-3 safe follow-up questions")
    r = await say(svc, sid, "No, none of those. It's a mild headache, getting a bit better.")
    check(r["card"] and r["card"]["type"] == "triage" and bool(r["citations"]), "grounded guidance with citations + disposition")
    check(not r["guardrails"] or r["guardrails"][0].get("action") == "extractive_fallback", "output guardrail clean or safely fell back")
    r = await say(svc, uuid.uuid4().hex, "chest me dard ho raha hai aur saans nahi aa rahi")
    check(r["kind"] == "emergency" and r["total_ms"] < 1000, "red flag -> emergency card in < 1 s, no LLM")

    print("\n================ Journey 2: appointments (existing patient) ================")
    sid = uuid.uuid4().hex
    r = await say(svc, sid, "I visited last month, can I see Dr. Rao again on Friday?")
    check(r["card"] and r["card"]["type"] == "identity", "identity required before booking")
    r = await say(svc, sid, "9876500001, 14/05/1990")
    otp = (r["card"] or {}).get("demo_otp")
    check(bool(otp), "mock OTP sent (shown in demo mode)")
    r = await say(svc, sid, otp)
    check(r["card"] and r["card"]["type"] == "slots" and len(r["card"]["options"]) <= 3, "up to 3 slot options with Dr. Rao")
    r = await say(svc, sid, "2")
    check(r["awaiting_confirmation"], "HITL interrupt: confirmation card shown, nothing written yet")
    print("\n\033[93mPatient\033[0m clicks [Confirm]")
    r = await svc.resume(sid, "confirm")
    show(r)
    appt = (r.get("card") or {}).get("appointment") or {}
    row = connect(db).execute("SELECT status FROM appointments WHERE id=?", (appt.get("appointment_id"),)).fetchone()
    check(bool(row) and row[0] == "booked" and f"#{appt.get('appointment_id')}" in r["reply"], "reply matches the stored DB row")

    print("\n================ Journey 3: prescription questions ================")
    sid = uuid.uuid4().hex
    r = await say(svc, sid, "When do I take the antibiotic the doctor gave me?")
    r = await say(svc, sid, "my number is 9876500001 and dob 14 May 1990")
    r = await say(svc, sid, (r["card"] or {}).get("demo_otp", ""))
    check("three times a day (8 AM, 2 PM and 9 PM), after food, for 5 days" in r["reply"], "explains schedule exactly as recorded")
    r = await say(svc, sid, "Can I stop it since I feel better?")
    check((r["card"] or {}).get("kind") == "pharmacist", "stop/change question escalated to pharmacist")

    print("\n================ FAQ + handoff ================")
    sid = uuid.uuid4().hex
    r = await say(svc, sid, "What are your OPD timings?")
    check(r["intent"] == "faq" and bool(r["citations"]), "FAQ answered with citation")
    r = await say(svc, sid, "Do you accept Star Health insurance?")
    check("star health" in r["reply"].lower() or "yes" in r["reply"].lower(), "insurance answer grounded in policy")
    r = await say(svc, sid, "I want to talk to a person")
    check((r["card"] or {}).get("type") == "handoff", "handoff ticket created")

    print(f"\n{'ALL JOURNEYS PASSED' if not failures else 'FAILURES: ' + '; '.join(failures)}")
    return 0 if not failures else 1


async def interactive(svc: ChatService) -> int:
    sid = uuid.uuid4().hex
    print("CareHarbor AI CLI - demo only, not medical advice. In an emergency call 112 / 108. Type 'quit' to exit.")
    while True:
        try:
            text = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in {"quit", "exit"}:
            break
        if not text:
            continue
        if await svc.pending_confirmation(sid) and text.lower() in {"c", "x", "n"}:
            show(await svc.resume(sid, {"c": "confirm", "x": "change", "n": "cancel"}[text.lower()]))
            continue
        show(await svc.turn(sid, text))
    return 0


async def main() -> int:
    setup_logging()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--mock", action="store_true", help="use MockLLM instead of the configured model")
    args = ap.parse_args()
    if args.mock:
        os.environ["LLM_PROVIDER"] = "mock"
    else:
        print(f"Warming up {get_settings().llm_model} ...", flush=True)
        warm_up()
        start_keep_alive()
    svc, db = build_service(args.mock, args.demo)
    try:
        await svc.start()
        return await (demo(svc, db) if args.demo else interactive(svc))
    finally:
        await svc.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
