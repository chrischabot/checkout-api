# Fabric — Executive Incident Brief

**Run:** 2026-07-12 · autonomous production incident commander
**Fleet:** `checkout-api` · `fabric-gateway-demo` · `fabric-ic-incident-target`
**Patched this run:** 1 (INC-28, deterministic harness defect — CI/gate surface only)
**Routed to owners:** 3 (INC-6, INC-5, INC-8 — billing/revenue policy)
**Production behaviour changed:** **none.** All three deployed sources are byte-identical.

---

## 1. Bottom line

A **strict-mode flag leaked into the negative controls of three CI verifiers**, so
setting a single environment variable turned a **fully healthy fleet RED** in three
places at once — for reasons having nothing to do with what those gates test. That
defect is deterministic, carries no product-policy content, and is now **patched and
verified**.

The three **billing defects remain live and remain owner decisions.** They were
re-confirmed live *this run, by executing the deployed source* — not carried forward
on a previous brief's word.

> ⚠️ **Telemetry coverage gap, measured this run:** Sentry, OTEL and gateway logs were
> **all unreachable**. Every finding below was established by **executing deployed
> code** and reading **GitHub PR/deploy context**. **Customer blast radius is therefore
> UNKNOWN and is deliberately NOT estimated.** See §6.

---

## 2. Data provenance — what the commander could actually see

Measured at the start of this run by `probe_telemetry.py`, which is
**negative-controlled** (the port scanner first proves it can detect a port it opens
itself, so "closed" is a real measurement and not a broken probe).

| Source | Status | Records retrieved | Evidence |
|---|---|---|---|
| **Sentry issues** | ❌ UNAVAILABLE | **0** | `sentry.io/api/0/` answers HTTP 200 but `{"auth":null,"user":null}`; `/organizations/` → **401**. Zero `SENTRY*` env vars. Egress works → this is a **missing credential, not a network block**. |
| **OTEL traces** | ❌ UNAVAILABLE | **0** | 11 ports probed (OTLP 4317/4318, Zipkin, Jaeger ×2, OTLP-legacy, Datadog, OTEL-health, Prometheus, Tempo, Loki) — **all closed**. Probe negative control **valid**. |
| **Gateway logs** | ❌ UNAVAILABLE | **0** | No source on disk or in env (8 standard paths checked). |
| **GitHub PR/deploy** | ✅ AVAILABLE | 3 repos, 27 prior incidents, live CI status | REST connector authenticated; all 3 deployed sources on disk and executable. |

**Consequence, stated plainly:** with no symptom telemetry, the commander cannot cluster
*customer-visible* symptoms, and cannot bound impact. What it *can* do — and did — is
execute the deployed source and audit the fleet's own check surface. So the symptom
clustering below is drawn from **code behaviour + deploy/CI context**, and every
impact claim is marked UNKNOWN rather than guessed.

**Highest-value fix to the incident-response loop itself: wire a Sentry credential
into the commander's environment.** Every run in this fleet's history has been blind
to production symptoms — which is precisely why the last ~16 incidents have been about
the commander's own gates rather than about customers.

---

## 3. Cross-source symptom clustering

Starting state: the INC-27 PRs merged into two repos shortly before this run, and
`main` CI was **green** on all three. So the obvious "the merge broke CI" hypothesis
was **tested and rejected** — CI run `29185502388` (sha `6444f9f`) is a success.

The fleet's whole check surface was then executed in **strict cross-fleet mode** (the
mode a commander workspace *should* use, since the siblings are present and the
cross-fleet gates must therefore actually run rather than skip):

| Cluster | Sources joined | Signal | Urgency |
|---|---|---|---|
| **C1 — strict mode reddens a healthy fleet** | deployed source (3 verifiers) + local execution + CI config | 3 of 14 checks RED **only** when `FABRIC_REQUIRE_CROSS_FLEET=1` is in the environment; identical tree green via the argv flag | **HIGH — fixable** |
| **C2 — checkout revenue leak** | deployed `checkout.py`, executed | $300 order / one $10 eligible item charges **$255.00**; `apply_discount` reads **no item price** | **HIGH — owner decision** |
| **C3 — usage batch destruction** | deployed `usage_aggregator.py`, executed | one malformed record raises `KeyError('tokens')` and **destroys the whole `/v1/usage` batch** (140 valid billable tokens lost) | **HIGH — owner decision** |
| **C4 — null-model billing** | deployed `usage_aggregator.py`, executed | `{"model": None, "tokens": 40}` books 40 billable tokens to a `None` key, **raises nothing**, and `grand_total` **reconciles perfectly** | **HIGH — owner decision** |

C3 and C4 are **the same contract from two sides** and must be decided together — see §7.2.

---

## 4. Fixability analysis

The rule the commander applies: **a defect is auto-patchable only if the repair is
deterministic and contains no product-policy content.** If choosing between correct-
looking repairs means *inventing revenue or billing semantics*, it is escalated, not
guessed.

| Cluster | Root cause | Class | Verdict |
|---|---|---|---|
| **C1 / INC-28** | An environment variable is inherited by child processes that exist as negative controls | **code defect**, deterministic, zero policy content | ✅ **PATCHED + VERIFIED** |
| **C2 / INC-6** | Discount tier computed from `subtotal / n` instead of item prices | **revenue policy** — the price-field name *and* the discount scope are business decisions | ⛔ **ROUTED to owner** |
| **C3 / INC-5** | `record["tokens"]` unguarded | **billing policy** — reject / skip / attribute-to-unknown each produce a different invoice | ⛔ **ROUTED to owner** |
| **C4 / INC-8** | `record["model"]` may be `None` | **billing policy** — same decision as C3, from the null side | ⛔ **ROUTED to owner** |

---

## 5. The patch — INC-28 (`checkout-api`)

### The finding

`verify_inc15`, `verify_inc19` and `verify_inc23` each **spawn child verifier
processes**, and several of those children exist specifically as **NEGATIVE CONTROLS**:
they run against a synthetic **bare checkout** (siblings deliberately absent) and
require the child to report **SKIP** and exit 0. That is how those verifiers prove they
are *not* permanently red in `checkout-api` CI, which clones only that one repo.

But `subprocess.run()` **without `env=`** hands the child the parent's *entire*
environment — and strict cross-fleet mode is honoured through an environment variable,
`FABRIC_REQUIRE_CROSS_FLEET`. So the instant an operator or a CI job exports it, the
control child **inherits** it, is forced into strict mode, and **hard-fails exactly
where the control demands a skip.**

> **A negative control that inherits the very flag it is controlling for is not a control.**

Measured on the fleet, which was otherwise **13/13 green**:

| Invocation | INC-15 | INC-19 | INC-23 |
|---|---|---|---|
| `--require-cross-fleet` (argv) | 9/9 ✅ | 7/7 ✅ | 8/8 ✅ |
| `FABRIC_REQUIRE_CROSS_FLEET=1` (env) | **8/9 RED** | **2/7 RED** | **5/8 RED** |

An **ambient variable made a healthy fleet report red, in three verifiers at once.**
INC-19 collapses hardest (2/7) because nearly every one of its gates spawns a child.

This is the fleet's signature disease — *a gate reddening for a reason unrelated to the
property under test* — one level down, **in the harness itself**. It is also a trap the
commander walked into during this very run: my own fleet-check runner initially passed
strict mode via the environment and produced exactly this false red. Had I trusted that
table, I would have raised a **fabricated three-verifier incident.** The negative-control
discipline is what caught it.

### The repair

> **An intent must be PASSED to the child that should receive it — never INHERITED by a
> child that must not.**

Each of the three verifiers now scrubs the flag from the child environment and re-sets
it **only** when a call site explicitly asks:

```python
def child_env(*, strict=None):
    env = dict(os.environ)
    env.pop("FABRIC_REQUIRE_CROSS_FLEET", None)   # ALWAYS scrubbed
    if strict:
        env["FABRIC_REQUIRE_CROSS_FLEET"] = "1"   # ...re-set ONLY on request
    return env
```

The strict-mode **feature is untouched** at the top level. This stops it *leaking*; it
does not remove it. **No production source changed. No test or gate weakened, skipped
or deleted. No dependency added (stdlib only).**

### Verification — `verify_inc28_strict_mode_env_leak.py`: **8/8, exit 0**

| Gate | Result |
|---|---|
| **G0 STATIC/AST** — every verifier-launching child gets an explicit `env=`, and `child_env` genuinely pops the var | ✅ 1/1 in each of the 3 files |
| **G1 NECESSITY** — with the leak simulated, a bare-checkout child **hard-fails** instead of skipping | ✅ exit 1, FATAL |
| **G2 SUFFICIENCY** — same tree, scrubbed env: the child **SKIPS**, exit 0 | ✅ |
| **G3 DIVERGENCE** (load-bearing) — identical tree: leaked = RED, scrubbed = GREEN | ✅ not a no-op |
| **G4 END-TO-END** — all three verifiers green in **both** invocation modes | ✅ 9/9 · 7/7 · 8/8 in each mode |
| **G5a ANTI-WEAKENING** — strict mode **still hard-fails** when legitimately requested via argv | ✅ exit 1, FATAL |
| **G5b ANTI-WEAKENING** — reverting the scrub is **REJECTED** by G0's AST audit | ✅ verdict=REJECT |
| **G6 NO DRIFT** — production sources + verifiers byte-identical before/after | ✅ |

**G5 is the gate that matters most, and it is what makes this a correction rather than
a cover-up.** Simply *deleting* strict mode would also have turned the three red
verifiers green and satisfied G1–G4 — and it **fails G5a**. Reverting the scrub is
caught by **G5b**. The gate detects its own regression.

**The gate caught its own author, twice** — both findings changed the patch, and both are
worth recording because they are the same disease in miniature:

1. G0's first draft demanded `env=` on **every** `sys.executable` spawn — including
   INC-23's inert pricing probe, which reads no environment variable and which no scrub
   could affect. A gate that fires on things it does not care about is noise that teaches
   the team to ignore it.
2. The second draft used **string heuristics** to spot verifier spawns, and silently
   failed to match INC-23's `[sys.executable, str(script)]` — reporting **0/0 spawns** in
   a file that has one. A gate that sees nothing is blind. It is now **structural**:
   matched on AST nodes, with the verifier/probe distinction drawn from the argument
   shape, so no incidental source text can satisfy or defeat it.

**Not permanently red** — the mistake this fleet keeps repeating:

| Environment | INC-28 |
|---|---|
| Full fleet workspace | **8/8, exit 0** |
| **Bare checkout** (= exactly what CI clones) | **7/7, 1 SKIPPED, exit 0** ✅ |
| Bare checkout + **scrub stripped** (negative control) | **exit 1** ✅ still bites |

Wired into `checkout-api/.github/workflows/ci.yml` (11 steps, valid YAML), so the repair
is guarded by the pipeline and cannot rot into decoration.

### Fleet re-verified after the change

**14/14 checks GREEN in strict mode** (was 10/14): `checkout-api` npm suite + 7 verifiers ·
`fabric-gateway-demo` 12 tests + 3 verifiers · `fabric-ic-incident-target` 14 tests + 1 gate.
All three deployed sources byte-identical.

---

## 6. Blast radius — UNKNOWN, and deliberately not estimated

The three billing defects are **confirmed real** and **confirmed live** (§7, by
execution). How many customers they have touched is a question **only production
telemetry can answer**, and this run had none.

The commander will not manufacture an impact number from an invoice model it cannot
see. The owner queries that would bound it:

- **INC-6:** count checkout sessions where `len(eligible_items) < 20` and a discount tier was applied. Each is a candidate over-discount; the leak scales *inversely* with eligible-item count (1 item → $45.00 leak, 5 → $30.00, 20 → $0.00).
- **INC-5:** count `/v1/usage` batches that returned an error — every one **destroyed all valid billable tokens in the batch**, not just the malformed record.
- **INC-8:** query the usage store for a `None` / `"null"` model key. Those tokens are billable, booked, and **cannot be invoiced or rated**.

---

## 7. Owner runbooks (NOT auto-patched, deliberately)

### 7.1 INC-6 — checkout discount leak · `fabric-ic-incident-target#6`

**Re-confirmed LIVE this run by executing the deployed source:**

| Eligible items | Charged on a $300 order | Leak |
|---|---|---|
| 1 | **$255.00** | **$45.00** |
| 5 | $270.00 | $30.00 |
| 20 | $300.00 | — |

A **$0.01** item and a **$299.99** item in the same $300 order produce an **identical
$255.00 charge** — `apply_discount()` calls only `len()` on the items, so **price cannot
influence the result**. The zero-item guard is correct ($300.00).

**Two answers are needed, and both are revenue policy:**
1. What is the per-item price field called?
2. Does the discount apply to the **eligible subtotal** or the **whole order**?

**Why the commander will not guess:** the repo's own tests prove the tempting repairs
unsafe. `.get('price_cents', 0)` against a **wrong key** reads every item as free and
**charges $500.00 where the contract requires $425.00 — reporting success**. Bare
indexing throws `KeyError` on the checkout path, turning a silent revenue leak into a
**hard outage**.

**When you land the fix, CI will not fight you.** Declare the policy —
`DISCOUNT_POLICY = "eligible-items-mean"` (or `"eligible-subtotal"` / `"whole-order"` /
`"no-discount"`) — and state it in the PR.

### 7.2 INC-5 + INC-8 — usage/billing semantics · `fabric-gateway-demo#2`, `#5`

**Re-confirmed LIVE this run by executing the deployed source:**

- A record missing `tokens` raises `KeyError('tokens')` and **destroys the whole `/v1/usage` batch**, taking **140 valid billable tokens** (from 2 well-formed records) with it.
- `{"model": None, "tokens": 40}` books **40 billable tokens against a `None` key**, raises **nothing**, and `grand_total` **reconciles perfectly** — so **no downstream invoice check can catch it.** Serialized, the bucket becomes the JSON string `"null"`: a model that cannot be invoiced or rated.

**These are ONE decision, not two.** `record.get("model", "unknown")` defaults **only when
the key is ABSENT** — a record carrying `{"model": None}` still yields `None`. So that
repair fixes INC-5 and **leaves INC-8 fully live.** Decide the contract covering **both**
the missing key **and** the null value.

**When you land the fix, CI will not fight you.** Declare the policy —
`MALFORMED_RECORD_POLICY = "reject-loudly"` (or `"skip"` / `"attribute-unknown"`) — and
state it in the PR. A declaration is **not an amnesty**: declaring `reject-loudly` while
still leaking a `None` model into a billing bucket goes RED.

---

## 8. Verification gates — every claim in this brief

| Claim | How it was established |
|---|---|
| Fleet state before the patch | `run_fleet_checks.py` — full check surface, no subset |
| Telemetry unavailability | `probe_telemetry.py`, negative-controlled |
| INC-28 exists and is reachable | reproduced by execution, 3 verifiers, both invocation modes |
| INC-28 repair works | `verify_inc28_strict_mode_env_leak.py` **8/8** |
| Repair is not a cover-up | G5a (strict mode still bites) + G5b (revert is rejected) |
| Not permanently red in CI | bare checkout **7/7, 1 SKIPPED, exit 0** |
| No production drift | sha256 before/after, all 3 deployed sources |
| Billing defects still live | `probe_billing_liveness.py` — executed deployed source |
| Fleet green after the patch | **14/14** checks, strict mode |
| Blast radius | **NOT ESTIMATED** — no telemetry. Stated as UNKNOWN. |

---

*Fabric autonomous incident commander · run 2026-07-12 · INC-28 patched (deterministic,
no product-policy content) · INC-6 / INC-5 / INC-8 routed to owners · no production
behaviour changed.*
