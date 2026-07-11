# Fabric — Autonomous Production Incident Commander
## Executive Incident Brief · run 2026-07-11 ~23:40 UTC

**Fleet:** `chrischabot/checkout-api` · `chrischabot/fabric-gateway-demo` · `chrischabot/fabric-ic-incident-target`

---

## 0. Bottom line

| | |
|---|---|
| **Patched & verified this run** | **1** — INC-15: the fleet's cross-fleet re-confirmation gates were unreachable dead code reporting `6/6 passed` ([PR #14](https://github.com/chrischabot/checkout-api/pull/14), **CI green**) |
| **Re-confirmed LIVE, owner-blocked** | **3** — INC-6 (checkout revenue leak), INC-5/3 (`/v1/usage` batch failure), INC-8 (null-model billing bucket) |
| **Production source changed** | **NONE.** All 3 deployed sources byte-identical to their deployed revisions (full sha256) |
| **Telemetry reachable** | **NONE.** Sentry unauthenticated, no OTEL collector, no gateway logs. **Blast radius is therefore UNKNOWN and is deliberately NOT estimated.** |
| **Process defect fixed** | Duplicate-PR pile-up: PRs **#11** and **#13** both carried this repair and **neither merged**. Both now **closed as superseded** by #14 |

---

## 1. Telemetry provenance — stated plainly

The commander was asked for fresh Sentry issues, OTEL traces, gateway logs and PR/deploy context. **Three of those four sources were unreachable this run.** Each was probed, not assumed:

| Source | Probe | Result |
|---|---|---|
| **Sentry** | `GET https://sentry.io/api/0/` | **HTTP 200** but `{"version":"0","auth":null,"user":null}` — reachable, **unauthenticated**. No credential in the environment. **Zero issue data.** |
| **OTEL traces** | TCP connect to `4317`, `4318`, `9411`, `16686`, `14268` | **all closed.** No collector, no configured endpoint. |
| **Gateway logs** | `/var/log/gateway`, `/var/log/fabric`, `/mnt/logs`, disk scan | **no source** on disk or mounted. |
| **PR / deploy context** | GitHub REST connector | ✅ **LIVE** — the only source available. |

**Consequence, and it is a real limitation:** every defect claim in this brief was established by **executing the deployed source**, not by reading production telemetry. The defects below are therefore *confirmed real and confirmed live* — but **how many customers, orders, or billable tokens have actually been affected is UNKNOWN to the commander.** No blast radius is estimated. The owner queries needed to bound it are in §6.

*Absence of telemetry is not absence of incidents.* There may be live production failures this run could not see.

**Date correction (carried forward):** several earlier reports in this fleet are stamped "2026-07-14". The system clock reads **2026-07-11**, and every GitHub timestamp agrees. The 2026-07-14 dates were a wrong date copied between runs.

---

## 2. Symptom clustering

Four distinct incidents, clustered by mechanism rather than by repo. All three repos share **one systemic cause**: *every incident this fleet has produced shipped through a PR that changed a code path no test executed.*

### Cluster A — Silent revenue/billing corruption (3 incidents, ALL owner-blocked)

These share a signature: **nothing throws, no error rate moves, no alert fires.** They are invisible to exactly the telemetry that is unavailable, which is why they were found by execution.

| ID | Where | Re-confirmed live this run (by execution) |
|---|---|---|
| **INC-6** | `fabric-ic-incident-target` · `checkout.py::apply_discount()` | $300 order with one **$10** eligible item → **charges $255.00** (contract: $300.00). Item price ignored **entirely** — a $0.01 and a $299.99 eligible item give an identical discount. |
| **INC-5/3** | `fabric-gateway-demo` · `usage_aggregator.py::aggregate_usage()` | One malformed record → `KeyError` → **the whole `/v1/usage` batch dies.** Loud, but takes good records with it. |
| **INC-8** | same function | `{'model': None, 'tokens': 10}` → `{'per_model': {None: 10}}` — **no exception.** 10 billable tokens booked against a `None` key. Silent. |

### Cluster B — Gates that cannot fail (the meta-incident, PATCHED this run)

| ID | Finding | Status |
|---|---|---|
| **INC-15** | The verifier's cross-fleet gates (G6a/b/c) — the mechanism that re-confirms Cluster A is still live — **never executed in any environment**, and the skip was **laundered into a `6/6 passed` tally**. | ✅ **Patched + verified** |

---

## 3. Urgency ranking

| Rank | ID | Why this rank |
|---|---|---|
| **1** | **INC-6** | Live money leaving the business on every mixed order. Leak **scales inversely with eligible-item count** — worst on orders that deserve *no* discount. Silent. |
| **2** | **INC-8** | Silently mis-attributes billable tokens to a `None` model. `grand_total` still reconciles, so **no reconciliation check can see it.** Unattributed, not rejected, revenue. |
| **3** | **INC-5/3** | Destroys whole `/v1/usage` batches. Ranked below INC-8 only because it is **loud** — you lose the batch, but you know it happened. |
| **4** | **INC-15** | No customer impact, but it is the incident that **hides the other three.** Fixed first precisely because it is the only one the commander is allowed to fix. |

---

## 4. Fixability analysis

### Why the three billing defects were NOT auto-patched

This is the central judgment of the run, and it is deliberate.

**INC-6** — a correct fix must read each eligible item's price. The deployed `apply_discount()` **never reads any field off the item dicts** — it only calls `len()` on them. No caller, no schema, no test, and no reachable telemetry names a price field. Two things are unknowable from any authoritative source:
1. **What is the price field called?** (`price`, `price_cents`, `unit_price`…) The repo's own tests prove the tempting guesses are unsafe: `.get('price_cents', 0)` against a wrong key reads every item as free, selects the 0% tier, **charges $500.00 instead of $488.00 — and reports success.** Indexing instead throws `KeyError` on the checkout path, converting a silent revenue leak into a **hard outage**.
2. **What is the discount's scope** — eligible subtotal only, or the whole order? These produce materially different customer charges.

Both are **revenue policy**, not engineering. Guessing mischarges real customers with no error signal — *the same class of failure as the bug itself.*

**INC-5/3 + INC-8** — these are one contract seen from two sides. Every candidate repair encodes a different **invoicing policy**:

| Candidate | Billing consequence |
|---|---|
| reject the record (fail loud) | safest for invoice integrity; costs `/v1/usage` availability |
| skip it + emit a metric | preserves availability, **under-bills silently** unless the alert is truly wired |
| attribute to `model="unknown"` | preserves totals, **mis-attributes spend**, hides the producer bug |
| leave as-is (today) | a `None` bucket silently pollutes per-model billing |

Critically: **a repair guarding only *absent* keys (`record.get("model", "unknown")`) passes a `None` value straight through**, because the key *is* present — its value is null. **Fixing INC-5 without deciding the null case leaves INC-8 live in production.**

> The commander will not invent billing semantics. Choosing one of these autonomously would corrupt customer invoices with no error signal. These are escalated, not guessed.

### Why INC-15 WAS safe to patch

Deterministic code defect · zero production surface · no policy content · a wrong directory name. Fully verifiable by execution.

---

## 5. The patch — INC-15

### The defect

`checkout-api/artifacts/incident/verify_inc9_ci_gate.py` carries three cross-fleet gates (G6a/G6b/G6c) whose only job is to re-confirm — **by executing the deployed source** — that the three owner-blocked billing defects are still live. That is the mechanism that stops the commander from carrying findings forward on a previous run's word.

Its sibling discovery searched for directories named:

```
incident-target/          gateway/
```

The fleet repos are actually named **`fabric-ic-incident-target`** and **`fabric-gateway-demo`**.

The lookup **never matched — in any environment**, including the commander workspace it was written for. G6 always took the SKIP path, so **G6a/G6b/G6c never executed.** Worse, the skip path computed `passed == total` over only the gates that *had* run, printing a confident:

```
GATES: 6/6 passed
```

…while **a third of the verifier was unreachable dead code.**

That is this fleet's signature failure — *a gate that cannot fail is decoration* — **reproduced inside the verifier whose only job is to police it.** A skip laundered into a pass count is worse than a missing check: it actively asserts coverage it does not have.

**Process finding:** PRs **#11** and **#13** each diagnosed this exact mismatch. **Neither merged.** The repair never reached `main`, so the blind discovery was still live — and a third consecutive run would have filed a third duplicate. That pile-up is itself a fleet pathology worth naming.

### The repair (3 files, no production source)

1. **Discovery matches the real repo names**, with the legacy names kept as **fallbacks** — the fix *adds* names, never replaces them, so other checkout layouts keep working (proven by G3 against a synthetic legacy layout).
2. **A skip can no longer masquerade as a pass.** Skips are tracked in a separate list and reported as `SKIPPED`, structurally incapable of entering the pass tally or the denominator.
3. **Strict mode** — `--require-cross-fleet` / `FABRIC_REQUIRE_CROSS_FLEET=1` — makes a missing sibling **FATAL** where the gates are expected to run.
4. **The new verifier runs in CI**, so the repair is itself guarded.

#### On strictness — why the skip is not *unconditionally* fatal

`checkout-api` CI clones **only that repo**, so the siblings are legitimately absent there. Making the skip always fatal would leave the verifier **permanently red in the very CI job that runs it** — which is precisely the expired-precondition bug **INC-11/INC-12 were raised to repair.** Re-committing it would be a regression. The honest answer is a third state: **`SKIP`** — reported, never passed, promotable to fatal by a caller that knows the siblings ought to be there.

---

## 6. Verification — every number below was produced by a command run this turn

### INC-15 verifier — **8/8, exit 0**

| Gate | Result |
|---|---|
| G1 repaired verifier passes **AND the cross-fleet gates actually RAN** | **9/9, exit 0**, executed `['G6a','G6b','G6c']` — was **6/6 with all three unreachable** |
| G2 the **DEPLOYED** verifier's discovery resolves both real repo names | both **FOUND** (drives the shipped `_find`/`_TARGET_DIRS`/`_GATEWAY_DIRS`, not a copy) |
| G3 legacy names still resolve (fix adds, never replaces) | pass, against a synthetic legacy layout |
| **G4 WITNESS A — pre-repair discovery is BLIND** | old logic finds **neither** sibling **despite both being present on the same filesystem** |
| **G5 WITNESS B — DIVERGENCE (load-bearing)** | **OLD → skips all 3 gates · NEW → executes all 3** |
| **G6 NEGATIVE CONTROL** | genuinely-absent siblings → **SKIPPED, exit 0**: never a silent pass, never permanently red |
| G7 strict mode refuses to pass un-run gates | bare checkout + `--require-cross-fleet` → **exit 1, FATAL** |
| G8 no production drift | 3/3 sources byte-identical on the **full sha256** |

**G5 is the whole argument.** It does not merely assert the new code works — it proves the **old code was blind on the same filesystem** where the new code succeeds. Had both behaved alike, the repair would be a no-op and G5 would say so.

### The gate caught its own author — and CI is what caught it

Worth stating plainly, because it changed the patch and it is the most useful thing that happened this run.

The first version of the CI step invoked the INC-15 verifier unconditionally. **GitHub Actions failed it immediately** ([run 29172630670](https://github.com/chrischabot/checkout-api/actions/runs/29172630670)):

```
[FAIL] G1 ... cross-fleet gates executed=[]
[FAIL] G2 ... fabric-ic-incident-target=MISSING fabric-gateway-demo=MISSING
[FAIL] G5 ... NEW -> executes []
FileNotFoundError: .../fabric-gateway-demo/service/usage_aggregator.py
```

G1/G2/G5 **structurally require the sibling fleet repos**, and `checkout-api` CI clones only its own repo. So the gate could never pass there: **a permanently-red gate — the exact INC-11 expired-precondition bug, committed inside the very incident that diagnoses it.** A gate that can never pass is as worthless as one that can never fail; both teach the team to ignore the red.

The repair, and it is the honest shape:

- **Sibling-dependent gates (G1/G2/G4/G5) report `SKIPPED`** where the siblings are absent — excluded from the numerator **and the denominator**, so a skip can never become a pass, and never a false failure either.
- **New `G2b`** — a *static* assertion that the shipped verifier still carries the real fleet repo names. It needs **no siblings**, so it guards the repair **inside CI**. This is what keeps the fix from rotting: strip a real name and G2b goes **RED on a bare checkout (4/5, exit 1)** — verified.
- **`G0`** — strict mode still hard-fails (exit 1, FATAL) where the siblings are expected but missing.
- **`G8`** hashes only the sources present; an absent file is a different environment, not drift.

**Proven in all three environments:**

| Environment | Result |
|---|---|
| Commander workspace (all 3 repos) | **9/9, exit 0** — cross-fleet gates execute |
| **Bare checkout (= `checkout-api` CI)** | **5/5 passed, 4 SKIPPED, exit 0** ✅ not permanently red |
| Bare checkout + `--require-cross-fleet` | **exit 1, FATAL** ✅ refuses to pass un-run gates |

**GitHub Actions on the final head (`470e64f`): all 8 steps green**, including the INC-15 step — [run 29172700665](https://github.com/chrischabot/checkout-api/actions/runs/29172700665). The gate does not merely pass locally; it passes *in the job that runs it*, and it still bites there.

### The gates provably bite (mutation test on the SHIPPED code)

A gate made *green* rather than *correct* is worthless, so the repaired discovery was regressed back to the blind names **inside the shipped verifier**:

| Tree | INC-15 result |
|---|---|
| repaired (as shipped) | **8/8, exit 0** |
| **shipped discovery regressed to blind names** | **5/8 — G1, G2, G5 FAIL** ✅ the gates catch it |
| restored | **8/8, exit 0** |

This also closes a real review finding: the gates now drive the **deployed** lookup function, so they would go red if the shipped discovery regressed — rather than validating a private copy of the fix and passing regardless.

### Cross-fleet re-confirmation — the 3 billing defects are STILL LIVE on `main`

Executed against deployed source, now that the gates finally run:

| Gate | Re-confirmation |
|---|---|
| **G6a** | **INC-6** — $300 order / one $10 eligible item → **charges $255.00**; item price ignored entirely |
| **G6b** | **INC-5** — one malformed record → `KeyError`, **whole `/v1/usage` batch dies** |
| **G6c** | **INC-8** — `{'model': None, 'tokens': 10}` → `{'per_model': {None: 10}, 'grand_total': 10}`, **no error raised** |

### Full fleet gate surface — green, and non-vacuous (real test counts)

| Repo / verifier | Result |
|---|---|
| `checkout-api` — `npm test` | **10 pass / 0 fail** |
| `checkout-api` — INC-9 verifier (strict) | **9/9, exit 0** |
| `checkout-api` — INC-12 verifier | **6/6, exit 0** |
| `checkout-api` — **INC-15 verifier (new)** | **8/8, exit 0** |
| `fabric-gateway-demo` — suite | **16 tests, OK** |
| `fabric-gateway-demo` — INC-5 / INC-8 / INC-10 verifiers | exit 0 · exit 0 · exit 0 |
| `fabric-ic-incident-target` — suite | **10 tests, OK** |
| `fabric-ic-incident-target` — checkout gate | **3/3, exit 0** |

**36 tests + 7 verifiers, zero failures.** All three production sources hash-match their deployed revisions.

---

## 7. Owner runbooks — the work only you can do

### INC-6 · checkout revenue leak → **Gateway / checkout revenue owner**

1. **Quantify exposure first.** Query completed orders where the **eligible-item count is low but the order subtotal is high** — those are the over-discounted ones. The leak is largest at `n=1`; start there. This determines whether this is a rounding nuisance or a material revenue problem. **The commander cannot do this — it requires order data.**
2. **Consider a temporary tier ceiling** (or disable volume discounts on orders with fewer than N eligible items). A config/feature-flag action that stops the bleeding without resolving the policy question.
3. **Do NOT "fix the average" by dividing by the whole cart's item count.** That silently changes which orders qualify for a discount. It looks like a bug fix and is not one.
4. **Answer the two questions** — the price field name, and the discount scope — and the guarded patch can be written and gated.

### INC-5/3 + INC-8 · `/v1/usage` billing → **Gateway / billing owner**

1. **Decide the malformed-record contract ONCE, covering BOTH cases:** a **missing** `model`/`tokens` key, *and* a **null-valued** one. Answering only the first leaves the second live in production.
2. **Check whether `None` buckets already exist** in any stored per-model aggregate or invoice breakdown since the 2026-07-11 deploy (PR #1, commit `e368005`). This tells you whether mis-attribution has already reached billing data.
3. **Fix the producer too.** A null model reaching the aggregator means an upstream emitter is already writing it. Patching only the consumer masks the source.
4. **Add a validation boundary at ingest** so the aggregator can assume well-formed input.
5. **Reconcile** any usage rows dropped or mis-attributed since that deploy.

### Process · **PR hygiene** — DONE this run

PRs **#11** and **#13** were duplicate diagnoses of INC-15 that never merged. Both are now **closed as superseded** by **#14**, which is CI-green against current `main`. Merging #14 lands the repair; leaving duplicates open is what invited each successive run to file another.

### Restore observability (blocks every future run)

The commander is currently **blind to production**. Provision a Sentry token, an OTEL endpoint, and a gateway log source. Until then, every run can only reason about code it can execute — and **no incident brief can bound customer impact.**

---

## 8. Coverage gap — stated once more, plainly

**No telemetry was reachable this run.** Sentry answered but unauthenticated; no OTEL collector on any standard port; no gateway logs on disk. Only the GitHub REST connector was live.

Every claim in this brief was established by **executing the deployed source**. The four incidents are real and live. **Their blast radius is UNKNOWN and deliberately not estimated** — §7 lists the owner queries that would bound it.

---

*Fabric autonomous incident commander · run 2026-07-11 · INC-15 patched & verified · INC-6 / INC-5 / INC-8 re-confirmed live and routed to owners.*
