# Fabric Autonomous Incident Commander — Executive Incident Brief

**Run:** INC-16 · **2026-07-11 ~23:52 UTC** (timestamp taken from the sandbox
system clock and cross-checked against GitHub event times; see §8 on dates).
**Fleet:** `chrischabot/checkout-api`, `chrischabot/fabric-gateway-demo`,
`chrischabot/fabric-ic-incident-target`.
**Change shipped:** verifier + CI + brief only. **No production source modified.**
All three deployed sources are byte-identical to their deployed revisions on the
**full** sha256 (§6).

---

## 1. Executive summary

This run found and repaired a defect **inside the fleet's own verification
machinery**, and re-confirmed — by executing deployed source — that three
previously-escalated billing defects are **still live on `main`**.

The headline finding (INC-16): `checkout-api`'s
`artifacts/incident/verify_inc9_ci_gate.py` carries three **cross-fleet gates**
(G6a/G6b/G6c) whose only job is to re-confirm the three owner-blocked billing
defects are still live, *by executing the deployed source*. Its sibling-repo
discovery searched for directories named `incident-target` and `gateway`; the
real repos are `fabric-ic-incident-target` and `fabric-gateway-demo`. **The
lookup never matched in any environment**, so those three gates were unreachable
dead code — and the skip path then tallied `passed == total` over only the gates
that *had* run, printing a confident **"GATES: 6/6 passed"** while a third of the
verifier never executed.

That is this fleet's signature failure — *a gate that cannot fail is decoration*
— reproduced **inside the verifier whose only job is to police it.** A skip
laundered into a pass count is worse than a missing check: it actively asserts
coverage it does not have.

> **This exact defect was diagnosed by PRs #11, #13 and #14 — and NONE merged**
> (#11 and #13 closed unmerged, #14 left open). So the repair never landed on
> `main`, and the blind discovery was **still live there** at the start of this
> run. This run lands it against current `main` and adds a dedicated verifier
> that proves the gates now execute.

**Nothing was auto-patched in production.** The three billing defects remain
owner decisions (§5). This run makes the existing guards *run*; it does not
pre-empt the owner.

---

## 2. Telemetry coverage gap (stated plainly, up front)

**No production telemetry was reachable this run.** Every finding below was
established by **executing the deployed source**, not by reading production
signal. Concretely, probed this run:

| Source | Probe result | Consequence |
|---|---|---|
| **Sentry** | `https://sentry.io/api/0/` → **HTTP 200 but `{"auth":null,"user":null}`** — reachable, unauthenticated, no credential in the environment | **zero issue data** |
| **OTEL / traces** | ports 4317, 4318, 9411, 16686, 14268 all **closed** (no listener) | **no collector, no traces** |
| **Gateway logs** | no source on disk, none mounted, no endpoint configured | **no log stream** |
| **GitHub REST** (scoped connector) | live | the only usable evidence source |

**Blast radius is therefore UNKNOWN and is deliberately NOT estimated.** How many
orders were over-discounted, how many `/v1/usage` batches died, and how many
tokens were mis-attributed to a `None` model are all questions that require the
telemetry above. The owner queries that would bound them are in §7 — those are
steps only an owner with credentials can run.

*Absence of telemetry is not absence of incidents.* There may be live production
issues this run simply could not see.

---

## 3. Incident clusters (urgency-ranked)

### Cluster A — the verification machinery lies about its own coverage · **CODE-FIXABLE · fixed this run**

- **INC-16.** `verify_inc9_ci_gate.py` cross-fleet discovery is blind; the skip
  path launders un-run gates into a "6/6 passed" tally. Deterministic, no billing
  semantics involved → safely patchable. **Repaired + verified this run.**

### Cluster B — live billing/revenue defects · **NOT code-fixable · owner decision**

All three re-confirmed **live on `main`** this run by executing deployed source
(the now-repaired G6a/G6b/G6c):

- **INC-6** (`fabric-ic-incident-target#6`) — checkout volume-discount leak. A
  $300 order with one $10 eligible item is **charged $255.00** (contract:
  $300.00). Item price is ignored entirely; the leak grows as eligible items get
  cheaper. Highest urgency in this cluster (silent revenue loss, scales badly).
- **INC-5 / INC-3** (`fabric-gateway-demo#2`) — one malformed `/v1/usage` record
  raises `KeyError` and **destroys the whole batch**. Loud failure, availability
  impact.
- **INC-8** (`fabric-gateway-demo#5`) — a `null` model **silently** books billable
  tokens under a `None` key: `{'model': None, 'tokens': 10}` →
  `{'per_model': {None: 10}, 'grand_total': 10}`, no error. Quietest of the three;
  corrupts per-model billing with no signal.

---

## 4. Fixability analysis

**Code-deterministic defects with no policy content are patched; anything that
encodes a billing/revenue decision is routed to the owner.**

- **INC-16 is code-deterministic.** Two repo names were wrong and a tally counted
  skips as passes. There is exactly one correct set of names and exactly one
  correct way to count. No customer-visible behaviour, no invoicing policy. →
  **Patched.**
- **INC-6 / INC-5 / INC-8 are policy.** Each candidate repair encodes a different
  invoicing decision, and the repo's own tests prove the tempting one-liners are
  unsafe:
  - INC-6: deployed `apply_discount()` calls only `len()` — it never reads any
    item price field, and no caller/schema/test names one. `.get('price_cents', 0)`
    against the wrong key reads every item as free (0% tier, **charges $500.00
    instead of $488.00 and reports success**); indexing throws `KeyError` on the
    checkout path (silent leak → hard outage). The price field name AND the
    discount scope are both revenue-policy decisions.
  - INC-5 + INC-8 are one contract from two sides: a repair guarding only *absent*
    keys (`.get("model", "unknown")`) passes a **`None` value straight through**,
    because the key *is* present. Fixing INC-5 without deciding the null case
    leaves INC-8 live.
  → **Escalated, not guessed.** The commander will not invent billing semantics.

---

## 5. The repair (INC-16) and why it is safe

**Files changed (all in `checkout-api`, none in production):**
- `artifacts/incident/verify_inc9_ci_gate.py` — discovery matches the real repo
  names (legacy names kept as fallbacks); skips tracked in their own list, so a
  skip can never enter the pass tally *or* the denominator; strict mode
  (`--require-cross-fleet` / `FABRIC_REQUIRE_CROSS_FLEET=1`) makes a missing
  sibling FATAL where the gates are expected to run.
- `artifacts/incident/verify_inc16_cross_fleet_gate.py` — **new** verifier that
  proves the gates now execute. Environment-aware (see below).
- `.github/workflows/ci.yml` — runs the new verifier, so the repair is guarded.

**Three design points that keep this honest:**

1. **The fix ADDS names, never replaces them.** Any checkout using the legacy
   layout keeps working (G3 proves it against a synthetic legacy tree).
2. **A skip is a third state.** Not fatal (that would leave `checkout-api` CI —
   which clones only one repo — permanently red, the INC-11/INC-12 bug), and not
   a pass. Reported, and promotable to fatal by a caller that knows the siblings
   ought to be there.
3. **The gates test the SHIPPED code, not a copy.** G2/G3/G5 drive the deployed
   `_find` / `_TARGET_DIRS` / `_GATEWAY_DIRS` symbols imported from the real
   module. A first draft re-implemented the lookup locally — which would have
   passed even if the shipped lookup were still blind. Fixed.

---

## 6. Verification — what was actually run this turn

### The INC-16 finding, proven by DIVERGENCE on the same filesystem

Same workspace, both siblings present:

| | Result |
|---|---|
| **Deployed (pre-repair) verifier** | `GATES: 6/6 passed`, exit 0 — **G6a/G6b/G6c never executed** |
| **Repaired verifier** | `GATES: 9/9 passed`, exit 0 — **executed `['G6a','G6b','G6c']`** |

G4/G5 of the INC-16 verifier prove the old discovery was **blind on the very
filesystem** where the new one succeeds — so the repair is not a no-op.

### INC-16 verifier — 8/8, exit 0 (fleet workspace)

G1 repaired verifier 9/9 with cross-fleet gates RUN · G2 shipped discovery
resolves both real names · G3 legacy names still resolve · **G4 WITNESS A** old
discovery blind · **G5 WITNESS B** divergence (OLD skips 3 · NEW executes 3) ·
**G6 NEGATIVE CONTROL** absent siblings → SKIPPED, exit 0 · **G7** strict mode →
exit 1 FATAL · **G8** no production drift (full sha256).

### The gate provably bites, and is not permanently red — all four modes

| Environment | INC-16 result |
|---|---|
| Bare checkout, default (= `checkout-api` CI) | **3/3 passed, 1 SKIPPED, exit 0** — not permanently red |
| Bare checkout, `--require-cross-fleet` | **exit 1, FATAL** — refuses to pass un-run gates |
| Fleet workspace, as shipped | **8/8, exit 0** |
| **Shipped discovery regressed to the blind names** | **4/8, exit 1** — G1/G2/G4/G5 FAIL ✅ caught (SKIP cannot launder it green) |

Environment detection uses a lookup **independent** of the shipped code, so a
regressed (blind) discovery cannot masquerade as "siblings absent" and skip its
way to green.

### Cross-fleet defects re-confirmed LIVE on `main` (executed deployed source)

- **G6a / INC-6** — $300 order / one $10 eligible item → **charges $255.00**.
- **G6b / INC-5** — one malformed record → `KeyError`, whole batch dies.
- **G6c / INC-8** — `{'model': None, 'tokens': 10}` → `{'per_model': {None: 10}}`.

### Full fleet gate surface — 36 tests, 7 verifiers, zero failures

| Repo | Suite | Verifiers |
|---|---|---|
| `checkout-api` | `npm test` **10/10** | inc9 **9/9** · inc12 exit 0 · inc16 **8/8** |
| `fabric-gateway-demo` | unittest **16/16** | inc5 · inc8 · inc10 all exit 0 |
| `fabric-ic-incident-target` | unittest **10/10** | checkout gate exit 0 |

### No production drift

| File | sha256 (full) |
|---|---|
| `checkout-api/service/checkout/session.js` | `b45a8eeceaa142dd70aea4182930d02edb2d23ce90f0f02527910abb5f18d7e8` |
| `fabric-ic-incident-target/checkout.py` | `da2a02fd87aec668467114e0bc30ff7c2fe7fd3d8f105f5f156361b9c87c5c5e` |
| `fabric-gateway-demo/service/usage_aggregator.py` | `bb21e50f7b5dab4463b71984bbe86a5df2b6ba442ffeff84d9b70815781750e5` |

---

## 7. Owner runbooks (for the escalated billing defects)

These are the steps only an owner with production credentials can perform — they
bound the blast radius this run could not measure.

### INC-6 — checkout discount leak
1. **Quantify exposure.** Query completed orders with a **low eligible-item
   count but a high subtotal** — those are the over-discounted ones. The leak is
   largest at `n=1`; start there.
2. **Consider a temporary tier ceiling** (or disable volume discounts for orders
   with fewer than N eligible items) if exposure is material. Config/flag action,
   not a code fix.
3. **Do not "fix the average" by dividing by the whole-cart item count** — that
   silently changes which orders qualify and is itself a policy change.
4. **Answer the two policy questions** (price field name; discount scope =
   eligible-subtotal only vs. whole order), then land a schema-guarded patch with
   a regression gate.

### INC-5 / INC-3 + INC-8 — malformed / null usage records
1. **Decide the malformed-record contract ONCE, covering both cases:** a
   **missing** `model`/`tokens` key (INC-5/3), and a **`null`** value (INC-8).
   Answering only the first leaves the second live.
2. **Check whether `None` buckets already exist** in any stored per-model
   aggregate or invoice breakdown since the 2026-07-11 deploy (PR #1, commit
   `e368005`).
3. **Fix the producer too** — a `null`/missing field reaching the aggregator means
   an upstream emitter already writes it; patching only the consumer masks it.
4. **Add an ingest validation boundary**, then **reconcile** any rows dropped or
   mis-attributed since that deploy.

**Suggested owner:** the gateway/billing owner for `/v1/usage` and its emitters;
the checkout/revenue owner for `apply_discount()`.

---

## 8. Housekeeping

- **Date correction.** Several earlier reports in this fleet are dated
  "2026-07-14". That is a wrong date copied forward between runs and is
  contradicted by the GitHub timestamps on those same PRs/issues (all
  2026-07-11). This run is dated from the system clock: **2026-07-11**.
- **Superseded PRs.** PRs **#11** and **#13** (closed unmerged) and **#14**
  (open) all diagnosed this same defect but never landed. The commander
  recommends closing #14 as superseded by this run, which lands the repair
  against current `main` and adds the dedicated INC-16 verifier + CI guard.

---
*Fabric autonomous incident commander · INC-16. Every claim above was established
by executing deployed source; no production telemetry was reachable this run, and
blast radius is deliberately not estimated.*
