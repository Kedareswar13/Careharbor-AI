# Advanced RAG concepts: what CareHarbor AI implements and what to say about each

> **CareHarbor AI — your clinic's first point of contact for patient questions, appointment workflows, prescription queries, and safe escalation to staff.**

CareHarbor AI's retrieval is **vectorless**: a document tree plus BM25, with no embeddings and no vector database. Most "advanced RAG" ideas still apply, because they are about *what you retrieve, how you rank it, and how you check the answer*, not about vectors.

**Status key:**
- ✅ **Implemented:** in the code, with a test or eval.
- 🟡 **Partial:** a simpler form is implemented.
- ⬜ **Not implemented:** a deliberate scope decision. Know the concept anyway.

## A. Indexing and chunking

| # | Technique | Status | Where / what to say |
|---|---|---|---|
| 1 | **Structure-aware chunking** (split on headings) | ✅ | `rag/index.py` `parse_markdown`: every `##` section is a node. "Chunks follow document structure, so a citation points to a real section such as *Insurance → Insurers we work with*, not an arbitrary 512-token window." |
| 2 | **Hierarchical / tree index** (doc → section) | ✅ | `RagIndex.docs` + `nodes`. Enables section-level citations and the triage "pick Self-care / See a doctor / Emergency" navigation. |
| 3 | **Parent–child ("small-to-big") retrieval** | ✅ | `rag/advanced.py` `parent_context`: match a small section, give the model that section *plus the parent document's outline*. |
| 4 | **Metadata enrichment** (tags, audience, tenant, source URL) | ✅ | Front matter → `Node.audience/tenant/source_url`. Metadata drives access control (Layer 2) and citations. |
| 5 | **Field boosting** (title × 3, tags × 2, heading × 2) | ✅ | `parse_markdown` token weighting: BM25F-style boosting without a second index. |
| 6 | **Index versioning / invalidation** | ✅ | `content_fingerprint`: a content hash. The index rebuilds and the FAQ cache invalidates automatically when a document changes. |
| 7 | Sliding-window / overlap chunking | ⬜ | Not needed: the documents are short and well structured. Say *why*: overlap is for long unstructured text. |
| 8 | Proposition / semantic chunking | ⬜ | Splits text into atomic facts using an LLM or embeddings. Too costly on a CPU laptop for little gain on this corpus. |
| 9 | Contextual retrieval (prepend document context to each chunk before indexing) | 🟡 | The title and tags are folded into every section's tokens, which is a lexical version of the same idea. |

## B. Query understanding

| # | Technique | Status | Where / what to say |
|---|---|---|---|
| 10 | **Query normalisation and expansion** (synonyms, Hinglish, UK/US spelling) | ✅ | `rag/text.py`. The ablation shows this is the biggest win: Hit@1 rises from 78% to 100% over raw BM25. |
| 11 | **Multi-query retrieval** | ✅ | `advanced.query_variants`: original, keyword-only and expanded variants. |
| 12 | **Reciprocal Rank Fusion (RRF)** | ✅ | `advanced.rrf`: `score = Σ 1/(60 + rank)` across the variant rankings. Robust because no single variant can dominate. |
| 13 | **Conversational query rewriting** (follow-ups) | ✅ | `advanced.contextualize`: "and on Sunday?" + the previous question gives a self-contained query. Shown as "Searched as" in the evidence panel. |
| 14 | **Intent routing / query classification** | ✅ | `agent/graph.py` router: rules first, then an LLM fallback on the fast model. Routes to triage / booking / prescription / FAQ / handoff. |
| 15 | HyDE (hypothetical document embeddings) | ⬜ | Generate a fake answer and search with it. Meant for embedding search; with BM25 it mostly adds hallucinated keywords. A deliberate no. |
| 16 | Step-back prompting / query decomposition | ⬜ | For multi-hop questions ("compare A and B"). Clinic FAQs are single-hop. |
| 17 | LLM query rewriting | 🟡 | Available as LLM table-of-contents navigation (`RAG_MODE=tree`). Measured slower and no more accurate on this CPU, so it's off by default. |

## C. Retrieval and ranking

| # | Technique | Status | Where / what to say |
|---|---|---|---|
| 18 | **BM25 / sparse lexical retrieval** | ✅ | `RagIndex.bm25`. Explainable, sub-millisecond, and strong on proper nouns ("Star Health", "CGHS", "Dr. Rao"). |
| 19 | **Re-ranking** | ✅ | `advanced.rerank`: a feature re-ranker (query coverage, heading match, title match, BM25 score, clinic-source preference). A cheap stand-in for a cross-encoder. |
| 20 | Cross-encoder re-ranking (e.g. bge-reranker) | ⬜ | Would need a Hugging Face model download (blocked on this network) and roughly 0.2–1 s per query on CPU. The feature re-ranker has the same interface, so it's swappable. |
| 21 | Dense / embedding retrieval | ⬜ | A deliberate choice (vectorless). `nomic-embed-text` is installed, so hybrid search is a ~50-line extension: add a dense ranking to the RRF list. |
| 22 | Hybrid search (sparse + dense) | 🟡 | The RRF fusion layer already exists. Adding a dense ranking makes it hybrid. |
| 23 | **Access-controlled retrieval** (pre-filtering) | ✅ | Role and tenant filter applied *before* scoring (`can_read`). An interviewer favourite: "post-filtering top-k can leak or starve results; pre-filtering can't." |
| 24 | **Diversity** (MMR-like) | 🟡 | `_diverse`: at most 2 sections per document in the top-k. |
| 25 | **Reasoning-based navigation** (PageIndex-style table of contents) | ✅ | `navigator.llm_pick_sections`. Off by default, with measured numbers to justify it. |

## D. Generation and grounding

| # | Technique | Status | Where / what to say |
|---|---|---|---|
| 26 | **Context compression** | ✅ | Top-3 sections, triage bullets only, `[:900]` caps. Keeps CPU prefill (≈55 tokens/s) inside the latency budget. |
| 27 | **Fixed prompt templates with data slots** | ✅ | `security.TEMPLATES`: retrieved text and user text go into delimited slots that can't break the prompt structure. |
| 28 | **Citations** (section-level) | ✅ | Every grounded answer lists the source sections; the UI shows them. |
| 29 | **Sentence-level attribution** | ✅ | `advanced.attribute`: each answer sentence → best supporting section + support %. Shown in the "Evidence map". |
| 30 | **Answer abstention** ("I don't know") | ✅ | The FAQ prompt returns `NOT_FOUND`; the CRAG gate refuses on weak evidence; the scope guard refuses off-topic questions. |
| 31 | **Groundedness / hallucination check** | ✅ | `guardrails.unsupported_sentences` + numbers-must-appear-in-context. A failure falls back to an extractive quote. |
| 32 | **Corrective RAG (CRAG)** | ✅ | `advanced.grade`: high / ambiguous / low. A low grade triggers a corrective retry, then a refusal. A multi-part question ("do you offer X *and* what does it cost?") is graded on the **joint** coverage of the top-3 sections the model will see. The top section alone covered 14% and was wrongly refused on the live model (G28). |
| 33 | Self-RAG (reflection tokens, self-critique) | 🟡 | The *deterministic* critique (claim support, numbers, diagnosis/dose rules) runs after generation. No LLM self-critique, to save latency, but the role is the same. |
| 34 | Extractive fallback | ✅ | When generation fails a check, CareHarbor AI quotes the most relevant source lines verbatim (`_relevant_lines`). |
| 35 | GraphRAG / knowledge-graph RAG | ⬜ | Useful for entity relationships across many documents. The clinic data is relational, so it lives in SQL behind MCP tools instead. |
| 36 | Agentic RAG (tools + retrieval in a loop) | ✅ | The LangGraph supervisor decides between retrieval (leaflets / FAQ) and tools (records, bookings). Retrieval is one tool among many. |

## E. Caching, evaluation and operations

| # | Technique | Status | Where / what to say |
|---|---|---|---|
| 37 | **Semantic answer cache** (vectorless) | ✅ | `cache.py`: Jaccard similarity over expanded query terms, keyed by document version + model. FAQ only; never PHI. |
| 38 | **Retrieval metrics** (Hit@k, MRR) **+ ablation** | ✅ | The "Retrieval quality" eval suite compares 4 pipeline configurations on 40 labelled queries. |
| 39 | **RAGAS-style metrics** (faithfulness, context precision, answer correctness) | ✅ | FAQ grounding suite, computed by deterministic code instead of an LLM judge. |
| 40 | LLM-as-judge | ⬜ | A deliberate choice: deterministic scoring is reproducible and doesn't grade a model with a model. It could be added as a *second opinion* (the brief allows that). Real evidence it is needed: the live 3B model answered "No." to "Is a follow-up visit free?" while quoting a line saying it *is* free, and the fact-presence eval still passed. That is now caught by a deterministic **polarity check** (verdict vs source negation, G40). An NLI model or LLM judge is the general fix. |
| 41 | **Golden / regression datasets** | ✅ | `evals/golden/`: multi-turn conversations with database checks, run after every phase. |
| 42 | **Retrieval observability** | ✅ | The audit log records who retrieved which document ids, the decision and the grade. Never document text; PII masked. |
| 43 | Prompt-injection defence for RAG (indirect injection) | ✅ | `sanitise_retrieved` drops instruction-like lines from documents; tested with planted attacks. |

---

## The 12 most likely interview questions (with model answers)

**1. Why vectorless RAG instead of a vector database?**
"The corpus is about 50 well-structured clinic documents. BM25 over a section tree is explainable, sub-millisecond, and strong on the proper nouns patients actually type: 'Star Health', 'CGHS', 'Dr. Rao'. I *measured* it: with synonym and Hinglish expansion it scores 100% Hit@1 on a 40-query labelled set, versus 78% for raw BM25. Embeddings would add a model download and latency for no gain here. The fusion layer is ready if a larger corpus needs hybrid search."

**2. Walk me through your retrieval pipeline.**
"Conversational rewrite → three query variants → BM25 for each, pre-filtered by role and tenant → reciprocal rank fusion → feature re-ranker → a CRAG-style grade. A low grade triggers a corrective retry, then a refusal. Then small-to-big context, a fixed prompt template, generation, and a deterministic groundedness check, with an extractive fallback and sentence-level attribution."

**3. How do you stop hallucinations?**
"Four mechanisms. The model can say `NOT_FOUND`. A CRAG gate refuses on weak evidence. After generation, every number must appear in the context and every sentence must be mostly supported by it. If anything fails, I quote the source instead. Patient records never go through the LLM at all: they're templated from database rows, and an eval checks every 'booked' claim against the database."

**4. How do you evaluate RAG?**
"Retrieval: Hit@1, Hit@3 and MRR with an ablation per pipeline stage. Generation: answer correctness, context precision and faithfulness, RAGAS-style but computed by code. End to end: a golden set of multi-turn conversations with database assertions. All of it runs in CI with a deterministic mock model, plus a live run on the real model."

**5. What did the ablation teach you?**
"That most of the gain came from query expansion, not from the fancy parts. RRF and re-ranking didn't improve Hit@1 on a saturated set; they add robustness. CRAG's value showed up in refusals. An early run even showed the advanced pipeline *below* the baseline, because the re-ranker lost the clinic-source boost. I only found that because I measured."

**6. How do you secure a RAG system?**
"Four layers:
- validate the query before the pipeline;
- enforce role and tenant access *before* retrieval;
- use fixed templates with data slots;
- log document ids, never text, with PII masked.

Plus sanitising retrieved text against indirect injection, and binding patient identity to a signed, server-issued session rather than anything the client or model supplies."

**7. Pre-filtering vs post-filtering for access control?**
"Post-filtering the top-k can leak data through scores or citations, and can return nothing when every top-k hit is filtered out. I filter before scoring, so unauthorised sections are never candidates. A tenant canary document with a different fee proves it in tests."

**8. How do you handle follow-up questions?**
"Conversational query rewriting: a short follow-up with an anaphor ('and on Sunday?') is combined with the previous question. The rewritten query is shown to the user as 'Searched as …' for transparency."

**9. What is RRF and why use it?**
"Reciprocal Rank Fusion: each document scores Σ 1/(k + rank) across rankings, with k = 60. It merges rankings from different query variants or retrievers without calibrating their scores against each other. That's also how you'd make it hybrid later."

**10. How does your cache stay correct?**
"FAQ only, never patient data. The key includes the document-version hash and the model, so editing a policy invalidates every cached answer. Matching uses Jaccard similarity on expanded query terms, so it's semantic without embeddings."

**11. What's corrective RAG?**
"Grade the retrieval before generating. CareHarbor AI grades on top score, query coverage and the margin over the next document. Low means a corrective retry with the broadest query variant; still low means refuse. That turns 'I'm not sure' into a decision instead of a hallucination."

**12. What would you add with more time or a GPU?**
"A cross-encoder re-ranker and dense retrieval fused into the existing RRF (hybrid search), an LLM-judge second opinion on a held-out eval set, a Postgres backend, and a larger, independently written held-out test set. The current eval sets were written by me, so there's overfitting risk, and I say that openly."
