# CareHarbor AI architecture

> **CareHarbor AI — your clinic's first point of contact for patient questions, appointment workflows, prescription queries, and safe escalation to staff.**

## Request path

```
Browser (React, port 5174)                  Telegram (optional)
  │  POST /api/chat/stream  (Server-Sent Events: real stage events, then the final result)
  ▼
FastAPI gateway (port 8010) ── rate limit · input validation · staff auth (HMAC tokens) · /mcp debug mount
  │
  ▼
ChatService (careharbor/agent/runtime.py)
  │  thread = chat session id · SQLite checkpointer (conversation state) · SQLite store (long-term memory)
  │  paused on a confirmation card? typed "yes/no/another time" → Command(resume=...)
  ▼
LangGraph (careharbor/agent/graph.py)
  START → safety_gate ──red flag──► emergency ──────────────────────────────► finalize → END
            │
            ▼
          router  (Layer 1 query validation → rules first → LLM fallback with fast model)
            ├─► identity ──verified──► (resume) booking | prescription
            ├─► triage ──(LLM safety 2nd layer)──► follow-up questions → guidance (LLM, grounded) → offer booking
            ├─► booking ──slot chosen──► confirm [interrupt() → UI card] ──► commit | change | abort
            ├─► prescription (record read-back, template) | pharmacist escalation
            ├─► faq (vectorless RAG + LLM, semantic cache)
            ├─► handoff · smalltalk · blocked · out_of_scope
            ▼
          output_guard (no diagnosis / dosing / new medicines; numbers + claims grounded) → finalize → END
  │
  ▼  ToolClient (per-agent allow-lists; session id in MCP _meta)
FastMCP server (careharbor/tools/mcp_server.py, stdio subprocess) → ClinicTools (service.py) → SQLite
```

## Components

| Layer | Files | Notes |
|---|---|---|
| Config and logging | `careharbor/config.py`, `logging_setup.py` | `.env`-driven settings; UTF-8 console; PII masking filter |
| Data | `scripts/seed.py`, `careharbor/synthea.py`, `careharbor/db.py` | Synthea-derived histories + clinic schedule; a partial unique index blocks double-booking |
| Tools | `careharbor/tools/service.py`, `mcp_server.py`, `client.py` | 12 MCP tools; identity bound to the verified session; idempotent writes; audit log |
| Retrieval | `careharbor/rag/index.py`, `navigator.py`, `text.py` | Vectorless: document tree + BM25 + Hinglish/synonym expansion; optional LLM table-of-contents navigation |
| Security | `careharbor/security.py`, `guardrails.py` | 4 RAG security layers; red flags; output guard; see [SECURITY.md](SECURITY.md) |
| Agents | `careharbor/agent/graph.py`, `triage.py`, `parsing.py`, `runtime.py` | Supervisor graph, HITL interrupt, deterministic parsers, streaming |
| LLM | `careharbor/llm.py` | OpenAI-compatible client (Ollama / Groq / Gemini), MockLLM, warm-up and keep-alive |
| Caching | `careharbor/cache.py`, `tools/service.py` | FAQ-only semantic answer cache (invalidated by the document version); 30 s slot/doctor cache |
| API | `careharbor/api.py`, `channels/telegram.py` | Patient + staff endpoints, SSE streaming, chat-history endpoints, built UI served in production |
| Chat history | `careharbor/history.py` | Saved conversations (PII-masked, no OTPs); refresh / restart / re-login restore; earlier conversations linked by the HMAC memory key; sign-out and delete |
| UI | `frontend/src/*` | Three-pane clinical workstation: visit desk with saved conversations, conversation with live stages and cards, evidence & safety panel / conversation overview; staff console |
| Evals | `careharbor/evals/*`, `evals/datasets`, `evals/golden`, `scripts/run_evals.py`, `scripts/verify_phases.py` | 10 suites (incl. retrieval ablation) + golden regression, CI gate |

## Design decisions

1. **Deterministic where correctness matters, LLM where language matters.**
   - Red flags, routing rules, identity, bookings and prescription read-back involve no LLM, so they are fast, testable and can't hallucinate.
   - The LLM writes triage guidance and FAQ answers from retrieved text, then the output guard checks them.
2. **Identity is bound to the session, not to arguments.** The model never sees or supplies a patient id. Tools resolve the verified patient from the session id passed in MCP `_meta`.
3. **Confirm only what the database says.** Writes happen only after the `interrupt()` confirmation. Tools re-read the stored row, and replies are templated from that row.
4. **Vectorless RAG.**
   - The clinic corpus is small and well structured, so BM25 over a section tree beats embeddings on latency (under 1 ms) and is explainable.
   - Hinglish and spelling synonyms close the vocabulary gap.
   - LLM navigation of the table of contents exists (`RAG_MODE=tree`), but measurements showed it was slower and no more accurate on this CPU.
5. **Performance on a laptop CPU.** Short constant prompts, compressed context, a small fast model for JSON classification, keep-alive, and an FAQ-only semantic cache. See `evals/reports/timing_report.md`.

## Data model (SQLite)

- **Patient and clinic data:** `patients`, `doctors`, `slots` (14 days ahead), `appointments` (partial unique index on live slot), `prescriptions`.
- **Sessions and staff workflow:** `identity_sessions` (OTP state per chat session), `handoffs` (staff queue: handoff / emergency / pharmacist).
- **Observability:** `audit_log` (tools, retrievals, guardrails, staff actions; PII masked), `feedback`.
- **Chat history:** `conversations` (title, opaque `patient_key`, `ended` after sign-out) and `chat_messages` (masked user text; the assistant reply plus a JSON payload of cards, citations, evidence and stages, so the evidence panel works for old messages too).
- **LangGraph files:** `data/checkpoints.db` (conversation checkpoints) and `data/checkpoints-memory.db` (long-term store).
- **Why three stores?**
  - The checkpoint is the *agent's* working state: the graph needs it to resume an `interrupt()`.
  - The chat history is the *user's* readable transcript.
  - The memory store holds *cross-conversation* preferences.
  - Mixing them would either leak agent internals to the UI or lose the transcript whenever a thread is reset.
- **Production database:** the app uses SQLite. In Docker, all of it lives on the `/data` volume, so it survives redeploys. Moving to Neon Postgres (for several replicas) is a documented path that isn't wired in yet; see [DEPLOYMENT.md](DEPLOYMENT.md).
