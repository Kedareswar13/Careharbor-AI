"""Golden regression dataset runner.

Each case is a scripted multi-turn conversation (evals/golden/*.jsonl) using the QA test account
(P004, 9000000001 / 01-01-2000) and fixed demo data. Every turn has expectations on intent, card,
must/must-not-contain facts, citations, latency and - for writes - the database itself.
It runs on a freshly seeded, isolated database so results are reproducible after every build.

Placeholders: "{otp}" = the demo OTP from the previous turn's card; "{confirm}" = press Confirm.
"""

from __future__ import annotations

import json
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from careharbor.config import ROOT_DIR
from careharbor.db import connect

GOLDEN_DIR = ROOT_DIR / "evals" / "golden"


@dataclass
class TurnResult:
    user: str
    reply: str
    ok: bool
    failures: list[str] = field(default_factory=list)
    ms: float = 0.0


@dataclass
class CaseResult:
    id: str
    journey: str
    ok: bool
    turns: list[TurnResult]


def load_cases(path: Path | None = None) -> list[dict]:
    files = [path] if path else sorted(GOLDEN_DIR.glob("*.jsonl"))
    cases = []
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                cases.append(json.loads(line))
    return cases


def check_turn(r: dict, expect: dict, db: Path) -> list[str]:
    fails: list[str] = []
    reply = r.get("reply", "")
    card = r.get("card") or {}
    if "intent" in expect and r.get("intent") != expect["intent"]:
        fails.append(f"intent {r.get('intent')} != {expect['intent']}")
    if "kind" in expect and r.get("kind") != expect["kind"]:
        fails.append(f"kind {r.get('kind')} != {expect['kind']}")
    if "card" in expect and card.get("type") != expect["card"]:
        fails.append(f"card {card.get('type')} != {expect['card']}")
    if "disposition" in expect and card.get("disposition") != expect["disposition"]:
        fails.append(f"disposition {card.get('disposition')} != {expect['disposition']}")
    if "awaiting_confirmation" in expect and bool(r.get("awaiting_confirmation")) != expect["awaiting_confirmation"]:
        fails.append("awaiting_confirmation mismatch")
    for s in expect.get("contains", []):
        if s not in reply:
            fails.append(f"missing '{s}'")
    if expect.get("contains_any") and not any(s in reply for s in expect["contains_any"]):
        fails.append(f"missing any of {expect['contains_any']}")
    for s in expect.get("not_contains", []):
        if s in reply:
            fails.append(f"must not contain '{s}'")
    for s in expect.get("not_contains_ci", []):
        if s.lower() in reply.lower():
            fails.append(f"must not contain '{s}'")
    if "citation_doc" in expect and not any(c.get("id", "").startswith(expect["citation_doc"] + "#") for c in r.get("citations", [])):
        fails.append(f"no citation from {expect['citation_doc']}")
    if "max_ms" in expect and r.get("total_ms", 0) > expect["max_ms"]:
        fails.append(f"too slow {r.get('total_ms')}ms > {expect['max_ms']}ms")
    if "db" in expect:
        got = connect(db).execute(expect["db"]["sql"]).fetchone()[0]
        if got != expect["db"]["equals"]:
            fails.append(f"db {got} != {expect['db']['equals']}")
    if expect.get("reply_matches_db"):
        appt = card.get("appointment") or {}
        row = connect(db).execute("SELECT status FROM appointments WHERE id=?", (appt.get("appointment_id"),)).fetchone()
        if not row or f"#{appt.get('appointment_id')}" not in reply or row[0] != "booked":
            fails.append("reply does not match a stored booked row")
    return fails


async def run_cases(cases: list[dict], *, llm_factory: Callable[[str | None], Any] | None = None, model: str | None = None, work_dir: Path | None = None) -> list[CaseResult]:
    """Run golden cases on an isolated, freshly seeded database (real tools, real graph)."""
    from careharbor.agent.runtime import ChatService
    from careharbor.llm import get_llm
    from careharbor.tools.client import ToolClient
    from careharbor.tools.mcp_server import build_server
    from careharbor.tools.service import ClinicTools
    from scripts.seed import seed

    work = Path(work_dir or tempfile.mkdtemp(prefix="careharbor-golden-"))
    db = work / "golden.db"
    seed(db, None)
    svc = ChatService(ToolClient(build_server(ClinicTools(db, demo_mode=True))), llm_factory=llm_factory or get_llm, db_path=db,
                      checkpoint_path=work / "golden-cp.db", trace_dir=work / "traces")
    results: list[CaseResult] = []
    try:
        await svc.start()
        for case in cases:
            sid = f"golden-{case['id']}-{uuid.uuid4().hex[:8]}"
            last: dict = {}
            turns: list[TurnResult] = []
            for t in case["turns"]:
                user = t["user"]
                if user == "{confirm}":
                    r = await svc.resume(sid, "confirm", model)
                else:
                    if "{otp}" in user:
                        user = user.replace("{otp}", str((last.get("card") or {}).get("demo_otp", "")))
                    r = await svc.turn(sid, user, model)
                fails = check_turn(r, t.get("expect", {}), db)
                turns.append(TurnResult(user, r.get("reply", ""), not fails, fails, r.get("total_ms", 0)))
                last = r
            results.append(CaseResult(case["id"], case.get("journey", ""), all(x.ok for x in turns), turns))
    finally:
        await svc.stop()
    return results


def summarise(results: list[CaseResult]) -> str:
    lines = []
    for c in results:
        lines.append(f"{'PASS' if c.ok else 'FAIL'}  {c.id:<4} {c.journey:<12} {sum(t.ms for t in c.turns) / 1000:6.1f}s")
        for t in c.turns:
            if not t.ok:
                flat = re.sub(r"\s+", " ", t.reply)[:160]
                lines.append(f"        turn '{t.user[:40]}': {'; '.join(t.failures)}")
                lines.append(f"        reply: {flat}")
    passed = sum(c.ok for c in results)
    lines.append(f"\nGolden regression: {passed}/{len(results)} cases passed")
    return "\n".join(lines)
