# Incident Brief — Fabric Autonomous Incident Commander

**Run:** INC-21 · **2026-07-12, ~01:52–02:20 UTC**

**Date provenance:** the system clock (`Sun Jul 12 01:52:25 UTC 2026`) and the GitHub API `Date` header (`Sun, 12 Jul 2026 01:52:10 GMT`) **agree**. Several earlier briefs in this fleet say "2026-07-14" — a wrong date copied forward between runs and contradicted by their own GitHub timestamps. Not repeated here.

**Fleet:** `chrischabot/checkout-api` · `chrischabot/fabric-gateway-demo` · `chrischabot/fabric-ic-incident-target`

---

## 1. Executive summary

One new incident found, classified **code-fixable**, patched, verified (7/7 gates) and shipped: **the fleet's own CI drift gate hard-failed the moment an owner repaired a billing defect.** It punished the exact remediation this commander has been escalating for six consecutive runs.

The three revenue-affecting billing defects remain **owner decisions** and are re-confirmed **LIVE** by executing the deployed source — deliberately not patched, because every candidate repair encodes a different invoicing policy.

| | |
|---|---|
| **Patched this run** | INC-21 — the drift gate punished an owner's repair (deterministic, no product-policy content) |
| **Still owner-blocked** | INC-6 (checkout revenue leak) · INC-5 (usage batch failure) · INC-8 (unattributed billing) |
| **Blast radius** | **UNKNOWN and deliberately NOT estimated** — no telemetry source was reachable (§2) |
| **Fleet check surface after the patch** | 36 tests + 10 verifiers, **zero failures** |

---

## 2. Telemetry provenance — MEASURED THIS RUN, not copied forward

**Only 1 of the 4 requested sources was reachable.** Stated plainly, because it bounds every claim in this brief.

| Source | Status this run | Evidence |
|---|---|---|
| **Sentry** | ❌ **UNREACHABLE — no credential** | `sentry.io/api/0/` → **HTTP 200** but `{"version":"0","auth":null,"user":null}`; `/api/0/organizations/` → **HTTP 401** `"Authentication credentials were not provided."` Egress works ⇒ **a missing secret, not a network block.** **Zero issue data.** |
| **OTEL traces** | ❌ **UNREACHABLE — no collector** | Ports **4317, 4318, 9411, 16686, 14268, 55681, 8126, 13133, 9090, 3200 — all CLOSED**. No OTEL/OTLP endpoint configured (env scanned: 34 vars, none matching otel/otlp/tempo/jaeger/honeycomb). |
| **Gateway logs** | ❌ **NO SOURCE** | Nothing on disk or mounted: `/var/log/gateway`, `/var/log/fabric`, `/logs`, `/data/logs`, `./logs` all absent. |
| **GitHub PR/deploy** | ✅ **LIVE (authenticated REST)** | PR/issue history, workflow runs and deploy context all read. GraphQL → **403** (scoped REST connector only). |

**Consequence — without euphemism:** every finding below was established by **executing the deployed source**, not by reading production telemetry. Findings are *confirmed real* and *confirmed live*. **Customer-facing blast radius is UNKNOWN and is NOT estimated** — the owner queries needed to bound it are in §5.

> **The single highest-value fix to the incident-response loop itself: wire a Sentry credential into the commander's environment.** Every run so far has been blind to production symptoms.

---

## 3. Symptom clustering (urgency-ranked)

With no telemetry, clustering was performed over the authoritative sources that *were* live: deployed-source behaviour + PR/deploy/CI history.

### Cluster A — Revenue integrity (owner-blocked · highest business urgency)

Three defects, one shared shape: **billing paths that silently produce wrong money with no error signal.** All three re-confirmed **LIVE** this run by execution:

| ID | Symptom (measured) |
|---|---|
| **INC-6** | A `$300` order with one `$10` eligible item is charged **`$255.00`** — a **`$45.00` leak**. A `$0.01` item and a `$299.99` item produce an **identical `$255.00` charge**: `apply_discount()` **never reads any item field**, only `len()`. |
| **INC-5** | One malformed usage record raises `KeyError('model')` → the **entire `/v1/usage` batch dies**. |
| **INC-8** | `{"model": None, "tokens": 10}` → `{'per_model': {'gpt-4': 100, None: 10}, 'grand_total': 110}` — **10 billable tokens booked against a `None` key, no error raised.** `grand_total` reconciles perfectly, so no reconciliation check sees anything wrong. |

### Cluster B — Verification integrity (code-fixable · patched this run)

The fleet's gates keep encoding **merge-time facts as permanent assertions**. INC-21 is the **seventh** repetition — and the most dangerous variant, because it points **outward, at the owners**.

| | The expired precondition |
|---|---|
| INC-11 | G3 asserted *"`ci.yml` is NEW"* — permanently false the instant it merged |
| INC-12 | required `ci.yml` byte-identical to `main` — forbade the repo from editing its own CI |
| INC-15 | the cross-fleet gates were unreachable dead code; the skip was laundered into `6/6 passed` |
| INC-17 | the gate policing that laundering hardcoded the count it was policing (`== 6`) |
| INC-18 | the gates asserted the billing defects were **still broken** |
| INC-19 | the witnesses depended on ambient clone-directory names |
| **INC-21** | **the drift gate hard-fails the moment an owner REPAIRS a billing defect** |

---

## 4. INC-21 — the incident patched this run

### Fixability decision: **CODE-FIXABLE**

A deterministic defect in verification tooling. **No product-policy content, no billing semantics, no production source touched.** Safe to patch autonomously.

### The finding

`verify_inc15_cross_fleet_discovery.py` gate **G8 ("NO PRODUCTION DRIFT")** required every deployed source to be byte-identical to a **hardcoded sha256 baseline**, and was **fatal** on any difference.

That is a **merge-time fact frozen into a permanent gate.** It encodes *"nobody has fixed the billing defects yet"* — a statement about **the calendar**, not about correctness.

### Reproduced by execution, BEFORE repairing it

I simulated the exact remediation this commander has escalated for six consecutive runs — an owner landing the correct INC-6 repair, choosing the discount tier from the eligible items' mean price:

```python
avg_cents = sum(i["price_cents"] for i in eligible_items) / n
```

That repair is genuinely **correct**: the `$300` order goes from a leaking **`$255.00`** to the contractual **`$300.00`**, and a 5 × `$100` order prices correctly at **`$425.00`** (avg `$100` → 15% tier). On that healthy, correctly-repaired tree:

| Verifier | Result |
|---|---|
| `verify_inc15_cross_fleet_discovery.py` | **exit 1 — `[FAIL] G8`** (8/9) |
| `verify_inc19_layout_and_count_invariance.py` | **exit 1 — `[FAIL] G1`** (6/7) |
| `verify_inc9_ci_gate.py` | exit 0 (already immunized by INC-18) |
| `verify_inc18_gate_punishes_remediation.py` | exit 0 |

**One root cause, two red gates** — INC-19's `G1` merely re-runs the INC-15 verifier, so it inherits the failure.

**The owner does precisely the thing we keep asking for, and CI goes hard RED on a repo where nothing is wrong.** INC-18 diagnosed this disease and cured it in `verify_inc9_ci_gate.py` — leaving the identical frozen-baseline bug alive in the sibling gate.

> A gate that **punishes the remediation it exists to request** is worse than no gate at all. A gate that can never fail and a gate that can never pass teach the team the same lesson: **ignore the red.**

### The repair — assert the invariant, not the calendar

What G8 legitimately protects is the verifier's **own side effects**: it mutates files during mutation testing and must restore every one. That is a property of **this process**, not of the fleet's bug backlog. G8 now compares a **start-of-run snapshot** against the bytes on disk at the end:

| Condition | Verdict |
|---|---|
| bytes moved **during our own run** (the verifier failed to restore what it mutated) | **FATAL — still bites** |
| differs from the historical baseline but **stable across our run** = an **owner edit** | **REPORTED as provenance, never fatal** |

The frozen hashes are kept as **provenance reference values only**. No new merge-time constant is introduced — re-committing that pattern is the very bug being fixed.

### Verification — `verify_inc21_drift_gate_punishes_owner_fix.py`, **7/7, exit 0**

| Gate | Result |
|---|---|
| **G0 STATIC** (no siblings needed) | the shipped INC-15 verifier carries the repair — **this is what guards it inside CI** |
| G1 no regression | the repaired verifier is still **9/9** on an untouched fleet |
| **G2 WITNESS A (necessity)** | the **PRE-repair** predicate **REJECTS** a correct owner repair |
| **G3 WITNESS B (sufficiency)** | the **repaired** verifier **PASSES** on that same tree, reporting the owner's edit as provenance |
| **G4 DIVERGENCE (load-bearing)** | identical tree: **PRE = REJECT [RED] · POST = GREEN**, and INC-19 recovers with it — the repair is **not a no-op** |
| **G5 ANTI-WEAKENING** | a verifier that leaves production **MUTATED across its own run** is **STILL rejected, exit 1** |
| G6 no drift from this verifier | 3/3 sources byte-identical before/after; all mutation testing in throwaway copies |

**G5 is the gate that matters most.** Simply **deleting** G8 would have turned the red gate green *and* satisfied G2/G3/G4 — and it **fails G5**. That is the difference between a **correction** and a **cover-up**. This is not a relaxation.

**On G2's soundness:** the necessity witness is anchored to the **frozen historical baseline constant** (parsed from the verifier's own `BASELINES` dict), *not* to the bytes present when the verifier starts. Anchoring to runtime bytes would be confounded on a tree where the owner fix had already landed — the "baseline" would silently become the repaired file, the old predicate would appear to accept it, and the gate would prove nothing.

### Not permanently red — the mistake this incident is about

| Environment | INC-21 |
|---|---|
| Full fleet workspace | **7/7, exit 0** |
| **Bare checkout** (= what `checkout-api` CI clones) | **1/1 passed, 6 SKIPPED, exit 0** — skips are in **neither the numerator nor the denominator** |
| **Bare checkout + the repair reverted** (negative control) | **0/1, G0 RED, exit 1** ✅ |

The new step is green in the very job that runs it, so it **cannot become the INC-11 permanently-red bug it diagnoses** — while stripping the repair still reddens CI. That negative control is the point: the gate is made **correct**, not merely **green**.

---

## 5. Fleet check surface — re-run after the patch

| Repo | Result |
|---|---|
| `checkout-api` | `npm test` **10 pass / 0 fail** · INC-9 **9/9** · INC-12 **6/6** · INC-15 **9/9** · INC-18 **6/6** · INC-19 **7/7** · **INC-21 7/7** — all exit 0 |
| `fabric-gateway-demo` | **Ran 16 tests, OK** · INC-5 / INC-8 / INC-10 verifiers exit 0 |
| `fabric-ic-incident-target` | **Ran 10 tests, OK** · checkout gate exit 0 |

**36 tests + 10 verifiers, zero failures.** `py_compile` clean on all verifiers. `ci.yml` parses as valid YAML and carries all 7 steps, including the new INC-21 gate.

**No production drift:** `session.js` `b45a8eeceaa1…` · `usage_aggregator.py` `bb21e50f7b5d…` · `checkout.py` `da2a02fd87ae…` — byte-identical on the **full** sha256. No test assertion weakened. No dependency added.

---

## 6. Routed to owners — NOT patched, deliberately

These are **billing and revenue semantics**, where every candidate repair encodes a different invoicing policy. **The commander will not invent billing semantics.**

### INC-6 — checkout discount leak · `fabric-ic-incident-target#6`

Re-confirmed live: a **`$45.00` leak** on a single `$300` order; a `$0.01` and a `$299.99` eligible item are charged **identically**, because `apply_discount()` only calls `len()`.

**Why not auto-patched:** the deployed function never reads any item field, so a correct per-item average is not computable without **inventing a schema**. The repo's own tests prove the tempting repairs are unsafe:

- `.get('price_cents', 0)` against a wrong key reads **every item as free**, selects the 0% tier, **charges `$500.00` instead of `$425.00` and reports success** — a silent misprice, forever.
- indexing instead throws `KeyError` **on the checkout path** — turning a silent revenue leak into a **hard outage**.

**Owner decision required:** the price-field name, and whether the tier is chosen from the eligible items' mean or the whole-order mean.

### INC-5 / INC-8 — usage aggregation · `fabric-gateway-demo#2`, `#5`

These are **the same contract from two sides.** A repair guarding only *absent* keys (`.get("model", "unknown")`) passes a **`None` value straight through**, because the key *is* present — **so fixing INC-5 without deciding the null case leaves INC-8 live.**

**Owner decision required:** reject-loudly / skip / attribute-to-`unknown`, for **both** the absent-key and the null-value case.

### Recovery runbook — the queries needed to bound blast radius

Blast radius **cannot** be estimated from source alone. To bound it, owners should run:

1. **INC-6:** sum `subtotal_cents - charged_cents` over all orders with ≥ 1 eligible item since the defect shipped. The per-order leak is a function of the item-count-vs-price divergence, so it **cannot** be inferred without order data.
2. **INC-5:** count `KeyError` / 5xx responses on `POST /v1/usage`. Each one is a **whole batch of billable usage destroyed**, not a single record.
3. **INC-8:** count usage records where `model IS NULL` and sum their `tokens`. That sum is **billable revenue attributed to no customer-nameable model** — serialize the bucket and the key becomes the JSON string `"null"`, which no invoice line can rate.
4. **Wire a Sentry credential** into the commander's environment, so the next run can answer 1–3 automatically instead of reporting UNKNOWN.

---

## 7. Verification gates — provenance metadata

| Property | Value |
|---|---|
| Authoritative sources used | deployed source (executed) + GitHub REST (PR/deploy/CI) |
| Sources unavailable | Sentry (401, no credential) · OTEL (10 ports closed) · gateway logs (no source) · GraphQL (403) |
| Production source changed | **none** — 3/3 byte-identical on the full sha256 |
| Test assertions weakened | **none** |
| Dependencies added | **none** (stdlib Python + zero-dependency `node:test`) |
| Double-witness | necessity (G2) + sufficiency (G3) + divergence (G4) |
| Anti-weakening control | **G5** — a cover-up (deleting G8) fails this gate |
| Not-permanently-red control | bare checkout **exit 0**; repair reverted → **exit 1** |
| Blast radius | **UNKNOWN — deliberately not estimated** (§6 runbook) |

---

*Fabric autonomous incident commander · INC-21 · classified code-fixable (deterministic, no product-policy content).*
