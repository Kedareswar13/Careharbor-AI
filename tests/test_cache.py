"""P6: caching - FAQ semantic answer cache (vectorless), invalidation, tool-result TTL cache."""

from __future__ import annotations

import json
from datetime import date, datetime

from careharbor.agent.runtime import ChatService
from careharbor.cache import AnswerCache
from careharbor.llm import MockLLM
from careharbor.tools.client import ToolClient
from careharbor.tools.mcp_server import build_server
from careharbor.tools.service import ClinicTools
from scripts.seed import seed


def test_semantic_match_and_threshold():
    c = AnswerCache()
    c.put("What are your OPD timings?", "v1", "m", {"reply": "8 AM to 6 PM", "citations": []})
    assert c.get("what are the OPD timings", "v1", "m")["reply"] == "8 AM to 6 PM"
    assert c.get("opd timings?", "v1", "m") is not None
    assert c.get("Do you accept Star Health insurance?", "v1", "m") is None


def test_invalidated_by_document_version_and_model():
    c = AnswerCache()
    c.put("What are your OPD timings?", "v1", "m", {"reply": "x", "citations": []})
    assert c.get("What are your OPD timings?", "v2", "m") is None  # documents changed
    assert c.get("What are your OPD timings?", "v1", "other-model") is None
    c.put("Can I pay by UPI?", "v2", "m", {"reply": "y", "citations": []})
    assert c.stats()["size"] == 1  # stale-version entries were evicted


def test_lru_eviction():
    c = AnswerCache(maxsize=2)
    for q in ("opd timings", "star health insurance", "consultation fee"):
        c.put(q, "v", "m", {"reply": q, "citations": []})
    assert c.get("opd timings", "v", "m") is None and c.get("consultation fee", "v", "m")


async def test_faq_second_ask_served_from_cache_and_phi_never_cached(tmp_path):
    from careharbor.cache import faq_cache

    faq_cache._items.clear()
    db = tmp_path / "c.db"
    seed(db, date(2026, 11, 2))
    mock = MockLLM({"router": lambda s, u: json.dumps({"intent": "out_of_scope"}), "safety": lambda s, u: '{"emergency": false}'})
    now = datetime(2026, 11, 2, 8, 0)
    svc = ChatService(ToolClient(build_server(ClinicTools(db, clock=lambda: now, demo_mode=True))), llm_factory=lambda m=None: mock, clock=lambda: now,
                      db_path=db, checkpoint_path=tmp_path / "cp.db", trace_dir=tmp_path / "t")
    try:
        first = await svc.turn("cache-sess-0001", "What are your OPD timings?")
        answers = sum(1 for c in mock.calls if c["task"] == "answer")
        second = await svc.turn("cache-sess-0002", "what are the opd timings")
        assert second["reply"] == first["reply"] and any(s["key"] == "cache" for s in second["stages"])
        assert sum(1 for c in mock.calls if c["task"] == "answer") == answers  # no new LLM call
        # Prescription answers are never cached: each one comes from the patient's own record.
        r = await svc.turn("cache-sess-0003", "show my prescriptions")
        assert r["card"]["type"] == "identity"
        assert all("Cetirizine" not in e.value["reply"] for e in faq_cache._items.values())
    finally:
        await svc.stop()


def test_tool_read_cache_and_invalidation_on_write(tmp_path):
    db = tmp_path / "t.db"
    seed(db, date(2026, 11, 2))
    t = ClinicTools(db, clock=lambda: datetime(2026, 11, 2, 8, 0), demo_mode=True)
    a = t.get_slots("D01", "2026-11-06")
    assert t.get_slots("D01", "2026-11-06") is a  # served from the 30 s cache
    code = t.verify_identity("cache-tool-sess-1", "9876500001", "1990-05-14")["demo_otp"]
    t.confirm_otp("cache-tool-sess-1", code)
    t.book_appointment("cache-tool-sess-1", a["slots"][0]["slot_id"])
    b = t.get_slots("D01", "2026-11-06")
    assert b is not a and a["slots"][0]["slot_id"] not in {s["slot_id"] for s in b["slots"]}  # writes invalidate
