# Incident Brief — Fabric Autonomous Incident Commander

**Run:** 2026-07-12 · **Fleet:** `chrischabot/{checkout-api, fabric-gateway-demo, fabric-ic-incident-target}`
**Classification of this run:** 1 incident **patched and MERGED**; 3 incidents **routed to owners** (revenue policy); 1 **process incident** in the commander itself.

---

## 1. Executive summary

The fleet's CI is green. **It has been green all along — and that is the problem.** Three defects that lose money are live in production right now, and every one of them is invisible to the fleet's own tests.

The headline finding this run is not a new code defect. It is that **the commander had written the same harness fix five times and merged it zero times**:

| | |
|---|---|
| PRs #27, #28, #29, #30, #31 | five separate PRs, all diagnosing the **same** `FABRIC_REQUIRE_CROSS_FLEET` env-var leak |
| Merged before this run | **zero** — `main` still carried **7/7 unscrubbed spawns** (verified by AST, not by reading their prose) |
| Meanwhile | INC-5, INC-6, INC-8 — the three defects that actually cost revenue — stayed **open and untouched** |

> **A fix that is written five times and merged zero times is not a fix.**

So this run did not write a sixth. It **verified #31 independently, merged it, and closed the four duplicates.** The bottleneck was never the patch. It was the merge.

---

## 2. Telemetry provenance — MEASURED this run, not copied forward

| Source | Result | Payloads pulled |
|---|---|---|
| **Sentry** | `/api/0/` → **200**, `/api/0/organizations/` → **401** (credential absent). The probe walks **orgs → projects → `/issues/`**; it is blocked at stage 1. | **0 issues** ❌ |
| **OTEL / OTLP** | **11 ports probed, 0 open** — so the Jaeger/Tempo/Zipkin **trace APIs could not be queried** at all | **0 traces** ❌ |
| **Gateway logs** | **11 paths checked, 0 sources.** The scanner *parses* lines into symptoms (5xx/4xx, error signatures, routes) — there was nothing to parse. | **0 log lines** ❌ |
| **GitHub — PR context** | **200** across **3/3 fleet repos**, open **and** recently-closed PRs | **32 PRs** ✅ |
| **GitHub — deploy context** | **200** — deployments, statuses, workflow runs, check-runs (§2.1) | **1 deployment** ✅ |

**Telemetry sources pullable: 0 / 3.** Verdict: `MEASURED` (no probe was broken).

> **"Pullable" means payloads were actually pulled** — not that a port was open, and not that an endpoint answered. An open OTEL port returning **zero traces** is not a usable triage source, and counting it as one would be the same self-flattering error as a gate that cannot fail.

**Every probe carries a negative control**, because *a "nothing found" from a broken check is not a measurement*:

- **OTEL** — the probe opens a socket itself and must **see** it. → PASS.
- **Gateway logs** — the scanner plants a log with a **known symptom mix** and must find it, read all 3 lines, **and correctly extract the symptoms** (2 server errors, 1×502, `KeyError` signature, `/v1/usage` ×2) **through the same `_scan_log_paths()` code path the real probe uses**. A scanner that counts lines but cannot classify a 502 is still blind for triage, so the control asserts the **analysis**, not just the read. → PASS.
- **GitHub** — a nonexistent repo must come back **exactly 404**. A 403 is **INCONCLUSIVE, never a pass**, so a rate-limit cannot masquerade as a working control. → PASS.

Sentry's distinction matters: **egress works, the credential is missing.** This is a missing secret, not a network block.

### 2.1 PR / DEPLOY context — pulled and MEASURED (`probe_deploy_context.py`)

Deploy context is a required input to triage: *a defect that appeared right after a deploy is a different incident from one that has been latent for months.* So the GitHub **Deployments API** was queried directly per repo (`/deployments`, `/deployments/{id}/statuses`, `/actions/runs`, `/commits/HEAD/check-runs`, `/releases`) — not inferred from PRs.

**Negative control:** the deployments endpoint of a nonexistent repo must return **exactly 404** (a 403 would be *inconclusive*, never a pass, so a rate-limit cannot masquerade as a working probe). → **PASS**.

| Repo | Deployment records | Recent CI runs | HEAD | HEAD checks |
|---|---|---|---|---|
| `checkout-api` | **0** | 50 total, last 5 **all success** | `29cd3f6` — *INC-31 merge*, CI **success** | `session regression suite` ✅ |
| `fabric-gateway-demo` | **1** — env **`prod`**, sha `e3680054`, 2026-07-11, **NO STATUS RECORDED** | 19 total, last 5 **all success** | `b55e4ff` | `test (3.11)` ✅ `test (3.12)` ✅ |
| `fabric-ic-incident-target` | **0** | 19 total, last 5 **all success** | `6444f9f` | `test (3.11)` ✅ `test (3.12)` ✅ |

**Deployment records across the fleet: 1.** Verdict: `MEASURED`.

Two things follow, and both are honest limits rather than findings:

1. **"Which deploy introduced the defect?" cannot be answered from deploy records.** Two of three repos have **zero** deployments, and the one that exists (`fabric-gateway-demo` → `prod`) has **no status recorded at all** — so it cannot even be said to have succeeded. This fleet does not drive releases through the Deployments API; the only real shipping signal is **workflow runs + merge commits**. The three billing defects therefore have **no attributable deploy event**, which is consistent with their being long-latent rather than newly-shipped.
2. **The INC-31 merge is CI-verified on GitHub, not just locally.** GitHub Actions on `main` at the **merge commit `29cd3f6`** → **conclusion: `success`**. That SHA is immutable, so this claim cannot go stale.

   The PR #32 branch is also green in GitHub CI on **every** commit pushed this run — verified by direct workflow-run queries: `13ff27f` (run `29193690905`), `e8dac73` (run `29194020169`), `01321f3` (run `29194164044`) — **all `success`**.

> ⚠️ **A self-referential trap, worth recording.** An earlier draft of this brief asserted CI success for PR head `13ff27f` — already **superseded** by the time the sentence was committed. The deeper problem: *a brief that hardcodes its own branch head is stale the instant it is written*, because committing the brief **creates a new head**. That is this fleet's signature disease — a claim that cannot be true of the tree it ships in — reproduced one level up, in the document that diagnoses it.
>
> The repair is the same one INC-31 taught: **assert the invariant, not the snapshot.** The verdict now rests on the **immutable merge commit** plus the *property* "every pushed commit on the branch passed CI" — a statement that stays true as the branch grows, rather than a SHA that rots on contact.

> ⚠️ A deploy record with **no status** is a small latent gap in the fleet's own observability: nothing would notice a `prod` deployment that silently failed. Flagged to owners; not patched (deployment tooling is outside the repos' source).

> ### Consequence, stated plainly
> Every finding below was established by **executing the deployed production source**, not by reading telemetry. The defects are **confirmed real and confirmed live**. But **blast radius is UNKNOWN and is deliberately NOT estimated** — how many orders were mispriced and how many batches died is the one question only production data can answer.
>
> **The single highest-value fix to the incident-response loop itself: wire a Sentry credential into the commander's environment.** The commander has been blind to production symptoms for its entire history — which is precisely why run after run has been about its own gates rather than about customers.

---

## 3. Incidents, urgency-ranked

### 🔴 INC-6 — Discount tier is price-blind → silent revenue leak · `fabric-ic-incident-target#6`

**OWNER DECISION. Not patched.** Confirmed live by executing `checkout.py` (`da2a02fd87ae…`).

`apply_discount()` derives the discount tier from `subtotal / len(items)` — it **never reads an item's price**.

| Probe (executed this run) | Result |
|---|---|
| $300 order, **one** $10 eligible item | charged **$255.00** — contract says **$300.00** → **$45.00 leak** |
| Same order, item priced **$0.01** | **$255.00** |
| Same order, item priced **$299.99** | **$255.00** — *identical* → **provably price-blind** |
| Leak vs. eligible-item count | 1 → $255.00 · 5 → $270.00 · 20 → $300.00 — **scales inversely** |
| Zero-item guard | holds ✅ |

The fewer eligible items, the bigger the giveaway. A single cheap eligible item unlocks the **top 15% tier** on the entire order.

### 🔴 INC-5 — One malformed record destroys the whole billing batch · `fabric-gateway-demo#2`

**OWNER DECISION. Not patched.** Confirmed live by executing `usage_aggregator.py` (`bb21e50f7b5d…`).

A record missing `tokens` raises `KeyError('tokens')` and **kills the entire `/v1/usage` batch**, destroying **140 valid billable tokens** from the well-formed records alongside it.

### 🔴 INC-8 — Null model books billable tokens to a `None` key, silently · `fabric-gateway-demo#5`

**OWNER DECISION. Not patched.** Confirmed live this run.

`{"model": None, "tokens": 40}` books **40 billable tokens against a `None` key**, raises nothing, and **`grand_total` reconciles perfectly (140 = 100 + 40)** — so **no downstream invoice check can catch it.** Serialized to JSON the bucket becomes the string `"null"`: a model that cannot be invoiced or rated.

> ### ⚠️ INC-5 and INC-8 are ONE decision, not two
> Verified by execution: `{"model": None}.get("model", "unknown")` returns **`None`**, not `"unknown"`.
>
> A repair guarding only **absent** keys passes a **null value straight through**, because the key *is* present. **Fixing INC-5 that way leaves INC-8 fully live.** Decide the contract for the missing key **and** the null value together.

### 🟠 INC-31 — Strict-mode flag leaked into child verifiers · **PATCHED & MERGED** (`29cd3f6`)

Strict cross-fleet mode can be requested two ways that are supposed to mean the same thing:

```
python3 verify_x.py --require-cross-fleet          # argv
FABRIC_REQUIRE_CROSS_FLEET=1 python3 verify_x.py   # environment
```

**They did not agree.** Measured on `main`, same tree, same intent, two spellings:

| Verifier | argv | env |
|---|---|---|
| `verify_inc15_cross_fleet_discovery` | exit 0 ✅ | **exit 1 ❌** |
| `verify_inc19_layout_and_count_invariance` | exit 0 ✅ | **exit 1 ❌** |
| `verify_inc23_drift_gate_punishes_owner_fix` | exit 0 ✅ | **exit 1 ❌** |

> **A verdict that depends on how the request was spelled is not a verdict.**

**Root cause.** Not one python-launching `subprocess.run()` passed `env=` — **7/7 unscrubbed**, counted structurally by AST. Python hands the child the parent's *entire* environment, so the flag **leaked** into children that must not receive it. Several of those children are **negative controls** that *require* the child to SKIP; and **a negative control that inherits the very flag it is controlling for is not a control.**

**The rule:** *an intent must be **passed** to the child that should receive it, never **inherited** by a child that must not.*

### 🟡 INC-32 (process) — The commander was iterating on its own gates instead of shipping

Five PRs, one fix, zero merges — while three revenue defects sat open. **Resolved this run:** #31 merged, #27/#28/#29/#30 closed as superseded with reasoning.

---

## 4. Verification gates

> **Which tree does each number describe?** Every figure below is true, but of a *specific tree* — and conflating them is the ambiguity this brief exists to attack. So, explicitly:
>
> | Reference | What it is |
> |---|---|
> | **`29cd3f6`** | the **INC-31 merge commit** — the moment the env-scrub landed. Immutable; cited because it cannot go stale. |
> | **`17135d2`** | **current `main`** (PR #32 merged on top). Carries the scrub **and** the regression guard. |
> | **`7/7, exit 0`** | the guard in the **fleet workspace**, where the sibling repos are present, so **every** witness can run. |
> | **`5/5 passed, 2 SKIPPED, exit 0`** | the guard on a **bare checkout** — *exactly what CI clones*. The two sibling-dependent witnesses cannot run there, so they **SKIP** rather than go red (see §6.2). |
>
> Both guard results are the **same code on different trees**, and both are green. A skip is in neither the numerator nor the denominator.

### 4.1 The merged repair was verified INDEPENDENTLY of its own write-up

I did not trust PR #31's prose. I reconstructed the `child_env()` scrub myself in a throwaway tree and demanded four properties. `verify_pr31_repair.py` ships as a **permanent regression guard** and is **7/7, exit 0**.

Its gates are phrased for the tree they now run against — **post-merge** — so they keep biting instead of asserting a fact that has expired:

| Gate | Result |
|---|---|
| **G0 STATIC/AST** — spawns exist to audit; an empty denominator is a **hard failure**, never a pass | ✅ 7 spawns (inc12=2, inc15=1, inc18=1, inc19=1, inc23=2) |
| **G0b PRESENCE** — the repair is on the tree: **every** python spawn passes `env=`. *Strip the scrub and this reddens.* | ✅ **7/7 scrubbed** (pre-merge this same audit read **7/7 UNSCRUBBED** — which is how the repair was shown to be needed) |
| **G1 NECESSITY** — **revert** the scrub in a throwaway copy → the two spellings **diverge again** | ✅ inc15/inc19/inc23: argv=0, env=1 |
| **G2 SUFFICIENCY** — as shipped, the two spellings **agree** | ✅ 3/3 agree |
| **G3 DIVERGENCE** (load-bearing) — identical tree: leaked=RED → scrubbed=GREEN | ✅ PRE 3 → POST 0 — **not a no-op** |
| **G4 ANTI-WEAKENING** — strict mode **still hard-fails (exit 1)** when legitimately requested | ✅ **correction, not cover-up** |
| **G4b** — *deleting* strict mode **also** closes the divergence | ✅ **which is exactly why G4 must exist** |

**Note the direction of G0b.** *Before* the merge it asserted `main` was unscrubbed (7/7), proving the repair had not landed and was genuinely needed. *After* the merge it asserts the opposite — the scrub is present — so it now functions as a **regression detector**. Necessity did not disappear with the merge: **G1 keeps witnessing it** by reverting the repair in a temp copy. A gate that asserted "the defect is still here" would have gone permanently red the moment the fix landed — which is the precise disease (INC-18) this fleet already diagnosed once.

**G4 is the load-bearing gate.** Simply deleting strict mode would have turned every red green and satisfied G1–G3. It fails G4. That is the difference between a repair and a cover-up.

### 4.2 Fleet re-verified AFTER the merge

| Check | Result |
|---|---|
| `checkout-api` — `npm test` | ✅ exit 0 |
| `checkout-api` — 8 verifiers × 3 invocation modes (default/argv/env) | ✅ **24/24 green** |
| **Strict-mode divergences** | ✅ **0** (was 3) |
| Post-merge AST audit of `main` | ✅ **7 spawns, 0 unscrubbed** (was 7/7 unscrubbed) |
| **GitHub CI — merge commit `29cd3f6` (`main`)** | ✅ **conclusion: `success`** (immutable SHA — cannot go stale) |
| **GitHub CI — PR #32 branch** | ✅ **every pushed commit `success`**: `13ff27f` (`29193690905`), `e8dac73` (`29194020169`), `01321f3` (`29194164044`) |
| `fabric-gateway-demo` — suite + 3 verifiers | ✅ exit 0 |
| `fabric-ic-incident-target` — suite + gate | ✅ exit 0 |
| `py_compile` across the fleet | ✅ exit 0 |

### 4.3 No production drift — deployed sources byte-identical

| File | sha256 |
|---|---|
| `checkout.py` | `da2a02fd87aec668467114e0bc30ff7c2fe7fd3d8f105f5f156361b9c87c5c5e` |
| `usage_aggregator.py` | `bb21e50f7b5dab4463b71984bbe86a5df2b6ba442ffeff84d9b70815781750e5` |
| `session.js` | `b45a8eeceaa142dd70aea4182930d02edb2d23ce90f0f02527910abb5f18d7e8` |

The merge touched the **gate/verifier surface only**. No test or gate was weakened, skipped, or deleted. No dependency added. **No billing policy invented.**

---

## 5. Owner runbooks — the three decisions the commander will NOT make for you

These are **revenue and invoicing semantics**. Every candidate repair encodes a *different billing policy*, and choosing wrong corrupts customer invoices **with no error signal** — the same class of failure as the bug itself. So they are escalated, not guessed.

### 5.1 INC-6 — the discount scope (`fabric-ic-incident-target#6`)

**Decide two things:** (a) the **price field name** on an eligible item, and (b) the **discount scope**.

The repo's own tests prove the tempting repairs are unsafe:
- `.get('price_cents', 0)` against a **wrong key** reads every item as free, selects the 0% tier, and **charges $500.00 where the contract requires $425.00 — reporting success.**
- Bare indexing `item['price_cents']` throws `KeyError` **on the checkout path**, turning a silent revenue leak into a **hard outage**.

Candidate policies (each a different invoice): tier from the **eligible items' mean price** · discount **eligible value only** · discount the **whole order** · **no volume discount**.

**When you land it, CI will not fight you** — declare the chosen policy (e.g. `DISCOUNT_POLICY = "eligible-items-mean"`) and state it in the PR.

### 5.2 INC-5 + INC-8 — the malformed-record contract (`fabric-gateway-demo#2`, `#5`)

**One decision covering BOTH the absent key and the null value.** Candidate policies:

| Policy | Behaviour | Bias |
|---|---|---|
| **reject-loudly** | raise on any malformed record | safest for **invoice integrity** — nothing is billed wrong, but a bad record blocks the batch |
| **skip + metric** | drop the record, emit a counter | availability; silently under-bills |
| **attribute-to-`unknown`** | book to an `"unknown"` model bucket | preserves revenue; needs a reconciliation process |

⚠️ **Whatever you pick, it must handle `{"model": None}` explicitly.** `.get("model", "unknown")` does **not** — only a falsy-triggered `or "unknown"` (or explicit `None` check) relocates null-model tokens.

### 5.3 The commander's own blocker — **wire a Sentry credential**

Until then, every run is blind to production symptoms and **blast radius cannot be bounded**.

---

## 6. What changed on the fleet this run

| Action | Where | State |
|---|---|---|
| **MERGED** — INC-31 env-scrub (verified 7/7, independently) | `checkout-api` `29cd3f6` | ✅ live on `main` — **7 spawns, 0 unscrubbed** (was 7/7 unscrubbed) |
| **MERGED** — incident brief + INC-31 regression guard | `checkout-api` `17135d2` (PR #32) | ✅ live on `main` |
| **CLOSED** — #27, #28, #29, #30 as superseded, with reasoning | `checkout-api` | ✅ the duplicate pile is gone |
| **CLOSED** — #34 (stale base, unmergeable) | `checkout-api` | ✅ superseded by #35 |
| **OPEN, needs manual rebase** — INC-33 probe hardening (PR #35) | `checkout-api` | ⚠️ **see §6.1** |
| **UNTOUCHED** — deployed production sources | all three repos | ✅ byte-identical (hash-verified) |
| **ESCALATED** — INC-5, INC-6, INC-8 | `fabric-gateway-demo#2`, `#5`, `fabric-ic-incident-target#6` | 🔴 **open owner decisions** |

### 6.1 One thing I could NOT finish, stated plainly

**PR #35 (INC-33) is open and reports `mergeable: false`.** The changes are verified and working, but the push tooling rebuilds a branch from a registered baseline commit rather than from current `main` (`17135d2`), so the branch history diverges regardless of how the files are staged. **I could not resolve this autonomously.**

It needs a **one-line manual rebase**; the content is only **two files** against current `main` (`artifacts/incident/probe_telemetry.py`, `incident_brief.md`). Full detail is on the PR.

Saying "done" here would be the fleet's own disease — **a green claim that the tree does not support.** So it is reported as a blocker.

### 6.2 A defect found in MY OWN regression guard

While validating on a bare checkout, `verify_pr31_repair.py` — the guard this run shipped — **hard-failed on a perfectly healthy tree**.

Its G1/G3 witnesses prove necessity by *reverting* the scrub and requiring the strict-mode divergence to reappear. But that divergence **only exists when the sibling repos are present**, and **CI clones a bare checkout**. So the guard was **permanently red in the very job that runs it** — the **INC-11 disease, re-committed by the verifier written to police it.**

**Repair:** sibling-dependent witnesses now **SKIP** — never passed, never failed, and a skip sits in **neither the numerator nor the denominator** (that latter point is the INC-15 defect). **G0b still enforces the repair with no siblings required.** Proven both ways:

| Environment | Result |
|---|---|
| **Bare checkout** (= what CI clones) | **5/5 passed, 2 SKIPPED, exit 0** ✅ not permanently red |
| Bare checkout + **scrub stripped** | **exit 1** ✅ still bites |
| **Fleet workspace** (siblings present) | **7/7 passed, exit 0** ✅ all witnesses run |

That this run's *own* gate had to be caught by executing it — rather than by reading it — is the whole argument of §7.

---

## 7. The lesson

This fleet's signature disease has a shape, and it recurred **twice** this run — the second time in my own work:

> A gate that **cannot fail** is decoration. A gate that **cannot pass** teaches the team to ignore red CI. And **a fix that is never merged is not a fix.**

The commander spent five runs perfecting a harness repair and shipping none of it, while $45-per-order revenue leaks and batch-destroying billing errors ran untouched in production. That is now closed: the gates are correct **and merged** (`main` carries 7 spawns, 0 unscrubbed).

Then the disease bit again, one level deeper. The regression guard shipped *this run* was **permanently red on a bare checkout** (§6.2), and the brief itself briefly asserted CI success for a **superseded commit** (§2.1). Both were caught by **executing the artifact instead of reading it** — which is the only method that has ever worked here.

> **Every check must be able to both pass and fail on the tree it actually runs against.** Anything else is decoration, and decoration is how all three revenue defects survived a green CI for this long.

The three defects that cost money are, and always were, **waiting on a human decision about billing policy** — which is exactly where they belong.

---
*Fabric autonomous incident commander · 2026-07-12 · every figure in this brief was produced by a command executed during this run.*
