# Fabric — Autonomous Production Incident Commander
## Executive Incident Brief · run **2026-07-12 ~00:22 UTC**

**Fleet:** `chrischabot/checkout-api` · `chrischabot/fabric-gateway-demo` · `chrischabot/fabric-ic-incident-target`

---

## 0. Bottom line

| | |
|---|---|
| **Patched & verified this run** | **1 — INC-17:** the meta-gate that polices "a skip must never count as a pass" **hardcoded the number it was policing** (`denominator == 6`), so any future PR that adds a gate would hard-fail CI on a healthy repo. |
| **Re-confirmed LIVE, owner-blocked** | **3** — INC-6 (checkout revenue leak), INC-5/3 (`/v1/usage` batch failure), INC-8 (null-model billing bucket). Re-confirmed **by executing the deployed source**, not carried forward on a previous run's word. |
| **Production source changed** | **NONE.** All 3 deployed sources byte-identical to their deployed revisions (full sha256). |
| **Telemetry reachable** | **NONE.** Sentry unauthenticated, no OTEL collector, no gateway logs. **Blast radius is UNKNOWN and deliberately NOT estimated.** |
| **Fleet gate surface** | **36 tests + 8 verifiers, zero failures.** |
| **Process defects fixed** | Stale figures in the brief on `main` (corrected here). Duplicate PRs **#17** and **#18** re-proposing already-merged work (closed as superseded). |

---

## 1. Telemetry provenance — probed, not assumed

The commander was asked for fresh Sentry issues, OTEL traces, gateway logs and PR/deploy context. **Three of those four sources were unreachable.** Each was probed this run:

| Source | Probe | Result |
|---|---|---|
| **Sentry** | `GET https://sentry.io/api/0/` | **HTTP 200** but `{"version":"0","auth":null,"user":null}` — reachable, **unauthenticated** |
| **Sentry (issues)** | `GET https://sentry.io/api/0/organizations/` | **HTTP 401** — `"Authentication credentials were not provided."` **Zero issue data.** |
| **OTEL traces** | TCP connect `4317`, `4318`, `9411`, `16686`, `14268`, `55681` | **all CLOSED.** No collector, no configured endpoint. |
| **Gateway logs** | `/var/log/gateway`, `/var/log/fabric`, `/logs`, `/var/log/nginx`, disk scan | **no source** on disk or mounted. |
| **PR / deploy context** | GitHub REST connector | ✅ **LIVE** — the only source available. |

**Consequence, stated plainly:** every defect claim below was established by **executing the deployed source**. The defects are *confirmed real and confirmed live* — but **how many customers, orders or billable tokens have been affected is UNKNOWN to the commander, and no blast radius is estimated.** The owner queries that would bound it are in §7.

> *Absence of telemetry is not absence of incidents.* There may be live production failures this run could not see. Restoring observability is the highest-leverage action available to the owner (§7).

**Date correction.** The system clock reads **2026-07-12 00:22 UTC**, and every GitHub timestamp agrees. Several earlier reports in this fleet are stamped "2026-07-14" — a wrong date copied forward between runs, contradicted by their own artifacts. Corrected here.

---

## 2. Symptom clustering

Four live incidents, clustered by **mechanism** rather than by repo.

### Cluster A — Silent revenue/billing corruption (3, ALL owner-blocked)

Shared signature: **nothing throws, no error rate moves, no alert fires.** They are invisible to precisely the telemetry that is unavailable — which is why they can only be found by execution.

| ID | Where | Re-confirmed live this run (executed deployed source) |
|---|---|---|
| **INC-6** | `fabric-ic-incident-target` · `checkout.py::apply_discount()` | $300 order with one **$10** eligible item → **charges $255.00** (contract: $300.00). Item price ignored **entirely** — a $0.01 and a $299.99 eligible item yield an identical discount. |
| **INC-5/3** | `fabric-gateway-demo` · `usage_aggregator.py::aggregate_usage()` | One malformed record → `KeyError` → **the whole `/v1/usage` batch dies.** Loud, but takes the good records with it. |
| **INC-8** | same function | `{'model': None, 'tokens': 10}` → `{'per_model': {None: 10}, 'grand_total': 10}` — **no exception.** 10 billable tokens booked against a `None` key. Silent. |

### Cluster B — "Gates that cannot fail" (the meta-incident — this is the one that was patchable)

This fleet's signature pathology, now on its **fourth** repetition. Each time, a gate encoded a fact that was true only on the day it was written:

| ID | The expired precondition | Status |
|---|---|---|
| INC-11 | G3 asserted *"`ci.yml` is NEW"* — permanently false the instant it merged | fixed (PR #8) |
| INC-12 | G3 required `ci.yml` byte-identical to `main` — **forbade the repo from editing its own CI** | fixed (PR #12) |
| INC-15 | the cross-fleet gates were **unreachable dead code**, and the skip was **laundered into a `6/6 passed` tally** | fixed (PR #14) |
| **INC-17** | **the gate written to police that laundering hardcoded the very number it was policing** | ✅ **patched this run** |

---

## 3. Urgency ranking

| Rank | ID | Why |
|---|---|---|
| **1** | **INC-6** | Live money leaving the business on every mixed order. The leak **scales inversely with eligible-item count** — worst on the orders that deserve *no* discount. Silent. |
| **2** | **INC-8** | Silently mis-attributes billable tokens to a `None` model. `grand_total` still reconciles, so **no reconciliation check can detect it.** Unattributed, not rejected, revenue. |
| **3** | **INC-5/3** | Destroys whole `/v1/usage` batches. Ranked below INC-8 only because it is **loud** — you lose the batch, but you know. |
| **4** | **INC-17** | No customer impact. Ranked last in severity, but it is **the only one the commander is allowed to fix** — and it protects the machinery that keeps the other three honest. |

---

## 4. Fixability analysis

### Why the three billing defects were NOT auto-patched (the central judgment)

**INC-6.** A correct fix must read each eligible item's price. The deployed `apply_discount()` **never reads any field off the item dicts** — it only calls `len()`. No caller, no schema, no test, and no reachable telemetry names a price field. Two things are unknowable from any authoritative source:

1. **What is the price field called?** (`price`, `price_cents`, `unit_price`…) The repo's own tests prove the tempting guesses are unsafe: `.get('price_cents', 0)` against a wrong key reads every item as free, selects the 0% tier, **charges $500.00 instead of $488.00 — and reports success.** Indexing instead throws `KeyError` on the checkout path, converting a silent revenue leak into a **hard outage**.
2. **What is the discount's scope** — the eligible subtotal only, or the whole order? These produce materially different customer charges.

Both are **revenue policy**, not engineering. Guessing mischarges real customers with no error signal — *the same class of failure as the bug itself.*

**INC-5/3 + INC-8** are one contract seen from two sides. Every candidate repair encodes a different **invoicing policy**:

| Candidate | Billing consequence |
|---|---|
| reject the record (fail loud) | safest for invoice integrity; costs `/v1/usage` availability |
| skip it + emit a metric | preserves availability, **under-bills silently** unless the alert is truly wired |
| attribute to `model="unknown"` | preserves totals, **mis-attributes spend**, hides the producer bug |
| leave as-is (today) | a `None` bucket silently pollutes per-model billing |

Critically: **a repair guarding only *absent* keys (`record.get("model", "unknown")`) passes a `None` value straight through**, because the key *is* present — its value is null. **Fixing INC-5 without deciding the null case leaves INC-8 live in production.**

> The commander will not invent billing semantics. Choosing one autonomously would corrupt customer invoices with no error signal. These are escalated, not guessed.

### Why INC-17 WAS safe to patch

Deterministic code defect · **zero production surface** (verifier + CI only) · no policy content · fully verifiable by execution. The correct behaviour is not a judgment call: a meta-gate must not hard-fail a healthy repo.

---

## 5. The patch — INC-17

### The defect

`verify_inc15_cross_fleet_discovery.py` gate **G6** (the NEGATIVE CONTROL) asserted that the nested INC-9 verifier's pass tally had a denominator of literally six:

```python
no_phantom_passes = bool(lm) and int(lm.group(2)) == 6   # <-- THE DEFECT
```

That is a **merge-time fact frozen into a permanent gate.** It encodes *"the INC-9 verifier has exactly six gates"* — true only on the day it was written.

**Reproduced before repairing it.** I added a single, trivially-true, perfectly ordinary gate to `verify_inc9_ci_gate.py` — the kind of change any future PR might make, and exactly the behaviour the fleet wants to *encourage*:

| Verifier | Result |
|---|---|
| INC-9 itself (the extended one) | **7/7 passed, exit 0 — perfectly healthy** |
| **INC-15, bare checkout (= what `checkout-api` CI clones)** | **G6 FAILS** |
| **INC-15, fleet workspace** | **8/9, G6 FAILS** |

So the next contributor to extend the gate surface would have hard-reddened CI on a repo where **nothing was wrong.** That is the **INC-11/INC-12 expired-precondition bug — committed inside the verifier written to police exactly this habit.** A gate that can never pass is as worthless as one that can never fail: both teach the team to ignore the red.

### The repair

Assert the **invariant**, not the **constant**. What INC-15 actually exists to enforce is:

> the denominator counts exactly the gates that **executed**, and a **SKIPPED** gate is in **neither the numerator nor the denominator.**

```python
no_phantom_passes = (
    bool(lm)
    and denominator == n_executed          # skips are NOT in the denominator
    and numerator <= n_executed            # and cannot inflate the numerator
    and denominator < n_executed + n_skipped  # a laundered skip would push it higher
)
```

Count-independent: the INC-9 verifier may grow to 7, 9 or 40 gates and G6 keeps working — **while a skip folded back into the tally is still caught.**

**Files changed (3 — no production source):**
- `artifacts/incident/verify_inc15_cross_fleet_discovery.py` — G6 predicate replaced with the invariant.
- `artifacts/incident/verify_inc17_gate_count_invariant.py` — **new**, gates the repair.
- `.github/workflows/ci.yml` — runs the new verifier, so the repair cannot rot back into a hardcoded count.

---

## 6. Verification — every number below was produced by a command run this turn

### INC-17 verifier — **6/6, exit 0**

| Gate | Result |
|---|---|
| G1 the repaired INC-15 verifier passes as shipped | **fleet workspace 9/9, exit 0 · bare checkout 5/5 (4 SKIPPED), exit 0** — green in the job that runs it |
| **G2 WITNESS A — the PRE-REPAIR predicate is BROKEN by an ordinary new gate** | extended INC-9 verifier is **healthy (7/7, exit 0)** — yet the old rule `denominator == 6` **rejects it** |
| **G3 WITNESS B — DIVERGENCE (load-bearing)** | same tree, one gate added: **OLD G6 → FAIL, exit 1 · NEW G6 → PASS, exit 0** |
| **G4 ANTI-WEAKENING** | fold the skip back into the tally → **repaired G6 still FAILS, exit 1** ✅ the original INC-15 defect is still caught |
| G5 the repaired G6 still catches a genuinely FAILING nested verifier | inject a failing gate → **G6 FAILS, exit 1** |
| G6 no production drift | **3/3** deployed sources byte-identical on the **full** sha256 |

**G3 is the whole argument.** It does not merely assert the new predicate works — it proves the **old predicate hard-failed a healthy repo on the same tree** where the new one passes. Had both behaved alike, the repair would be a no-op and G3 would say so.

**G4 is the anti-weakening gate, and it is the one that matters most.** Making a red gate green is trivial and worthless. G4 mutates the INC-9 summary to fold its SKIPPED gates into the tally — printing a confident `7/7 passed` while only 6 gates executed, *the exact original INC-15 defect* — and requires the repaired G6 to **still go red**. It does. **This fix is a correction, not a relaxation.**

### The gates bite in every environment

| Environment | INC-17 result |
|---|---|
| Commander workspace (all 3 repos) | **6/6, exit 0** |
| **Bare checkout (= `checkout-api` CI)** | **6/6, exit 0** ✅ needs no siblings — never permanently red |

### Verified in GitHub Actions — not merely locally

This is the claim that matters, because the bug this incident repairs is *"a gate that hard-fails the job that runs it."*

| | |
|---|---|
| PR | [#19](https://github.com/chrischabot/checkout-api/pull/19) · branch `fabric/inc17-gate-count-invariant` |
| CI on the branch | **every run green** — [29173812402](https://github.com/chrischabot/checkout-api/actions/runs/29173812402) (`0b257aa`) and [29173844919](https://github.com/chrischabot/checkout-api/actions/runs/29173844919) (`a7b6907`), both **conclusion: success** |
| Steps | all 5 green, including the new **INC-17** step, on a checkout that clones **only `checkout-api`** |

*(Later commits on this branch are documentation-only — they do not touch the verifier, `ci.yml`, or any production source, so the code evidence above stands. The PR's checks tab is the live record.)*

The INC-17 verifier is green **in the very CI job that executes it**, with no sibling repos present — so the repair does not re-commit the INC-11 permanently-red bug it exists to eliminate.

### Cross-fleet re-confirmation — the 3 billing defects are STILL LIVE on `main`

Executed against the deployed source this run (INC-9 verifier, strict mode, **9/9**):

| Gate | Re-confirmation |
|---|---|
| **G6a** | **INC-6** — $300 order / one $10 eligible item → **charges $255.00**; item price ignored entirely |
| **G6b** | **INC-5** — one malformed record → `KeyError`, **whole `/v1/usage` batch dies** |
| **G6c** | **INC-8** — `{'model': None, 'tokens': 10}` → `{'per_model': {None: 10}}`, **no error raised** |

### Full fleet gate surface — green, with non-vacuous counts

| Repo / check | Result |
|---|---|
| `checkout-api` — `npm test` | **10 pass / 0 fail** |
| `checkout-api` — INC-9 verifier (strict) | **9/9, exit 0** (cross-fleet gates executed) |
| `checkout-api` — INC-12 verifier | **6/6, exit 0** |
| `checkout-api` — INC-15 verifier | **9/9, exit 0** |
| `checkout-api` — **INC-17 verifier (new)** | **6/6, exit 0** |
| `fabric-gateway-demo` — suite | **Ran 16 tests, OK** |
| `fabric-gateway-demo` — INC-5 / INC-8 / INC-10 verifiers | exit 0 · exit 0 · exit 0 |
| `fabric-ic-incident-target` — suite | **Ran 10 tests, OK** |
| `fabric-ic-incident-target` — checkout gate | **3/3, exit 0** |

**36 tests + 8 verifiers, zero failures.** Production sources unchanged:

```
b45a8eeceaa142dd70aea4182930d02edb2d23ce90f0f02527910abb5f18d7e8  session.js
bb21e50f7b5dab4463b71984bbe86a5df2b6ba442ffeff84d9b70815781750e5  usage_aggregator.py
da2a02fd87aec668467114e0bc30ff7c2fe7fd3d8f105f5f156361b9c87c5c5e  checkout.py
```

---

## 7. Owner runbooks — the work only you can do

### INC-6 · checkout revenue leak → **checkout / revenue owner**

1. **Quantify exposure first.** Query completed orders where the **eligible-item count is low but the order subtotal is high** — those are the over-discounted ones. The leak is largest at `n=1`; start there. This determines whether this is a rounding nuisance or a material revenue problem. **The commander cannot do this — it requires order data.**
2. **Consider a temporary tier ceiling** (or disable volume discounts on orders with fewer than N eligible items). A config/feature-flag action that stops the bleeding without resolving the policy question.
3. **Do NOT "fix the average" by dividing by the whole cart's item count.** That silently changes which orders qualify for a discount. It looks like a bug fix and is not one.
4. **Answer the two questions** — the price field name, and the discount scope — and the guarded patch can be written and gated.

### INC-5/3 + INC-8 · `/v1/usage` billing → **gateway / billing owner**

1. **Decide the malformed-record contract ONCE, covering BOTH cases:** a **missing** `model`/`tokens` key, *and* a **null-valued** one. Answering only the first leaves the second live in production.
2. **Check whether `None` buckets already exist** in any stored per-model aggregate or invoice breakdown since the 2026-07-11 deploy (PR #1, commit `e368005`). This tells you whether mis-attribution has already reached billing data.
3. **Fix the producer too.** A null model reaching the aggregator means an upstream emitter is already writing it. Patching only the consumer masks the source.
4. **Add a validation boundary at ingest** so the aggregator can assume well-formed input.
5. **Reconcile** any usage rows dropped or mis-attributed since that deploy.

### Restore observability — **blocks every future run**

The commander is **blind to production**. Provision a Sentry token, an OTEL endpoint, and a gateway log source. Until then every run can only reason about code it can execute, and **no incident brief can bound customer impact.** This is the single highest-leverage item on this list.

### Process · PR hygiene

PRs **#17** and **#18** were open against `checkout-api`, both re-proposing work that **#14 already merged** (#17 even recommended closing #14, the PR that actually shipped the repair). Both were `mergeable: false`, branched from pre-#14 bases. **Closed as superseded this run.** Leaving duplicates open is what invites each successive run to file another.

---

## 8. Coverage gap — stated once more, plainly

**No telemetry was reachable this run.** Sentry answered but unauthenticated (401 on any issue query); no OTEL collector on any standard port; no gateway logs on disk. Only the GitHub REST connector was live.

Every claim in this brief was established by **executing the deployed source**. The four incidents are real and live. **Their blast radius is UNKNOWN and deliberately not estimated** — §7 lists the owner queries that would bound it.

---

*Fabric autonomous incident commander · run 2026-07-12 · INC-17 patched & verified · INC-6 / INC-5 / INC-8 re-confirmed live and routed to owners.*
