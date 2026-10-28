"""LLM access through any OpenAI-compatible endpoint (Ollama locally, Groq/Gemini in the cloud).

- OpenAICompatLLM wraps langchain-openai's ChatOpenAI and records tokens and latency.
- MockLLM is deterministic (no network) for unit tests and offline demos.
- warm_up()/keep_alive() load the Ollama model once and keep it resident (60 min),
  using Ollama's native API because the OpenAI-compatible API has no keep_alive field.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable

import httpx

from careharbor.config import get_settings

log = logging.getLogger("careharbor.llm")


@dataclass
class LLMReply:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    ms: float = 0.0

    def json(self) -> dict:
        return parse_json(self.text)


def parse_json(text: str) -> dict:
    """Best-effort JSON object extraction (small models sometimes wrap JSON in prose or code fences)."""
    if not text:
        return {}
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                v = json.loads(m.group(0))
                return v if isinstance(v, dict) else {}
            except json.JSONDecodeError:
                return {}
    return {}


class BaseLLM:
    name: str = "base"

    def chat(self, system: str, user: str, *, task: str = "answer", json_mode: bool = False, max_tokens: int | None = None) -> LLMReply:
        raise NotImplementedError

    async def achat(self, system: str, user: str, *, task: str = "answer", json_mode: bool = False, max_tokens: int | None = None) -> LLMReply:
        return await asyncio.to_thread(self.chat, system, user, task=task, json_mode=json_mode, max_tokens=max_tokens)


class OpenAICompatLLM(BaseLLM):
    def __init__(self, model: str):
        from langchain_openai import ChatOpenAI

        s = get_settings()
        self.name = model
        self._max_tokens = s.llm_num_predict
        common = dict(model=model, base_url=s.llm_base_url, api_key=s.llm_api_key, temperature=s.llm_temperature, timeout=s.llm_timeout_s, max_retries=1)
        self._plain = ChatOpenAI(**common)
        self._json = ChatOpenAI(**common, model_kwargs={"response_format": {"type": "json_object"}})

    def chat(self, system: str, user: str, *, task: str = "answer", json_mode: bool = False, max_tokens: int | None = None) -> LLMReply:
        from langchain_core.messages import HumanMessage, SystemMessage

        client = (self._json if json_mode else self._plain).bind(max_tokens=max_tokens or self._max_tokens)
        t0 = time.perf_counter()
        msg = client.invoke([SystemMessage(content=system), HumanMessage(content=user)])
        ms = (time.perf_counter() - t0) * 1000
        usage = getattr(msg, "usage_metadata", None) or {}
        reply = LLMReply(str(msg.content).strip(), self.name, usage.get("input_tokens", 0), usage.get("output_tokens", 0), round(ms, 1))
        log.info("llm %s task=%s in=%s out=%s %.0fms", self.name, task, reply.prompt_tokens, reply.completion_tokens, ms)
        return reply


# --------------------------------------------------------------------------- mock
def _mock_navigate(system: str, user: str) -> str:
    ids = re.findall(r"^\s+(\S+#\S+) ::", user, re.M)
    return json.dumps({"sections": ids[:3]})


def _mock_answer(system: str, user: str) -> str:
    """Deterministic stand-in for an LLM so CI can run without a model.
    Triage: echo the first self-care bullets. FAQ: quote the two top-ranked retrieved sections verbatim (always faithful),
    or NOT_FOUND when the retrieved context covers too little of the question (like a careful model)."""
    ctx = re.search(r"<context>(.*?)</context>", user, re.S)
    q = re.search(r"<question>(.*?)</question>", user, re.S)
    context = ctx.group(1) if ctx else user
    if "triage" in system.lower() or "self-care" in system.lower():
        bullets = [ln.strip()[2:] for ln in context.splitlines() if ln.strip().startswith("- ")][:4]
        return "Here is what the clinic's guidance says:\n" + "\n".join(f"- {b}" for b in bullets)
    blocks = [b.strip() for b in re.split(r"(?m)^\[[^\]]*\]\s*$", context) if b.strip()]
    pre = lambda s: {w[:5] for w in re.findall(r"[a-z]+", s.lower()) if len(w) >= 3}  # noqa: E731
    qwords = pre(q.group(1) if q else "") - pre("your what clinic there have does with when which about should could the and can you are for how")
    covered = len(qwords & pre(context))
    if "NOT_FOUND" in system and (not blocks or covered < min(2, -(-len(qwords) // 2))):
        return "NOT_FOUND"
    if not blocks:
        return "I can help with appointments, prescriptions, symptoms and clinic questions."
    return "\n".join(re.sub(r"\s*\n\s*", " ", b)[:300] for b in blocks[:2])


class MockLLM(BaseLLM):
    """Deterministic stand-in for tests. Handlers are keyed by task and may be overridden per test."""

    def __init__(self, handlers: dict[str, Callable[[str, str], str]] | None = None):
        self.name = "mock"
        self.handlers: dict[str, Callable[[str, str], str]] = {"navigate": _mock_navigate, "answer": _mock_answer}
        self.handlers.update(handlers or {})
        self.calls: list[dict] = []

    def chat(self, system: str, user: str, *, task: str = "answer", json_mode: bool = False, max_tokens: int | None = None) -> LLMReply:
        handler = self.handlers.get(task)
        text = handler(system, user) if handler else ("{}" if json_mode else "")
        self.calls.append({"task": task, "system": system, "user": user, "reply": text})
        return LLMReply(text, "mock", len(system.split()) + len(user.split()), len(text.split()), 0.1)


# ----------------------------------------------------------------------- factory
@lru_cache(maxsize=8)
def _cached_llm(provider: str, model: str) -> BaseLLM:
    return MockLLM() if provider == "mock" else OpenAICompatLLM(model)


def get_llm(model: str | None = None) -> BaseLLM:
    s = get_settings()
    return _cached_llm(s.llm_provider, model or s.llm_model)


def _ollama_native_base() -> str | None:
    base = get_settings().llm_base_url.rstrip("/")
    if ":11434" not in base:
        return None
    return base[: -len("/v1")] if base.endswith("/v1") else base


def list_models() -> list[dict]:
    """Chat-capable models for the UI model menu (Ollama tags, else the configured models)."""
    s = get_settings()
    native = _ollama_native_base()
    if native and s.llm_provider != "mock":
        try:
            tags = httpx.get(f"{native}/api/tags", timeout=5).json().get("models", [])
            out = []
            for m in tags:
                caps = m.get("capabilities") or ["completion"]
                fam = (m.get("details") or {}).get("family", "")
                if "completion" in caps and "embedding" not in caps and "vision" not in caps and "bert" not in fam:
                    out.append({"name": m["name"], "size_gb": round(m.get("size", 0) / 1e9, 1), "params": (m.get("details") or {}).get("parameter_size", ""), "default": m["name"] == s.llm_model})
            if out:
                return sorted(out, key=lambda x: (not x["default"], x["size_gb"]))
        except (httpx.HTTPError, ValueError):
            pass
    names = [s.llm_model, s.llm_fallback_model] if s.llm_provider != "mock" else ["mock"]
    return [{"name": n, "size_gb": 0, "params": "", "default": i == 0} for i, n in enumerate(dict.fromkeys(names))]


def warm_up(model: str | None = None) -> bool:
    """Load model(s) into memory and pin them for LLM_KEEP_ALIVE (Ollama only; no-op elsewhere).
    With no argument, warms both the answer model and the fast classification model."""
    s = get_settings()
    native = _ollama_native_base()
    if not native or s.llm_provider == "mock":
        return False
    names = [model] if model else list(dict.fromkeys(m for m in (s.llm_model, s.llm_fast_model) if m))
    ok = True
    for name in names:
        try:
            httpx.post(f"{native}/api/generate", json={"model": name, "prompt": "", "keep_alive": s.llm_keep_alive}, timeout=120)
        except httpx.HTTPError as exc:
            log.warning("warm-up of %s failed: %s", name, exc)
            ok = False
    return ok


_keepalive_started = False


def start_keep_alive(interval_s: float = 240.0) -> None:
    """Background re-pin: OpenAI-compatible calls reset Ollama's keep_alive to its default (5 min)."""
    global _keepalive_started
    if _keepalive_started or _ollama_native_base() is None:
        return
    _keepalive_started = True

    def loop() -> None:
        while True:
            time.sleep(interval_s)
            warm_up()

    threading.Thread(target=loop, name="ollama-keepalive", daemon=True).start()
