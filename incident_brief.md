# Executive Incident Brief — Fabric Autonomous Incident Commander

**Run:** INC-18 · **2026-07-12 ~00:40 UTC**
**Date provenance:** taken from the GitHub API `Date` response header this run. Several earlier reports in this fleet are stamped "2026-07-14" — that date is **wrong**, contradicted by their own GitHub timestamps, and is not copied forward here.

**Fleet:** `chrischabot/checkout-api` · `chrischabot/fabric-gateway-demo` · `chrischabot/fabric-ic-incident-target`

**This run shipped:** one **verifier + CI + brief** change. **No production source was modified.** All three deployed sources are byte-identical to their deployed revisions on the **full** sha256 (see §6).

---

## 1. Headline

> **The gate that polices this fleet's billing defects was asserting that those defects were STILL BROKEN.** The moment an owner lands the INC-6 / INC-5 / INC-8 repair the commander has escalated for **four consecutive runs**, `checkout-api` CI would have gone **hard RED on a healthy repo** — punishing the owners for doing exactly what we asked. That is patched, double-witness verified, and wired into CI.

Nothing else in the fleet regressed. Every suite and every verifier is green on current `main`.

---

## 2. ⚠️ Telemetry coverage gap — read this before trusting any severity number

**No telemetry plane was reachable this run.** Every one was attempted:

| Plane | Attempted | Result |
|---|---|---|
| **Sentry issues** | `GET sentry.io/api/0/` | **HTTP 200** → `{"version":"0","auth":null,"user":null}` — no credential |
| | `GET sentry.io/api/0/organizations/` | **HTTP 401** — "Authentication credentials were not provided." |
| **OTEL / traces** | TCP 4317, 4318, 9411, 16686, 14268, 55681, 8126 | **ALL CLOSED** (no OTLP / Jaeger / Zipkin / Datadog collector) |
| **Gateway logs** | filesystem + mount scan | **No source.** No gateway or access log on disk or mounted |
| **PR / deploy context** | GitHub REST (connector) | ✅ **LIVE** — the only plane available (GraphQL quota: **0/0**) |

**Consequence, stated plainly:** every behavioural claim below was established by **EXECUTING THE DEPLOYED SOURCE**, not by reading production telemetry. Findings are therefore *confirmed real and confirmed live* — but **blast radius (how many customers, orders, or invoices are affected) is UNKNOWN to the commander and is deliberately NOT estimated.** The owner queries needed to bound it are in §7. That step is the one only you can perform, and it is what determines true urgency.

---

## 3. Clustered incidents, urgency-ranked

Symptoms are grouped by **root cause**, not by repository — the same defect class recurs across services.

### Cluster A — Revenue / billing correctness · **OWNER DECISION REQUIRED** · not auto-patchable

| # | Service | Defect (re-confirmed LIVE this run by executing deployed source) | Impact |
|---|---|---|---|
| **INC-6** | `fabric-ic-incident-target` | `apply_discount()` picks the tier from `subtotal / eligible_count`. A **$300 order with one $10 eligible item charges $255.00** — the contract requires $300.00. The item's price is **never read**; the function only calls `len()`. | **Silent revenue leak.** Worst on precisely the orders that deserve *no* discount; the leak scales inversely with eligible-item count. |
| **INC-5** | `fabric-gateway-demo` | One malformed usage record → `KeyError` → **the entire `/v1/usage` batch dies.** | Loud availability loss on a billing path. |
| **INC-8** | `fabric-gateway-demo` | `{'model': None, 'tokens': 10}` → `{'per_model': {None: 10}}` — **no error raised.** 10 billable tokens booked against a `None` key. | **Silent** invoice mis-attribution. |

**Why the commander refuses to patch these — a decision, not an omission.**

Every candidate repair **encodes a different invoicing policy**, and the repo contains no schema, caller, or test that names the authoritative one:

- **INC-6** needs two answers the code cannot supply: *(i)* what is the per-item price field called? *(ii)* does the discount apply to the **eligible subtotal** or the **whole order**? The repo's own tests prove the tempting fixes are unsafe — `.get('price_cents', 0)` against a wrong key reads every item as **free**, selects the 0% tier, **charges $500.00 instead of $425.00 and reports success**; indexing instead throws `KeyError` on the checkout path, converting a silent leak into a **hard outage**.
- **INC-5 and INC-8 are one decision, not two.** A repair guarding only the *absent* key passes a `None` value **straight through** — because the key *is* present. **Fixing INC-5 without deciding the null case leaves INC-8 live in production.**

Guessing wrong here **corrupts customer invoices with no error signal** — the same class of failure as the bug itself. The commander will not invent billing semantics. **Routed to owners** (§7).

### Cluster B — Verification integrity: gates that cannot do their job · **AUTO-PATCHED THIS RUN**

This fleet has a signature failure mode, now on its **fifth** repetition: **an expired precondition — a merge-time fact frozen into a permanent gate.**

| | The expired precondition |
|---|---|
| INC-11 | G3 asserted *"`ci.yml` is NEW"* — permanently false the instant it merged |
| INC-12 | G3 required `ci.yml` byte-identical to `main` — forbade the repo from editing its own CI |
| INC-15 | the cross-fleet gates were unreachable dead code; the skip was laundered into `6/6 passed` |
| INC-17 | the gate policing that laundering **hardcoded the very count it was policing** (`== 6`) |
| **INC-18 (this run)** | **the cross-fleet gates asserted the billing defects were STILL BROKEN — so they would hard-fail the moment an owner FIXED them** |

**INC-18, precisely.** `artifacts/incident/verify_inc9_ci_gate.py` gates G6a/G6b/G6c were:

```python
leak == 25_500 and price_blind   # G6a — "INC-6 is still leaking"
batch_died                       # G6b — "INC-5 still kills the batch"  (caught ONLY KeyError)
silent_null                      # G6c — "INC-8 is still silent"
```

Each is a statement about **the calendar**, not about correctness. They hold only for as long as nobody fixes the billing defects. And there was a second, sharper edge: **G6b caught only `KeyError`** — so an owner choosing the *reject-loudly* policy (raising a custom `ValidationError`, the option **safest for invoice integrity**) would have sent an uncaught exception through the verifier and **crashed it outright**.

> **A gate that punishes the remediation it exists to request is worse than no gate at all.** A gate that can never fail and a gate that can never pass teach the team the same lesson: *ignore the red.* That is the disease this fleet exists to cure.

**The repair — assert the invariant, not the calendar.** Defect liveness is now **REPORTED** as provenance (`STILL LIVE` / `REPAIRED UPSTREAM`); what is **ENFORCED** is the **policy-free baseline contract**: *well-formed input must still price and aggregate correctly.* Every candidate owner policy satisfies that baseline — so it can never punish a correct fix — while the tempting **broken** repairs fail it. This also keeps the commander from smuggling in the billing semantics it has repeatedly refused to invent.

---

## 4. Verification gates — INC-18 verifier: **6/6, exit 0**

`artifacts/incident/verify_inc18_gate_punishes_remediation.py` ships here and **runs in CI**.

| Gate | Result |
|---|---|
| G1 repaired INC-9 verifier passes as shipped | **fleet workspace 9/9, exit 0** · **bare checkout 6/6 (cross-fleet SKIPPED), exit 0** |
| **G2 WITNESS A — the PRE-REPAIR predicates are broken by a correct owner fix** | witness anchored to a **frozen snapshot of the defect** (never to today's production): on a correctly repaired fleet the OLD predicates go `{G6a: False, G6b: False, G6c: False}` **and G6b crashes with `UsageRecordError`** → the old gates hard-fail a healthy repo |
| **G3 WITNESS B — DIVERGENCE (load-bearing)** | same tree, one correct owner repair: **OLD G6 → REJECT** · **NEW G6 → PASS** |
| **G4 ANTI-WEAKENING** | the broken INC-6 repair **charges $500.00 where the contract requires $425.00 → still REJECTED**; the broken INC-5/8 repair books `grand_total: 0` → **still REJECTED** |
| G5 the INC-5 probe cannot CRASH the verifier | a reject-loudly repair raising `UsageRecordError` **escapes the OLD handler** (`escapes_old_handler=True`); the repaired probe catches it, reports the state, and the baseline still passes |
| **G6 PROVENANCE ONLY — never fatal** | source drift vs the run baseline is **reported, never enforced** (see the self-correction below) |

**G3 is the whole argument.** It does not merely assert that the new predicate works — it proves the **old predicate hard-failed a healthy repo on the same tree** where the new one passes. Had both behaved alike, the repair would be a no-op, and G3 would say so.

**G4 is the gate that matters most.** Making a red gate green is trivial and worthless. G4 feeds the new baseline the *tempting broken repairs* — the ones a hurried engineer would actually write — and requires it to **still go red**. It does. **This is a correction, not a relaxation.**

### The verifier caught ITSELF committing the bug — twice

Worth stating plainly, because it is the most useful thing that happened this run and it changed the patch.

The first draft of this verifier committed **the exact crime it prosecutes**, in two places:

1. **G6 asserted hardcoded sha256 baselines and ran as a fatal CI step.** That is the INC-12 padlock (*any* legitimate future edit to `session.js` would hard-redden CI) **and** the INC-18 bug itself (in a fleet workspace, an owner **repairing** `usage_aggregator.py` or `checkout.py` changes those bytes and **fails the gate** — punishing the exact remediation the incident exists to request).
2. **G2's `sim_valid` sanity check was anchored to the CURRENTLY DEPLOYED source.** The moment an owner repaired their billing file, the "deployed" tree stopped exhibiting the defect, `sim_valid` went `False`, and **G2 hard-failed CI.** Same crime, one layer deeper.

**Both were caught by *executing* an owner repair rather than by re-reading the code** — the second one was invisible to static review. Fixed: drift is **reported, never enforced**, and the witness is anchored to a **frozen snapshot of the defective behaviour**. A witness is a *record of the defect*, never a *demand about the state of production today*.

**Proven across four scenarios, all exit 0:**

| Scenario | Result |
|---|---|
| Unchanged fleet (all 3 repos present) | **6/6, exit 0** |
| Bare checkout (= what `checkout-api` CI clones) | **6/6, exit 0** |
| **Owners repair ALL THREE billing defects** | **6/6, exit 0** ✅ *(the pre-fix verifier scored 5/6, exit 1 here — it punished the fix)* |
| **A normal future PR edits `session.js`** | **6/6, exit 0** ✅ *(drift reported as provenance; the repo is not padlocked)* |

**Not permanently red, and not a padlock.** The gate is green in the very job that runs it, stays green when owners do the right thing, and stays green when the repo is legitimately edited — while still rejecting the broken repairs (G4).

---

## 5. Fleet re-verification after this change

| Repo | Suite | Verifiers |
|---|---|---|
| `checkout-api` | **10 pass / 0 fail** | INC-9 **9/9** · INC-12 **6/6** · INC-15 **9/9** · **INC-18 6/6** |
| `fabric-gateway-demo` | **Ran 16 tests, OK** | INC-5 · INC-8 · INC-10 — all exit 0 |
| `fabric-ic-incident-target` | **Ran 10 tests, OK** | checkout gate exit 0 |

**36 tests + 8 verifiers, zero failures.** No test assertion was weakened. No dependency was added.

---

## 6. Provenance metadata

| Source | Full sha256 | State |
|---|---|---|
| `checkout-api/service/checkout/session.js` | `b45a8eeceaa142dd70aea4182930d02edb2d23ce90f0f02527910abb5f18d7e8` | unchanged · INC-1 repaired + guarded |
| `fabric-gateway-demo/service/usage_aggregator.py` | `bb21e50f7b5dab4463b71984bbe86a5df2b6ba442ffeff84d9b70815781750e5` | unchanged · INC-5 / INC-8 **live** |
| `fabric-ic-incident-target/checkout.py` | `da2a02fd87aec668467114e0bc30ff7c2fe7fd3d8f105f5f156361b9c87c5c5e` | unchanged · INC-6 **live** |

- **Evidence basis:** execution of the deployed source (no telemetry reachable — see §2).
- **Files changed this run:** `artifacts/incident/verify_inc9_ci_gate.py` (G6a/b/c repaired), `artifacts/incident/verify_inc18_gate_punishes_remediation.py` (new), `.github/workflows/ci.yml` (runs the new gate), `incident_brief.md`.
- **Production code changed:** **none.** The three sources above are byte-identical to their deployed revisions as of this run. Note that this is *reported*, not *enforced* — G6 never fails on drift, precisely so that an owner repairing a billing source cannot be punished by our own gate.
- **Not a duplicate:** PR #19 (INC-17) is still open and repairs a *different* file's hardcoded count (`verify_inc15_cross_fleet_discovery.py`). This finding is in `verify_inc9_ci_gate.py` and is a distinct defect.

---

## 7. Owner actions — what only you can do

### 7a. Bound the blast radius (the commander cannot — it has no telemetry)

1. **INC-6 exposure:** query completed orders where the **eligible-item count is low but the order subtotal is high** — those are the over-discounted ones. The leak is largest at `n=1`, so start there. This tells you whether this is a rounding-error problem or a **material revenue problem**.
2. **INC-8 exposure:** check whether **`None` buckets already exist** in any stored per-model aggregate or invoice breakdown since the 2026-07-11 deploy (PR #1, commit `e368005`). This reveals whether mis-attribution has already reached billing data.
3. **INC-5 exposure:** check `/v1/usage` for dropped batches since that same deploy, and **reconcile** anything lost.

### 7b. Decide the two policy questions (this unblocks the patches)

- **INC-6** — `fabric-ic-incident-target#6`: *(i)* What is the per-item price field named? *(ii)* Does the discount apply to the **eligible subtotal** or the **whole order**?
- **INC-5 + INC-8 (ONE decision)** — `fabric-gateway-demo#2`, `fabric-gateway-demo#5`: what is the malformed-usage-record contract, covering **both** a **missing** key *and* a **null-valued** key? The options and their billing consequences:
  - **reject loudly** — safest for invoice integrity; costs `/v1/usage` availability
  - **skip + emit a metric** — preserves availability, but **under-bills silently** unless the alert is genuinely wired
  - **attribute to `unknown`** — preserves totals, but **mis-attributes spend** and hides the producer bug

### 7c. Interim mitigations (config / feature flags — **not** code, and therefore yours)

- Consider a **temporary tier ceiling**, or disabling volume discounts on orders with fewer than *N* eligible items. This stops the INC-6 bleeding without resolving the policy question.
- **Do not** "fix the average" by dividing by the whole cart's item count. That silently changes which orders qualify for a discount — it **looks** like a bug fix and is actually a **policy change**.
- **Fix the producers too.** A malformed or null-model record reaching the aggregator means an upstream emitter is **already writing bad rows**. Patching only the consumer masks the source.

---

## 8. The systemic lesson

Every incident in this fleet — INC-1 through INC-18 — reached production the same way: **a pull request changed a code path that no test executed.** The verifier layer now closes that hole in all three repos.

But Cluster B is the deeper lesson, and it is about **us**, not the owners. Five times now, a gate written to catch a defect has itself frozen a merge-time fact into a permanent assertion. INC-18 is the most dangerous variant yet, because its expired precondition pointed **outward**: it would have punished the owners at the exact moment they finally did the right thing — and taught them that the commander's red CI is noise.

**A gate must assert an invariant, never a date. If a check can only pass on the day it was written, it is not a gate — it is a timestamp with an exit code.**

And the corollary, learned the hard way this run: **you cannot tell whether a gate has this bug by reading it. You have to run it against the future you claim to want.** Both self-inflicted instances of the bug in this very patch (the fatal sha256 gate, and the witness anchored to live production) survived careful review and were caught only by *simulating an owner landing the repair* and watching our own CI go red.

---
*Fabric autonomous incident commander · INC-18 · one verified verifier+CI patch shipped · three billing incidents routed to owners with runbooks · blast radius explicitly not estimated (no telemetry reachable).*
