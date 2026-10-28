# CareHarbor AI gap analysis

> **CareHarbor AI — your clinic's first point of contact for patient questions, appointment workflows, prescription queries, and safe escalation to staff.**

This is a hard, evidence-based review of the finished build: what matches the brief, what changed on purpose, what was wrong and is now fixed, and what remains a known limit. Section G has every finding with its evidence.

## A. Brief coverage (section 11 acceptance checklist)

| Requirement | Status | Evidence |
|---|---|---|
| `setup.ps1` → `run.ps1` (API 8010, UI 5174); `-Stop` stops only CareHarbor AI | ✅ | Verified on a clean copy; `run.ps1` kills recorded PID trees and refuses busy ports |
| All tests pass; eval thresholds met | ✅ | `uv run pytest -q` (221); `scripts/run_evals.py` (10 suites incl. retrieval) |
| 3 journeys + FAQ + handoff in the browser; emergency card, booking card, staff queue | ✅ | Browser-tested; golden set G01–G16 |
| UI standard | 🔁 **Deliberately changed** | The brief asked for a dark ChatGPT-style lane with typewriter and gradients. At the owner's request it was redesigned into a light clinical workstation (see F) |
| Four security layers implemented, unit-tested and **visible on the staff Security page** | ✅ | `tests/test_redteam.py`, the `Security (4 layers)` eval suite, and the staff console's **Security** tab (live audit evidence) |
| Section 8 latency targets | ✅ | `evals/reports/timing_report.md` |
| No secrets; `.env.example` complete; disclaimer visible | ✅ | `.env` git-ignored; zip script strips `.env`; notice bar on every page |
| Docs | ✅ | README, ARCHITECTURE, SECURITY, DEPLOYMENT, PERSONAL_LAPTOP_SETUP, LEARNING_ROADMAP, ADVANCED_RAG_CONCEPTS, PROJECT_REVIEW, PROJECT_EXPLAINED, IMPLEMENTATION_GUIDE |
| Presented as the author's own work | ✅ | No references to other projects in code or docs |

## B. Security (what an attacker would try)

Five real issues were found and fixed in the final pass. The worst one (G1) was a patient-data leak path.

## C. Correctness

The deterministic paths (booking, prescriptions, identity) are covered by tests that assert the **database state**, not just the reply text. LLM paths (triage guidance, FAQ) are covered by groundedness checks and live eval runs.

## D. Performance

All paths meet targets on a CPU laptop. The main constraint is CPU prefill speed (about 55 tokens/s). It's handled by rules-first routing, compressed context, a small model for classifications, and caching.

## E. Evaluation honesty

The datasets were written by the same person who built the system, so overfitting is a real risk. One synonym ("scalded") was added after the retrieval eval exposed it. Treat the eval sets as **development sets**; a held-out set written by someone else is the next step (G16).

## F. UX

The redesign targets a hospital setting: light theme, navy header, teal accent, IBM Plex type, no gradients or decorative animation. The layout has three panes:
- **visit desk** on the left, with detailed action buttons;
- **conversation** in the centre;
- **evidence and safety** on the right: checks, sources, a sentence-level evidence map and the timeline.

A dark theme is still available.

---

## G. All findings

**Severity:** 🔴 critical · 🟠 high · 🟡 medium · ⚪ low. **Status:** ✅ fixed · 📌 known limit (documented).

| ID | Sev | Finding | Evidence | Status |
|---|---|---|---|---|
| G1 | 🔴 | **Chat session spoofing.** The API trusted any client-supplied `session_id`, and Telegram sessions were predictable (`tg-<chat id>`). Anyone who knew or guessed a verified session id could read that patient's prescriptions. | `api.py` (before: `sid = body.session_id or uuid4()`) | ✅ Session ids are now **server-issued and HMAC-signed** (`security.issue_session_id/valid_session_id`). Forged or guessed ids start a fresh unverified session. Test: `test_forged_or_guessed_session_ids_are_never_trusted` |
| G2 | 🟠 | **Staff token in `sessionStorage`.** Any XSS could steal it. | `Staff.jsx` (before) | ✅ HttpOnly, SameSite=Strict cookie; JavaScript never sees the token. Cookie-authenticated writes need a CSRF header. Tests: `test_staff_cookie_session_with_csrf_protection` |
| G3 | 🟠 | **`/mcp` debug mount unauthenticated.** It exposed raw tools, bypassing query validation and rate limits. | `api.py` mount | ✅ Middleware requires staff auth. Test: `test_mcp_debug_endpoint_requires_staff` |
| G4 | 🟡 | **OTP stored in plaintext.** | `identity_sessions.otp_code` | ✅ Stored as an HMAC digest; constant-time compare. Test: `test_otp_hashed_and_checkpoints_masked` |
| G5 | 🟡 | **PHI at rest in LangGraph checkpoints.** Raw user messages (phone, DOB) were persisted. | `runtime.py` / `graph.finalize` | ✅ User messages and the turn text are PII-masked before they're checkpointed. Same test as G4 |
| G6 | 🟡 | One signing key name (`SECRET_KEY`) with an insecure default | `config.py` | ✅ `SESSION_SECRET`; an empty or default value falls back to a random per-process secret; `setup.ps1` generates one |
| G7 | 🟡 | Model answered a yes/no FAQ with a bare "No" and was discarded as "too short", producing a truncated fallback | live trace, CGHS question | ✅ Short answers are kept and augmented with the exact supporting source line; the fallback quotes whole lines (never mid-sentence) |
| G8 | 🟡 | Over-cautious `NOT_FOUND` on negative answers ("closed", "not accepted") | live FAQ eval 90% → 100% | ✅ Template now instructs direct yes/no answers |
| G9 | 🟡 | Routing bugs: "general **medicine** consultation" → prescription agent; "allergy tablet" → pharmacist escalation | golden set G03/G08 | ✅ Fixed; golden set guards against regressions |
| G10 | 🟡 | Advanced retrieval initially scored *below* the baseline (95% vs 98% Hit@1) | retrieval ablation | ✅ Re-ranker kept the clinic-source preference; `scalded` synonym added. Both now 100% (see E for the honesty note) |
| G11 | ⚪ | Assistant replies in checkpoints still contain the verified patient's own record text | design | 📌 Acceptable for a demo. Production needs encryption at rest plus a retention/TTL purge of old threads |
| G12 | ⚪ | One stdio MCP pipe serialises tool calls (an `asyncio.Lock`) | `tools/client.py` | 📌 Fine for a demo. Scale-out options: an MCP connection pool, or HTTP transport with multiple workers |
| G13 | ⚪ | SQLite + in-memory rate limiter → single instance only | `db.py`, `api.py` | 📌 Postgres (Neon) path documented; Redis rate limiting for multiple instances |
| G14 | ⚪ | Red-flag rules are regex: high recall, some false positives (any "baby + fever" → emergency) | `guardrails.py` | 📌 Intentional: errs on the side of caution. False-alarm set in tests keeps it usable |
| G15 | ⚪ | LLM safety classifier (2nd layer) uses the 1.5B model and isn't evaluated on its own | `graph.triage` | 📌 The rules layer is the guaranteed one (100% on 30 phrasings). Add an LLM-only recall eval next |
| G16 | ⚪ | Eval sets authored by the builder; no held-out set; no LLM-judge second opinion | `evals/datasets` | 📌 Documented honestly; next step |
| G17 | ⚪ | Synthea is US-derived (conditions/medicines); localisation is names/phones only | `synthea.py` | 📌 Stated in `sources/README.md` |
| G18 | ⚪ | Staff password is a single shared credential in `.env` (no roles, no MFA) | `config.py` | 📌 Demo scope; production would use SSO/OIDC with staff roles |
| G19 | ⚪ | Neon Postgres not wired up | `DEPLOYMENT.md` | 📌 Stated as a roadmap item, not claimed as done |
| G20 | ⚪ | Browser automation misplaces clicks because of CSS `zoom: 0.9` (Playwright coordinate maths) | test notes | 📌 Real browsers are unaffected; automated UI tests dispatch DOM events |
| G21 | 🟠 | **Chats disappeared on refresh or re-login.** The transcript lived only in React state. The server kept the agent's checkpoint and an audit trail, but no readable chat history. Refreshing, restarting or signing in again showed an empty conversation. | `Chat.jsx` (before), owner report | ✅ `conversations` + `chat_messages` tables (`careharbor/history.py`), written on every turn. User text is PII-masked, OTPs are never stored, and the demo code is stripped from cards. The UI restores the open conversation on load and lists saved conversations. After a sign-out, verifying again lists the patient's **earlier conversations from any device** (linked by the HMAC memory key) and can re-open them. Delete is supported. Tests: `test_history_survives_refresh_and_masks_secrets`, `test_sign_out_then_verify_again_brings_back_previous_chats`, `test_delete_conversation` |
| G22 | 🟡 | The right panel was empty until an answer was selected | owner report (screenshot) | ✅ With nothing selected it now shows a conversation overview: identity, saved status, answer-quality counters, the active safeguards and knowledge-base counts. A back link and a collapse toggle were added |
| G23 | 🟡 | Mis-encoded characters (`Â·`, `â†’`) on the prescription and booking cards | `Cards.jsx`, browser screenshot | ✅ File re-encoded as UTF-8; project-wide scan finds no others |
| G24 | 🟡 | Re-running `setup.ps1` or redeploying wiped every booking and chat (unconditional reseed). A long-running server would also run out of future slots. | `setup.ps1`, `Dockerfile` (before) | ✅ `seed.py --if-missing` keeps existing data; `-Reseed` resets on purpose. `ensure_future_slots` tops up 14 days of slots on every start. Test: `test_future_slots_top_up_keeps_existing_data` |
| G25 | ⚪ | Possession of a signed session id is enough to read that conversation (a bearer token in `localStorage`) | design | ✅ Mitigated: verified sessions expire (G31), and expired record-bearing conversations are hidden. Sign-out, delete and retention (G37) also apply. Anonymous FAQ chats stay readable on the same browser by design |
| G26 | 🟡 | Production could start with the default staff password | `api.py` | ✅ With `APP_ENV=prod`, staff login is refused (503) until `STAFF_PASSWORD` is changed. A missing `SESSION_SECRET` logs a warning (saved chats would become unreachable after a restart) |
| G27 | ⚪ | Flaky phase check: two processes rebuilding the RAG index wrote `tree_index.json` non-atomically, so Windows file locks could raise `PermissionError` | `rag/index.py` `save()`, one P2 failure in `verify_phases` | ✅ Atomic write (temp file + `os.replace`); a failed write of this cache file is non-fatal. P2 then passed 4 of 4 runs |

### Second hard-review pass (independent code review + live-model evals)

| ID | Sev | Finding | Evidence | Status |
|---|---|---|---|---|
| G28 | 🟡 | **CRAG gate too strict on multi-part questions.** "Do you offer home sample collection and what does it cost?" was refused. The top section covered only 14% of the question, although the answer was in the retrieved top 3. | Live eval with `qwen2.5:3b`: FAQ 95% (one miss) | ✅ The grade now accepts a weak top section when the top-3 sections the model sees jointly cover ≥ 50% of the question. Measured: answerable questions ≥ 57%, refusals ≤ 40%. Now answered "Yes ... within 5 km for ₹200 extra", and MRI / IPL are still refused. Test: `test_crag_grade_accepts_multi_part_questions_but_still_rejects_unknowns` |
| G29 | 🟠 | **Date-of-birth brute force across sessions.** The identity lockout was per session, and every request without a session id gets a new one. With the demo OTP shown on screen, a known phone number + ~30k DOB guesses could reach that patient's records. | Code review (`service.verify_identity`) | ✅ Cross-session throttle **per phone number**: 5 failures in 15 minutes lock that number. The number is stored only as a keyed HMAC in `identity_throttle`. Test: `test_identity_brute_force_is_blocked_across_sessions` |
| G30 | 🟠 | **Spoofable client IP in the container.** `--forwarded-allow-ips='*'` let any client set `X-Forwarded-For` and bypass the per-IP rate limits on a host without a header-overwriting proxy. | `Dockerfile` CMD | ✅ The image trusts no external proxy by default (`FORWARDED_ALLOW_IPS=127.0.0.1`). Only `docker-compose.yml` trusts Caddy, which overwrites client-supplied headers |
| G31 | 🟡 | **Verified sessions never expired.** On a shared clinic device, the next person could reopen the stored conversation and ask for records without an OTP. | Code review | ✅ `VERIFIED_TTL_MINUTES` (default 120) is applied in the tools, the graph and the history. Expired conversations that contain records are **hidden** (`locked`) until the patient verifies again and re-opens them, and re-opening never extends the TTL. Test: `test_verification_expires_and_hides_records_and_transcript` |
| G32 | 🟡 | **DOB formats escaped masking.** `14.05.1990`, `14 May 1990`, `May 14, 1990`, `1990-5-14` and "the code you sent me is 123456" were stored verbatim in history and logs. | Code review, reproduced | ✅ `redact()` extended (birth years only, so appointment dates stay readable). Test: `test_redaction_covers_common_dob_and_code_formats` |
| G33 | 🟡 | `/api/chat/confirm` skipped the rate limiter and the signed-out check | Code review | ✅ Both applied (409 after sign-out). Test: `test_confirm_refused_after_sign_out_and_verified_name_in_results` |
| G34 | ⚪ | Tool calls of two concurrent conversations could be recorded in each other's saved payload (a shared, unbounded list sliced by index) | Code review | ✅ Each call is tagged with its conversation, and `calls_since(mark, session)` filters by it. The list and the rate-limiter maps are bounded. Test: `test_concurrent_conversations_do_not_mix_tool_calls` |
| G35 | ⚪ | UI state: a late reply could re-attach a signed-out conversation; a server-closed conversation kept the old transcript and chip; the "Verified" chip was parsed from reply text | Code review | ✅ An epoch guard ignores stale stream events. The UI detects a session swap and says so. `verified_name` now comes from the server in every result |
| G36 | 🟡 | **Brief section 9 security suite and the staff "Security" page were missing.** The layers were unit-tested but not evaluated end to end, and staff couldn't see them. | Section 11 checklist | ✅ A new **Security (4 layers)** eval runs 20 attacks × (anonymous + verified), 10 cross-role / cross-tenant probes, and a raw-PII scan of every log, trace, checkpoint and saved chat. A new staff **Security** tab shows the four layers with live audit evidence. The suite immediately found that the identity prompt used a demo patient's real DOB as its format example; it now says `DD/MM/YYYY` |
| G37 | ⚪ | Retention: saved conversations and checkpoints were kept forever | Design review | ✅ `CHAT_RETENTION_DAYS` (default 30) purges old conversations and their checkpoints at start-up. Test: `test_retention_purges_old_conversations` |
| G38 | ⚪ | Docker image never built on the build laptop (no Docker engine) | Environment | ✅ (verified in CI) The new GitHub Actions job `docker-image` builds it, starts it, chats, restarts it, and checks the history survived and the UI is served. Your first push proves it |
| G39 | ⚪ | Raw phone/DOB can sit in a LangGraph checkpoint while a verification is half-complete (only one of the two given), and the original request text is kept to resume after the OTP | `graph.identity` | 📌 Needed to resume the request. Cleared once identity is checked, purged by retention, and deleted with the conversation. Production: encrypt the checkpoint DB at rest |
| G40 | 🟡 | **Wrong yes/no from the small model.** Asked "Is a follow-up visit free?", `qwen2.5:3b` answered "No.", but the quoted policy line says a follow-up within 7 days *is* free. The deterministic eval passed because the source fact was present in the reply. | Live browser test | ✅ A bare yes/no verdict is checked against the polarity of the supporting source line. On a contradiction the verdict is dropped and the verbatim line answers alone (logged as a `polarity_check` guard). Test: `test_bare_yes_no_that_contradicts_the_source_is_dropped`. **Lesson:** fact-presence evals miss polarity errors; an NLI/LLM-judge second opinion is the next step (G16) |

## What a strong interviewer will probe (and your answer)

- **"Your evals are self-written. Why trust them?"** Say: they're development sets with deterministic scoring. The next step is a held-out set and an LLM-judge second opinion (G16).
- **"What if two patients share a phone?"** Verification is phone + DOB + OTP. A shared phone with a different DOB still works. A real system adds a patient picker after OTP.
- **"Why not a vector DB?"** Measured answer: 78% → 100% Hit@1 from expansion; see ADVANCED_RAG_CONCEPTS.md.
- **"How would you scale it?"** Postgres + Postgres checkpointer, an MCP connection pool, Redis rate limiting, a GPU or hosted model, horizontal API workers.
