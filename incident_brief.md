# Incident Brief — Fabric Autonomous Production Incident Commander

**Run:** 2026-07-12 · **Fleet:** `checkout-api`, `fabric-gateway-demo`, `fabric-ic-incident-target`
**Patched this run:** 1 (INC-30, gate/verifier surface only) · **Routed to owners:** 3 (INC-5, INC-6, INC-8)
**Production source changed: NONE.** All three deployed sources are byte-identical to their deployed revisions.

---

## 1. Executive summary

| | |
|---|---|
| **Highest-urgency finding** | **INC-30** — a strict-mode flag leaked into child verifier processes, so **five** of the fleet's CI gates returned a *different verdict depending on how the flag was spelled*. Patched, verified, wired into CI. |
| **Live customer-impacting defects** | **3**, all re-confirmed LIVE **by executing the deployed source** this run. All three are **billing/revenue policy decisions** and are deliberately **NOT auto-patched**. |
| **Blast radius** | **UNKNOWN, and deliberately not estimated.** No telemetry source was reachable — measured, see §2. |
| **Highest-value owner action** | **Wire a Sentry credential into the commander's environment.** Every run in this fleet's history has been blind to production symptoms — which is exactly why the incident stream keeps surfacing the commander's own gates instead of customer impact. |

---

## 2. Telemetry provenance — RETRIEVAL ATTEMPTED against every source

Every row below is backed by a **captured artifact committed alongside this brief**, not an
assertion. Both were written by this run's pullers and ship in the same change:

- [`artifacts/incident/provenance/telemetry_pull.json`](artifacts/incident/provenance/telemetry_pull.json)
  — every Sentry/OTEL/gateway fetch attempted, with its HTTP status and outcome.
- [`artifacts/incident/provenance/deploy_context.json`](artifacts/incident/provenance/deploy_context.json)
  — the fetched deploys, CI runs, open PRs, and the per-defect deploy correlation.

The scripts that produced them (`pull_telemetry.py`, `pull_deploy_context.py`) ship too, so
any reviewer can re-run the pull and diff the result against the committed artifact.

### 2.1 What was actually fetched

`pull_telemetry.py` is a **retrieval layer**, not a reachability probe: it issues the
real API calls and records each attempt with its HTTP status.

| Source | Status | Fetch attempted → result |
|---|---|---|
| **Sentry issues** | **DARK — no credential** | `GET https://sentry.io/api/0/` → **200** · `GET /api/0/organizations/` → **401 "Authentication credentials were not provided."** Zero `SENTRY*` env vars. **Egress works; the credential is missing** — a missing secret, not a network block. |
| **OTEL traces** | **DARK — no collector** | 11 backends probed (Jaeger, Tempo, Zipkin, OTLP http/grpc, Datadog, Prometheus, Loki…), **0 reachable**; 0 `OTEL*`/`OTLP*` env vars. Negative control **PASSED** (the probe opened port 45355 and detected it), so "closed" is a real measurement. |
| **Gateway logs** | **DARK — no source** | 15 candidate paths checked + `FABRIC_GATEWAY_LOG_PATH` (unset) → **0 found**, 0 lines read. |
| **GitHub PR/deploy** | ✅ **PULLED** | **21 deploys, 30 CI runs, 4 open PRs** across 3/3 repos — itemized in §2.3. |

> **Symptoms retrieved from production telemetry: 0.** So every defect finding in
> this brief was established by **EXECUTING THE DEPLOYED SOURCE**. The findings are
> *confirmed real and confirmed live*; **customer impact is UNKNOWN and is NOT
> estimated anywhere in this document.** Inventing a number from no signal would be
> worse than admitting the gap.

### 2.2 The retrieval path is PROVEN to work — it is the sources that are absent

A puller that retrieves nothing looks identical to a puller that *cannot* retrieve.
That ambiguity is itself a trap, so `verify_telemetry_pull.py` stands up **real
sources** and proves the code pulls from them — **4/4, exit 0**:

| Gate | Result |
|---|---|
| **T1** a live HTTP server speaking the Jaeger trace API | ✅ FOUND, QUERIED, error span extracted (`KeyError` / 500 / `/v1/usage`) |
| **T2** a real gateway log file on disk | ✅ READ; ERROR + WARN + `KeyError` extracted, and the clean `/healthz` line correctly **not** flagged |
| **T3** a live Sentry-API server enforcing auth (401 without a Bearer token) | ✅ AUTHENTICATED, issue fetched, **stack frame** extracted → `service/usage_aggregator.py:6 aggregate_usage` |
| **T4** NEGATIVE CONTROL — sources removed | ✅ **0 symptoms fabricated**; every source honestly reports `unavailable` |

**T4 is what makes T1–T3 trustworthy.** A puller that hallucinated data would pass
T1–T3 and fail here. Together they establish: it pulls when there is something to
pull, and it reports honest emptiness when there is not. **Wire in a
`SENTRY_AUTH_TOKEN` and issues flow with no code change.**

> *A defect the pull layer caught in itself:* the log parser first used one
> alternating regex (`level|status|exception`), which short-circuits on the first
> alternative — so on `ERROR ... KeyError: 'tokens'` it matched `ERROR` and
> **silently dropped the exception type**, the one field that ties a symptom to a
> code defect. T2 caught it. Each facet is now extracted independently.

### 2.3 PR / deploy context — fetched, and correlated to each defect

This is what deploy context is *for*: it turns "this function is wrong" into **"the
earliest this function could have been wrong is this commit, on this date"** — which is
what bounds the window an owner must reconcile.

**Precisely what the correlation claims.** It is **file-level, not line-level blame**,
and the commit history for each file was **paginated to the end** (`history_exhaustive:
true` in the committed
[`deploy_context.json`](artifacts/incident/provenance/deploy_context.json)). So the date
below is a **hard upper bound**: the defect *cannot* predate the commit in which its file
first appeared. The defective *line* may have arrived later, so the true exposure window is
a **subset** of the one stated. Line-level attribution needs `git blame` over the function,
which the REST API does not expose — rather than fake that precision, the bound is reported
and labelled.

| Defect | File first appeared (exhaustive history) | Exposure window opens at or after | Currently deployed as |
|---|---|---|---|
| **INC-6** checkout discount leak | `71c0d6206b66` — *"Add tiered volume discounts to checkout (#1)"* (1 commit, history complete) | **2026-07-08T12:42:35Z** | `71c0d6206b66` |
| **INC-5** malformed record kills batch | `f63682c249ff` — *"Add usage aggregator (pre-incident baseline)"* (2 commits, history complete) | **2026-07-11T02:32:14Z** | `e3680054c178` — *"Add per-model usage breakdown to /v1/usage (#1)"* |
| **INC-8** null model → `None` bucket | `f63682c249ff` (2 commits, history complete) | **2026-07-11T02:32:14Z** | `e3680054c178` |

Fleet CI state as fetched (21 deploys, 30 CI runs, 4 open PRs across 3/3 repos):
`fabric-gateway-demo` `main` @ `b55e4ff8507f` → **success** · `fabric-ic-incident-target`
`main` @ `6444f9f43d91` → **success** · `checkout-api` `main` @ `8fdfdd4` → **success**.
This run's own PR branch (`fabric/inc-30-strict-mode-env-leak`, `76dbf39`) → **success**.

**Read the exposure windows carefully.** The INC-6 discount leak's file has existed since
**2026-07-08** — about four days. That is the outer interval to query for over-discounted
orders (runbook §5.1 step 1). The window is *reported*; the number of affected orders
inside it **cannot** be derived from deploy context alone and is therefore **not
estimated**.

---

## 3. Symptom clustering

Two clusters, of fundamentally different kinds.

**Cluster A — the gate/verifier surface (deterministic, code-fixable) → INC-30, PATCHED.**
Found by an **independent detector** (`detect_mode_divergence.py`) that runs every verifier in all three invocation modes and compares the two strict modes against each other. It reads no prior write-up and hardcodes no list of suspects: it discovers repos and verifiers structurally, then reports what it *observes*.

**Cluster B — billing / revenue semantics (NOT code-fixable) → INC-5, INC-6, INC-8, ROUTED TO OWNERS.**
Three live defects in the money path. Every candidate repair encodes a **different invoicing policy**. Guessing wrong mis-bills real customers *with no error signal* — the same class of failure as the bug itself. Escalated, not guessed. See §5.

---

## 4. INC-30 — the patch (urgency HIGH · code-fixable)

### The finding

Strict cross-fleet mode can be requested two ways, and they are supposed to mean the same thing:

```
python3 verify_x.py --require-cross-fleet          # argv
FABRIC_REQUIRE_CROSS_FLEET=1 python3 verify_x.py   # environment
```

Five verifiers gave a **different verdict depending on which spelling was used** — on the identical tree:

| Environment | Verifier | argv-strict | env-strict |
|---|---|---|---|
| Full fleet workspace | `verify_inc15` | exit 0 | **exit 1** |
| Full fleet workspace | `verify_inc19` | exit 0 | **exit 1** |
| Full fleet workspace | `verify_inc23` | exit 0 | **exit 1** |
| **Bare checkout** (= what CI clones) | `verify_inc12` | exit 0 | **exit 1** |
| **Bare checkout** (= what CI clones) | `verify_inc18` | exit 0 | **exit 1** |

> **A verdict that depends on HOW the request was spelled is not a verdict.**

### Root cause

**Not one** verifier-launching `subprocess.run()` passed `env=`. Python hands the child the parent's *entire* environment, so the strict flag **leaked** into children that must not receive it:

- `verify_inc12` / `verify_inc18` spawn `verify_inc9_ci_gate.py` to ask exactly one question — *"does the shipped INC-9 verifier pass on this tree?"* In a **bare checkout** INC-9 has no sibling repos, so it correctly **SKIPs** its cross-fleet gates and exits 0. With the flag leaked in, the child is **forced** into strict mode and **hard-fails for want of siblings that are legitimately absent.** The parent then reports *"INC-9 does not pass"* — which is **false**, and has nothing to do with the property being tested.
- `verify_inc15` / `verify_inc19` / `verify_inc23` spawn children as **negative controls** against synthetic bare trees, and the control *requires* the child to SKIP. **A negative control that inherits the very flag it is controlling for is not a control.**

### The rule

> **An intent must be PASSED to the child that should receive it, never INHERITED by a child that must not.**

### The repair

```python
def child_env(*, strict=False):
    env = dict(os.environ)
    env.pop(STRICT_ENV_VAR, None)   # ALWAYS scrubbed
    if strict:
        env[STRICT_ENV_VAR] = "1"   # ...re-set ONLY on request
    return env
```

Threaded through **all 7** python-launching spawns across the 5 files. **The strict-mode feature is untouched at the top level** — this stops the flag *leaking*; it does not remove it.

### Verification — `verify_inc30_strict_mode_env_leak.py`: **7/7, exit 0**

| Gate | Result |
|---|---|
| **G0 STATIC/AST** — every python-launching spawn carries an explicit `env=`; `child_env` genuinely pops the var | **7/7 spawns scrubbed** |
| **G1 NECESSITY** — with the leak restored, the two strict modes **diverge again** | inc12 argv=0/env=1 · inc18 argv=0/env=1 |
| **G2 SUFFICIENCY** — as shipped, the two strict modes **agree** | inc12 0/0 · inc18 0/0 |
| **G3 DIVERGENCE** (load-bearing) — identical tree: leaked = divergent · scrubbed = clean | not a no-op |
| **G4 ANTI-WEAKENING** — strict mode **STILL hard-fails** when legitimately requested | default exit 0 · argv-strict exit 1 FATAL · env-strict exit 1 FATAL |
| **G5 SELF-REGRESSION** — reverting the scrub is **REJECTED** by G0's own AST audit | 0/2 scrubbed → REJECT |
| **G6 NO DRIFT** — all 3 deployed sources byte-identical before/after | 3/3 |

**G4 is what makes this a correction and not a cover-up.** Simply *deleting* strict mode would also have turned all five reds green and would have satisfied G1–G3 — and it **fails G4**, which demands that strict mode still bite when it is genuinely asked for. Reverting the scrub is caught by **G5**: the gate detects its own regression.

### Not permanently red

| Environment | INC-30 |
|---|---|
| Full fleet workspace | **7/7, exit 0** |
| **Bare checkout** (= exactly what CI clones) | **7/7, exit 0** |
| Bare checkout + **scrub stripped** (negative control) | **4/7, exit 1** — still bites |

The new CI step is green in the very job that runs it, so it cannot become the INC-11 permanently-red bug — while stripping the repair still reddens CI.

### The gate caught its own author

Worth recording, because it changed the patch. G0's first draft resolved local *names* bound to a verifier path, and reported a confident **"4/4 verifier spawns scrubbed"**. **Four was wrong — the true total is seven.** The audit was **blind** to `verify_inc19` and `verify_inc23`, whose spawn targets arrive as **function parameters** and are bound to no resolvable name at all; it would have certified both files as clean while they were fully unscrubbed. It was caught only because the reported denominator contradicted a count taken from a different gate.

The fix was to stop inferring *which script* is launched — an unwinnable name-resolution arms race — and ask a **structural** question instead: *does this spawn launch `sys.executable`?* Every child that could read the flag is a Python process, and no parameter, alias, or f-string can hide that. The denominator is now **asserted exactly (7)**, so a blind audit can never again masquerade as a clean one.

---

## 5. Still NOT patched, deliberately — owner decisions

All three **re-confirmed LIVE this run by executing the deployed source** (`probe_defect_liveness.py`), never by trusting a prior run's word.

### 5.1 INC-6 — checkout volume-discount leak (`fabric-ic-incident-target#6`)

`checkout.py` → `apply_discount()`. The tier is selected from `subtotal_cents / n`, where the subtotal is the **whole order** but `n` counts **only eligible items**. Measured on a $300 order:

| Eligible items | Charged | Leak |
|---|---|---|
| 1 | **$255.00** | **$45.00** (15% tier) |
| 2 | $255.00 | $45.00 |
| 5 | $270.00 | $30.00 |
| 20 | $300.00 | — |

**The leak scales inversely with eligible-item count** — it is worst on precisely the orders that should receive *no* discount. And `apply_discount()` **reads no item price at all**: measured this run, a **$0.01** and a **$299.99** eligible item produce an **identical** charge.

**Why not patched.** A correct fix must know **each eligible item's price**, and the deployed function never reads any item field — only `len()`. Two answers are required, and neither can be derived from any authoritative source in the repo:

1. **What is the per-item price field called?** (`price`, `price_cents`, `unit_price`, `amount`…?) Guessing wrong either throws `KeyError` on the checkout path — turning a silent revenue leak into a **hard outage** — or silently reads every item as free (`.get(key, 0)`), charging **$500.00** where the contract requires **$425.00** *while reporting success*.
2. **What is the discount scope?** Eligible subtotal only, or the whole order? These produce materially different charges.

Both are **revenue policy**, not engineering. The commander will not invent billing semantics.

**Runbook**

1. **Quantify exposure first.** Query completed orders where the eligible-item count is low but the order subtotal is high. The leak is largest at `n=1` — start there. This is the step only you can perform, and it is what determines whether this is urgent.
2. **Consider a temporary tier ceiling** (or disable volume discounts on orders with fewer than N eligible items). This is a config/feature-flag action, not a code fix.
3. **Do NOT "fix the average" by dividing by the whole cart's item count.** That silently changes which orders qualify for a discount. It looks like a bug fix and is not one.
4. **Then answer the two questions above** and land the repair. **CI will not fight you** — the gates now enforce policy-free pricing invariants and report defect liveness as provenance.

### 5.2 INC-5 — one malformed record destroys the whole `/v1/usage` batch (`fabric-gateway-demo#2`)

Measured: a record missing `tokens` raises **`KeyError('tokens')`** and **destroys the entire batch**, taking **140 valid billable tokens** (from two well-formed records) with it.

**Why not patched** — the correct behaviour is a **billing decision**:

- **Reject the batch (fail loud)** — safest for invoice integrity; costs `/v1/usage` availability.
- **Skip the record + alert** — preserves availability, but **under-bills silently** unless the alert is genuinely wired up.
- **Attribute to `unknown`, tokens `0`** — preserves the totals shape, but **hides producer bugs**.

### 5.3 INC-8 — a null model books billable tokens to a `None` key (`fabric-gateway-demo#5`)

Measured: `{"model": None, "tokens": 40}` books **40 billable tokens against a `None` key**, raises **nothing**, and `grand_total` **reconciles perfectly** — so **no downstream invoice check can detect it**. Serialized, the bucket becomes the JSON string `"null"`: a model that cannot be invoiced or rated.

### INC-5 and INC-8 are ONE decision, not two

> `record.get("model", "unknown")` defaults **only when the key is ABSENT**. A record carrying `{"model": None}` still yields `None`, because the key *is* present — its value is null.
>
> **Fixing INC-5 that way leaves INC-8 fully live in production.** Decide the contract covering **both** the missing key **and** the null value.

**Runbook (INC-5 / INC-8)**

1. **Decide the input contract** for a malformed *and* a null-valued usage record — one decision, covering both shapes.
2. **Fix the producer as well.** A malformed record reaching the aggregator means an upstream emitter is *already* writing bad rows. Patching only the consumer masks the real source.
3. **Add a validation boundary at ingest** so the aggregator can safely assume well-formed input.
4. **Reconcile** any usage rows dropped or mis-attributed since the deploy.
5. Land it and **declare the policy** — the gates accept any declared policy that satisfies the policy-free billing invariant.

**Owners:** gateway / billing owner (whoever owns `/v1/usage` and its emitters) for INC-5 and INC-8; checkout / revenue owner for INC-6.

---

## 6. Fleet verification after this change

| Check | Result |
|---|---|
| **All real `ci.yml` run-steps, executed as GitHub would** | **14/14 GREEN** |
| **Cross-mode sweep** (11 verifiers × 3 invocation modes) — fleet workspace | **33 runs, 0 divergences** (was 3) |
| **Cross-mode sweep** — bare checkout (= what CI clones) | **33 runs, 0 divergences** (was 2) |
| `checkout-api` | `npm test` GREEN · 7 verifiers exit 0 |
| `fabric-gateway-demo` | suite GREEN · 3 verifiers exit 0 |
| `fabric-ic-incident-target` | suite GREEN · gate exit 0 |
| `verify_inc30_strict_mode_env_leak.py` | **7/7, exit 0** · bare checkout **7/7** · scrub stripped **4/7, exit 1** |

**Deployed sources — byte-identical, full sha256 (measured this run):**

| File | sha256 |
|---|---|
| `checkout-api/service/checkout/session.js` | `b45a8eeceaa142dd70aea4182930d02edb2d23ce90f0f02527910abb5f18d7e8` |
| `fabric-gateway-demo/service/usage_aggregator.py` | `bb21e50f7b5dab4463b71984bbe86a5df2b6ba442ffeff84d9b70815781750e5` |
| `fabric-ic-incident-target/checkout.py` | `da2a02fd87aec668467114e0bc30ff7c2fe7fd3d8f105f5f156361b9c87c5c5e` |

**No test or gate was weakened, skipped, or deleted. No dependency added (stdlib only).**

---

## 7. What the owner should do next

1. **Wire a Sentry credential into the commander's environment.** This is the highest-value fix to the incident-response loop itself. The commander is currently **blind to production symptoms** — it can prove defects are live by executing code, but it cannot tell you **how many customers are affected**. That gap is why the incident stream keeps surfacing the commander's own gates instead of customer impact.
2. **Answer the INC-6 questions** (price-field name; discount scope) — §5.1.
3. **Decide the INC-5 / INC-8 contract** — one decision covering both the absent key and the null value — §5.3.
4. **Merge the INC-30 patch.** It closes a leak that could turn a healthy fleet red on the say-so of one ambient environment variable.

---

*Fabric autonomous incident commander · run 2026-07-12 · INC-30 classified code-fixable (deterministic, no product-policy content) · INC-5 / INC-6 / INC-8 classified non-code-fixable (owner decision required).*
