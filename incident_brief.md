# Fabric — Autonomous Production Incident Commander
## Executive Incident Brief · Run INC-22

**Run window:** 2026-07-12 ~02:38–03:05 UTC
**Clock provenance:** system clock and the GitHub API `Date` header agree (`Sun, 12 Jul 2026 02:38:30 GMT`). Several earlier briefs in this fleet say *"2026-07-14"* — a wrong date copied forward between runs and contradicted by their own GitHub timestamps. Corrected here.
**Fleet:** `chrischabot/checkout-api` · `chrischabot/fabric-gateway-demo` · `chrischabot/fabric-ic-incident-target`
**Classification:** 1 code-fixable defect patched and verified · 3 incidents remain owner-blocked (billing policy) · 0 fabricated findings

---

## 1. Headline

**The fleet's CI was rigged to punish the owners for fixing the bug we keep asking them to fix.**

`verify_inc15_cross_fleet_discovery.py` gate **G8 ("NO PRODUCTION DRIFT")** required every deployed source to be byte-identical to a **hardcoded sha256 baseline**, and was **fatal** on any difference. That constant encodes *"nobody has fixed the billing defects yet"* — **a statement about the calendar, not about correctness.**

This commander has escalated the INC-6 discount leak to its owner for **seven consecutive runs**. The instant that owner complies, G8 goes hard red — and INC-19's G1 reddens with it, because it re-runs the INC-15 verifier.

> **Reproduced by execution, before repairing anything.** I landed the exact remediation we have been requesting (tier chosen from the eligible items' mean price). It is genuinely correct: the `$300` order with one `$10` eligible item goes from a leaking **`$255.00`** to the contractual **`$300.00`**, and 5 × `$100` prices correctly at **`$425.00`**. On that healthy, correctly-repaired tree:
>
> | Verifier | Result |
> |---|---|
> | `verify_inc15_cross_fleet_discovery.py` | **exit 1 — `[FAIL] G8`** (8/9) |
> | `verify_inc19_layout_and_count_invariance.py` | **exit 1 — `[FAIL] G1`** (6/7) |
>
> **The owner does precisely the thing we keep asking for, and CI goes hard RED on a repo where nothing is wrong.**

A gate that **punishes the remediation it exists to request** is worse than no gate at all. A gate that can never fail and a gate that can never pass teach the team the same lesson: **ignore the red.**

**INC-21 (PR #24) diagnosed this and never merged — so the defect was still live on `main` when this run began.** It is now fixed and guarded in CI.

---

## 2. Telemetry provenance — MEASURED THIS RUN, not copied forward

This is the section that determines how much of this brief you can trust. **Three of the four requested sources do not exist in this environment.** I measured each one rather than inheriting a previous run's claim.

| Source | Status | Evidence |
|---|---|---|
| **Sentry** | ❌ **NO DATA** | `GET sentry.io/api/0/` → **HTTP 200**, body `{"version":"0","auth":null,"user":null}`. `GET /api/0/organizations/` → **HTTP 401** `"Authentication credentials were not provided."` **0 `SENTRY*` env vars.** Egress works ⇒ this is a **missing secret, not a network block.** |
| **OTEL / OTLP traces** | ❌ **NO COLLECTOR** | ports **4317, 4318, 9411, 16686, 14268, 55681, 8126, 13133, 9090, 3200 — all CLOSED.** **0 `OTEL*`/`OTLP*` env vars.** |
| **Gateway logs** | ❌ **NO SOURCE** | `/var/log/gateway`, `/var/log/fabric`, `/var/log/nginx`, `./logs`, `./gateway-logs`, `/mnt/logs` — **all ABSENT.** No `*.log` anywhere on disk or mounted. |
| **GitHub PR / deploy context** | ✅ **LIVE** | REST connector authenticated. All PR, issue, branch and deploy context in this brief came from here. GraphQL → **403**. |

### What this means, stated plainly

**Every defect claim in this brief was established by EXECUTING THE DEPLOYED SOURCE — not by reading production telemetry.** The findings are therefore **confirmed real and confirmed live**.

But: **blast radius is UNKNOWN and deliberately NOT estimated.** I cannot tell you how many orders were over-discounted, how many `/v1/usage` batches died, or how many tokens were mis-attributed. Anyone who gives you those numbers from this environment is guessing. The queries that would bound it are in §7 — they are the steps only you can run.

> ### The single highest-value fix to the incident-response loop itself
> **Wire a Sentry credential into the commander's environment.** Every run in this fleet's history has been blind to production symptoms. We are diagnosing by reading and executing code, which finds *real* defects but can never tell you which ones are *actually hurting customers right now*. That prioritisation is exactly what telemetry buys, and it is the one thing we do not have.

---

## 3. Symptom clusters (urgency-ranked)

With no telemetry, clustering is over **code-behaviour symptoms + PR/deploy context**, not over error volumes.

### Cluster A — “the guardrails point the wrong way” · **PATCHED THIS RUN**
A family of gates that assert **merge-time facts** instead of invariants. Eighth repetition of this fleet's signature failure:

| | The expired precondition |
|---|---|
| INC-11 | G3 asserted *"`ci.yml` is NEW"* — permanently false the instant it merged |
| INC-12 | required `ci.yml` byte-identical to `main` — forbade the repo from editing its own CI |
| INC-15 | the cross-fleet gates were unreachable dead code; the skip was laundered into `6/6 passed` |
| INC-17 | the gate policing that laundering hardcoded the count it was policing (`== 6`) |
| INC-18 | the gates asserted the billing defects were **still broken** |
| INC-19 | the witnesses depended on ambient clone-directory names |
| INC-20/21 | diagnosed the drift gate — **never merged** |
| **INC-22** | **the drift gate hard-fails the moment an owner REPAIRS a billing defect** |

**Root cause of the family:** the gates encode *when they were written* rather than *what must always be true*. **Fixability: CODE-FIXABLE** — deterministic, no product-policy content. **Patched and verified below.**

### Cluster B — “billing semantics are undecided” · **OWNER-BLOCKED (3 incidents)**
All three re-confirmed **LIVE** this run by executing the deployed source:

| Incident | Deployed behaviour (executed, this run) | Where |
|---|---|---|
| **INC-6** | `$300` order, one `$10` eligible item → **charged `$255.00`** (a **`$45.00` leak**). `apply_discount()` **never reads any item field** — only `len()`. A `$0.01` and a `$299.99` eligible item produce an **identical** charge. | `fabric-ic-incident-target#6` |
| **INC-5 / INC-3** | one malformed record → **`KeyError('model')`**, the **whole `/v1/usage` batch dies** | `fabric-gateway-demo#2` |
| **INC-8** | `{"model": None, "tokens": 10}` → `{'per_model': {None: 10}, 'grand_total': 10}` — **10 billable tokens booked to a `None` key, no error raised**, and `grand_total` still reconciles perfectly | `fabric-gateway-demo#5` |

**Fixability: NOT CODE-FIXABLE — requires an owner decision.** See §5.

---

## 4. The patch (Cluster A) — assert the invariant, not the calendar

**Scope: verifier + CI + brief only. NO production change.** All three deployed sources are byte-identical on the **full** sha256, re-measured after every gate ran:

```
session.js           b45a8eeceaa1…   unchanged
usage_aggregator.py  bb21e50f7b5d…   unchanged
checkout.py          da2a02fd87ae…   unchanged
```

What G8 **legitimately** protects is the verifier's **own side effects**: it mutates files during mutation testing and must restore every one. That is a property of **this process**, not of the fleet's bug backlog. So G8 now compares a **start-of-run snapshot** against the bytes on disk at the end:

| Condition | Verdict |
|---|---|
| bytes moved **during our own run** (the verifier failed to restore what it mutated) | **FATAL — still bites** |
| differs from the historical reference but **stable across our run** = an **owner edit** | **REPORTED as provenance, never fatal** |

The frozen hashes are retained **as provenance reference values only**. No new merge-time constant is introduced — re-committing that pattern *is* the bug being fixed.

---

## 5. Why INC-6 / INC-5 / INC-8 are STILL not patched — deliberately

These are **billing and revenue semantics**, where **every candidate repair encodes a different invoicing policy**. Guessing wrong mischarges real customers **with no error signal** — the same class of failure as the bug itself.

**INC-6.** A correct fix must know each eligible item's **price field name** and the **discount scope**. The deployed `apply_discount()` never reads any item field, and no caller, schema, or test in the repo names one. The repo's own tests prove the tempting repairs unsafe:
- `.get('price_cents', 0)` against a **wrong key** reads every item as free → selects the 0% tier → **charges `$500.00` where the contract requires `$425.00`, and reports success**
- indexing instead throws **`KeyError` on the checkout path** — trading a silent revenue leak for a **hard outage**

**INC-5 + INC-8 are the same contract from two sides.** A repair guarding only *absent* keys (`.get("model", "unknown")`) passes a **`None` value straight through**, because the key *is* present. **Fixing INC-5 without deciding the null case leaves INC-8 live in production.**

| Candidate repair | Billing consequence |
|---|---|
| reject the record (fail loud) | safest for invoice integrity; costs `/v1/usage` availability |
| skip it + emit a metric | preserves availability, **under-bills silently** unless the alert is truly wired |
| attribute to `model="unknown"` | preserves totals, **mis-attributes spend**, hides the producer bug |
| leave as-is (**today's behaviour**) | a `None` bucket silently pollutes per-model billing |

**The commander will not invent billing semantics.** This run makes the *guardrails* correct; it does not pre-empt the owner. **And as of this patch, landing any of those repairs no longer reddens the fleet's CI — which was the entire point.**

---

## 6. Verification gates

### `verify_inc22_drift_gate_punishes_owner_fix.py` — **7/7, exit 0**

| Gate | Result |
|---|---|
| **G0 STATIC** (no siblings needed) | the shipped INC-15 verifier carries the repair — **this is what guards it inside CI** |
| G1 NO REGRESSION | the repaired verifier is still **9/9** on an untouched fleet |
| **G2 WITNESS A (necessity)** | owner repair verified **CORRECT by execution** (`$300.00` / `$425.00` / zero-item guard holds) — and the **PRE-repair frozen-baseline predicate REJECTS it** |
| **G3 WITNESS B (sufficiency)** | the **repaired** verifier **PASSES** on that same tree, surfacing the owner's edit as provenance |
| **G4 DIVERGENCE (load-bearing)** | identical tree: **PRE = REJECT [RED] · POST = GREEN**, and INC-19 recovers with it — the repair is **not a no-op** |
| **G5 ANTI-WEAKENING** | a verifier that leaves production **MUTATED across its own run** is **STILL rejected, exit 1** |
| G6 NO DRIFT | 3/3 sources byte-identical on the full sha256; all mutation testing in throwaway copies |

**G5 is the gate that matters most.** Simply **deleting** G8 would also have turned the red gate green *and* satisfied G2/G3/G4 — and it **fails G5**. That is the difference between a **correction** and a **cover-up**. This is not a relaxation.

**On G2's soundness:** the necessity witness is anchored to the **frozen historical constant** (parsed from the verifier's own reference dict), *not* to the bytes present when the verifier starts. Anchoring to runtime bytes would be confounded on a tree where the owner's fix had already landed — the "baseline" would silently become the repaired file, the old predicate would appear to accept it, and the gate would prove nothing.

### Not permanently red — the mistake this incident is about

| Environment | INC-22 |
|---|---|
| Full fleet workspace | **7/7, exit 0** |
| **Bare checkout** (= what `checkout-api` CI clones) | **1/1 passed, 6 SKIPPED, exit 0** — skips are in **neither the numerator nor the denominator** |
| **Bare checkout + the repair reverted** (negative control) | **0/1, `[FAIL] G0`, exit 1** ✅ |

The new step is green in the very job that runs it, so it **cannot become the INC-11 permanently-red bug it diagnoses** — while **stripping the repair still reddens CI.** That negative control is the point: the gate is made **correct**, not merely **green**.

### Full fleet check surface, re-run after the change

| Repo | Result |
|---|---|
| `checkout-api` | `npm test` **10 pass / 0 fail** · INC-9 **9/9** · INC-12 **6/6** · INC-15 **9/9** · INC-18 **6/6** · INC-19 **7/7** · **INC-22 7/7** — all exit 0 |
| `fabric-gateway-demo` | **Ran 16 tests, OK** · INC-5 / INC-8 / INC-10 verifiers all exit 0 |
| `fabric-ic-incident-target` | **Ran 10 tests, OK** · checkout gate exit 0 |

**36 tests + 10 verifiers, zero failures.** `py_compile` clean on all verifiers. `ci.yml` parses as valid YAML and carries all **7** steps, including the new INC-22 gate.

---

## 7. What we need from the owners

### Billing / revenue owner — `checkout.py` (INC-6)
1. **What is the per-item price field called?** (`price_cents`? `unit_price`? `amount`?) Guessing crashes checkout or misprices silently.
2. **What is the discount scope** — the eligible subtotal only, or the whole order? These produce materially different charges.
3. **Quantify exposure (only you can do this):** query completed orders where the eligible-item count is **low** but the order subtotal is **high**. The leak is largest at `n=1`. This tells you whether this is a rounding error or a material revenue problem.
4. **Interim mitigation:** if exposure is material, cap the tier or disable volume discounts on orders with fewer than *N* eligible items. That is a **config/feature-flag action** — it belongs to you, not to the commander.
5. ⚠️ **Do NOT "fix the average" by dividing by the whole cart's item count.** It silently changes which orders qualify. It looks like a bug fix and is not one.

### Gateway / billing owner — `/v1/usage` (INC-5 + INC-8)
1. **Decide the malformed-record contract ONCE, covering BOTH cases:** a **missing** `model`/`tokens` key **and** a **null-valued** one. Answering only the first leaves INC-8 live in production.
2. **Check whether `None` buckets already exist** in any stored per-model aggregate or invoice breakdown since the 2026-07-11 deploy (PR #1, commit `e368005`).
3. **Fix the producer too.** A malformed/null record reaching the aggregator means an upstream emitter is *already* writing bad rows. Patching only the consumer masks the source.
4. **Add a validation boundary at ingest**, then **reconcile** any usage rows dropped or mis-attributed since that deploy.

### Platform / observability owner
**Provision a Sentry credential (and an OTEL endpoint) for the incident commander.** Until then every run is blind to production symptoms and cannot rank incidents by real customer impact.

---

## 8. Fleet PR hygiene

`checkout-api` **#24 (INC-21)** is open and diagnoses this same G8 defect, but its branch is cut from a pre-INC-19 base. This run's patch lands the repair against **current `main`** with a stronger anti-weakening witness (G5) and a CI step. **Recommend closing #24 as superseded.**

Open owner-decision issues that remain correct and should stay open: `fabric-ic-incident-target#6` (INC-6), `fabric-gateway-demo#2` (INC-5/3), `fabric-gateway-demo#5` (INC-8).

---

## 9. Honest limitations of this run

- **No production telemetry was available.** Findings are code-true and execution-verified; **customer impact is unmeasured.**
- **Blast radius is deliberately not estimated.** No order counts, no revenue figures, no affected-user numbers — the data to compute them does not exist in this environment.
- **No new application defect was found in the deployed sources.** The fleet's suites and gates were green on arrival, and I did not manufacture a finding to have something to report. The real defect this run was in **the guardrails themselves**, and it was only visible by simulating an owner's fix.

---

*Fabric autonomous incident commander · INC-22 · 2026-07-12 · 1 patch (code-fixable, deterministic, no product-policy content) · 3 incidents routed to owners with runbooks.*
