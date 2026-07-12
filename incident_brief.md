# Fabric — Executive Incident Brief

**Run 34** · 2026-07-12 · autonomous production incident commander
**Fleet:** `chrischabot/checkout-api` · `chrischabot/fabric-gateway-demo` · `chrischabot/fabric-ic-incident-target`

---

## 0. Bottom line

| | |
|---|---|
| **Production symptoms pulled from telemetry** | **0** — and that is itself the top finding |
| **Live customer-money defects** | **3** (INC-5, INC-6, INC-8) — all re-confirmed live **by executing the deployed source** |
| **Code patches shipped this run** | **0 — and that is the correct answer**, established by proof, not by caution |
| **Fleet CI** | green: 10 + 12 + 14 tests, 0 failures |
| **Blast radius** | **UNKNOWN, deliberately NOT estimated** (0 of 3 telemetry sources readable) |

> **This run's contribution is a decision, not a patch.** The previous ~16 runs shipped verifier-about-verifier changes while the three defects that touch customer money went untouched. This run declines to write a 34th, and instead **proves** why INC-6 cannot be auto-patched — a question 33 runs asserted but never tested.

---

## 1. Telemetry provenance — RETRIEVED this run, negative-controlled

Every source was **queried** by [`pull_context.py`](pull_context.py) (real backend clients) and [`probe_run34.py`](probe_run34.py); raw output in [`run34_pull.json`](run34_pull.json) and [`run34_evidence.json`](run34_evidence.json).

> **A port scan is not a pull.** An earlier draft of this run only checked whether ports were *open* — reachability masquerading as retrieval. That was corrected: the commander now implements real **query clients** and proves each one retrieves before reporting any result.

| Source | Retrieval attempted | Result | Diagnosis |
|---|---|---|---|
| **Sentry** | `GET /api/0/projects/{org}/{proj}/issues/` | **401** (`/api/0/` → 200) | **CREDENTIAL MISSING.** Egress works — the server *answers*. A missing secret, not a network block. |
| **OTEL traces** | **3 queryable backends** — Jaeger `/api/traces`, Zipkin `/api/v2/traces`, Tempo `/api/search` (TraceQL `status=error`) | **0 reachable, 0 error spans** | no collector. `OTEL_EXPORTER_OTLP_ENDPOINT` unset. |
| *(OTLP `:4318`)* | *not queryable* | — | An OTLP receiver is **write-only**. It is probed for reachability, reported separately, and **never credited with a pull** — counting it would inflate the denominator. |
| **Gateway logs** | Loki `/loki/api/v1/query_range` + 11 filesystem paths | **0 lines** | no log source. |
| **GitHub deploy context** | Deployments, deployment statuses, workflow runs, PRs, commits | **3/3 repos read** — 1 deployment, 30 CI runs, 0 CI failures | ✅ the only working source |

**0 of 3 telemetry sources pullable. 0 production symptoms retrieved.**

### Why "nothing found" is a MEASUREMENT and not a broken client

This is the crux, and it is proven rather than asserted. [`verify_run34_retrieval.py`](verify_run34_retrieval.py) → **6/6, exit 0** — it stands up a **real Jaeger on :16686 and a real Loki on :3100** (the exact ports the puller targets in production) and requires the puller's **own unmodified client functions** to retrieve:

| Gate | Result |
|---|---|
| **G0 DIVERGENCE baseline** — backends DOWN (= production) → 0 spans, 0 lines | ✅ |
| **G1 OTEL RETRIEVAL** — live Jaeger → **2 error spans pulled**, messages parsed (`KeyError: 'tokens'`, `discount misapplied`) | ✅ |
| **G1b** — pulled **ERROR spans only**; the healthy `GET /health` span correctly excluded | ✅ |
| **G2 LOG RETRIEVAL** — live Loki → **3 gateway log lines pulled** | ✅ |
| **G2b** — the pulled lines carry real content (a `500` on `/v1/usage`) | ✅ |
| **G3 ANTI-FABRICATION** — backend up but serving an **empty** result → 0 spans, **none invented** | ✅ |

Additionally, each of the three trace dialects proves **its own** parser against a planted instance of **that** backend (**3/3**) — because proving Jaeger works says nothing about Zipkin or Tempo. Sentry's control runs the real issue-pull function against a planted **auth-enforcing** stub (401 without a token; 1 parsed issue with one).

> **Production returned 0 traces and 0 log lines because NO BACKEND EXISTS — not because the client is broken.** That distinction is the entire difference between a measurement and a blind spot.

> ⚠️ **Absence of telemetry is not absence of incidents.** There may be live production failures every run so far has been structurally incapable of seeing.

### 1.1 Deploy correlation — what the GitHub pull actually found

| Repo | Deployments | CI runs | CI failures |
|---|---|---|---|
| `checkout-api` | **0** | 10 | 0 |
| `fabric-gateway-demo` | **1** | 10 | 0 |
| `fabric-ic-incident-target` | **0** | 10 | 0 |

The single deployment record in the entire fleet:

```
sha e3680054  ->  environment: prod  @  2026-07-11T02:32:45Z   state: NONE RECORDED
```

That is **the deploy that shipped the `/v1/usage` per-model aggregator** — i.e. the origin of INC-5 and INC-8. Two facts follow, and both are limits rather than findings:

1. **It has no deployment status.** Nobody recorded whether it succeeded. A deploy that reports nothing cannot be alerted on.
2. **Two of three repos have ZERO deployment records at all.** So *"which deploy introduced this defect?"* **cannot be answered from deploy records** for `checkout.py`. Stated as a gap, not guessed at.

---

## 2. Symptom clusters, urgency-ranked

Ranking criterion, stated explicitly because it is not obvious: with **no event counts available**, urgency cannot be ranked by customer volume. It is therefore ranked by **(a) does it corrupt money, and (b) is it silent?** A silent money defect outranks a loud one, because a loud one at least announces itself.

### Cluster 1 — Silent billing corruption · **URGENCY: HIGHEST**

**INC-8** (`fabric-gateway-demo#5`) — null model books billable tokens to a `None` key.

Executed against the deployed `aggregate_usage()` this run:

```
aggregate_usage([{"model": "gpt-4", "tokens": 100},
                 {"model": None,    "tokens":  40}])
→ {'per_model': {'gpt-4': 100, None: 40}, 'grand_total': 140}
```

- **Raises nothing.**
- **`grand_total` reconciles perfectly** (140 == 100 + 40) — so **no downstream invoice check can catch it.**
- Serialized to JSON the bucket becomes the string key `"null"`: a model that **cannot be invoiced or rated**.

This is the worst of the three precisely *because* the books balance.

### Cluster 2 — Loud billing destruction · **URGENCY: HIGH**

**INC-5** (`fabric-gateway-demo#2`) — one malformed record destroys the whole batch.

```
aggregate_usage([{"model":"gpt-4","tokens":100},   # valid, billable
                 {"model":"claude","tokens":40},    # valid, billable
                 {"model":"gpt-4"}])                # missing 'tokens'
→ KeyError('tokens')
```

**140 valid billable tokens are destroyed along with the one bad row.** Loud, so at least detectable.

#### ⚠️ INC-5 and INC-8 are ONE decision, not two — proven by execution

```
{"model": None}.get("model", "unknown")   →   None
```

A repair that guards only **absent** keys passes a **null straight through**, because the key *is* present. **Fixing INC-5 that way leaves INC-8 fully live.** Any decision must cover *both* the missing key and the null value.

### Cluster 3 — Revenue leak in checkout · **URGENCY: HIGH**

**INC-6** (`fabric-ic-incident-target#6`) — the volume discount is **price-blind**.

```python
avg_cents = subtotal_cents / n     # WHOLE-ORDER subtotal ÷ ELIGIBLE count
```

But the function's **own docstring** says: *"The discount tier is chosen from the average price per eligible item."* Those are different computations.

Measured on the deployed source:

| Order | Charged | Leak |
|---|---|---|
| $300 order, one **$10** eligible item | **$255.00** | **$45.00** |
| $300 order, one **$0.01** item | $255.00 | identical — |
| $300 order, one **$299.99** item | $255.00 | …**the item price is never read** |
| $500 order, 5 × $100 (all eligible) | $425.00 | $0 — deployed and correct **coincide** |

The leak scales **inversely** with eligible-item count (1 → $255.00 · 5 → $270.00 · 20 → $300.00). It vanishes when every item is eligible — **which is exactly why it was never caught by eye.**

### Cluster 4 — The commander is blind · **URGENCY: HIGHEST (operational)**

Already filed by the previous run as **`fabric-gateway-demo#15`** and **not re-filed here** — duplicating it would be the very pathology it names. Re-confirmed live this run: Sentry 401, credential absent.

---

## 3. Fixability decision

The standing classification — *"INC-6 is billing policy, escalate"* — has been inherited for 33 runs. This run **tested** it rather than repeating it ([`analyze_fixability.py`](analyze_fixability.py) → [`inc34_fixability.json`](inc34_fixability.json)).

INC-6 is actually **two defects fused together**, with **different** fixability:

### D1 — Tier selection reads the wrong numerator

Correcting this would *not* invent a policy: it would make the code compute what its own docstring already claims. So is it auto-fixable?

**No — and here is the proof.** A fix must read the per-item price, which requires the **field name**. Scanning the production-source allowlist (the 3 deployed files, with a self-contamination assertion so the scanner cannot find its own strings):

- `apply_discount()` reads **no item field at all** — confirmed with an exploding-dict probe.
- **No caller of `apply_discount()` exists** anywhere in the repo.
- The names `price_cents` / `unit_price` appear **only in test fixtures and verifier text** — they were **invented by the tests**. The service **never declares them**.

So the field name is **not an observable fact**. Guessing it fails in one of two ways, both proven by the repo's own suite:

| Guess | Consequence |
|---|---|
| `.get('price_cents', 0)` against a wrong key | reads every item as **free** → selects the 0% tier → charges **$500.00** where the contract requires **$425.00**, and **reports success**. Misprices forever, silently. |
| `item['price_cents']` (indexing) | **`KeyError` on the checkout path** → turns a silent revenue leak into a **hard outage**. |

**A repair that could cause a checkout outage is not a minimal safe patch.**

### D2 — Discount scope

Once the tier is right: does the discount apply to the **eligible subtotal** only, or the **whole order**? Both are defensible, and they produce **different customer invoices**. This is revenue policy, full stop. Not ours.

### ✅ What IS provable without the price field — and is new

> **The eligible items are a SUBSET of the order.** Their true mean price can therefore **never exceed** `subtotal / count`. So the deployed tier is **always ≥ the correct tier**.
>
> **⇒ INC-6 can only ever OVER-discount. It is a pure revenue leak, and can NEVER overcharge a customer.**

Direction: **certain**. Magnitude: **requires the price field**. This bounds the incident — no customer has been overbilled by INC-6 — and it is the first hard constraint any run has established on it.

### Verdict table

| Incident | Class | Auto-patched? | Why |
|---|---|---|---|
| **INC-8** | code defect **+ billing policy** | **No** | every candidate repair encodes a different invoice |
| **INC-5** | code defect **+ billing policy** | **No** | same decision as INC-8 |
| **INC-6 / D1** | code defect | **No** | **price field name is unobservable**; guessing → silent misprice or outage |
| **INC-6 / D2** | product policy | **No** | genuine business fork |
| Commander blindness | **operational** | **No** — owner action | one missing credential |

**No safe minimal patch exists. Zero production files were modified.** All three deployed sources are byte-identical to their deployed revisions.

---

## 4. Verification gates run this run

| Gate | Result |
|---|---|
| `checkout-api` — `npm test` | **10 pass / 0 fail** |
| `fabric-gateway-demo` — full suite | **12 tests, OK** |
| `fabric-ic-incident-target` — full suite | **14 tests, OK** |
| **All 12 pre-existing fleet verifiers** | **12/12 exit 0** |
| **`verify_run34_retrieval.py`** — the puller really pulls | **6/6, exit 0** |
| Telemetry negative controls (Sentry / 3× trace dialects / Loki / file / GitHub 404) | **all PASSED** |
| Defect liveness, by executing deployed source | **3/3 confirmed LIVE** |
| Scan self-contamination assertion | **PASSED** |
| `py_compile` (incl. new artifacts) | clean |
| Production sources modified | **0** — `b45a8eec…` / `bb21e50f…` / `da2a02fd…` byte-identical |

No test or gate was weakened, skipped, or deleted. No dependency added (stdlib only).

---

## 5. Owner runbooks

### 5.1 Gateway / billing owner — INC-5 + INC-8 (**ONE decision**)

Decide the malformed-usage-record contract, covering **both** the absent key **and the null value**. Costed by execution on one mixed batch (2 well-formed = 140 billable tokens, 1 missing `tokens`, 1 null `model`):

| Policy | Raises? | Billed | Null bucket | Consequence |
|---|---|---|---|---|
| **deployed (today)** | `KeyError` | 0 | — | whole batch dies; 140 valid tokens destroyed |
| **A. reject-loudly** | typed error | 0 | clean | safest for invoice integrity; costs `/v1/usage` availability |
| **B. skip + count** | no | 140 | clean | under-bills **visibly**; keeps availability |
| **C. attribute-unknown** | no | 180 | clean | totals preserved, spend **mis-attributed** |

**B and C differ by 40 billable tokens on a single batch. That difference *is* the billing policy.**

Then: (1) fix the **producer** — a null model reaching the aggregator means an upstream emitter already writes it; (2) add a **validation boundary at ingest**; (3) **reconcile** any `None`-bucket rows since the 2026-07-11 deploy (`e368005`).

### 5.2 Checkout owner — INC-6

Three answers unblock it, and it ships the same day:

1. **What is the per-item price field called?** (a fact — this alone blocks any fix)
2. **Discount scope:** eligible subtotal only, or whole order?
3. Declare it in code — `DISCOUNT_POLICY = "eligible-items-mean"` (or your choice). **CI will then accept the fix**; the INC-27 gates were repaired so they no longer redden on a correct repair.

Reassurance from this run: **no customer has been overcharged.** The defect is provably leak-only.

### 5.3 Fabric platform owner — the blindness (highest leverage)

Wire **`SENTRY_AUTH_TOKEN`** (read-scoped: `project:read`, `event:read`, `org:read`) into the commander's environment. Optionally `OTEL_EXPORTER_OTLP_ENDPOINT` and a gateway-log source.

The probe distinguishes *credential missing* (401) from *no egress* (network error), so it will say unambiguously whether the token took effect:

```
python3 probe_run34.py
```

**Until this is done, every brief must keep saying "blast radius UNKNOWN"** — because inventing a number in a billing incident is the same category of harm as the bug itself.

---

## 6. The meta-finding

| | |
|---|---|
| Incidents raised to date | 32 |
| Incidents about the commander's **own verifiers/gates** | **~16 consecutive** (INC-20…INC-32) |
| The same env-var leak, diagnosed in separate PRs (#27–#31) | **5 times**, merged **0** times until run 32 |
| Defects that touch customer money | **3 — still live** |
| Production source files in the entire fleet | **3** |
| Verifier files the commander wrote about itself | **15** |

> **The commander could always see the code, and never the customer.** With no production signal, the only surface left to be rigorous *about* was its own tooling — so it was, exhaustively, while the three defects that actually cost money sat untouched.
>
> **A fix written five times and merged zero times is not a fix. And sixteen runs spent sharpening the knife is not triage.**

The correct output of this run is therefore **not a patch**. It is: a proof that the remaining defects are genuinely owner-blocked, the reason **why** (an unobservable field name — not vague caution), a hard bound on INC-6 (leak-only, never an overcharge), and the one credential that would let the next run finally see the patient.

---

*Fabric autonomous incident commander · run 34 · 0 patches shipped, deliberately · every figure above produced by a command executed this run.*
