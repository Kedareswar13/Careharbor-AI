# CareHarbor AI timing report

Generated 2026-10-27 18:35 on this laptop (CPU only, Ollama). Each run uses a fresh, isolated database.

How the targets are met: deterministic red-flag rules before any LLM; rules-first routing; deterministic templates for identity, booking and prescriptions; short constant system prompts (KV-prefix reuse); compressed top-3 context; `num_predict` 120-160; `keep_alive` 60 m with warm-up and a keep-alive ping; a faster model for short JSON classifications; a semantic answer cache for FAQ only; a 30 s cache for doctor and slot lookups.

### Answer model qwen2.5:3b (classifier qwen2.5:1.5b)

| Path | Runs | p50 | max | Target | Result |
|---|---|---|---|---|---|
| Emergency (red-flag path, no LLM) | 5 | 0.05s | 0.40s | < 1s | PASS |
| FAQ answer (cold, LLM) | 4 | 3.11s | 14.19s | ≤ 10s | PASS |
| FAQ answer (semantic cache hit) | 4 | 0.19s | 0.47s | < 1s | PASS |
| Triage follow-up questions (LLM safety 2nd layer) | 3 | 0.71s | 0.78s | ≤ 10s | PASS |
| Triage guidance (LLM, grounded) | 3 | 5.86s | 7.61s | ≤ 10s | PASS |
| Booking turns (deterministic + DB) | 5 | 0.03s | 0.06s | ≤ 2s | PASS |
| Prescription read-back (deterministic) | 1 | 0.05s | 0.05s | ≤ 2s | PASS |

FAQ generation stage: median 1.51s over 4 calls.

Triage generation stage: median 5.78s over 3 calls.
### Answer model qwen2.5:1.5b (fallback)

| Path | Runs | p50 | max | Target | Result |
|---|---|---|---|---|---|
| Emergency (red-flag path, no LLM) | 5 | 0.04s | 0.05s | < 1s | PASS |
| FAQ answer (cold, LLM) | 4 | 3.93s | 4.81s | ≤ 10s | PASS |
| FAQ answer (semantic cache hit) | 4 | 0.07s | 0.27s | < 1s | PASS |
| Triage follow-up questions (LLM safety 2nd layer) | 3 | 0.62s | 0.62s | ≤ 10s | PASS |
| Triage guidance (LLM, grounded) | 3 | 11.18s | 13.74s | ≤ 10s | MISS |
| Booking turns (deterministic + DB) | 5 | 0.04s | 0.07s | ≤ 2s | PASS |
| Prescription read-back (deterministic) | 1 | 0.08s | 0.08s | ≤ 2s | PASS |

FAQ generation stage: median 3.83s over 4 calls.

Triage generation stage: median 11.11s over 3 calls.

## Trade-off (measured, not assumed)

Read the two tables above. On this laptop `qwen2.5:1.5b` is fast for short JSON classifications, so it handles the router fallback and the safety second layer: triage follow-up questions dropped from about 10 s to under 1 s. For written answers, 1.5b tended to produce longer texts, which made triage guidance slower than with 3b, and its phrasing is weaker. So `qwen2.5:3b` stays the answer model. The model menu in the composer lets you switch per conversation.

Total measurement time: 1.7 min.
