# CareHarbor AI project review: scorecard

> **CareHarbor AI — your clinic's first point of contact for patient questions, appointment workflows, prescription queries, and safe escalation to staff.**

An honest, interviewer-style assessment of the finished project. Scores are out of 10. Each score is explained, and the gaps are listed in [GAP_ANALYSIS.md](GAP_ANALYSIS.md).

## Verdict

**Strong flagship project for an AI-engineer role.** It shows the things interviewers rarely see working together:
- deterministic safety before the LLM;
- human-in-the-loop writes;
- identity-bound tool access over MCP;
- vectorless RAG with measured ablations;
- layered RAG security;
- deterministic evals as a CI gate;
- a real, professional UI.

Everything runs free on a laptop CPU. It's a portfolio system, not a production one; the limits are documented, not hidden.

## Scorecard

| Area | Score | Why |
|---|---|---|
| **Safety design** | 9 | Red-flag rules run before any model (100% on 30 phrasings incl. Hinglish/typos), an LLM second layer, an output guard (no diagnosis/dosing/new medicines), and grounded numbers and claims. −1: the LLM safety layer isn't evaluated on its own (G15). |
| **Agent architecture** | 9 | LangGraph supervisor, per-agent tool allow-lists, `interrupt()` HITL, checkpointer + long-term store, deterministic templates for records, streaming stages. Clear, defensible split between deterministic and LLM work. |
| **Tooling / MCP** | 8.5 | 12 FastMCP tools; identity via `_meta`, never as arguments; idempotent writes; double-booking blocked by the DB; audit log. −1.5: single stdio pipe (G12). |
| **RAG quality** | 8.5 | Vectorless tree + BM25 + expansion, multi-query RRF, re-ranking, CRAG gate, small-to-big, attribution, conversational rewriting, semantic cache. Ablation: 100% Hit@1 on 40 queries. −1.5: development-set risk (G16), no dense/cross-encoder option shipped. |
| **Security** | 9 | Four RAG layers (end-to-end eval suite + live staff Security page), signed sessions that expire, a cross-session brute-force lockout per phone, HttpOnly + CSRF staff cookie, protected `/mcp`, hashed OTPs, PII masking in logs, checkpoints and saved chats, retention purge, and a tenant canary. −1: shared staff credential, no encryption at rest (G18, G39). |
| **Evaluation** | 8.5 | 10 deterministic suites (incl. a retrieval ablation and an end-to-end security suite), golden regression with DB assertions, per-phase verifier, CI with a Docker smoke test, live runs on the real model (all 100% / 0 leaks). −1.5: self-authored sets, and no LLM-judge/NLI second opinion. G40 showed that fact-presence checks miss a wrong yes/no (G16). |
| **Performance** | 8 | Every section 8 target met on CPU, with a measured trade-off report. The first cold call is still 14–22 s. |
| **UX / UI** | 8.5 | Clinical three-pane workstation, detailed action buttons, evidence & safety panel, accessible contrast, reduced-motion support, staff console. −1.5: no mobile-first layout or i18n of the UI text. |
| **Data realism** | 8 | Synthea-derived histories, MedlinePlus references, clinic-authored leaflets and policies, provenance on every row. −2: US-derived clinical patterns (G17). |
| **Engineering / ops** | 8.5 | Typed config, uv lockfile, 221 tests, `setup.ps1` / `run.ps1` / portable zip, a Docker image with a `/data` volume, Docker Compose + Caddy HTTPS for a free VM, CI. Re-running setup or redeploying keeps chats and bookings. −1.5: single-instance storage (G13); the Docker stack is verified in CI, not on the build laptop. |
| **Documentation / interview readiness** | 9.5 | Architecture, security, deployment, laptop guide, learning roadmap (M1–M15), advanced RAG concepts with interview answers, implementation guide, review and gap analysis. |

**Overall: 8.7 / 10.** The deductions are scale and production concerns, not design flaws, and you can discuss every one of them.

## What changed in the final review pass

1. **Security fixes (G1–G6).** The critical one was session spoofing (G1).
2. **Advanced RAG.** Conversational rewriting, multi-query + RRF, feature re-ranking, a CRAG-style grade with corrective retry, small-to-big context, sentence-level attribution, and the retrieval ablation suite.
3. **Privacy-preserving long-term memory.** An opaque HMAC memory key gives "Welcome back, last time you booked with Dr. Rao" without the agent ever knowing the patient id, plus a "forget my preferences" command (DPDP right to erasure).
4. **UI redesign.** A light clinical workstation with an evidence and safety panel, replacing the previous dark chat layout.
5. **Answer quality.** Direct yes/no FAQ answers with a verbatim supporting line; the extractive fallback no longer truncates.
6. **Persistent chat history (G21).** Conversations survive refresh, restart and re-login. Earlier conversations come back after re-verification, even on another device. Stored text is PII-masked and contains no OTPs.
7. **A useful right panel (G22).** Shows a conversation overview, safeguards and knowledge base when no answer is selected, and the evidence for an answer when one is. It can be collapsed.
8. **Deployment.** Docker Compose (CareHarbor AI + Ollama + Caddy HTTPS) for an Oracle Cloud Always Free VM, data kept across redeploys, and production guards on the staff password.

## Second hard-review pass (independent code review + live-model evals)

An independent code review and a full live-model run found 13 more issues (G28–G40). All but one documented limit are fixed and tested:
- **Identity brute force across sessions (G29)** and a **spoofable client IP** in the container (G30): the two most serious.
- **Verified sessions that never expired (G31)**, now 2 hours, with record-bearing chats hidden after expiry.
- **Dates of birth that escaped masking (G32).**
- **A CRAG gate that refused a multi-part question its documents answer (G28).**
- **A wrong yes/no from the 3B model that the evals didn't catch (G40).**
- **The missing section 9 security suite and the staff Security page (G36).** The new suite found its own first bug.

After the fixes:
- 221 tests pass.
- All 10 suites pass on `qwen2.5:3b`: FAQ faithfulness 100%, security 0 leaks in 51 probes.
- The golden set passes 16/16.

The two best interview stories from this pass are G29 (a lockout that a new session silently reset) and G40 (why fact-presence evals are not enough).

## Is this enough for the interview?

Yes, if you can explain the *why* behind each decision. Prepare these five stories (scripts in [PROJECT_EXPLAINED.md](PROJECT_EXPLAINED.md)):
1. Safety before the LLM (red flags, the HITL interrupt, "confirm only what the DB says").
2. Identity bound to the session (MCP `_meta`, signed sessions; the G1 bug you found and fixed).
3. Vectorless RAG with an ablation (expansion was the real win; the fancy parts add robustness).
4. Deterministic evals as a CI gate (and their honest limits).
5. Measured performance on a CPU (the fast classifier model; the 1.5b-is-slower-for-answers surprise).
