# Fabric — Autonomous Incident Commander · Executive Brief

**Run:** INC-23 · 2026-07-12 (date from the system clock and the GitHub API `Date`
header, which agree — note several earlier briefs in this fleet say "2026-07-14", a
wrong date copied forward and contradicted by their own GitHub timestamps).
**Fleet:** `chrischabot/checkout-api` · `chrischabot/fabric-gateway-demo` · `chrischabot/fabric-ic-incident-target`
**Production code changed this run:** **none.** All three deployed sources are
byte-identical on the full sha256 (verified before *and* after every gate).

---

## 1. Bottom line

| | |
|---|---|
| **Patched (code-fixable, verified)** | **1** — the CI drift gate that hard-failed the moment an owner *repaired* a billing defect |
| **Routed to owners (not code-fixable)** | **3** — INC-6, INC-5, INC-8. Billing/revenue **policy**, re-confirmed LIVE by execution |
| **Operational blocker** | **1** — the commander is **blind to production telemetry**. No Sentry credential, no OTEL collector, no gateway logs |
| **Money at risk, measured** | a `$300.00` order with one `$10` eligible item is charged **`$255.00`** — a **`$45.00` leak per order**, and `apply_discount()` is **provably price-blind** |
| **Fleet check surface** | **3 suites (36 tests) + 10 verifiers + 3 CI modes — all green, exit 0** |

---

## 2. Telemetry provenance — MEASURED THIS RUN, not copied forward

This section is first on purpose. **Every code claim below was established by
EXECUTING the deployed source, because no telemetry source was reachable.**

| Source | Probe | Result |
|---|---|---|
| **Sentry** | `GET sentry.io/api/0/` | **HTTP 200**, body `{"version":"0","auth":null,"user":null}` |
| **Sentry** | `GET sentry.io/api/0/organizations/` | **HTTP 401** — `"Authentication credentials were not provided."` |
| **Sentry** | `env \| grep -i sentry` | **0 variables** |
| **OTEL / traces** | TCP 4317, 4318, 9411, 16686, 14268, 55681, 8126, 13133, 9090, 3200, 3100 | **all CLOSED** |
| **OTEL** | `env \| grep -iE 'otel\|otlp'` | **0 variables** |
| **Gateway logs** | `/var/log`, all mounts, `find / -xdev` for `*gateway*log*` | **no source on disk or mounted** |
| **GitHub REST** | connector | **LIVE, authenticated** — PR/deploy context came from here |
| **GitHub GraphQL** | connector | **403** |

**Egress to sentry.io works** (200 on the unauthenticated root), so this is a
**missing secret, not a network block.**

> ### Consequence, stated plainly
> **Zero Sentry issues, zero OTEL traces, and zero gateway log lines were available
> to cluster.** Symptom clustering across those sources **did not happen this run** —
> it *could not*. **Blast radius is therefore UNKNOWN and is deliberately NOT
> estimated.** Any brief that reports a customer-impact number under these conditions
> is fabricating it.
>
> **The single highest-value fix to the incident-response loop itself: wire a Sentry
> credential (and an OTEL endpoint) into the commander's environment.** Every run so
> far has been blind to production symptoms. Owner queries to bound the blast radius
> are in §7.

---

## 3. Incident groups, urgency-ranked

Clustered on **root cause**, from the evidence that *was* available: the deployed
source of all three repos, their full test/verifier surface, and GitHub PR/CI state.

### 🔴 P1 · INC-23 — the CI gate punished the remediation it exists to request  ✅ PATCHED

**Cluster:** `checkout-api` gates `verify_inc15` **G8** + `verify_inc19` **G1**
(one root cause, two red gates — INC-19's G1 merely re-runs the INC-15 verifier and
inherits its failure).

`verify_inc15_cross_fleet_discovery.py` gate **G8 ("NO PRODUCTION DRIFT")** required
every deployed source to be byte-identical to a **hardcoded sha256 baseline**, and
was **fatal** on any difference. That `BASELINES` dict includes the **sibling billing
sources** (`usage_aggregator.py`, `checkout.py`).

That is a **merge-time fact frozen into a permanent gate.** It encodes *"nobody has
fixed the billing defects yet"* — a statement about **the calendar**, not about
correctness.

**Reproduced by execution, before repairing it.** I landed the exact remediation this
commander has escalated for many consecutive runs — an owner choosing the discount
tier from the eligible items' mean price:

```python
avg_cents = sum(i["price_cents"] for i in eligible_items) / n
```

That repair is **genuinely correct**, established by execution: the `$300` order with
one `$10` eligible item goes from a leaking **`$255.00`** to the contractual
**`$300.00`**; 5 × `$100` prices correctly at **`$425.00`** (avg `$100` → 15% tier);
the zero-item guard still holds. On that **healthy, correctly-repaired** tree:

| Verifier | Result |
|---|---|
| `verify_inc15_cross_fleet_discovery.py` | **exit 1 — `[FAIL] G8`** |
| `verify_inc19_layout_and_count_invariance.py` | **exit 1 — `[FAIL] G1`** |
| `verify_inc9_ci_gate.py` | exit 0 (already immunized by INC-18) |
| `verify_inc18_gate_punishes_remediation.py` | exit 0 |

**The owner does precisely the thing we keep asking for, and CI goes hard RED on a
repo where nothing is wrong.**

Second edge, same root cause: G8 **padlocked `checkout-api`'s own `session.js`** —
appending one ordinary line to it turned INC-15 red on a bare checkout. That is the
**INC-12 padlock, re-committed.**

#### This fleet's signature failure, on its eighth repetition

| | The expired precondition |
|---|---|
| INC-11 | G3 asserted *"`ci.yml` is NEW"* — permanently false the instant it merged |
| INC-12 | required `ci.yml` byte-identical to `main` — forbade the repo from editing its own CI |
| INC-15 | the cross-fleet gates were unreachable dead code; the skip was laundered into `6/6 passed` |
| INC-17 | the gate policing that laundering hardcoded the count it was policing (`== 6`) |
| INC-18 | the gates asserted the billing defects were **still broken** |
| INC-19 | the witnesses depended on ambient clone-directory names |
| **INC-23** | **the drift gate hard-fails the moment an owner REPAIRS one** |

> A gate that **punishes the remediation it exists to request** is worse than no gate
> at all. A gate that can never fail and a gate that can never pass teach the team the
> same lesson: **ignore the red.** This variant points **outward**, at the owners we
> keep asking to act.

### 🟠 P2 · INC-6 — checkout discount leak  ⛔ OWNER DECISION (re-confirmed LIVE)

`fabric-ic-incident-target/checkout.py`, issue **#6**. The deployed code reads:

```python
avg_cents = subtotal_cents / n      # divides the SUBTOTAL by the item count
```

It **never reads any item's price.** Measured by executing the deployed source:

| Order | Charged | Contract |
|---|---|---|
| `$300`, one `$10` eligible item | **`$255.00`** | `$300.00` — a **`$45.00` leak** |
| `$300`, one `$0.01` eligible item | `$255.00` | *identical* — |
| `$300`, one `$299.99` eligible item | `$255.00` | *identical* — **provably price-blind** |

### 🟠 P2 · INC-5 — one malformed record destroys the whole `/v1/usage` batch  ⛔ OWNER DECISION

`fabric-gateway-demo/service/usage_aggregator.py`, issue **#2**. Executed this run:
`aggregate_usage([{"model":"gpt-4","tokens":100}, {"tokens":40}])` raises
**`KeyError('model')`** — the entire batch dies, including the **100 valid tokens**.

### 🟠 P2 · INC-8 — null-model tokens booked to an unnameable bucket  ⛔ OWNER DECISION

Same file, issue **#5**. Executed this run:
`{"model": None, "tokens": 10}` →
`{'per_model': {'gpt-4': 100, None: 10}, 'grand_total': 110}`.

**No error is raised, and `grand_total` reconciles perfectly** (110 = 100 + 10). Ten
billable tokens sit in a bucket **no invoice line can name**. Any reconciliation that
compares `grand_total` against the sum of `per_model` sees **nothing wrong**. This is
**unattributed revenue, not rejected revenue** — the nastiest of the three.

### 🟡 P3 · Process incident — the meta-gate PR pileup  ✅ RESOLVED

**Four PRs were open** (`checkout-api` #24, #25; `fabric-ic-incident-target` #7;
`fabric-gateway-demo` #8), all diagnosing variants of this same frozen-precondition
disease, **none merged**, several conflicting. Consecutive commander runs each found no
repair merged and filed another. That is a real failure of the loop, so it was handled
— not merely recommended.

**But "they're all duplicates, close them" would have been wrong and destructive.** I
checked by execution: I landed a correct owner repair in each sibling repo and ran
**that repo's own** gates.

| Repo | On an owner-repaired tree | Verdict |
|---|---|---|
| `checkout-api` #24, #25 | same file, same G8 defect as #26, stale bases, conflict with each other | **genuinely superseded — CLOSED** |
| `fabric-ic-incident-target` #7 | `verify_checkout_gate.py` **exit 1 — `[FAIL] G1, G2`**; its 10-test suite **reddens** | **live defect — KEPT OPEN** |
| `fabric-gateway-demo` #8 | `verify_inc5_usage_gate` **`[FAIL] G1`** · `verify_inc8_null_model_gate` **`[FAIL] G1,G3,G5,G10,G11`** · `verify_inc10` **`[FAIL] G4,G5,G6`**; 16-test suite **reddens** | **live defect — KEPT OPEN** |

#7 and #8 attack the same disease in **files #26 does not touch.** Closing them as
"superseded" would have destroyed real work. Each now carries a comment with the
measured evidence and the two hard-won lessons from #26 (a substring guard is
bypassable; the witness helper must be idempotent).

**A near-miss worth recording.** My first probe ran the gateway's verifiers on the
**unmodified** tree, saw exit 0, and concluded #8 was redundant. That is unsound —
`verify_inc8_null_model_gate.py` exits 0 *precisely because the defect is still
deployed*. Exit 0 on a broken tree says nothing about what happens when the owner acts.
Only after landing a real repair did the gates go red. **The same class of error the
fleet keeps making: reading a green from a check that could not have gone red.**

**Resolution: land one (#26), close only the true duplicates (#24, #25), keep the two
that fix independent live defects (#7, #8).**

---

## 4. Fixability analysis — why exactly one thing was patched

| Incident | Class | Decision |
|---|---|---|
| **INC-23** | **Code defect.** Deterministic, no product-policy content: the gate's own verdict logic contradicts its own purpose. | **PATCHED + verified** |
| **INC-6 / INC-5 / INC-8** | **Product policy.** Every candidate repair encodes a *different invoicing semantics*. | **ROUTED TO OWNERS** |

**The commander will not invent billing semantics.** For INC-6, guessing the
price-field name either **crashes the checkout path** (`KeyError` → outage) or
**silently misprices forever**: `.get('price_cents', 0)` against a wrong key reads
every item as free, selects the 0% tier, and **charges `$500.00` where the contract
requires `$425.00` — while reporting success.** For INC-5/INC-8, a repair guarding
only *absent* keys passes a `None` value **straight through**, because the key *is*
present — so fixing INC-5 without deciding the null case **leaves INC-8 live.**

These are decisions about **who gets billed what**. They belong to their owners.

---

## 5. The patch — assert the invariant, not the calendar

### Exact scope (itemised — no "about 3 files" hand-waving)

**In the PR against `checkout-api` — 4 files. No production source touched.**

| File | Op | What |
|---|---|---|
| `artifacts/incident/verify_inc15_cross_fleet_discovery.py` | **modified** | **the repair itself** — G8 now asserts the run invariant, not a frozen baseline |
| `artifacts/incident/verify_inc23_drift_gate_punishes_owner_fix.py` | **created** | the 8-gate double-witness verifier that guards the repair |
| `.github/workflows/ci.yml` | **modified** | +1 step, so the repair cannot rot into decoration |
| `incident_brief.md` | **created** | this document |

**Also written, in the incident-commander workspace — 5 scripts. NOT part of the PR;
they are the evidence trail for this run and live outside the fleet repos:**

| File | What it establishes |
|---|---|
| `probe_live_state.py` | re-confirms INC-6/5/8 LIVE by executing deployed source; hashes all 3 sources |
| `witness_pre_repair.py` | reproduces the P1 defect **before** repairing it (necessity, by execution) |
| `verify_fleet.py` | the whole-fleet gate: 3 suites + 10 verifiers + py_compile + YAML + 3 CI modes + no-drift |
| `test_g0_bypass_resistance.py` | proves the semantic G0 rejects all 4 syntactic bypasses |
| `test_owner_already_fixed.py` | proves the verifier does **not** crash or fail once the owner lands the INC-6 fix |

That is **9 artifacts in total** — 4 shipped, 5 retained as the run's evidence.
Production source files modified: **zero.**

### The mechanism

What G8 legitimately protects is the verifier's **own side effects**: it mutates files
during mutation testing and must restore every one. That is a property of **this
process**, not of the fleet's bug backlog. So G8 now compares a **start-of-run
snapshot** (`RUN_SNAPSHOT`) against the bytes on disk at the end:

| Condition | Verdict |
|---|---|
| bytes moved **during our own run** (we mutated production and failed to restore it) | **FATAL — still bites** |
| a source **present at start but missing at the end** (we deleted it) | **FATAL** |
| differs from the historical reference but **stable across our run** = an **owner edit** | **REPORTED as provenance, never fatal** |

The frozen hashes are kept **as provenance reference values only**. No new merge-time
constant is introduced — re-committing that pattern is the very bug being fixed.

### The verifier must survive its own success

A gate asserting "an owner's fix must not be punished" would be self-refuting if it
**crashed** once the owner landed that fix. The first draft did exactly that —
`land_owner_repair()` raised an `AssertionError` when `checkout.py` no longer contained
the defect line, i.e. **the moment the owner complied.** That is the INC-23 disease,
re-committed inside the verifier that diagnoses it (the fleet's ninth repetition).

Fixed: the helper is **idempotent** and returns `applied` / `already-repaired` /
`unrecognised`; on an already-repaired tree the necessity and divergence witnesses
cannot be hosted (there is no defect left to revert), so they report **SKIPPED — never
a pass, never a hard failure**, while G0/G1/G3/G7 still execute and still guard the
repair. Proven by `test_owner_already_fixed.py`: on the owner-repaired fleet the
INC-23 verifier is **exit 0, no crash, witnesses SKIPPED**, and INC-15 / INC-19 are
both **GREEN** where they previously exited 1.

---

## 6. Verification gates — `verify_inc23_drift_gate_punishes_owner_fix.py` · **8/8, exit 0**

| Gate | Result |
|---|---|
| **G0 STATIC** (no siblings needed) | the shipped INC-15 verifier carries the repair — **this is what guards it inside CI** |
| G1 no regression | the repaired verifier is still **9/9** on an untouched fleet |
| **G2 WITNESS A (necessity)** | the **PRE-repair** predicate **REJECTS** a correct owner repair (`drift=['checkout.py']`) |
| **G3 WITNESS B (sufficiency)** | the **repaired** verifier **PASSES** on that same tree, reporting the owner's edit as provenance |
| **G4 DIVERGENCE (load-bearing)** | identical tree: **PRE = REJECT [RED] · POST = GREEN**, and INC-19 recovers with it — the repair is **not a no-op** |
| **G5 ANTI-WEAKENING** | a verifier that leaves production **MUTATED across its own run** is **STILL rejected, exit 1** |
| G6 the witness is sound | the owner repair used as the witness is **genuinely correct**, established by execution (`$300.00` / `$425.00` / zero-item guard) |
| G7 no drift from this verifier | **3/3** sources byte-identical before/after; all mutation testing in throwaway copies |

**G5 is the gate that matters most.** Simply **deleting** G8 would have turned the red
gate green *and* satisfied G2/G3/G4 — and it **fails G5**. That is the difference
between a **correction** and a **cover-up**. This is not a relaxation.

### The gate caught its own author — twice. Both times it changed the patch.

Worth recording, because it is the whole point of the double-witness design.

**1. The negative control caught a bypassable G0.** My first G0 checked for the
substring `not self_inflicted and checked > 0`. Reverting the repair by *widening* the
verdict to `not owner_edits and not self_inflicted and checked > 0` **still contains
that substring** — so the gate stayed **green while the punishing behaviour was fully
restored.** A gate a real regression can slip past is decoration: the exact failure
this fleet exists to cure, nearly shipped *inside the incident that diagnoses it.*

**2. Syntactic hardening kept losing.** Requiring the identifier `checked` is beaten
by `checked >= 0`; requiring a `checked > 0` AST node with no `Or` is beaten by
`not (checked > 0)`, which *inverts* the guard.

**Fix: certify the verdict SEMANTICALLY.** G0 now parses G8's verdict expression from
the AST and **evaluates it against the truth table the repair demands** — sweeping
many positive `checked` values so a `checked == N` special-case cannot satisfy it:

| `self_inflicted` | `owner_edits` | `checked` | required |
|---|---|---|---|
| no | no | > 0 | **PASS** (clean tree) |
| no | **YES** | > 0 | **PASS** — an owner edit is provenance, never a failure |
| **YES** | no | > 0 | **FAIL** — we mutated production |
| no | no | **0** | **FAIL** — nothing examined is not a passing check |

Proven in [`test_g0_bypass_resistance.py`](test_g0_bypass_resistance.py): the shipped
verdict is **certified**, and **all 4 bypasses are REJECTED**.

### Not permanently red — the mistake this incident is about

| Environment | INC-23 |
|---|---|
| Full fleet workspace | **8/8, exit 0** |
| **Bare checkout** (= what `checkout-api` CI clones) | **1/1 passed, 1 SKIPPED, exit 0** — skips are in **neither the numerator nor the denominator** |
| **Bare checkout + the repair reverted** (negative control) | **exit 1, `[FAIL] G0`** ✅ |

The new CI step is green in the very job that runs it, so it **cannot become the
INC-11 permanently-red bug it diagnoses** — while stripping the repair still reddens
CI. That negative control is the point: the gate is made **correct**, not merely
**green**.

### Fleet re-verified after the change — [`verify_fleet.py`](verify_fleet.py), exit 0

| Repo | Suite | Verifiers |
|---|---|---|
| `checkout-api` | **10 pass / 0 fail** | INC-9, INC-12, INC-15 (**9/9**), INC-18, INC-19, **INC-23 (8/8)** — all exit 0 |
| `fabric-gateway-demo` | **Ran 16 tests, OK** | INC-5, INC-8, INC-10 — all exit 0 |
| `fabric-ic-incident-target` | **Ran 10 tests, OK** | checkout gate — exit 0 |

**36 tests + 10 verifiers + 3 CI modes, zero failures.** `py_compile` clean on all 10
verifiers. All three `ci.yml` files parse as valid YAML; `checkout-api`'s carries 10
steps including the new INC-23 gate.

**No production drift:** `session.js` `b45a8eeceaa142dd…` · `usage_aggregator.py`
`bb21e50f7b5dab44…` · `checkout.py` `da2a02fd87aec668…` — byte-identical on the full
sha256, before and after.

---

## 7. Owner runbooks — the three incidents the commander will NOT patch

### INC-6 · `fabric-ic-incident-target#6` — checkout discount leak

**Decision required:** which price drives the discount tier?

1. **Mean of eligible items** (`sum(i["price_cents"]) / n`) — the repair verified
   correct by execution this run: `$300.00` charged, `$425.00` on 5 × `$100`.
2. **Mean of the whole order** (today's `subtotal_cents / n`) — the current behaviour;
   if this is intended, then the *tiers* are mis-specified, not the code.
3. **Sum of eligible items** — a different contract again.

**Recovery:** bound the leak, then decide.
```sql
-- orders where eligible items are a small fraction of the subtotal are the leakers
SELECT order_id, subtotal_cents, eligible_item_count, charged_cents,
       subtotal_cents - charged_cents AS discount_given
FROM checkout_orders
WHERE eligible_item_count > 0
  AND created_at > now() - interval '30 days'
ORDER BY discount_given DESC;
```
**Guard already in place:** the repo's own tests prove the tempting repairs unsafe —
`.get('price_cents', 0)` on a wrong key charges **`$500.00` instead of `$425.00` and
reports success**; indexing throws `KeyError` on the checkout path (**outage**).

### INC-5 · `fabric-gateway-demo#2` — malformed record kills the batch

**Decision required:** reject the batch (today), reject only the bad record, skip it,
or attribute it to an `"unknown"` bucket. Each is a different invoicing policy; on one
batch the candidates disagree by **60 billable tokens**.

**Recovery:**
```sql
-- how many batches are we losing entirely, and how much valid usage with them?
SELECT date_trunc('hour', received_at) AS hour,
       count(*) AS failed_batches,
       sum(record_count) AS records_lost
FROM usage_batch_errors
WHERE error_type = 'KeyError'
  AND received_at > now() - interval '7 days'
GROUP BY 1 ORDER BY 1 DESC;
```

### INC-8 · `fabric-gateway-demo#5` — null-model tokens, silently unattributed

**Decision required:** reject, bucket as `"unknown"`, or bill to a fallback tenant.
**Note the interlock:** a repair guarding only *absent* keys leaves this live, because
the key **is** present — its value is `None`.

**Recovery — this is the one that needs a query most urgently**, because nothing has
ever alarmed on it:
```sql
-- tokens already booked against a null model: unattributed revenue, invisible to
-- any grand_total reconciliation
SELECT date_trunc('day', ts) AS day, sum(tokens) AS unattributed_tokens
FROM usage_records
WHERE model IS NULL
  AND ts > now() - interval '90 days'
GROUP BY 1 ORDER BY 1 DESC;
```

### INC-TELEMETRY · the commander itself

**Owner action:** provision `SENTRY_AUTH_TOKEN` (+ org/project slugs) and an
`OTEL_EXPORTER_OTLP_ENDPOINT`, and mount or ship the gateway access logs. Until then
every run is **blind to production symptoms** and blast radius is **unknowable** —
the commander can only reason about code it can execute, which is what it did here.

---

## 8. Provenance metadata

| Field | Value |
|---|---|
| Run ID | INC-23 |
| Date | 2026-07-12 (system clock ≡ GitHub API `Date` header) |
| Telemetry sources reachable | **0 of 3** (Sentry 401 · OTEL all ports closed · gateway logs absent) |
| Evidence basis | **execution of the deployed source** + GitHub REST (authenticated) |
| Production source modified | **none** (3/3 byte-identical, full sha256, before *and* after) |
| Test assertions weakened | **none** |
| Dependencies added | **none** (Python stdlib + zero-dependency `node:test`) |
| Blast radius | **UNKNOWN — deliberately not estimated.** See §2 and §7 |
| Findings status | **confirmed real, confirmed live** — by execution, this run |

---
*Fabric autonomous incident commander · INC-23 · one code-fixable defect patched and
verified; three billing-policy incidents routed to owners with runbooks; one
operational blocker escalated.*
