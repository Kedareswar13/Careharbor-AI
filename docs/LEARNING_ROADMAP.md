# Learning roadmap: every concept in CareHarbor AI (M1–M15)

> **CareHarbor AI — your clinic's first point of contact for patient questions, appointment workflows, prescription queries, and safe escalation to staff.**

Work through the modules in order. For each module:
1. **Watch:** search the YouTube phrase and watch one good video.
2. **Find:** open the files and locate the concept in the code.
3. **Write:** make the handwritten note.
4. **Check:** answer the self-check questions aloud.
5. **Practise:** answer the interview question in under 60 seconds.

Suggested pace: two modules a day, so the whole roadmap takes about 8 days. M15 covers the advanced concepts added in the final review pass. Interviewers ask about these most.

| Module | Topic | Core idea in one line |
|---|---|---|
| M1 | Project setup, config and secrets | Reproducible env (uv lockfile) + 12-factor config; secrets never in git |
| M2 | Realistic data without privacy risk | Synthea-derived patients + public-domain MedlinePlus + authored leaflets |
| M3 | SQLite transactions and constraints | The database, not the LLM, guarantees "no double booking" |
| M4 | MCP tools and identity isolation | Identity travels out-of-band (`_meta`); tools never take `patient_id` |
| M5 | Vectorless RAG fundamentals | BM25 over a document tree + query expansion |
| M6 | LangGraph state machines and memory | State, reducers, checkpointer (short-term) vs store (long-term) |
| M7 | Human-in-the-loop | `interrupt()` + `Command(resume)`: no write without consent |
| M8 | Safety engineering | Rules before models; output guard; groundedness checks |
| M9 | RAG security layers | Validate → access-control-before-retrieval → fixed templates → masked logs |
| M10 | Small-model prompting, latency and caching | Short constant prompts, a fast classifier model, semantic cache |
| M11 | FastAPI, SSE and secure sessions | Real streaming stages; signed session ids; HttpOnly cookie + CSRF |
| M12 | Clinical UI engineering | Three-pane workstation, evidence panel, safe Markdown, accessibility |
| M13 | Evals, golden sets and CI gates | Deterministic metrics, ablations, regression with DB assertions |
| M14 | Observability and portable ops | Traces vs audit; PID-tree process control; zip/Docker |
| **M15** | **Advanced RAG and privacy-preserving memory** | RRF, re-ranking, CRAG, small-to-big, attribution, ablation, erasure |

---

## M1. Project setup, config and secrets
- **Where:** `pyproject.toml`, `uv.lock`, `.python-version`, `careharbor/config.py`, `.env.example`, `.gitignore`, `setup.ps1`, `scripts/make_portable_zip.ps1`
- **YouTube:** "uv python package manager tutorial", "12 factor app config", "pydantic-settings tutorial"
- **Handwritten note:**
  - venv → lockfile → `uv sync`;
  - one setting's path: `.env` → `Settings` → code;
  - why `UV_LINK_MODE=copy` matters on OneDrive.
- **Self-check:**
  - What does `uv.lock` guarantee?
  - Why does `setup.ps1` generate `SESSION_SECRET` instead of shipping one?
  - How does the zip script make sure `.env` never leaves the laptop?
- **Interview:** "How do you make a Python project reproducible, and how do you handle secrets in demo vs production?"

## M2. Realistic data without privacy risk
- **Where:** `scripts/fetch_sources.py`, `careharbor/synthea.py`, `scripts/seed.py`, `sources/README.md`, `data/`
- **YouTube:** "Synthea synthetic patient generator", "synthetic data in healthcare AI", "MIMIC dataset access"
- **Handwritten note:**
  - kept: age, allergies, conditions, medicines;
  - dropped: identifiers;
  - why dates are re-anchored to today;
  - provenance on every row.
- **Self-check:**
  - Why not use MIMIC? (A data-use agreement plus credentialing.)
  - What does `SIG_TABLE` add?
  - How is a penicillin allergy respected?
- **Interview:** "How did you get realistic data without privacy risk?"

## M3. SQLite transactions and constraints
- **Where:** `careharbor/db.py` (partial unique index), `tools/service.py` (`book_appointment`, `BEGIN IMMEDIATE`, idempotency key)
- **YouTube:** "SQLite transactions BEGIN IMMEDIATE", "partial unique index", "idempotency keys API"
- **Handwritten note:** the double-booking race and how the index plus the transaction stop it.
- **Self-check:**
  - What happens if two people book the same slot in the same millisecond? (`test_concurrent_booking_race_has_one_winner`)
  - Why re-read the row before replying?
- **Interview:** "How do you guarantee idempotent, race-free writes from an agent?"

## M4. MCP tools and identity isolation
- **Where:** `tools/mcp_server.py`, `tools/client.py`, `tools/service.py` (`verify_identity`, `confirm_otp`, `session_patient`), `graph.py` (`identity`)
- **YouTube:** "Model Context Protocol explained", "FastMCP tutorial", "account enumeration attack", "OTP authentication flow"
- **Handwritten note:**
  - client ↔ server over stdio, plus the tool schema;
  - `_meta` carries the session id;
  - identity state machine: details → otp → verified, with lockout.
- **Self-check:**
  - Why does no tool take `patient_id`?
  - Why is "not found" identical for other patients' records?
  - What are per-agent allow-lists?
- **Interview:** "How do you stop an LLM from reading another user's data through tools?"

## M5. Vectorless RAG fundamentals
- **Where:** `rag/index.py` (tree, field boosts, `bm25(expand=...)`), `rag/text.py` (stemmer, synonyms, Hinglish), `rag/navigator.py`, `scripts/rag_smoke.py`
- **YouTube:** "BM25 explained", "vectorless RAG PageIndex", "keyword vs semantic search", "query expansion information retrieval"
- **Handwritten note:**
  - BM25 = IDF × saturated term frequency, with length normalisation;
  - tree = document → sections;
  - field boosts: title × 3, tags × 2.
- **Self-check:**
  - When is BM25 better than embeddings? (Small curated corpus, exact terms, explainability, CPU.)
  - Why is LLM table-of-contents navigation off by default? (Measured: slower and less accurate on 3B.)
- **Interview:** "Why didn't you use a vector database?" Answer with the ablation: raw BM25 Hit@1 78% → 100% with expansion.

## M6. LangGraph state machines and memory
- **Where:** `agent/graph.py` (`ChatState`, reducers, `build()`), `agent/runtime.py` (checkpointer, store, `astream`)
- **YouTube:** "LangGraph tutorial state graph", "LangGraph checkpointer memory", "LangGraph store long term memory"
- **Handwritten note:**
  - the graph diagram from `docs/ARCHITECTURE.md`;
  - the reset reducer (the `["__reset__"]` sentinel);
  - checkpointer = per-thread state, store = cross-thread memory.
- **Self-check:**
  - What does `durability="exit"` do, and why did it cut emergency latency?
  - How does `astream(stream_mode="updates")` power the live stages?
- **Interview:** "How does your agent remember a conversation, and a patient across conversations?"

## M7. Human-in-the-loop
- **Where:** `graph.py` (`confirm`, `commit`, `change`, `abort`), `runtime.py` (`resume`, `decision_from_text`), `frontend/src/Cards.jsx`
- **YouTube:** "LangGraph interrupt human in the loop", "Command resume LangGraph"
- **Handwritten note:** slot → `interrupt(card)` → UI card → Confirm → `Command(resume)` → tool → re-read → template reply.
- **Self-check:**
  - Why must nodes be side-effect-free before `interrupt()`? (The node re-runs on resume.)
  - What happens if the user types something unrelated while a card is pending?
- **Interview:** "How do you ensure an agent never books without consent?"

## M8. Safety engineering
- **Where:** `careharbor/guardrails.py`, `agent/triage.py`, `tests/test_redteam.py`
- **YouTube:** "LLM guardrails tutorial", "clinical triage red flags", "hallucination detection groundedness"
- **Handwritten note:** the layered order: rules → LLM safety classifier → output guard (diagnosis, dosing, new medicine) → numbers and claims grounded → extractive fallback.
- **Self-check:**
  - Why can't a simple negation window be used? ("I don't know, my chest hurts.")
  - How does the claim-support check work?
- **Interview:** "How did you reach 100% recall on emergencies, and how do you prove it?"

## M9. RAG security layers
- **Where:** `careharbor/security.py`, `rag/index.py` (filter before scoring), `docs/SECURITY.md`
- **YouTube:** "prompt injection attacks explained", "indirect prompt injection", "RAG access control", "OWASP top 10 for LLM"
- **Handwritten note:** the four layers, one line each, with an attack each one stops; the tenant canary document.
- **Self-check:**
  - Why must access control happen *before* retrieval?
  - Why `[[slot]]` replacement instead of `str.format`?
  - What must never appear in logs?
- **Interview:** "A document in your corpus says 'ignore previous instructions'. What happens?"

## M10. Small-model prompting, latency and caching
- **Where:** `security.TEMPLATES`, `llm.py` (`warm_up`, keep-alive, fast model), `careharbor/cache.py`, `tools/service.py` (`_TTLCache`), `evals/reports/timing_report.md`
- **YouTube:** "Ollama OpenAI compatible API", "KV cache prefix caching", "semantic cache LLM"
- **Handwritten note:**
  - constant system prompt + short context gives a faster time-to-first-token on CPU;
  - cache key = terms + document version + model;
  - PHI is never cached.
- **Self-check:**
  - Why use the 1.5B model for JSON classification but the 3B for answers? (Measured: 1.5B was *slower* overall for guidance, at 11 s vs 5.9 s.)
  - How is the cache invalidated when a policy document changes?
- **Interview:** "How did you get acceptable latency on a laptop CPU?"

## M11. FastAPI, SSE and secure sessions
- **Where:** `careharbor/api.py`, `security.issue_session_id/valid_session_id`, `tests/test_api.py`
- **YouTube:** "FastAPI tutorial", "Server-Sent Events FastAPI", "HMAC signed tokens", "HttpOnly cookie vs localStorage", "CSRF explained"
- **Handwritten note:**
  - the `/api/chat/stream` event sequence: start → stage* → result;
  - the signed session format `id.hmac`;
  - cookie flags: HttpOnly, SameSite=Strict;
  - the CSRF header on writes.
- **Self-check:**
  - Why are client-chosen session ids dangerous? (Finding G1: session fixation/spoofing.)
  - Why does a custom header defeat CSRF?
  - Why is `/mcp` behind staff auth?
- **Interview:** "Walk me through a security bug you found in your own system and how you fixed it." (G1 is your best story.)

## M12. Clinical UI engineering
- **Where:** `frontend/src/App.jsx`, `Chat.jsx` (Workspace, EvidencePanel, Composer), `Cards.jsx`, `Markdown.jsx`, `Staff.jsx`, `styles.css`
- **YouTube:** "CSS custom properties theming", "accessible color contrast WCAG", "Web Speech API", "React state management basics"
- **Handwritten note:**
  - three panes: visit desk | conversation | evidence & safety;
  - design tokens: navy, teal, status colours;
  - why the Markdown renderer builds React elements (no `innerHTML`).
- **Self-check:**
  - Why no gradients or decorative animation in a clinical UI?
  - How is `prefers-reduced-motion` respected?
  - What does the evidence panel show and where does that data come from?
- **Interview:** "How do you make an AI answer *auditable* for the user?"

## M13. Evals, golden sets and CI gates
- **Where:** `careharbor/evals/suites.py`, `evals/datasets/`, `evals/golden/`, `scripts/run_evals.py`, `scripts/verify_phases.py`, `.github/workflows/evals.yml`
- **YouTube:** "LLM evaluation deterministic", "RAGAS faithfulness context precision", "golden dataset regression testing", "Hit@k MRR retrieval metrics"
- **Handwritten note:**
  - the 10 suites, each with its metric and target;
  - golden conversations assert DB state;
  - CI uses MockLLM.
- **Self-check:**
  - What does "disagreements err on the side of caution" mean?
  - Why are the eval sets "development sets"? (Self-authored, so there's overfitting risk.)
- **Interview:** "How do you know your agent is still safe after you change a prompt?"

## M14. Observability and portable ops
- **Where:** `runtime._trace`, `db.audit`, `security.log_retrieval`, staff console (Traces, Audit), `setup.ps1`, `run.ps1`, `Dockerfile`
- **YouTube:** "LLM observability tracing", "audit logging best practices", "PowerShell scripting basics", "Hugging Face Spaces Docker"
- **Handwritten note:**
  - trace fields: stages, ms, tokens, tools;
  - audit fields: who, what, decision;
  - why processes are killed by PID tree, never by name.
- **Self-check:**
  - Which personal data is masked, and where? (Logs, audit, *and* checkpoints since the final pass.)
  - Why re-seed at container start? (It doesn't any more: `seed.py --if-missing` keeps chats and bookings, and `ensure_future_slots` keeps the calendar current.)
- **Interview:** "A patient complains about a wrong answer. How do you investigate?"

## M15. Advanced RAG and privacy-preserving memory (final review pass)
- **Where:** `careharbor/rag/advanced.py`, `rag/navigator.py` (`shortlist_for`), `graph.py` (`faq`, `_remember`, `_recall`, `forget`), `security.memory_key`, `evals` retrieval suite, `docs/ADVANCED_RAG_CONCEPTS.md`
- **YouTube:** "reciprocal rank fusion", "multi query retriever", "corrective RAG CRAG", "parent document retriever small to big", "RAG citation attribution", "conversational query rewriting", "ablation study machine learning", "right to be forgotten GDPR DPDP"

### The concepts, one line each
| Concept | What CareHarbor AI does | File |
|---|---|---|
| Conversational query rewriting | A follow-up ("and on Sunday?") is rewritten with the previous question | `advanced.contextualize` |
| Multi-query retrieval | Original + expanded + key-term variants, each searched | `advanced.query_variants` |
| Reciprocal Rank Fusion | Merges ranked lists: score = Σ 1/(k + rank), k = 60 | `advanced.rrf` |
| Re-ranking | Feature re-ranker: coverage, title/heading hits, clinic-source preference (cross-encoder is the upgrade) | `advanced.rerank` |
| CRAG (corrective RAG) | Grade retrieval high/ambiguous/low; low triggers a broader retry, and if still low the bot says "not in our documents" | `advanced.grade`, `graph.faq` |
| Small-to-big (parent-child) | Match on small sections, give the model the parent context | `advanced.parent_context` |
| Attribution / evidence map | Each answer sentence is linked to its supporting source line | `advanced.attribute` |
| Ablation study | Raw BM25 → + expansion → + advanced → full; Hit@1, Hit@3, MRR | retrieval eval suite |
| Privacy-preserving memory | Long-term store keyed by `HMAC(secret, patient_id)`; the agent never sees the id | `security.memory_key` |
| Right to erasure | "Forget my preferences" deletes the memory namespace | `graph.forget` |

- **Handwritten note:**
  - draw the advanced pipeline: rewrite → variants → BM25 × n (ACL-filtered) → RRF → re-rank → grade → [retry] → small-to-big → LLM → groundedness → attribution;
  - then write the ablation table from the eval report.
- **Self-check:**
  1. Why does RRF use ranks, not scores? (BM25 scores aren't comparable across queries.)
  2. Why did "advanced" first score *below* the baseline, and what fixed it? (The re-ranker lost the clinic-source preference; finding G10.)
  3. Why use an HMAC rather than a plain hash for the memory key? (Without the secret, nobody can link memories to patients.)
  4. What's the difference between CRAG and Self-RAG?
- **Interview:**
  - "Which advanced RAG techniques did you use, and how did you prove they helped?"
  - "How does your assistant remember a returning patient without storing who they are?"

### Persistent chat history (added after owner testing: finding G21)
- **Where:** `careharbor/history.py`, `runtime._package` (saves every turn), `api.py` (`/api/chat/history`, `/conversations`, `/linked`, `/reopen`, `/signout`, `/delete`), `Chat.jsx` (`openConversation`, `fromHistory`, `SessionOverview`)
- **Concepts:**
  - **agent state vs user transcript vs long-term memory** (three stores, three purposes);
  - **bearer-token access** to a conversation (the signed session id);
  - **soft sign-out** (`ended=1` hides transcripts until re-verification);
  - **cross-device linking** through the HMAC memory key;
  - **data minimisation** (masked user text, OTPs never stored).
- **Self-check:**
  1. Why is the LangGraph checkpoint *not* used as the chat history? (It holds agent internals, is keyed only by thread, and is reset or overwritten per turn.)
  2. After sign-out, why can't someone on the same browser read the old chats? (The conversations are marked ended and the server returns no messages until a verified session with the same memory key re-opens them.)
  3. What happens to saved chats if `SESSION_SECRET` changes? (All signed ids become invalid, so the chats are unreachable. Keep the secret stable in production.)
- **Interview:** "A user said their chat vanished after refreshing. How did you find the cause and fix it, and what did you deliberately *not* store?"

### Second hard-review pass: concepts worth knowing cold
| Concept | Where | The one-line answer |
|---|---|---|
| Brute force across sessions | `service._phone_locked`, `identity_throttle` | A lockout keyed by the *session* resets when the attacker opens a new session, so key it by the *target* (phone, as a keyed HMAC). |
| Trusted proxy headers | `Dockerfile` `FORWARDED_ALLOW_IPS`, `docker-compose.yml` | `X-Forwarded-For` is client-controlled unless a proxy you trust overwrites it, so never trust `*` without one. |
| Session TTL vs retention | `db.verified_cutoff`, `history.purge_older_than` | TTL limits *who can still use* a verified session (shared kiosk); retention limits *how long data exists* (minimisation). |
| End-to-end security eval | `evals.suite_security`, `evals/datasets/security.jsonl` | Unit tests prove each layer. The suite proves the *assembled* agent leaks nothing, including in its own logs. |
| Polarity errors | `graph._polarity_conflict` | "Fact present in the answer" can still be wrong if the model says "No" to a "yes" fact. Check verdict vs source, or use an NLI/LLM judge. |
| Multi-part CRAG grading | `advanced.grade` | Grade the evidence the model will actually see (top-3 jointly), not only the top section. |

- **Self-check:**
  1. Why does the throttle store an HMAC of the phone, not the phone?
  2. What did the security suite catch on its first run? (A demo patient's real DOB used as the format example in a prompt.)
  3. Why was the 95% → 100% FAQ change a *retrieval-grading* fix and not a prompt fix?

---

## One-page interview pitch (practise until fluent)

"CareHarbor AI is an AI front desk for a primary-care clinic: symptom guidance, booking, prescription questions, FAQ and human handoff. I built it from what I learned shipping conversational agents at work, applied to a stricter domain.

- **Safety first.** Deterministic red-flag rules run before any model, at 100% recall on 30 phrasings including Hinglish, with an LLM classifier as a second layer.
- **Human in the loop.** Writes go through LangGraph `interrupt()`, so nothing is booked without a click. Replies are templated from the re-read database row.
- **Identity isolation.** Sessions are server-issued and HMAC-signed. Tools get the identity out-of-band through MCP `_meta`, never as an argument.
- **Retrieval.** Vectorless BM25 over a document tree with multi-query fusion, re-ranking, a corrective gate, small-to-big context and sentence-level attribution. I measured each step in an ablation: raw BM25 Hit@1 78% → 100%.
- **Proof.** Nine deterministic eval suites plus a golden regression set run as a CI gate, all on a laptop CPU with free local models."
