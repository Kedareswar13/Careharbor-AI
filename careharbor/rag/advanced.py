"""Advanced, still vectorless, retrieval techniques.

Pipeline used by retrieve(..., pipeline="advanced"):

  1. Conversational query rewriting - a follow-up like "and on Sunday?" is rewritten with the previous question.
  2. Multi-query expansion - the original query, a synonym/Hinglish-expanded variant and a keyword-only variant.
  3. Reciprocal Rank Fusion (RRF) - fuses the BM25 ranking of every variant (robust to any single bad variant).
  4. Feature re-ranking - a cheap "cross-feature" re-ranker: query coverage, heading match, title match, BM25 score.
  5. Corrective RAG (CRAG-style) grading - high / ambiguous / low confidence; low triggers a corrective retry with the
     broadest variant, and if it is still low the caller refuses instead of guessing.
  6. Small-to-big context - a matched section (child) is given with its parent document's outline.
  7. Sentence-level attribution - each answer sentence is mapped to the section that supports it (evidence map).

Every step is deterministic and sub-millisecond on this corpus, so it can be evaluated with an ablation study
(see the "Retrieval quality" eval suite).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from careharbor.rag.index import RagIndex, SearchHit
from careharbor.rag.text import STOPWORDS, expand_query, tokenize

FOLLOW_UP = re.compile(r"(?i)^\s*(and|also|what about|how about|what if|same for|is it|are they|does it|do they|then|ok and|and on|and for)\b|\b(it|that|this|those|them|there)\b")
QUESTION_WORDS = {"what", "when", "where", "which", "who", "how", "is", "are", "do", "does", "can", "could", "should", "will", "would", "your", "you", "please", "tell"}


def contextualize(query: str, previous: str | None) -> tuple[str, bool]:
    """Rewrite a short follow-up using the previous question so retrieval has the missing topic."""
    if not previous:
        return query, False
    words = re.findall(r"[a-zA-Z]+", query)
    if len(words) <= 6 and FOLLOW_UP.search(query):
        return f"{query} (follow-up to: {previous})", True
    return query, False


def query_variants(query: str) -> list[str]:
    """Multi-query: original, keyword-only and synonym-expanded variants (deduplicated)."""
    keywords = " ".join(w for w in re.findall(r"[a-zA-Z0-9]+", query.lower()) if w not in STOPWORDS and w not in QUESTION_WORDS)
    expanded = " ".join(dict.fromkeys(expand_query(query)))
    out: list[str] = []
    for v in (query, keywords, expanded):
        if v and v not in out:
            out.append(v)
    return out


def rrf(rankings: list[list[SearchHit]], k: int = 60) -> list[SearchHit]:
    """Reciprocal Rank Fusion: score(d) = sum over rankings of 1 / (k + rank)."""
    fused: dict[str, float] = {}
    best: dict[str, SearchHit] = {}
    for ranking in rankings:
        for rank, h in enumerate(ranking, start=1):
            fused[h.node_id] = fused.get(h.node_id, 0.0) + 1.0 / (k + rank)
            if h.node_id not in best or h.score > best[h.node_id].score:
                best[h.node_id] = h
    return sorted(best.values(), key=lambda h: (-fused[h.node_id], h.node_id))


def _terms(text: str) -> set[str]:
    return set(tokenize(text))


def rerank(query: str, hits: list[SearchHit], index: RagIndex) -> list[SearchHit]:
    """Feature re-ranker (vectorless stand-in for a cross-encoder): coverage of the query's terms by the section,
    heading/title matches, plus the normalised BM25 score. Ties keep the fused order."""
    q = set(expand_query(query)) or _terms(query)
    if not hits or not q:
        return hits
    top = max(h.score for h in hits) or 1.0
    scored = []
    for pos, h in enumerate(hits):
        body, head, title = _terms(h.text), _terms(h.section), _terms(h.title + " " + index.docs[h.doc_id].get("tags", ""))
        coverage = len(q & (body | head | title)) / len(q)
        primary = 0.4 if index.docs[h.doc_id]["folder"] in {"leaflets", "policies"} else 0.0  # clinic-authored sources first
        s = 2.0 * coverage + 1.0 * len(q & head) / len(q) + 0.5 * len(q & title) / len(q) + 1.0 * (h.score / top) + primary
        scored.append((s, -pos, h))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [h for _, _, h in scored]


@dataclass
class Grade:
    level: str          # high | ambiguous | low
    top_score: float
    coverage: float
    reason: str


def grade(query: str, hits: list[SearchHit], min_score: float = 2.5, min_coverage: float = 0.34, min_joint_coverage: float = 0.5) -> Grade:
    """CRAG-style retrieval evaluator: is the best evidence strong enough to answer from?
    Multi-part questions ("do you offer X and what does it cost?") are answered by several sections, so a weak top section is
    acceptable when the top-3 sections the model will see jointly cover the question (measured: answerable >= 0.57, refusals <= 0.40)."""
    if not hits:
        return Grade("low", 0.0, 0.0, "no results")
    q = set(expand_query(query)) or _terms(query)
    h = hits[0]
    cov = len(q & _terms(h.text + " " + h.section + " " + h.title)) / max(1, len(q))
    if h.score < min_score:
        return Grade("low", h.score, round(cov, 2), "weak keyword evidence")
    if cov < min_coverage:
        joint = len(q & set().union(*(_terms(x.text + " " + x.section + " " + x.title) for x in hits[:3]))) / max(1, len(q))
        if joint < min_joint_coverage:
            return Grade("low", h.score, round(cov, 2), "weak keyword evidence")
        return Grade("ambiguous", h.score, round(joint, 2), "answer spans several sections")
    second = next((x for x in hits[1:] if x.doc_id != h.doc_id), None)
    if second and second.score >= 0.9 * h.score:
        return Grade("ambiguous", h.score, round(cov, 2), "two documents compete")
    return Grade("high", h.score, round(cov, 2), "clear best section")


def parent_context(hit: SearchHit, index: RagIndex) -> str:
    """Small-to-big: the matched section plus its parent document's title and outline."""
    doc = index.docs[hit.doc_id]
    outline = ", ".join(doc["sections"])
    return f"[{hit.title} - {hit.section}] (document outline: {outline})\n{hit.text}"


def attribute(answer: str, hits: list[SearchHit]) -> list[dict]:
    """Sentence-level attribution: for each answer sentence, the section with the highest word overlap."""
    pre = lambda s: {w[:5] for w in re.findall(r"[a-z]+", s.lower()) if len(w) > 3}  # noqa: E731
    out = []
    for sentence in [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", answer) if len(s.strip()) > 3]:
        words = pre(sentence)
        if not words or not hits:
            continue
        best = max(hits, key=lambda h: len(words & pre(h.text + " " + h.section)))
        support = len(words & pre(best.text + " " + best.section)) / len(words)
        out.append({"sentence": sentence[:200], "source": best.node_id, "title": f"{best.title} - {best.section}", "support": round(support, 2)})
    return out
