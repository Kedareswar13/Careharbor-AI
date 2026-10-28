# Implementation guide: how CareHarbor AI was built, step by step

> **CareHarbor AI — your clinic's first point of contact for patient questions, appointment workflows, prescription queries, and safe escalation to staff.**

Use this guide to rebuild the project, or to explain *how* you built it. Each phase lists the goal, the files, the key decisions, and the command that proves it works. The order is the order to build in.

## P0: Foundation and data
- **Goal:** a reproducible project with realistic, safe data.
- **Steps:**
  1. `uv venv --python 3.11; uv sync` with the dependencies in `pyproject.toml`; `UV_LINK_MODE=copy` (OneDrive-safe).
  2. `careharbor/config.py`: pydantic-settings reads `.env`. `logging_setup.py`: UTF-8 console + PII redaction filter.
  3. `careharbor/db.py`: the schema. Key trick: `CREATE UNIQUE INDEX ... ON appointments(slot_id) WHERE status='booked'`, so the database itself prevents double-booking.
  4. `scripts/fetch_sources.py`: downloads the Synthea sample zip and MedlinePlus topics (with retries).
  5. `careharbor/synthea.py` + `scripts/seed.py`:
     - keep age, sex, allergies, conditions and outpatient medicines;
     - drop identifiers;
     - map each medicine to a dosing schedule (`SIG_TABLE`);
     - skip penicillins for allergic patients;
     - re-anchor dates to "today";
     - fixed seed for determinism.
  6. Write the clinic leaflets and policies (Markdown with front matter: `id`, `title`, `tags`, `audience`, `tenant`).
- **Proof:** `uv run python scripts/seed.py` prints the row counts; `uv run pytest tests/test_seed.py`.

## P1: MCP tools
- **Goal:** every clinic action is a tool with the business rules enforced server-side.
- **Steps:**
  1. `tools/service.py`: plain-Python `ClinicTools` (12 methods). Writes use `BEGIN IMMEDIATE`, an idempotency key and a re-read of the stored row. Not-found and not-yours give the same error (no enumeration).
  2. Identity: `verify_identity` → OTP (HMAC-hashed at rest) → `confirm_otp` marks the session verified. PHI tools call `session_patient(session_id)`, never a `patient_id` argument.
  3. `tools/mcp_server.py`: FastMCP wraps the service. The session id is read from the request `_meta` (or an HTTP header), so it's not in any tool schema.
  4. `tools/client.py`: `ToolClient` with per-agent allow-lists and one persistent stdio session.
- **Proof:** `uv run pytest tests/test_tools.py` (includes a two-thread double-booking race).

## P2: Vectorless RAG
- **Goal:** fast, explainable retrieval with citations.
- **Steps:**
  1. `rag/index.py`: parse Markdown into a doc → section tree, with field-boosted tokens (title × 3, tags × 2, heading × 2). BM25 per collection (`care`, `faq`, `staff`). The index is persisted as JSON and rebuilt when the content hash changes.
  2. `rag/text.py`: tokeniser, light stemmer, synonym/Hinglish/UK-US expansion.
  3. `rag/navigator.py`: `retrieve()`. For triage, the tree picks the leaflet's Self-care / See a doctor / Emergency sections. Optional LLM table-of-contents navigation.
  4. `rag/advanced.py` (final pass): conversational rewrite, multi-query, RRF, feature re-rank, CRAG grade + corrective retry, small-to-big context, attribution.
- **Proof:** `uv run python scripts/rag_smoke.py` and `uv run python scripts/run_evals.py --mock --suite retrieval` (ablation table).

## P3: Agents (LangGraph)
- **Goal:** a supervisor graph that routes each turn and keeps state.
- **Steps:**
  1. `agent/graph.py`:
     - `ChatState` (TypedDict) with reducers; a per-turn reset reducer for stages, guardrails and usage.
     - Nodes: `safety_gate → router → {identity, triage, booking, prescription, faq, handoff, smalltalk, blocked, out_of_scope, forget} → output_guard → finalize`.
     - Booking goes through `confirm`, which calls `interrupt(card)`, then `commit | change | abort`.
  2. `agent/parsing.py`: deterministic parsers (phone, DOB, OTP, dates like "Friday" / "kal" / "5 Nov", time of day, option choice, doctor names).
  3. `agent/triage.py`: follow-up questions from the leaflet's emergency signs; a rule-based disposition that errs on caution.
  4. `agent/runtime.py`: `ChatService`:
     - SQLite checkpointer (conversation) + SQLite store (long-term memory);
     - typed "yes / no / another time" mapped onto the paused card;
     - `astream(stream_mode="updates")` for live stages;
     - JSON-lines traces.
- **Proof:** `uv run pytest tests/test_graph.py` and `uv run python scripts/chat_cli.py --demo`.

## P4: Guardrails and security
- **Goal:** safe by construction.
- **Steps:**
  1. `guardrails.py`:
     - red-flag regexes plus typo normalisation and negated-symptom-list stripping;
     - an output guard (diagnosis / dosing / new medicine);
     - numbers-must-be-grounded and unsupported-claim checks.
  2. `security.py`: the four layers.
     - Layer 1: `validate_query`.
     - Layer 2: `Principal` + `can_read`, applied inside `bm25()` before scoring.
     - Layer 3: `PromptTemplate` with neutralised slots.
     - Layer 4: `log_retrieval`, ids only.
     - Also: signed session ids, OTP digests and memory keys.
  3. Evals scaffolding: `careharbor/evals/golden.py` + `evals/golden/regression.jsonl` with the QA test account; `scripts/verify_phases.py`.
- **Proof:** `uv run pytest tests/test_redteam.py` and `uv run python scripts/verify_phases.py`.

## P5: API and UI
- **Goal:** a usable, transparent product.
- **Steps:**
  1. `careharbor/api.py`:
     - `/api/chat` + `/api/chat/stream` (SSE) + `/api/chat/confirm`;
     - signed sessions; rate limit;
     - staff login sets an HttpOnly SameSite=Strict cookie; CSRF header on writes; `/mcp` requires staff.
  2. `frontend/src`:
     - `App.jsx` (header, notice, routing);
     - `Chat.jsx` (three-pane workspace: visit desk, conversation, evidence & safety panel);
     - `Cards.jsx` (emergency, confirmation, slots, OTP, triage, prescriptions);
     - `Markdown.jsx` (safe renderer, no `innerHTML`);
     - `Staff.jsx` (console);
     - `styles.css` (clinical design tokens).
- **Proof:** `uv run pytest tests/test_api.py`, `.\run.ps1`, then open <http://127.0.0.1:5174>.

## P6: Performance and caching
- **Steps:**
  1. Short constant system prompts; compressed context; `max_tokens` 120–160.
  2. `warm_up()` + keep-alive ping for both models.
  3. A fast model for JSON classifications.
  4. `cache.py`: FAQ semantic cache; a 30 s slot cache in the tools.
- **Proof:** `uv run python scripts/perf_report.py --compare` → `evals/reports/timing_report.md`.

## P7: Evals and CI
- **Steps:** `careharbor/evals/suites.py` holds 10 suites (retrieval ablation, security (4 layers), emergency, no diagnosis/dosing, booking, prescription fidelity, identity gate, injection, triage disposition, FAQ grounding). `scripts/run_evals.py` writes the Markdown + JSON report and exits non-zero on a miss. `.github/workflows/evals.yml` runs tests + evals (MockLLM) + the UI build.
- **Proof:** `uv run python scripts/run_evals.py --golden` (live) and `--mock` (CI).

## P8: Portability
- **Steps:**
  - `setup.ps1`: idempotent, repairs a broken venv, generates `SESSION_SECRET`.
  - `run.ps1`: port check, PID-tree stop.
  - `scripts/make_portable_zip.ps1`: excludes the venv, `node_modules`, `.env` and databases.
  - `Dockerfile` for HF Spaces (re-seeds at start).
- **Proof:** copy the folder somewhere clean, then `.\setup.ps1`, `.\run.ps1`, `.\run.ps1 -Stop`.

## The daily loop while developing
```powershell
uv run pytest -q                              # fast feedback
uv run python scripts/verify_phases.py        # which phase broke?
uv run python scripts/run_evals.py --mock     # safety/accuracy gate (no model needed)
uv run python scripts/run_evals.py --golden   # before a demo: live model
```
