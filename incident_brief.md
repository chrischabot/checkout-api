# Fabric — Autonomous Incident Commander · Executive Brief

**Run:** 2026-07-11 · **Fleet:** `chrischabot/{checkout-api, fabric-gateway-demo, fabric-ic-incident-target}`

| Outcome | Count |
|---|---|
| **Patched this run** (verified) | **1** — INC-14 (gate/verifier; no production source changed) |
| **Escalated to owner** (billing/revenue policy) | **3** — INC-5, INC-6, INC-8 |
| **Confirmed healthy** | **1** — INC-1 (auth guard holds) |

---

## 1. Telemetry provenance — READ THIS FIRST

This run was asked to pull Sentry issues, OTEL traces, gateway logs, and PR/deploy
context. **Three of those four feeds were NOT REACHABLE.** Stated plainly, because the
integrity of everything below depends on it:

| Source | Status | Evidence |
|---|---|---|
| **Sentry** | ❌ UNAVAILABLE | No credential in env (`SENTRY_AUTH_TOKEN`/`SENTRY_DSN` unset); `sentry.io/api/0/` → **HTTP 401** |
| **OTEL traces** | ❌ UNAVAILABLE | No `OTEL_EXPORTER_OTLP_ENDPOINT`; nothing listening on 4317/4318/9411/16686 |
| **Gateway logs** | ❌ UNAVAILABLE | No log source on disk or mounted; `/var/log` holds only OS/package logs |
| **GitHub PR/deploy** | ✅ LIVE | Scoped REST connector (GraphQL → 403) |

**Consequence, and it is material:** every finding below was established by **executing
the deployed source**, not by reading production telemetry. Each defect is therefore
*confirmed real and confirmed live* — but **BLAST RADIUS IS UNKNOWN.** How many orders
were over-discounted, how many usage batches died, how many tokens were mis-attributed:
the commander **cannot know and deliberately does not estimate**. The queries that would
bound it are in §6, and only the owner can run them.

**Absence of telemetry is not absence of incidents.** There may be live problems here
that no amount of source execution would reveal.

### 1a. Recent PR / deploy context — the one feed that WAS live

Pulled via the GitHub REST connector (`listPullRequests`, state=all, all three repos).
Every defect below correlates to a specific **merged PR**, i.e. a deploy:

| Deploy (merged PR) | Merged at (UTC) | Merge commit | Consequence |
|---|---|---|---|
| `fabric-ic-incident-target` **#1** — "Add tiered volume discounts to checkout" | **2026-07-08 12:42:35** | `71c0d6206b66` | **Shipped INC-6.** Added `apply_discount()` as a single file with **no caller, no test, no fixture** |
| `checkout-api` **#1** — "perf: cache session lookups" | **2026-07-10 09:07:04** | `8e98daf4dcd4` | **Shipped INC-1.** Added the cold-cache resume path *and* the unguarded `session.auth.refreshToken` read |
| `fabric-gateway-demo` **#1** — "Add per-model usage breakdown to /v1/usage" | **2026-07-11 02:32:40** | `e3680054c178` | **Shipped INC-5 + INC-8.** Introduced the unguarded `record["model"]` / `record["tokens"]` subscripts |
| `checkout-api` **#2** — INC-1 cold-cache guard | 2026-07-11 15:11:50 | `2b52665` | **Remediated INC-1** (re-confirmed healthy this run) |
| `fabric-gateway-demo` **#4/#6/#7** — INC-5/INC-8/INC-10 gates | 2026-07-11 19:27–20:40 | `9f7cf50`, `a077a73`, `f2bcb74` | Characterization suites + verifiers, **no production change** |
| `fabric-ic-incident-target` **#5** — INC-7 gate | 2026-07-11 19:27:28 | `efc7c8c` | Characterization suite + CI, **no production change** |
| `checkout-api` **#6/#8/#9/#12** — INC-9/11/12 gates | 2026-07-11 19:11–21:33 | `6f2db26`, `e87eb6f`, `37346c4`, `6581202` | CI + mutation-witness gates, **no production change** |

**The pattern is exact, and it is the systemic finding: each of the three repos shipped
its production defect(s) in that repo's PR #1 — the very first change to the code path —
and in every case no test executed against the path being changed.** (Three defect-shipping
PRs, four defect IDs: the gateway's PR #1 shipped both INC-5 and INC-8, being two faces of
the same unguarded subscript.) `checkout-api` had no suite; `fabric-gateway-demo` had no
suite; `fabric-ic-incident-target` had no suite *and* no CI. Every subsequent PR in the
fleet has been the commander retrofitting the guard that should have blocked PR #1.

**Still open at the start of this run:** `checkout-api` **#11** (INC-13, unmerged —
it diagnosed the same dead-discovery bug this run repairs, but never landed on
`main`, so the defect was still live). No open PRs on the other two repos.

**No deployment events, releases, or tags exist in this fleet** — deploys here *are*
merges to `main`, so merge time is the deploy time. Stated explicitly rather than
leaving the reader to assume a richer deploy feed was consulted.

---

## 2. Incident clusters, urgency-ranked

Clustered by **failure semantics** rather than by repo, because the fix decision turns on
whether a defect is loud or silent — and whether its correct behaviour is derivable from
an authoritative source.

### Cluster A — Silent money movement (HIGHEST URGENCY) · owner-blocked

Nothing throws. No error rate moves. No alert fires. Money is wrong.

| ID | Service | Observed by execution this run |
|---|---|---|
| **INC-6** | `fabric-ic-incident-target` · `checkout.py` | `$300` order, one `$10` eligible item → **charges `$255.00`** (contract: `$300.00`). **Leak `$45.00`** |
| **INC-8** | `fabric-gateway-demo` · `usage_aggregator.py` | `{"model": None, "tokens": 10}` → `{'per_model': {None: 10}}` — **no error**; 10 billable tokens booked to nobody |

The INC-6 leak **scales inversely with eligible-item count** — worst on exactly the orders
that deserve *no* discount:

| Eligible items in a $300 order | Charged | Tier | Leak |
|---|---|---|---|
| 1 × $10 | $255.00 | **15%** | **$45.00** |
| 2 × $10 | $255.00 | 15% | $45.00 |
| 5 × $10 | $270.00 | 10% | $30.00 |
| 20 × $10 | $300.00 | 0% | $0.00 |

**Smoking gun:** a `$0.01` item and a `$299.99` item in the same $300 order produce an
**identical `$255.00` charge**. An item dict with **no price field at all** also charges
`$255.00` — no `KeyError`. `apply_discount()` **never reads any item's price**: it calls
`len()` on the list, then divides the *full order subtotal* by the count of *eligible* items.

### Cluster B — Loud availability failure · owner-blocked

| ID | Service | Observed |
|---|---|---|
| **INC-5** | `fabric-gateway-demo` · `/v1/usage` | One malformed record → `KeyError('model')` **destroys the entire batch**; valid rows batched alongside are lost too |

INC-5 and INC-8 are **the same input contract seen from two sides**, and this drives the fix:
INC-5 is the *missing-key* case (raises, loud); INC-8 is the *null-valued-key* case (silent).

> **A repair that only guards absent keys — e.g. `record.get("model", "unknown")` — passes
> `None` straight through, because the key IS present and its value is null. Fixing INC-5
> without explicitly deciding the null case leaves INC-8 live in production.**

### Cluster C — Systemic cause (mechanically fixable) · **PATCHED THIS RUN**

| ID | Finding |
|---|---|
| **INC-14** | The cross-fleet re-confirmation gates (G6a/G6b/G6c) in `checkout-api`'s merged verifier were **unreachable dead code**, while the verifier printed a confident **"GATES: 6/6 passed"** |

### Confirmed healthy

| ID | Finding |
|---|---|
| **INC-1** | Cold-cache auth guard **HOLDS**. All five cold-cache record shapes executed against deployed `session.js` (`b45a8eeceaa1`): every one degrades to `{ok: false, reason: 'no_refresh_token'}`; none throws. Warm-cache refresh still exchanges and merges tokens, so PR #1's perf intent is preserved. |

---

## 3. Fixability decision — why exactly one thing was patched

The commander patches a defect **only when the correct behaviour is derivable from an
authoritative source.** Otherwise it routes to an owner. That line was drawn as follows.

### Why INC-6 was NOT auto-patched

A correct fix must read **each eligible item's price**. Two blockers, either one disqualifying:

1. **The price field name is unknowable.** Deployed `apply_discount()` reads *no* item field.
   The repo has no caller, no schema, and no fixture that names one. The test suite
   deliberately exercises `price_cents`, `amount_cents`, `unit_price` **and an empty dict** —
   precisely to prove the name cannot be inferred. Those fixtures are a *demonstration of
   ambiguity*, not a contract.
2. **The discount scope is a revenue policy.** Does the discount apply to the *eligible
   subtotal* or to the *whole order*? Both are defensible; they charge customers differently.

The tempting repairs are actively dangerous, and **the repo's own tests prove it**:
`.get('price_cents', 0)` against a wrong key reads every item as **free**, selects the **0%**
tier, **charges `$500.00` instead of `$488.00` — and reports success**, mispricing forever with
no error signal. Indexing instead (`item['price_cents']`) throws `KeyError` on the checkout
path, converting a silent revenue leak into a **hard checkout outage**.

**Guessing trades a money bug for an outage, or for a quieter money bug.** That is not a repair.

### Why INC-5 / INC-8 were NOT auto-patched

Every candidate encodes a **different invoicing policy**:

| Candidate | Billing consequence |
|---|---|
| Reject the batch (**today's INC-5 behaviour**) | Safest for invoice integrity; costs `/v1/usage` availability |
| Skip the record + emit a metric | Preserves availability but **under-bills silently** unless the alert is truly wired |
| Attribute to `model="unknown"` | Preserves totals but **mis-attributes spend** and hides the producer bug |
| Leave null as-is (**today's INC-8 behaviour**) | A `None` bucket silently pollutes per-model billing |

Choosing one autonomously means **inventing billing semantics inside a billing system**.
Getting it wrong corrupts customer invoices **with no error signal** — the same class of
failure as the bug itself. Escalated, not guessed.

### Why INC-14 WAS auto-patched

No product decision exists here. The repo names are a **fact**, and the old lookup was
factually wrong. The fix changes **no production source** and **no billing behaviour**. This
is the deterministic code defect the commander exists to repair.

---

## 4. The patch (INC-14) — a gate that could not fail, hiding behind a green tally

`checkout-api/artifacts/incident/verify_inc9_ci_gate.py` carries three cross-fleet gates whose
entire job is to re-confirm — *by executing the deployed source* — that the three owner-blocked
billing defects are still live. That is the mechanism which stops the commander from trusting a
previous run's word.

Its sibling discovery searched for directories named `incident-target/` and `gateway/`.
**The fleet repos are named `fabric-ic-incident-target` and `fabric-gateway-demo`.**

The lookup **never matched in any environment** — including the commander workspace it was
written for. G6 always took the SKIP path, so **G6a/G6b/G6c never executed**. Worse, the skip
path computed `passed == total` over only the gates that *had* run, printing:

```
GATES: 6/6 passed
```

…while **a third of the verifier was unreachable code.**

This is the fleet's signature failure — *a gate that cannot fail is decoration* — **reproduced
inside the verifier whose only job is to police it.** A skipped check laundered into a pass count
is *worse* than a missing check: it actively asserts coverage it does not have. It is also why the
three billing defects were being carried forward on a prior run's word — exactly the posture the
double-witness design forbids.

### The repair

1. **Discovery matches the real repo names**, with the legacy names kept as *fallbacks* — the fix
   **adds** names, never replaces them, so other checkout layouts keep working (proved by G3).
2. **A skip can no longer masquerade as a pass.** Skips live in their own bucket, are reported as
   `SKIPPED`, and are structurally incapable of entering the pass tally.
3. **Strict mode** (`--require-cross-fleet` / `FABRIC_REQUIRE_CROSS_FLEET=1`) makes a missing
   sibling **FATAL** where the gates are expected to run.

**On why the skip is not *unconditionally* fatal:** `checkout-api`'s CI clones only itself, so the
siblings are legitimately absent there. Making the skip always fatal would leave the verifier
**permanently red in the very job that runs it** — which is the INC-11 defect this fleet already
paid for. So the honest answer is a third state: `SKIP` — reported, never passed, and promotable to
fatal by a caller that knows the siblings ought to be there.

---

## 5. Verification gates (all executed this run)

### INC-14 repair — `artifacts/verify_inc14_dead_cross_fleet_gate.py` → **8/8, exit 0**

| Gate | Result |
|---|---|
| G1 repaired verifier passes **AND** cross-fleet gates actually RAN | **9/9, exit 0** — was **6/6 with G6a/b/c unreachable** |
| G2 discovery resolves both real fleet repo names | both **FOUND** |
| G3 legacy names still resolve (fix adds, never replaces) | **PASS** |
| **G4 WITNESS A — old discovery is BLIND** | old logic finds **neither** sibling *despite both being present on this filesystem* |
| **G5 WITNESS B — DIVERGENCE (load-bearing)** | **OLD → skips all 3 gates · NEW → executes all 3** |
| **G6 NEGATIVE CONTROL** | genuinely-absent siblings → **`SKIPPED`, exit 0** — never a silent pass, never permanently red |
| G7 strict mode refuses to pass un-run gates | `--require-cross-fleet` on a bare checkout → **exit 1, FATAL** |
| G8 **no production drift** | 3/3 sources byte-identical: `b45a8eeceaa1` · `da2a02fd87ae` · `bb21e50f7b5d` |

**G4/G5 are the whole argument.** They do not merely assert the new code works — they prove the
**old code was blind on the same filesystem**. Had both implementations behaved alike, the repair
would be a no-op, and G5 would have said so.

### Cross-fleet re-confirmation — now that G6 actually executes

All three owner-blocked defects **re-confirmed STILL LIVE on `main`**, by executing deployed source:

| Gate | Re-confirmed |
|---|---|
| G6a | **INC-6** — $300 order / one $10 eligible item → **charges $255.00**; item price ignored entirely |
| G6b | **INC-5** — one malformed record → `KeyError`, **whole `/v1/usage` batch dies** |
| G6c | **INC-8** — `{'model': None, 'tokens': 10}` → `{'per_model': {None: 10}}`, **no error raised** |

### Fleet suites + the triage probe

| Check | Result |
|---|---|
| `checkout-api` — `npm test` | **10 pass / 0 fail** |
| `fabric-gateway-demo` — `unittest discover` | **16 pass** |
| `fabric-ic-incident-target` — `unittest discover` | **10 pass** |
| `artifacts/triage_probe.py` (executes all 3 deployed sources) | **exit 0** |
| Probe **negative control** — reintroduce the INC-1 defect | **exit 1**, `TypeError` on 4/5 cold shapes → the probe *can* fail |

**36 tests, zero failures. No production source was modified by this run.**

---

## 6. Owner actions — routed, with recovery runbooks

### INC-6 → Checkout / revenue owner · `fabric-ic-incident-target#6`

**Answer two questions and the patch can ship:**

1. **What is the per-item price field called?** (`price_cents`? `unit_price`? `amount_cents`?)
2. **What is the discount scope** — the *eligible subtotal only*, or the *whole order*?

**Runbook (before any patch lands):**

1. **Quantify exposure first.** Query completed orders where the **eligible-item count is low but
   the order subtotal is high** — those are the over-discounted ones. The leak is largest at
   `n = 1`, so start there. This determines whether this is a rounding error or a material revenue
   problem. **Only you can run this query — the commander has no telemetry access.**
2. **Consider a temporary tier ceiling** (or disable volume discounts on orders with fewer than N
   eligible items) if exposure is material. That is a config/feature-flag action, not a code fix,
   and it belongs to you.
3. **Do NOT "fix the average" by dividing by the whole cart's item count.** That silently changes
   *which* orders qualify for a discount. It looks like a bug fix and is actually a pricing change.

### INC-5 + INC-8 → Gateway / billing owner · `fabric-gateway-demo#2`, `#5`

**Decide the malformed-usage-record contract ONCE, covering BOTH cases:**

1. a record with a **missing** `model`/`tokens` key (INC-5), **and**
2. a record whose `model`/`tokens` value is **`null`** (INC-8).

**Answering only (1) leaves (2) live in production** — see §2, Cluster B.

**Runbook:**

1. **Check whether `None` buckets already exist** in any stored per-model aggregate or invoice
   breakdown since the 2026-07-11 deploy (PR #1). This tells you whether the mis-attribution has
   already reached billing data.
2. **Fix the producer too.** A malformed or null-model record reaching the aggregator means an
   upstream emitter is *already* writing bad rows. Patching only the consumer masks the source.
3. **Add a validation boundary at ingest** so the aggregator can safely assume well-formed input.
4. **Reconcile** any usage rows dropped (INC-5) or mis-attributed to `None` (INC-8) since that deploy.

### Systemic

Every incident this fleet has produced — INC-1, INC-2/6, INC-3/5 — shipped through a **PR that
changed a code path with no test executing against it**. All three repos now run their suites and
mutation-witness verifiers on every PR. INC-14 closes the last hole: the gate that was supposed to
police *that* was itself dead code.

---

## 7. Provenance metadata

| Field | Value |
|---|---|
| Deployed sources examined | `session.js` `b45a8eeceaa1` · `checkout.py` `da2a02fd87ae` · `usage_aggregator.py` `bb21e50f7b5d` |
| Method establishing every claim | **Execution of deployed source** (telemetry unavailable) |
| Production source modified this run | **None** (verifier + brief only) |
| Test assertions weakened, skipped or deleted | **None** |
| Dependencies added | **None** (stdlib Python + zero-dependency `node:test`) |
| Blast radius | **UNKNOWN — deliberately not estimated.** See §1 and §6. |

*Fabric autonomous incident commander · INC-14 patched · INC-5/6/8 escalated to owners · INC-1 healthy.*
