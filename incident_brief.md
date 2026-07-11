# Incident Brief — Fabric Autonomous Incident Commander

**Run:** 2026-07-14 · **Fleet:** chrischabot/{checkout-api, fabric-ic-incident-target, fabric-gateway-demo}
**Patches applied:** 1 (CI gate, zero production source touched) · **Owner-routed:** 2 · **Verified closed:** 1

---

## Executive summary

Two revenue-affecting code defects are **still live in production**, and both remain
**blocked on policy, not on engineering**. Neither is safe to auto-patch: every
mechanical repair silently encodes a different billing or revenue policy, and the
authoritative source that would settle the question does not exist in reach.

The finding that *is* actionable is systemic, and this run fixes it. **No repo in the
fleet runs any check on a pull request.** `checkout-api` is the sharpest case: it carries
a merged, passing 10-test regression suite whose mutation check provably fails on the
defect that caused INC-1 — and **nothing ever executed it.** The guard was dead code.
This run makes it bite, verified 8/8 gates.

> **The pattern behind every incident this fleet has produced:** each one shipped through
> a pull request that changed a code path with **no test executing against it.** INC-1,
> INC-2, INC-3 — three for three. The defects are symptoms. The missing gate is the cause.

### Telemetry provenance gap — read before trusting any severity number

No Sentry, OTEL, or gateway-log source was reachable this run. Only the GitHub connector
was connected (GraphQL returned 403; no telemetry connector is configured, and no
credentials or fixture feeds exist in the workspace or in the repos).

**Every symptom below was therefore established by executing the deployed source
directly, not by reading production telemetry.** The defects are *confirmed real and
confirmed live*. But **user-facing blast radius — event counts, affected customers,
revenue actually lost — is UNKNOWN and is deliberately not estimated here.** No incident
in this brief is inferred from a signal I could not read, and nothing is invented to
fill the gap.

To restore true cross-source clustering, wire a Sentry/OTEL connector or provide a log
feed; the commander will then correlate these code defects against real event volume.

---

## Incident clusters (urgency-ranked)

### INC-6 · Checkout discount leak — LIVE · revenue-affecting · OWNER-BLOCKED

`fabric-ic-incident-target/checkout.py` at `apply_discount()` — sha256 `da2a02fd87ae`

The discount tier is selected from `subtotal_cents / n`, where `subtotal_cents` is the
**full order subtotal** but `n` counts **only eligible items**. Ineligible items inflate
the average, so an order buys a tier it never earned.

Reproduced this run (triage_probe.py, P1–P2):

| Order shape | Charged | Tier applied | Leak |
|---|---|---|---|
| 1 eligible item in a $300 order | $255.00 | **15%** | **$45.00** |
| 2 eligible items in a $300 order | $255.00 | 15% | $45.00 |
| 5 eligible items in a $300 order | $270.00 | 10% | $30.00 |
| 20 eligible items in a $300 order | $300.00 | 0% | $0.00 |

**The leak scales inversely with eligible-item count — the fewer items qualify, the
larger the discount.** That is backwards, and it is worst on precisely the orders that
should receive no discount at all.

**Why it is not auto-patched.** A correct fix must know each eligible item's price. Probe
P3 confirms `apply_discount()` **never reads any field off the item dicts** — it only
takes `len()`. The repo has no caller, no schema, and no test that names a price field.
A fix therefore requires (a) guessing the price field name and (b) choosing a discount
scope: eligible subtotal only, or whole order. Both are revenue-policy decisions.
Guessing wrong mischarges customers with no error signal.

**Owner action:** name the per-item price field and the discount scope. PR #3 already
carries a schema-guarded patch proposal (both policy options, 22/22 gates) that lands
the moment those two answers exist.

---

### INC-5 · /v1/usage batch failure — LIVE · billing-integrity · OWNER-BLOCKED

`fabric-gateway-demo/service/usage_aggregator.py` at `aggregate_usage()` — sha256 `bb21e50f7b5d`

A record missing `model` or `tokens` raises KeyError and **takes the entire batch down
with it.** Reproduced this run (P4–P5): in a 4-record batch containing one malformed row,
**357 billable tokens across 3 valid records are lost.**

**Why it is not auto-patched.** The mechanical fix is a one-liner — and that is exactly
the trap. Each variant encodes a different billing policy:

| Candidate repair | Consequence |
|---|---|
| default the tokens lookup to 0 | under-bills the customer |
| skip the malformed record | drops revenue silently |
| attribute it to model=unknown | mis-attributes spend |
| fail the batch (**today's behaviour**) | protects invoice integrity, costs availability |

Getting this wrong corrupts customer invoices **with no error signal**. The commander
will not invent billing semantics. Issue #2 has been open since 2026-07-11, unanswered.

**Owner action:** choose the malformed-record policy. PR #4 carries a characterization
suite that pins today's behaviour and goes **red if someone quietly ships the
default-to-zero repair** — so the billing decision can no longer be made by accident.

---

### INC-7 · The fleet has no PR gate — FIXED THIS RUN

**This is the root cause behind all of the above.** Verified by direct inspection of all
three clones: **no .github/workflows/ directory exists in any repo.** Every incident this
fleet has produced merged through a PR with no test executing against the changed path.

`checkout-api` is the sharpest case. It **already has** a passing 10-test regression suite
on main (test/session.test.js, merged in PR #3) whose mutation check fails on the INC-1
defect. Nothing ran it. **A guard that never executes is decoration.**

**Patch applied:** `fleet/checkout-api/.github/workflows/ci.yml` — runs npm test on every
pull_request and push to main (Node 20 + 22). No production source touched. No dependency
added: the suite is zero-dependency node:test.

---

### INC-1 · Cold-cache auth TypeError — REMEDIATED, verified closed

`checkout-api/service/checkout/session.js` — sha256 `b45a8eeceaa1`

The guard is present in the deployed source and behaves correctly: probe P6 executed a
cold-cache resume against live main and observed `{ok: false, reason: no_refresh_token}`
instead of a throw. **No regression.** The only thing missing was a gate that runs the
suite proving it — which INC-7 now supplies.

---

## Verification gates — INC-7 patch

`python3 artifacts/incident/verify_inc7_ci_gate.py` → **8/8 PASS, exit 0**

| Gate | Check | Result |
|---|---|---|
| G1 | Production source byte-identical to upstream main; ci.yml genuinely new | **PASS** — 3/3 files identical, 0 drifted, ci.yml absent upstream |
| G2 | ci.yml is valid YAML | **PASS** |
| G3 | Triggers on pull_request AND push:[main] | **PASS** |
| G4 | Invokes package.json's **real** test script | **PASS** — npm test → node --test test/*.test.js |
| G5 | Suite **GREEN** against deployed HEAD | **PASS** — 10 pass / 0 fail |
| G6 | **MUTATION:** reintroduce the INC-1 defect → suite goes **RED** | **PASS** — 4 pass / **6 fail**, exit 1 |
| G6b | Mutation reproduces the **exact** production error | **PASS** — TypeError: Cannot read properties of null (reading refreshToken) |
| G7 | Production source sha256 unchanged by this run | **PASS** — b45a8eeceaa142dd |

**G6/G6b are the load-bearing gates.** A CI workflow that cannot fail is decoration. This
one goes red on the precise defect that caused INC-1 — meaning **it would have blocked the
PR that caused INC-1.**

G1 deserves a note on method. Its first implementation shelled out to `git status`. The
clone carries no .git directory, so that check reported "nothing changed" and passed
**vacuously**. It now fetches upstream bytes from raw.githubusercontent.com and compares
sha256 directly — a gate that can actually fail. A safety check that cannot observe the
thing it guards is worse than no check at all.

---

## Provenance

| Source | Status | What it contributed |
|---|---|---|
| Deployed source (3 repos, cloned this run) | authoritative | Every symptom, reproduced by execution |
| GitHub PR / deploy / issue context | connector OK | PR #1 causal chain per repo; 4 stranded gate PRs; INC-3 open and unanswered |
| Sentry issues | **unreachable** | No connector configured — no event volume, no severity data |
| OTEL traces | **unreachable** | No connector configured — no latency/span correlation |
| Gateway logs | **unreachable** | No feed in reach — no request-level blast radius |
| GitHub GraphQL | 403 | Token lacks scope; REST connector used instead |

Reproduction evidence: `artifacts/incident/triage_probe.py` (P1–P6, executed against live main)
Verification: `artifacts/incident/verify_inc7_ci_gate.py` (8/8 gates, exit 0)

---

## Owner decision queue

The commander is blocked on two answers, not on engineering. Both patches are already
written and verified.

1. **INC-6 / checkout discount** — What is the per-item price field, and does the discount
   apply to the eligible subtotal or the whole order? Unblocks PR #3 (22/22 gates, both
   policy options pre-built).
2. **INC-5 / /v1/usage malformed records** — Under-bill, drop, mis-attribute, or keep
   failing the batch? Unblocks the billing patch; PR #4's suite already prevents this from
   being decided silently.
3. **Merge the four stranded gate PRs.** Every guard this fleet has built lives in an
   unmerged PR. Until they land, the next incident ships exactly the way the last three did.
