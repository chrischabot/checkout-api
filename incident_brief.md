# Fabric — Autonomous Production Incident Commander
## Executive Incident Brief · INC-20

**Run:** 2026-07-12, ~01:20–01:45 UTC
**Date provenance:** system clock and the GitHub API `Date` header agree (`Sun, 12 Jul 2026 01:22:49 GMT`). Several earlier reports in this fleet are dated “2026-07-14” — that is a wrong date copied forward between runs and is contradicted by their own GitHub timestamps.
**Fleet:** `chrischabot/checkout-api` · `chrischabot/fabric-gateway-demo` · `chrischabot/fabric-ic-incident-target`

---

## 0. Bottom line

| | |
|---|---|
| **New incident found & patched this run** | **INC-20** — the production-drift gate **hard-failed the moment an owner landed the billing repair this commander has requested for five consecutive runs.** Repaired, verified 8/8, wired into CI. |
| **Production source changed** | **None.** All three deployed sources byte-identical on the full sha256. |
| **Still owner-blocked (re-confirmed LIVE by execution)** | **INC-6** (checkout discount leak), **INC-5/3** (`/v1/usage` batch death), **INC-8** (null-model billable bucket) |
| **Fleet check surface after this change** | **36 tests + 10 verifiers — zero failures** |
| **Telemetry reachable** | **None of Sentry / OTEL / gateway logs.** Every claim below was established by **executing the deployed source**. |
| **Blast radius** | **UNKNOWN and deliberately not estimated** — see §5. |

---

## 1. Telemetry provenance — measured this run, not copied forward

This is stated first because it bounds everything else in this brief.

| Source | Status this run | Evidence |
|---|---|---|
| **Sentry** | **UNAVAILABLE — no credential** | `sentry.io/api/0/` → **HTTP 200** but `{"version":"0","auth":null,"user":null}`; `/api/0/organizations/` → **401 “Authentication credentials were not provided.”** Egress works, so this is a **missing secret, not a network block**. **Zero issue data.** |
| **OTEL / traces** | **UNAVAILABLE — no collector** | ports **4317, 4318, 9411, 16686, 14268, 55681, 8126, 13133 all CLOSED** |
| **Gateway logs** | **UNAVAILABLE — no source** | no log directory on disk, no telemetry mounts |
| **GitHub (PR/deploy context)** | **LIVE** | authenticated REST connector: commits, PRs, workflow runs, issues |

**Consequence, stated plainly.** Three of the four requested sources are dark. A finding can therefore be **confirmed real and confirmed live** (by running the deployed code), but its **frequency in production cannot be measured by this commander**. No incident rate, customer count, or revenue figure in this brief is inferred from telemetry, because there is none to infer from.

---

## 2. Cross-source clustering — urgency-ranked

With telemetry dark, clustering is over **deployed-source behaviour + PR/deploy context** rather than over Sentry/OTEL symptoms.

### Cluster A — **Gates that punish the people fixing the bugs** (NEW · patched this run · P1 for trust)

One root cause, two red gates:

| Symptom | Source |
|---|---|
| `verify_inc15_cross_fleet_discovery.py` → **exit 1, `[FAIL] G8`** on a correctly-repaired tree | executed this run |
| `verify_inc19_layout_and_count_invariance.py` → **exit 1, `[FAIL] G1`** on the same tree | executed this run (its G1 merely re-runs the INC-15 verifier, so it **inherits** the failure) |

**Why they cluster:** both trace to a single predicate — G8 required every deployed source to be **byte-identical to a hardcoded sha256**. That encodes *“nobody has fixed the billing defects yet”* — a statement about **the calendar**, not about correctness.

### Cluster B — **Billing / revenue semantics** (owner decisions · re-confirmed LIVE · unchanged)

| ID | Behaviour re-confirmed by executing deployed source |
|---|---|
| **INC-6** | `$300` order with one `$10` eligible item → **charged `$255.00`**. A `$0.01` and a `$299.99` eligible item yield an **identical** charge — item price cannot influence the tier at all. |
| **INC-5/3** | one malformed record → **`KeyError`, the entire `/v1/usage` batch dies** |
| **INC-8** | `{"model": None, "tokens": 10}` → `{'per_model': {None: 10}, 'grand_total': 10}` — **10 billable tokens booked against a `None` key, no error raised** |

### Cluster C — **Fleet hygiene** (resolved before this run; verified, not assumed)

The stale search index showed PRs **#19 / #21 / #22** as open and mutually conflicting. **The authoritative API shows zero open PRs**: INC-19 merged (its verifier is on `main`, CI green on `e2c372b`), #19/#21 closed as superseded. **No action needed — and no new PR invented for a problem that no longer exists.**

---

## 3. Fixability decision

The rule this commander applies: **a defect is code-fixable only if the correct behaviour is derivable from an authoritative source. If choosing the fix means choosing a product policy, it goes to the owner.**

| Cluster | Decision | The deciding fact |
|---|---|---|
| **A — INC-20** | ✅ **CODE-FIXABLE → patched** | Purely mechanical. The gate's own stated purpose (*“the commander did not tamper with production”*) is a **process invariant**, expressible with zero product-policy content. No billing semantics are touched. |
| **B — INC-6 / INC-5 / INC-8** | ⛔ **NOT code-fixable → routed to owners** | Every candidate repair **encodes a different invoicing policy.** For INC-6 the deployed `apply_discount()` **never reads any item field** — it only calls `len()`. No caller, no schema, no test names a price field, and with telemetry dark there is **no authoritative source** for either the field name or the discount scope. Guessing `.get('price_cents', 0)` against a wrong key reads every item as **free** → picks the 0% tier → **charges $500.00 where the contract requires $425.00, and reports success.** Indexing instead throws `KeyError` **on the checkout path** — turning a silent revenue leak into a **hard outage**. |

> **The commander will not invent billing semantics.** Mispricing customers with no error signal is the *same class of failure as the bug itself*.

---

## 4. INC-20 — the incident, the repair, the proof

### The finding

`verify_inc15_cross_fleet_discovery.py` gate **G8 “NO PRODUCTION DRIFT”** asserted every deployed source matched a **frozen** sha256 baseline.

**Reproduced by execution.** I simulated the exact remediation this commander has escalated for five runs — an owner landing the correct INC-6 repair (tier from the eligible items' mean price). The repaired function then correctly charges **$300.00** on the order that currently leaks $45. On that **healthy, correctly-repaired** tree:

```
verify_inc15_cross_fleet_discovery.py        -> exit 1   [FAIL] G8   (8/9)
verify_inc19_layout_and_count_invariance.py  -> exit 1   [FAIL] G1   (6/7)
```

**The owner does precisely the thing we keep asking for, and CI goes hard RED on a repo where nothing is wrong.**

This is **INC-18's disease, surviving in the sibling gate INC-18 did not touch.** INC-18 cured it in `verify_inc9_ci_gate.py` (whose G6a/G6b/G6c asserted the defects were *still broken*) and left the identical frozen-baseline bug alive here. It is this fleet's signature failure on its **sixth** repetition:

| | The expired precondition |
|---|---|
| INC-11 | G3 asserted *“`ci.yml` is NEW”* — permanently false the instant it merged |
| INC-12 | required `ci.yml` byte-identical to `main` — forbade the repo from editing its own CI |
| INC-15 | cross-fleet gates were unreachable dead code; the skip was laundered into `6/6 passed` |
| INC-17 | the gate policing that laundering hardcoded the count it was policing (`== 6`) |
| INC-18 | the gates asserted the billing defects were **still broken** |
| **INC-20** | **the drift gate hard-fails the moment an owner REPAIRS one** |

> A gate that **punishes the remediation it exists to request** is worse than no gate at all. A gate that can never fail and a gate that can never pass teach the team the same lesson: **ignore the red.**

### The repair — assert the invariant, not the calendar

What G8 legitimately protects is the verifier's **own side effects**: it mutates files during mutation testing and must restore every one. So it now compares a **start-of-run snapshot** against the bytes on disk at the end:

| Condition | Verdict |
|---|---|
| bytes moved **during our own run** (verifier failed to restore what it mutated) | **FATAL — still bites** |
| differs from the historical baseline but **stable across our run** = an **owner edit** | **REPORTED as provenance, never fatal** |

The frozen hashes are retained **as provenance reference values only**. No new merge-time constant is introduced — re-committing that pattern is the bug being fixed.

### Verification — `verify_inc20_drift_gate_punishes_owner_fix.py`, **8/8, exit 0**

| Gate | Result |
|---|---|
| **G0 STATIC** (needs no siblings) | the shipped verifier carries the repair — **this is what guards it inside CI** |
| G1 no regression | repaired INC-15 still **9/9** on the untouched fleet |
| **G2 WITNESS A — necessity** | the **PRE-repair** predicate **REJECTS** a correct owner repair (anchored to the **frozen historical constant**, not to runtime bytes) |
| **G3 WITNESS B — sufficiency** | the **repaired** verifier **PASSES** on that same tree |
| **G4 DIVERGENCE (load-bearing)** | identical tree: **PRE = REJECT [RED] · POST = GREEN**, and INC-19 recovers with it — the repair is **not a no-op** |
| **G5 ANTI-WEAKENING** | a verifier that leaves production **MUTATED across its own run** is **STILL rejected (exit 1)** |
| **G6 no correctness hole** | the **broken** owner repair (wrong price key → charges **$500.00** where the contract requires **$425.00**) is **still rejected** by the INC-18 baseline contract |
| G7 no drift from this verifier | `checkout.py` restored byte-for-byte |

**G5 is the gate that matters most.** Simply **deleting** G8 would have turned the red gate green *and* satisfied G2/G3/G4 — and it **fails G5**. That is the difference between a **correction** and a **cover-up**.

### Not permanently red — the mistake this incident is about

| Environment | INC-20 |
|---|---|
| Full fleet workspace | **8/8, exit 0** |
| **Bare checkout** (= what `checkout-api` CI clones) | **1/1 passed, 6 SKIPPED, exit 0** — skips are in **neither the numerator nor the denominator** |
| **Bare checkout + repair reverted** (negative control) | **0/1, G0 RED, exit 1** ✅ **the gate genuinely bites in CI** |

The new step is green in the very job that runs it, and **cannot become the INC-11 permanently-red bug it diagnoses** — while stripping the repair still reddens CI.

---

## 5. Coverage gap — what this brief does NOT tell you

**Blast radius is UNKNOWN and deliberately not estimated.** With Sentry, OTEL and gateway logs all unreachable, the commander can prove these defects **are live** but cannot measure **how often they have fired**. The queries below need credentials only the owners have — and they are what determine urgency:

1. **INC-6 exposure.** Query completed orders where the **eligible-item count is low** but the **order subtotal is high** — those are the over-discounted ones. The leak is **largest at `n=1`**, so start there. This is the difference between a rounding error and a material revenue problem.
2. **INC-8 exposure.** Check whether **`None` buckets already exist** in any stored per-model aggregate or invoice breakdown since the 2026-07-11 deploy (PR #1, `e368005`). This tells you whether mis-attribution has already reached billing data.
3. **INC-5 exposure.** Count `/v1/usage` batches that died with `KeyError` — each one is a **whole batch of billable usage lost**.
4. **Wire a Sentry credential into the commander's environment.** Every run so far has been blind to production symptoms. This is the single highest-value fix to the incident-response loop itself: the commander is currently reasoning from source alone.

---

## 6. Owner routing & recovery runbooks

### INC-6 — checkout volume-discount leak → **`fabric-ic-incident-target#6`** · *storefront/checkout revenue owner*

**Two questions block the fix, and both are policy:**
1. **What is the per-item price field called?** (`price_cents`? `unit_price`? `amount`?) Nothing in the repo names it.
2. **What is the discount scope** — the eligible subtotal only, or the whole order? These produce materially different charges.

**Before the patch lands:** quantify exposure (§5.1). If material, consider a **temporary tier ceiling** or disabling volume discounts on orders with fewer than *N* eligible items — a **config/feature-flag action, which belongs to you, not the commander**. **Do not “fix the average” by dividing by the whole cart's item count** — that silently changes which orders qualify. It looks like a bug fix and is not one.

### INC-5 / INC-3 + INC-8 — `/v1/usage` malformed & null records → **`fabric-gateway-demo#2`, `#5`** · *gateway/billing owner*

**Decide the malformed-record contract ONCE, covering both cases** — a **missing** key *and* a **null-valued** key. They are the same contract from two sides: a repair guarding only *absent* keys (`.get("model", "unknown")`) passes a **`None` straight through**, because the key *is* present. **Fixing INC-5 without deciding the null case leaves INC-8 live.**

| Candidate | Billing consequence |
|---|---|
| reject the record (fail loud) | safest for invoice integrity; costs `/v1/usage` availability |
| skip + emit a metric | preserves availability, **under-bills silently** unless the alert is truly wired |
| attribute to `model="unknown"` | preserves totals, **mis-attributes spend**, hides the producer bug |
| leave as-is (**today**) | batch death (INC-5) + a `None` bucket polluting per-model billing (INC-8) |

Then: **fix the producer too** (a null model reaching the aggregator means an upstream emitter is already writing it), **add a validation boundary at ingest**, and **reconcile** affected rows.

**Good news for the owners:** as of this run, landing any of those repairs **no longer reddens the fleet's CI.** That was INC-20.

---

## 7. Verification ledger — this run

| Check | Result |
|---|---|
| `checkout-api` · `npm test` | **10 pass / 0 fail** |
| `checkout-api` · INC-9 / INC-12 / INC-15 / INC-18 / INC-19 / **INC-20** | **all exit 0** (INC-20 **8/8**) |
| `fabric-gateway-demo` · suite | **Ran 16 tests, OK** |
| `fabric-gateway-demo` · INC-5 / INC-8 / INC-10 verifiers | **all exit 0** |
| `fabric-ic-incident-target` · suite | **Ran 10 tests, OK** |
| `fabric-ic-incident-target` · checkout gate | **exit 0** |
| `py_compile` on all 10 verifiers | **OK** |
| **Total** | **36 tests + 10 verifiers, zero failures** |
| **Production drift** | **NONE** — asserted on the **full** sha256 (values below, untruncated) |
| &nbsp; | `session.js` = `b45a8eeceaa142dd70aea4182930d02edb2d23ce90f0f02527910abb5f18d7e8` |
| &nbsp; | `usage_aggregator.py` = `bb21e50f7b5dab4463b71984bbe86a5df2b6ba442ffeff84d9b70815781750e5` |
| &nbsp; | `checkout.py` = `da2a02fd87aec668467114e0bc30ff7c2fe7fd3d8f105f5f156361b9c87c5c5e` |
| Test assertions weakened | **none** |
| Dependencies added | **none** (stdlib Python + zero-dependency `node:test`) |

---

*Fabric autonomous incident commander · INC-20 · 2026-07-12.*
*Cluster A classified code-fixable (deterministic, no product-policy content) and patched. Cluster B remains with its owners: billing and revenue semantics, deliberately not guessed.*
