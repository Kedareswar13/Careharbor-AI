"""Caches (P6).

- AnswerCache: a *semantic* answer cache for FAQ only - never for PHI, triage or bookings.
  Vectorless: two questions match when their expanded BM25 terms overlap (Jaccard >= 0.75), so
  "what are your opd timings" and "opd hours?" share an answer. Keys include the content index
  version and the model, so any document change invalidates every cached answer automatically.
- Tool-result cache for read-only doctor/slot lookups (30 s TTL) lives in careharbor/tools/service.py.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from careharbor.rag.text import expand_query


def query_terms(query: str) -> frozenset[str]:
    return frozenset(expand_query(query))


@dataclass
class CacheEntry:
    terms: frozenset[str]
    version: str
    model: str
    value: dict[str, Any]
    at: float


class AnswerCache:
    def __init__(self, maxsize: int = 256, threshold: float = 0.75, ttl_s: float = 6 * 3600):
        self.maxsize, self.threshold, self.ttl = maxsize, threshold, ttl_s
        self._items: OrderedDict[tuple, CacheEntry] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    @staticmethod
    def similarity(a: frozenset[str], b: frozenset[str]) -> float:
        return len(a & b) / len(a | b) if a and b else 0.0

    def get(self, query: str, version: str, model: str) -> dict[str, Any] | None:
        terms = query_terms(query)
        now = time.monotonic()
        with self._lock:
            exact = self._items.get((terms, version, model))
            best, best_sim = exact, 1.0 if exact else 0.0
            if best is None:
                for e in self._items.values():
                    if e.version == version and e.model == model:
                        sim = self.similarity(terms, e.terms)
                        if sim > best_sim:
                            best, best_sim = e, sim
            if best is not None and best_sim >= self.threshold and now - best.at < self.ttl:
                self._items.move_to_end((best.terms, best.version, best.model))
                self.hits += 1
                return {**best.value, "cache": {"hit": True, "similarity": round(best_sim, 2)}}
            self.misses += 1
            return None

    def put(self, query: str, version: str, model: str, value: dict[str, Any]) -> None:
        terms = query_terms(query)
        if not terms:
            return
        with self._lock:
            # Drop entries built on an older document version: they can never be served again.
            for key in [k for k, e in self._items.items() if e.version != version]:
                del self._items[key]
            self._items[(terms, version, model)] = CacheEntry(terms, version, model, value, time.monotonic())
            self._items.move_to_end((terms, version, model))
            while len(self._items) > self.maxsize:
                self._items.popitem(last=False)

    def stats(self) -> dict[str, Any]:
        return {"size": len(self._items), "hits": self.hits, "misses": self.misses}


faq_cache = AnswerCache()
