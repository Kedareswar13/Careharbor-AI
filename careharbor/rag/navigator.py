"""Reasoning-based navigation over the document tree (the "tree" half of vectorless RAG).

retrieve() is the single entry point used by the agents:

1. BM25 shortlists sections (sub-millisecond, deterministic).
2. If the top two documents score close together (ambiguous) and RAG_MODE=tree,
   the LLM reads a headings-only table of contents of the top documents and
   returns the section ids that answer the question. Invalid ids are ignored;
   any failure falls back to the BM25 order.
3. For symptom guidance the tree structure itself picks the sections: the
   chosen leaflet's "Self-care", "See a doctor if" and "Emergency signs".
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from careharbor.config import get_settings
from careharbor.llm import BaseLLM
from careharbor.rag.index import RagIndex, SearchHit, get_index
from careharbor.security import TEMPLATES, Principal

AMBIGUITY_RATIO = 0.8
TRIAGE_SECTIONS = ("self-care", "see-a-doctor-if", "emergency-signs")


@dataclass
class Retrieval:
    hits: list[SearchHit]
    mode: str                    # bm25 | tree-llm | tree-structure
    ambiguous: bool = False
    llm_ms: float = 0.0
    ms: float = 0.0
    candidates: list[str] = field(default_factory=list)
    pipeline: str = "basic"
    variants: list[str] = field(default_factory=list)
    grade: str = ""              # CRAG-style: high | ambiguous | low
    corrective: bool = False     # a corrective retry was needed

    @property
    def citations(self) -> list[dict]:
        seen, out = set(), []
        for h in self.hits:
            if h.node_id not in seen:
                seen.add(h.node_id)
                out.append(h.citation())
        return out


def top_docs(shortlist: list[SearchHit], n: int = 3, index: RagIndex | None = None, folders: tuple[str, ...] | None = None) -> list[tuple[str, float]]:
    best: dict[str, float] = {}
    for h in shortlist:
        if folders and index is not None and index.docs[h.doc_id]["folder"] not in folders:
            continue
        best[h.doc_id] = max(best.get(h.doc_id, 0.0), h.score)
    return sorted(best.items(), key=lambda x: -x[1])[:n]


PRIMARY_FOLDERS = {"care": ("leaflets",), "faq": ("policies",)}


def is_ambiguous(shortlist: list[SearchHit], index: RagIndex | None = None, collection: str = "care") -> bool:
    """Two different primary documents score within 20% of each other. A clinic leaflet and the
    MedlinePlus topic on the same subject are not ambiguity, so only primary folders are compared."""
    docs = top_docs(shortlist, 2, index, PRIMARY_FOLDERS.get(collection) if index else None)
    return len(docs) == 2 and docs[1][1] >= AMBIGUITY_RATIO * docs[0][1]


def llm_pick_sections(llm: BaseLLM, query: str, index: RagIndex, doc_ids: list[str]) -> tuple[list[str], float]:
    system, user = TEMPLATES["navigate"].render(context=index.toc(doc_ids), question=query)
    reply = llm.chat(system, user, task="navigate", json_mode=True, max_tokens=60)
    raw = reply.json().get("sections", [])
    ids = [i for i in raw if isinstance(i, str) and i in index.nodes and index.nodes[i].doc_id in doc_ids] if isinstance(raw, list) else []
    return ids, reply.ms


def shortlist_for(query: str, collection: str, idx: RagIndex, who: Principal | None, pipeline: str, limit: int = 12) -> tuple[list[SearchHit], dict]:
    """basic: one expanded BM25 pass. advanced: multi-query -> RRF -> feature re-rank -> CRAG grade (+ corrective retry)."""
    from careharbor.rag import advanced as A

    if pipeline != "advanced":
        hits = idx.bm25(query, collection, limit=limit, who=who)
        return hits, {"variants": [query], "grade": A.grade(query, hits).level, "corrective": False}
    variants = A.query_variants(query)
    fused = A.rrf([idx.bm25(v, collection, limit=limit, who=who) for v in variants])
    hits = A.rerank(query, fused[:limit], idx)
    g = A.grade(query, hits)
    corrective = False
    if g.level == "low" and len(variants) > 1:
        # Corrective step: retry with the broadest (expanded) variant alone, then re-grade.
        retry = A.rerank(query, idx.bm25(variants[-1], collection, limit=limit, who=who), idx)
        g2 = A.grade(variants[-1], retry)
        if g2.level != "low":
            hits, g, corrective = retry, g2, True
    return hits, {"variants": variants, "grade": g.level, "corrective": corrective}


def retrieve(query: str, collection: str = "care", *, llm: BaseLLM | None = None, mode: str | None = None, purpose: str = "faq", k: int | None = None,
             index: RagIndex | None = None, who: Principal | None = None, pipeline: str | None = None) -> Retrieval:
    """purpose='triage' returns the guidance sections of the best leaflet; 'faq' returns the best sections.
    `who` (role + tenant) filters documents before scoring (Layer 2 access control)."""
    t0 = time.perf_counter()
    s = get_settings()
    idx = index or get_index()
    mode = mode or s.rag_mode
    k = k or s.rag_top_k
    pipeline = pipeline or s.rag_pipeline
    shortlist, info = shortlist_for(query, collection, idx, who, pipeline)
    if not shortlist:
        return Retrieval([], "bm25", ms=round((time.perf_counter() - t0) * 1000, 2))
    ambiguous = is_ambiguous(shortlist, idx, collection)
    candidates = [d for d, _ in top_docs(shortlist, 3, idx, PRIMARY_FOLDERS.get(collection))] or [d for d, _ in top_docs(shortlist, 3)]
    llm_ids: list[str] = []
    llm_ms = 0.0
    if mode == "tree" and ambiguous and llm is not None:
        try:
            llm_ids, llm_ms = llm_pick_sections(llm, query, idx, candidates)
        except Exception:  # noqa: BLE001 - navigation is an optimisation; BM25 is the safe fallback
            llm_ids = []

    if purpose == "triage":
        lead_doc = idx.nodes[llm_ids[0]].doc_id if llm_ids else candidates[0]
        # Prefer the clinic's own leaflet when a MedlinePlus topic wins on keywords.
        if idx.docs[lead_doc]["folder"] != "leaflets":
            leaflet = next((d for d in candidates if idx.docs[d]["folder"] == "leaflets"), None)
            lead_doc = leaflet or lead_doc
        sections = [h for h in idx.doc_sections(lead_doc, who) if h.node_id.split("#", 1)[1].startswith(TRIAGE_SECTIONS)]
        score_of = {h.node_id: h.score for h in shortlist}
        for h in sections:
            h.score = score_of.get(h.node_id, 0.0)
        ref = next((h for h in shortlist if idx.docs[h.doc_id]["folder"] == "medlineplus"), None)
        hits = sections + ([ref] if ref else [])
        used = "tree-llm" if llm_ids else "tree-structure"
    elif llm_ids:
        hits = [idx._hit(idx.nodes[i], next((h.score for h in shortlist if h.node_id == i), 0.0)) for i in llm_ids][:k]
        used = "tree-llm"
    else:
        hits = idx._diverse(shortlist, k)
        used = "bm25"
    return Retrieval(hits, used, ambiguous, llm_ms, round((time.perf_counter() - t0) * 1000, 2), candidates,
                     pipeline, info["variants"], info["grade"], info["corrective"])
