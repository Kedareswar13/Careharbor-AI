# CareHarbor AI: an AI front desk for a primary-care clinic

> **CareHarbor AI — your clinic's first point of contact for patient questions, appointment workflows, prescription queries, and safe escalation to staff.**

**CareHarbor AI** answers patients the way a careful clinic receptionist would.
- It checks every message for emergency warning signs **before** any AI model runs.
- It gives symptom guidance only from the clinic's own care leaflets, with citations.
- It books, reschedules and cancels appointments, but **writes nothing until the patient presses Confirm**.
- It explains a patient's prescription **exactly as the doctor recorded it**, and only after identity verification.
- It refuses to diagnose, dose or answer out-of-scope questions.

It runs **fully free and local** on a laptop CPU: Ollama with `qwen2.5:3b` and SQLite.

> **Demo, not medical advice.** All patient data is synthetic. In an emergency call **112 / 108**.

---

## Features

| Journey | What happens |
|---|---|
| **Symptom guidance (triage-lite)** | Red-flag rules (English, Hinglish, typos) come first: any hit goes to an emergency card and a staff alert in under 1 s. Otherwise CareHarbor AI asks 1–3 safe follow-up questions, gives leaflet-grounded self-care advice and "when to see a doctor", sets a disposition (self-care / book / see a doctor today), and offers to book. |
| **Appointments** | Phone + date of birth + one-time code, then up to 3 slots, then a **confirmation card** (a LangGraph `interrupt()`). Booking happens only after Confirm, and the reply is built from the re-read database row. Reschedule and cancel work the same way. |
| **Prescriptions** | After verification, the patient's own record is read back verbatim (dose, times, food, duration). "Can I stop / double / take something else?" goes to the pharmacist queue instead. |
| **Clinic FAQ** | Vectorless RAG over clinic policies, with citations. Answers that aren't in the documents are refused. |
| **Human handoff** | A 3-line ticket in the staff queue. |
| **Staff console** | Handoff queue, today's appointments, audit log, per-turn traces, evals with a **Run evals** button, a **Security** page (the four layers with live audit evidence and deployment checks), and staff-only SOP search. |
| **Saved conversations** | Every conversation is saved on the server (PII-masked, no one-time codes). It survives a page refresh or a server restart. After signing out, verifying again brings back your earlier conversations, even from another device. |
| **Long-term memory** | Remembers a verified patient's preferred doctor ("Welcome back...") under an HMAC memory key, never the patient id. "Forget my preferences" erases it (right to erasure). |

The UI is a **light clinical workstation** (IBM Plex type, navy and teal, optional dark theme) with three panes:
- **Visit desk (left):** verification status and sign out, six task buttons, saved and earlier conversations, transcript download, erase preferences.
- **Conversation (centre):** answers tagged by grounding type (sources, clinic records, safety guard, emergency). Each answer has Copy, Read aloud, Evidence, Helpful / Not helpful / Report buttons, and a live processing card showing the **real** pipeline stages, streamed over Server-Sent Events. The composer has dictation and a model picker.
- **Evidence & safety (right, collapsible):**
  - for the selected answer: safety checks, sources, a sentence→source evidence map and stage timings;
  - with no answer selected: an overview of the conversation, answer quality so far, the active safeguards and the knowledge base.

## Architecture

```
React UI ──SSE──► FastAPI (8010) ──► LangGraph supervisor
                                      safety gate (rules) → router (query guard, rules, LLM fallback)
                                      ├ triage ─ leaflet RAG + LLM safety layer + grounded guidance
                                      ├ booking ─ interrupt() confirmation card → commit → DB re-read
                                      ├ prescription ─ verified record read-back / pharmacist escalation
                                      ├ faq ─ vectorless RAG + semantic cache
                                      └ handoff · identity · out-of-scope
                                      → output guard (no diagnosis/dosing, grounded numbers & claims)
                         MCP tools (FastMCP, stdio) ──► SQLite  (identity bound to the verified session)
```

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · Security: [docs/SECURITY.md](docs/SECURITY.md)

**Stack:** Python 3.11 (uv), FastAPI, LangGraph, FastMCP, langchain-openai (any OpenAI-compatible endpoint), SQLite, React + Vite, Ollama.

### Data
- **Patient histories** come from the open [Synthea](https://synthetichealth.github.io/synthea-sample-data/) dataset: realistic synthetic EHRs (ages, allergies, conditions, outpatient medicines). Identifiers are stripped and records are localised for a fictional Bengaluru clinic.
- **Demo personas and a QA test account** are fixed records used for the journeys and the regression set.
- **Care content:** 15 original clinic care leaflets, 8 clinic policies, and 23 public-domain [MedlinePlus](https://medlineplus.gov/) reference topics (cited with links).
- Sources and licences: [sources/README.md](sources/README.md).

### Vectorless RAG
- No embeddings and no vector database. Documents become a section tree, ranked with BM25 plus synonym, Hinglish and UK/US spelling expansion.
- For symptoms, the tree itself picks the leaflet's Self-care, See a doctor if and Emergency sections.
- **Advanced pipeline** (`RAG_PIPELINE=advanced`): each step is measured in a retrieval ablation (raw BM25 Hit@1 78% → 100%).
  - conversational query rewrite;
  - multi-query;
  - reciprocal rank fusion;
  - feature re-ranking;
  - a CRAG-style confidence grade with a corrective retry;
  - small-to-big context;
  - sentence-level attribution.
- See [docs/ADVANCED_RAG_CONCEPTS.md](docs/ADVANCED_RAG_CONCEPTS.md).

### Security (four layers around RAG)
1. **Query validation** before the pipeline (injection, smuggling, exfiltration, scope).
2. **Role and tenant access control** before retrieval.
3. **Fixed prompt templates** with neutralised slots.
4. **PII-masked observability** that logs document ids only.

**Identity and sessions:**
- Server-issued, HMAC-signed session ids.
- OTPs are stored as keyed hashes.
- A brute-force lockout per phone number that applies across sessions.
- Verified sessions expire after 2 hours (shared devices). Saved chats are deleted after 30 days.
- The staff cookie is HttpOnly and SameSite=Strict, and cookie-authenticated writes need a CSRF header.

Details are in [docs/SECURITY.md](docs/SECURITY.md).

## Results (measured on this laptop, CPU only)

**Eval suites, `qwen2.5:3b`:** `uv run python scripts/run_evals.py --golden`. Reports are in `evals/reports/`.

| Suite | Cases | Result | Target |
|---|---|---|---|
| Emergency red flags (incl. Hinglish/typos) | 30 | **100% recall** | 100% |
| No diagnosis / no dosing (adversarial) | 25 | **0 violations** | 0 |
| Booking correctness (multi-turn, DB-checked) | 20 | **100%** | 100% |
| Prescription fidelity (numbers/times vs record) | 20 | **100%** | 100% |
| Identity gate (no verification) | 10 | **0 PHI disclosures** | 0 |
| Prompt injection (incl. inside documents) | 15 | **0 tool misuse** | 0 |
| Triage disposition (labelled stories) | 30 | **100% agreement**, 0 unsafe | ≥ 90% |
| FAQ grounding (incl. refusals) | 20 | **100%** (faithfulness 100%, context precision 100%) | ≥ 90% |
| Security (4 layers): 20 attacks × anonymous + verified, 10 cross-role/tenant probes, raw-PII scan of every log, trace, checkpoint and saved chat | 51 | **0 leaks** | 0 |
| Retrieval ablation (Hit@1, 40 labelled queries) | 40 | raw BM25 78% → **100%** with expansion + advanced pipeline | ≥ 90% |
| Golden regression (QA account, multi-turn) | 16 | **16/16** | all |

**Latency:** [evals/reports/timing_report.md](evals/reports/timing_report.md).

| Path | p50 |
|---|---|
| Emergency | 0.05 s |
| FAQ answer | 3.1 s |
| FAQ cache hit | 0.19 s |
| Triage follow-up questions | 0.7 s |
| Triage guidance | 5.9 s |
| Booking and prescription turns | < 0.1 s |

All paths meet the targets.

## Run it

```powershell
.\setup.ps1          # one time: Python env, UI deps, .env, data, index, model check (keeps existing chats; -Reseed resets)
.\run.ps1            # UI http://127.0.0.1:5174 · API http://127.0.0.1:8010
.\run.ps1 -Stop      # stops only CareHarbor AI's own processes
```

Demo identities (synthetic):

| Persona | Phone | Date of birth |
|---|---|---|
| Aarav (has a past visit with Dr. Rao and an antibiotic) | `9876500001` | `14/05/1990` |
| Ananya (has an upcoming appointment) | `9876500002` | `02/11/1985` |
| QA test account | `9000000001` | `01/01/2000` |

In demo mode the one-time code is shown on screen.

```powershell
uv run pytest -q                              # 221 unit, red-team, API, history and security tests
uv run python scripts/verify_phases.py        # per-phase health check; failures point at the phase
uv run python scripts/run_evals.py --mock     # 10 eval suites (incl. retrieval ablation) with the deterministic MockLLM
uv run python scripts/run_evals.py --golden   # the same on the live model + golden regression
uv run python scripts/perf_report.py          # latency report
uv run python scripts/chat_cli.py --demo      # every journey in the terminal
```

- New laptop and GitHub: [docs/PERSONAL_LAPTOP_SETUP.md](docs/PERSONAL_LAPTOP_SETUP.md)
- Public deployment (Oracle Always Free VM + Docker Compose + Caddy HTTPS, or Hugging Face Spaces): [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)
- Learn every concept: [docs/LEARNING_ROADMAP.md](docs/LEARNING_ROADMAP.md) · Explain the project: [docs/PROJECT_EXPLAINED.md](docs/PROJECT_EXPLAINED.md) · How it was built, step by step: [docs/IMPLEMENTATION_GUIDE.md](docs/IMPLEMENTATION_GUIDE.md)
- Honest review and gaps: [docs/PROJECT_REVIEW.md](docs/PROJECT_REVIEW.md) · [docs/GAP_ANALYSIS.md](docs/GAP_ANALYSIS.md)

## Project layout

```
careharbor/           Python package: agent graph, tools (MCP), RAG, security, guardrails, API, evals
frontend/            React + Vite UI
content/             leaflets, policies, MedlinePlus topics, staff SOPs, other-tenant docs
sources/             downloaded open datasets (Synthea zip, MedlinePlus raw XML) + licences
evals/               datasets, golden regression set, reports
scripts/             seed, fetch, evals, perf, CLI, phase verification, portable zip
tests/               pytest suites (seed, tools, RAG, graph, red team, API, cache)
docs/                architecture, security, deployment, setup, learning roadmap
```

## Roadmap
- Postgres (Neon) backend so several API replicas can share bookings and chat history (SQLite is single-node).
- Multilingual replies (Hindi, Kannada) with the same deterministic safety layer.
- Real SMS OTP delivery behind a feature flag.
