# SPEC output template + worked example

Fill this shape. Keep it tight — a small slice specified deeply. Drop a row only
if it genuinely doesn't apply (and say so).

```
## SPEC — <one-line goal>

**Literal ask:** <what the user said>
**Real goal (decision it serves):** <the why / what this unblocks>

**This increment (smallest shippable slice):** <one concrete change>
**Parked for later:** <the rest — explicitly out of scope now>

**Assumptions & invariants:** <what must hold; *flag the risky ones*>
**Inputs → Outputs:** <data/preconditions in → artifact/state out>

**Approaches considered:**
1. <A> — pros: … / cons: …
2. <B> — pros: … / cons: …
→ **Recommended:** <pick> — because <one-line reason>

**Verification (done = measurable):**
- [ ] <criterion 1> — verified by <test / claims check / manual review / code review>
- [ ] <criterion 2> — verified by <…>

**Open questions blocking GO:** <none | numbered list>
```

A criterion with no verifier attached is not "done" — flag it rather than ship it.

---

## Worked example

Raw ask: *"put rate limiting on the search endpoint, it keeps falling over"*

```
## SPEC — Rate-limit the public search endpoint to stop retry storms

**Literal ask:** add rate-limiting to the search endpoint that "keeps falling over."
**Real goal (decision it serves):** decide whether per-key throttling alone keeps the
endpoint up under a burst, before we spend a quarter on a caching layer.

**This increment (smallest shippable slice):** a per-API-key sliding-window limiter
on the one hot endpoint, returning 429 with `Retry-After` — no caching, no schema change.
**Parked for later:** global quotas, a response cache, per-tenant dashboards.

**Assumptions & invariants:** every request carries an API key (RISKY — anonymous
traffic may not; verify before building); limiter state fits in the existing Redis;
no contract with a customer forbids 429s.
**Inputs → Outputs:** (api_key, timestamp) → allow | throttle(retry_after).

**Approaches considered:**
1. In-app sliding window in Redis — pros: precise, shared across instances;
   cons: one more Redis round-trip per request.
2. Gateway usage-plan throttle — pros: zero app code; cons: keyed per plan, not
   per client, so one noisy client throttles its neighbours.
→ **Recommended:** #1 — the burst is per-client, so the limit must key on the client.

**Verification (done = measurable):**
- [ ] Replaying the burst trace, the same key is throttled after N requests — unit test.
- [ ] Retries fall below 5% of requests, from a 20% baseline — check the before/after
      numbers with a claims-verifying pass before calling it a win.
- [ ] p95 latency added < 5ms — load test.

**Open questions blocking GO:**
1. N and the window length? (default: 5 calls / 60s — accept with one word.)
```

Note how the spec stays on **one slice**, makes the **risky assumption explicit**,
offers **approaches not code**, and ties **every "done" to a verifier** — including
checking the numbers claim against its source rather than asserting the win.
