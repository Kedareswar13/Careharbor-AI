"""Retrieval smoke test against the real index, optionally with live LLM tree navigation.

    uv run python scripts/rag_smoke.py            # BM25 + tree-structure (no LLM)
    uv run python scripts/rag_smoke.py --live     # also LLM navigation for ambiguous queries (Ollama)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from careharbor.llm import get_llm, warm_up  # noqa: E402
from careharbor.logging_setup import setup_logging  # noqa: E402
from careharbor.rag.index import get_index  # noqa: E402
from careharbor.rag.navigator import retrieve  # noqa: E402
from tests.test_rag import CARE, FAQ  # noqa: E402


def main() -> int:
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="use the configured LLM for ambiguous queries")
    args = ap.parse_args()
    idx = get_index(rebuild=True)
    llm = None
    if args.live:
        warm_up()
        llm = get_llm()
    print(f"Index v{idx.version}: {len(idx.docs)} documents, {len(idx.nodes)} sections (no embeddings)\n")
    passed = 0
    rows = [(q, e, "care", "triage") for q, e in CARE] + [(q, e, "faq", "faq") for q, e in FAQ]
    for q, expected, coll, purpose in rows:
        r = retrieve(q, coll, llm=llm, mode="tree" if args.live else "bm25", purpose=purpose, index=idx)
        top = r.hits[0].doc_id if r.hits else "-"
        good = top == expected
        passed += good
        print(f"{'PASS' if good else 'FAIL'}  {r.ms:7.1f} ms  {r.mode:<14} {q[:48]:<48} -> {top}")
    print(f"\n{passed}/{len(rows)} queries returned the expected document")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
