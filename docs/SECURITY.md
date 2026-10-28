# CareHarbor AI security model

> **CareHarbor AI — your clinic's first point of contact for patient questions, appointment workflows, prescription queries, and safe escalation to staff.**

CareHarbor AI touches sensitive data: patient identities, appointments and prescriptions. Its retrieval pipeline (RAG) also reads documents that the model must treat as data, not instructions. A single crafted prompt must not be able to steer the pipeline or leak private data, so security is built into the request path rather than added afterwards.

```
patient message
   │
   ├─ Safety gate ............ deterministic red-flag rules (emergency path, no LLM)
   ├─ LAYER 1 Query validation  injection / smuggling / encoded payloads / exfiltration / scope  ──► blocked or refused
   ├─ LAYER 2 Access control .. role + tenant filter BEFORE retrieval; PHI bound to the verified session
   ├─ LAYER 3 Fixed templates . user text and retrieved text only fill delimited slots
   ├─ Output guardrails ....... no diagnosis / dosing / new medicines; numbers and claims must be grounded
   └─ LAYER 4 Observability ... who / when / which document ids / decision; PII masked; no document text
```

All four layers live in [`careharbor/security.py`](../careharbor/security.py). They're exercised by [`tests/test_redteam.py`](../tests/test_redteam.py) and by the golden regression set in [`evals/golden/`](../evals/golden/).

## Layer 1: validate every query before it enters the pipeline

`validate_query()` runs in the graph's router, before any retriever, LLM or tool sees the text. It rejects:

| Check | Examples it stops |
|---|---|
| Instruction override | "ignore previous instructions", "bypass the guardrails" |
| Role hijack | "you are now…", "pretend you are the admin", "act as a pharmacist" |
| System-prompt probing | "show the system prompt", "developer mode", "jailbreak" |
| Data exfiltration | "dump the database", "show another patient's prescriptions", "export all records" |
| Identifier probing | internal ids such as `P002`, `patient_id`, `session_id` |
| SQL or code | `SELECT * …`, `DROP TABLE`, `eval(` |
| Delimiter smuggling | `</context><system>`, `[INST]`, `<|im_start|>` |
| Tool forcing | "call the tool book_appointment" |
| Encoded payloads | long base64-like tokens |
| Oversized input | more than 1,200 characters |
| Out of scope | cricket, weather, code, stock prices… (unless the message is also about health or the clinic) |

Blocked queries get a fixed reply and an audit entry (`query_blocked`, with the reason). Out-of-scope questions get a polite message saying CareHarbor AI only answers clinic and health questions. CareHarbor AI never guesses.

## Layer 2: authorisation happens before retrieval

- **Documents carry metadata:** `audience` (`public` or `staff`) and `tenant` (for example `sunrise-clinic`; MedlinePlus reference topics are global, `*`).
- **Callers have a `Principal`:** role (`anonymous`, `patient` or `staff`) plus tenant.
- **The filter runs before scoring.** `RagIndex.bm25()` drops every section the principal may not read, so an unauthorised document can never be retrieved, ranked or quoted.
  - Staff SOPs (`content/staff/`) are invisible to patients.
  - Another tenant's documents (`content/tenants/lakeside-clinic/`) are invisible to this deployment. Its different fee of ₹350 is used as a canary in tests.
- **Patient health information is bound to the verified session, not to arguments.**
  - MCP tools never accept a `patient_id`.
  - The chat session id travels out-of-band in the MCP request `_meta`.
  - The tool server resolves the *verified* patient for that session from the database.
  - Unverified sessions get `identity_required`.
  - Another patient's appointment returns the same "not found" message as a non-existent one, so ids can't be enumerated.
- **Tools are allow-listed per agent.** For example, the FAQ agent can't call `book_appointment`.

## Layer 3: fixed prompt templates

Every LLM call uses a `PromptTemplate` from `security.TEMPLATES` (router, safety, triage, faq, navigate). Each template has:

- **A constant system prompt.** It also lets Ollama reuse its cached prompt prefix, which helps latency.
- **A user message with named, delimited slots**, such as `<context>[[context]]</context>` and `<question>[[question]]</question>`.

Slot values are neutralised before insertion:

- Delimiter tags are stripped, so a user can't close `</context>` and open `<system>`.
- Slot markers typed by a user (`[[context]]`) are removed.
- Control characters are removed and each slot is length-capped.
- Rendering uses literal replacement, never `str.format`, so braces in user text are inert.
- A template refuses to render unless every slot is supplied explicitly.

Retrieved documents are also treated as untrusted. `sanitise_retrieved()` drops instruction-like lines, which defends against indirect injection planted inside a leaflet. Every system prompt states that text inside the slots is data, not instructions.

## Layer 4: observability without creating a new data leak

| What is logged | Where |
|---|---|
| Every tool call and its outcome | `audit_log` (actor `tool`) |
| Every retrieval: role, tenant, PII-masked query, **document and section ids only**, decision (`answered`, `answered_fallback`, `not_found_*`, `triage_*`) | `audit_log` (actor `retriever`) |
| Every guardrail hit: red flag, blocked query, output blocked, positive screen | `audit_log` (actor `guardrail`) |
| Per-turn trace: stages, latency, tokens, tool names, guardrails (no document text) | `data/traces/<date>.jsonl` |

Two rules apply to everything logged:

1. **Full retrieved documents are never logged.** Only their ids are.
2. **Personal data is masked before writing** by `logging_setup.redact()`. It covers phone numbers, email, Aadhaar, PAN, card numbers, bank account numbers, IFSC, UPI ids, dates of birth and OTPs. The same filter is attached to the application logger.

### Saved chat history (`conversations`, `chat_messages`)

- **What is stored:**
  - the patient's messages, **PII-masked** (a message that is only a one-time code is stored as `[one-time code]`);
  - the assistant replies, with the demo OTP stripped from cards and replies;
  - the evidence payload: citations, stages and guardrails.
- **Who can read it:**
  - only a holder of the HMAC-signed session id. Unsigned or guessed ids get a 404.
  - after **Sign out**, the conversations are marked `ended`, and no transcript is returned until the same patient verifies again (same HMAC memory key) and re-opens them.
- **Deletion:** a patient can delete a conversation. That removes its messages, identity row and LangGraph checkpoint.
- **Expiry and retention:**
  - Verified sessions expire after `VERIFIED_TTL_MINUTES` (default 120). After that, record-bearing conversations are hidden (`locked`) until the patient verifies again and re-opens them, and re-opening never extends the TTL.
  - Conversations are purged after `CHAT_RETENTION_DAYS` (default 30), together with their checkpoints.
- **Identity brute force:**
  - Besides the per-session limits, 5 failed phone + DOB matches for one phone number within 15 minutes lock that number across **all** sessions (`identity_throttle`, keyed HMAC of the number).
  - Starting new sessions can't bypass it, which matters because demo mode shows the OTP on screen.
- **Client IP:** the container trusts `X-Forwarded-For` only from an explicit proxy (`FORWARDED_ALLOW_IPS`), so rate limits can't be bypassed by spoofing the header.
- **Evidence:**
  - The `Security (4 layers)` eval suite runs 20 attacks against both an anonymous and a verified session, plus 10 cross-role and cross-tenant probes.
  - It then scans every log, trace, checkpoint and saved chat written during the run for raw PII.
  - The staff console's **Security** page shows live counts per layer.
- **Known limit (G25, mitigated by G31):** the signed id is a bearer token kept in the browser. Expiry, locking, sign-out and retention bound the exposure.

## Output guardrails (accuracy and no hallucination)

- **No diagnosis, dosing or new-medicine advice** in generated text. Sentences that defer to a clinician ("only as advised by your doctor") are allowed. A violation replaces the answer with an extractive quote from the cited document, or a safe fallback that offers a handoff.
- **Every number must be grounded.** Any number in an LLM answer must appear in the retrieved context or the user's message.
- **Every claim must be supported.** A sentence whose content words are mostly absent from the retrieved context counts as an unsupported claim and triggers the fallback.
- **The FAQ prompt must answer `NOT_FOUND`** when the context lacks the answer. CareHarbor AI then says it doesn't have that information instead of guessing.
- **Records are never paraphrased by an LLM.** Prescriptions and bookings are rendered from tool output using templates, so a reply can only state what the database holds. The golden set checks the database row behind every "Booked" reply.

## Tests and evals that keep this true

- **`tests/test_redteam.py`:**
  - 30 emergency phrasings at 100% recall, plus 10 non-emergency phrasings that must not trigger.
  - 15 injection attacks, including one hidden inside a document.
  - Role and tenant leakage checks.
  - Template integrity, PII masking, and retrieval logs that hold ids only.
  - 25 diagnosis and dosing outputs that must be blocked, plus hallucination detection.
- **`evals/golden/regression.jsonl`:** multi-turn golden cases using the QA test account (`9000000001` / `01/01/2000`). They run after every phase via `scripts/verify_phases.py`, with the deterministic MockLLM by default or the real model with `--live`.
