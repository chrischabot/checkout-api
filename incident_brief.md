# Incident Brief — Fabric Autonomous Production Incident Commander

**Run:** 2026-07-11 ~21:30 UTC (verified against the system clock, not inherited from prior run prose) · **Fleet:** `checkout-api`, `fabric-ic-incident-target`, `fabric-gateway-demo`
**Patched this run:** 1 (INC-13, verifier/CI — no production source touched) · **PR:** `checkout-api` #11
**Routed to owners:** 3 live billing/revenue defects (INC-6, INC-5/3, INC-8)
**Production code changed:** **none.** All three deployed sources are byte-identical to their deployed revisions (sha256 asserted before *and* after every gate).

> **Metadata note.** Several earlier incident reports in this fleet are dated "2026-07-14", but every GitHub timestamp on them reads **2026-07-11** — that date was wrong and got copied forward between runs. This brief uses the verified clock. It is a small thing, but a brief whose own provenance metadata is unreliable cannot be trusted about billing.

---

## 1. Executive summary

Three revenue-affecting defects remain **live in production** and are **not** auto-patchable — each requires a billing-policy decision that only an owner can make. They are unchanged from prior runs and were **re-confirmed live this run by executing the deployed source**.

The patchable finding this run was in the commander's **own verification machinery**: the gate that is supposed to re-confirm those three defects had been **silently dead in every environment**, while reporting itself green. It is fixed, and the fix is proven load-bearing.

| Urgency | ID | Symptom | Fixability | Status |
|---|---|---|---|---|
| **P1** | INC-6 | Checkout over-discounts: $300 order with one $10 eligible item charges **$255.00** | ❌ policy (revenue) | **Owner** — issue #6 |
| **P1** | INC-8 | Null model silently books billable tokens to a `None` key | ❌ policy (billing) | **Owner** — issue #5 |
| **P2** | INC-5/3 | One malformed usage record raises `KeyError`, kills the **whole** `/v1/usage` batch | ❌ policy (billing) | **Owner** — issue #2 |
| **P2** | **INC-13** | **Cross-fleet re-confirmation gate was dead code reporting "6/6 passed"** | ✅ **deterministic** | **PATCHED + verified** |

---

## 2. ⚠️ Telemetry provenance — read this before trusting any coverage claim

**No telemetry source was reachable this run.** Every source named in the commander's charter was attempted and failed:

| Source | Result | Evidence |
|---|---|---|
| **Sentry issues** | ❌ **unavailable** | No `SENTRY_DSN` / `SENTRY_AUTH_TOKEN` / org / project in env. `sentry.io` → **HTTP 401** unauthenticated. |
| **OTEL traces** | ❌ **unavailable** | No `OTEL_EXPORTER_OTLP_ENDPOINT`, no Tempo/Jaeger/Honeycomb config. No collector. |
| **Gateway logs** | ❌ **unavailable** | No Loki/ClickHouse/Datadog source; no log artifact on disk or mounted. |
| **GitHub PR / deploy context** | ✅ **available** | Scoped GitHub connector (REST). Raw `api.github.com` → 401; GraphQL → 403. |

**Consequence, stated plainly:** every finding below was established by **reading and executing the deployed source**, not by reading production telemetry. That makes each defect **confirmed real and confirmed live** — but it means:

> **Blast radius is UNKNOWN and is deliberately NOT estimated in this brief.** How many orders were over-discounted, how many usage batches died, and how many tokens were mis-attributed cannot be determined without the telemetry or the owner queries in §6. Any number here would be fabricated, and a fabricated number in a billing incident is worse than no number.

**Absence of telemetry is not absence of incidents.** There may be live production issues this run could not see.

---

## 3. The three owner-blocked defects (re-confirmed live)

All three were re-verified this run by executing the deployed source. They are **not** stale reports carried forward.

### INC-6 — checkout volume-discount leak · `fabric-ic-incident-target/checkout.py` (sha256 `da2a02fd87ae`)

`apply_discount()` picks the discount tier from `subtotal_cents / n`, where `subtotal_cents` is the **full order subtotal** but `n` counts **only eligible items**. Ineligible items inflate the per-item average, so an order buys a tier it never earned.

| Order shape | Charged | Tier | Leak |
|---|---|---|---|
| 1 eligible item in a $300 order | **$255.00** | 15% | **$45.00** |
| 5 eligible items in a $300 order | $270.00 | 10% | $30.00 |
| 20 eligible items in a $300 order | $300.00 | 0% | $0.00 |

**The leak is largest on the orders that should get *no* discount at all.** Reproduced this run: a $0.01 eligible item and a $299.99 eligible item in the same order produce an **identical** charge — item prices cannot influence the result.

**Why not auto-patched.** A correct fix must read each eligible item's price. The deployed function **never reads any field off the item dicts** — it only calls `len()`. No caller, schema, fixture, or test in the repo names a price field. So a fix requires two answers the commander cannot derive from any authoritative source:

1. **What is the per-item price field called?** (`price`, `price_cents`, `unit_price`, `amount`…) Guessing wrong either crashes checkout or silently computes garbage. The repo's own gate proves this: injecting the tempting `.get('price_cents', 0)` one-liner **charges $500.00 instead of $488.00 and reports success.**
2. **What is the discount scope?** Eligible subtotal only, or the whole order? These produce materially different customer charges.

Both are **revenue-policy decisions**. Guessing mischarges real customers with no error signal — the same class of failure as the bug. → **escalated, not guessed.**

### INC-5 / INC-3 — `/v1/usage` batch destruction · `fabric-gateway-demo/service/usage_aggregator.py` (sha256 `bb21e50f7b5d`)

`aggregate_usage()` indexes `record["model"]` and `record["tokens"]` unguarded. **One** malformed record raises `KeyError` and fails the **entire** aggregation batch. Loud, but total.

### INC-8 — null model silently mis-attributes billable tokens (same file)

```python
aggregate_usage([{"model": None, "tokens": 10}])
# -> {'per_model': {None: 10}, 'grand_total': 10}
```

**10 billable tokens booked against a `None` key. No error raised.** Distinct from INC-5: that one is loud, this one is **silent** and pollutes per-model invoicing.

**Critically:** a repair that only guards *absent* keys (`record.get("model", "unknown")`) passes `None` straight **through** — the key *is* present, its value is null. **Fixing INC-5/3 without explicitly handling null leaves INC-8 live in production.**

**Why neither is auto-patched.** Every candidate repair encodes a *different invoicing policy*:

| Candidate | Billing consequence |
|---|---|
| reject the record (fail loud) | safest for invoice integrity; costs `/v1/usage` availability |
| skip it + emit a metric | preserves availability, **under-bills silently** unless the alert is truly wired |
| attribute to `model="unknown"` | preserves totals, **mis-attributes spend**, hides the producer bug |
| leave as-is (**today**) | a `None` bucket silently pollutes per-model billing |

On one test batch the candidate repairs **disagree by 60 billable tokens.** Choosing one autonomously means inventing billing semantics and corrupting customer invoices with no error signal. → **escalated, not guessed.**

---

## 4. INC-13 — the patch applied this run (deterministic, verifier-only)

### The finding: a gate that could not fail, hiding behind a green exit

`checkout-api/artifacts/incident/verify_inc9_ci_gate.py` carries three **cross-fleet gates** (G6a/G6b/G6c) whose entire job is to re-confirm the three defects above are still live — the mechanism that stops the commander from trusting a previous run's word.

Its sibling-repo discovery searched for directories named:

```
incident-target/          gateway/
```

The fleet repos are actually named **`fabric-ic-incident-target`** and **`fabric-gateway-demo`**. The lookup **never matched — in any environment**, including the commander workspace it was written for. G6 always took the SKIP path, so **G6a/G6b/G6c never executed.** And the skip path counted only the gates that *had* run, then returned 0 — printing a confident **`GATES: 6/6 passed`** while a third of the verifier was unreachable code.

That is this fleet's signature failure — *a gate that cannot fail is decoration* (INC-9, INC-11, INC-12) — **reproduced inside the verifier whose only job is to police it.** A skipped check laundered into a pass count is worse than a missing check: it actively asserts coverage it does not have.

> PR #7 diagnosed this same mismatch, but was **closed unmerged** (superseded by #8/#12), so the repair never landed. It lands now.

### The repair

1. **Discovery matches the real repo names**, keeping the legacy names as fallbacks — the fix *adds* names, never replaces them, so other checkout layouts keep working.
2. **A skip can no longer masquerade as a pass.** Skips are tracked separately and reported as `SKIPPED`, never folded into the pass count.
3. **Strict mode** (`--require-cross-fleet` / `FABRIC_REQUIRE_CROSS_FLEET=1`) makes a missing sibling **FATAL** where the gates are expected to run, while a bare single-repo checkout (which is what `checkout-api` CI clones) stays legitimately green. Making the skip *unconditionally* fatal would have re-created the permanently-red expired-precondition bug that INC-11/INC-12 existed to fix.
4. **The verifier now runs in CI**, so the repair is itself guarded.

### Verification — and the moment the gate caught me

| Check | Result |
|---|---|
| INC-9 verifier, commander workspace | **9/9, exit 0** — was **6/6 with G6a/b/c unreachable** |
| G6a re-confirms INC-6 live | $300 order / one $10 item → **charges $255.00**; item price ignored entirely |
| G6b re-confirms INC-5 live | one malformed record → `KeyError`, whole batch dies |
| G6c re-confirms INC-8 live | `{'model': None, 'tokens': 10}` → `{'per_model': {None: 10}}`, no error |
| **INC-13 G5 — DIVERGENCE (load-bearing)** | **OLD discovery → skips all 3 gates · NEW → executes all 3** |
| **INC-13 G6 — NEGATIVE CONTROL** | genuinely-absent siblings still report **SKIPPED**, never a silent pass |
| INC-13 G7 — no production drift | 3/3 files byte-identical to pre-run hashes |
| Strict mode, bare checkout | **exit 1 FATAL** (refuses to pass un-run gates) |
| Default, bare checkout | **4/4 passed, 3 SKIPPED, exit 0** (correct — not permanently red) |

**G5 is the whole argument.** It does not merely assert the new code works; it proves the **old code was blind on the same filesystem**. Had both behaved alike, the repair would be a no-op and G5 would say so.

**The gate caught its own author.** My first version of the CI step ran the INC-13 verifier unconditionally — and against a bare checkout it reported **4/7, exit 1**. Left in, that would have shipped a permanently-red gate: *the exact INC-11 bug, committed by the incident that diagnoses it.* The verifier is now environment-aware, which is why the default bare-checkout path is green and strict mode is opt-in.

---

## 5. Fleet verification surface (all green, this run)

| Repo | Suite | Verifiers |
|---|---|---|
| `checkout-api` | **10/10** (`npm test`) | INC-9 **9/9** (strict) · INC-12 **6/6** · INC-13 **7/7** |
| `fabric-ic-incident-target` | **10/10** (`unittest`) | checkout gate **3/3** |
| `fabric-gateway-demo` | **16/16** (`unittest`) | INC-5 **7/7** · INC-8 ✅ · INC-10 ✅ |

**36 tests, 6 verifiers, zero failures.** All three deployed production sources hash-match their deployed revisions:
`session.js` = `b45a8eeceaa1` · `checkout.py` = `da2a02fd87ae` · `usage_aggregator.py` = `bb21e50f7b5d`

---

## 6. What is needed from owners

### Gateway / billing owner — `/v1/usage` (INC-5/3 + INC-8)

**Decide the malformed-usage-record contract once, covering BOTH cases:**
1. a record with a **missing** `model`/`tokens` key, **and**
2. a record whose `model`/`tokens` value is **`null`**.

Answering only (1) leaves (2) live in production.

**Runbook:**
1. **Check whether `None` buckets already exist** in any stored per-model aggregate or invoice breakdown since the 2026-07-11 deploy (PR #1, commit `e368005`). This tells you if mis-attribution has already reached billing data.
2. **Fix the producer too.** A malformed/null record reaching the aggregator means an upstream emitter is *already* writing bad rows. Patching only the consumer masks the source.
3. **Add a validation boundary at ingest** so the aggregator can assume well-formed input.
4. **Reconcile** any usage rows dropped or mis-attributed since that deploy.

### Checkout / revenue owner — `apply_discount()` (INC-6)

**Answer two questions:**
1. **What is the per-item price field called?**
2. **Does the discount apply to the eligible subtotal only, or the whole order?**

**Runbook:**
1. **Quantify exposure first.** Query completed orders where the eligible-item count is **low** but the order subtotal is **high** — those are the over-discounted ones. The leak is largest at `n=1`. This determines whether this is a rounding-error problem or a material revenue problem. **Only you can run this query — it is why the blast radius is UNKNOWN above.**
2. **Consider a temporary tier ceiling** (or disable volume discounts on orders with fewer than N eligible items) to stop the bleeding. This is a config/feature-flag action, not a code fix.
3. **Do NOT "fix the average" by dividing by the whole cart's item count.** It silently changes which orders qualify, looks like a bug fix, and is not one.

---

## 7. Systemic pattern

Every incident in this fleet — INC-1 through INC-13 — shipped through a **pull request that changed a code path with no test executing against it.** The three repos now all run their suites *and* their mutation-witness verifiers on every PR. INC-13 closed the last hole in that chain: the gate that watches the gates was itself unwatched.

The remaining exposure is **not** an engineering gap. It is three unanswered billing-policy questions, and they are worth more than the code that implements them.

---

*Fabric autonomous incident commander · run 2026-07-11 ~21:30 UTC · INC-13 patched and verified (PR #11); INC-3/5/6/8 routed to owners. Telemetry: Sentry 401, OTEL absent, gateway logs absent — findings established by executing deployed source, blast radius deliberately not estimated.*
