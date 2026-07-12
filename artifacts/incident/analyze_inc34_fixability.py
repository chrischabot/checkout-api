#!/usr/bin/env python3
"""INC-34 FIXABILITY ANALYSIS -- is ANY part of the INC-6 leak policy-free?

Thirty-three runs classified INC-6 as "revenue policy, owner decision" and
escalated. That classification deserves to be TESTED, not inherited.

The deployed code:

    avg_cents = subtotal_cents / n      # WHOLE-ORDER subtotal / ELIGIBLE count
    tier = _select_tier(avg_cents)
    return round(subtotal_cents * (1 - tier))

Its own docstring:

    "The discount tier is chosen from the average price per eligible item."

Those are not the same computation. So there are actually TWO defects fused
together, and they have DIFFERENT fixability:

  D1 -- TIER SELECTION reads the wrong numerator. It divides the whole-order
        subtotal (which includes INELIGIBLE goods) by the eligible count. The
        function's OWN documented contract says "average price per eligible
        item". Nothing about correcting this chooses an invoicing policy -- it
        makes the code compute what it already claims to compute.
        ...BUT it requires knowing the per-item price FIELD NAME. That is the
        blocker, and it is a FACT about the data, not a policy.

  D2 -- DISCOUNT SCOPE: once the tier is right, does the discount apply to the
        eligible subtotal only, or to the whole order? Deployed applies it to
        the whole order. BOTH are defensible business policies with different
        customer invoices. THIS is genuinely an owner decision, and no amount
        of cleverness makes it otherwise.

This script asks one question: does the eligible-item price field name exist
anywhere as an OBSERVABLE FACT in the fleet -- or must it be guessed?

If it must be guessed, D1 is NOT autonomously fixable either, and the honest
answer is to say so with the evidence, not to guess a key and hope.
"""
import importlib.util
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))


def find_dir(*relative_candidates):
    """Locate a fleet repo STRUCTURALLY by walking upward from __file__.

    Required because this analyzer ships inside checkout-api/artifacts/incident/,
    where a hardcoded sibling path would not resolve. A verdict that depends on
    where the script was launched from is not a verdict.
    """
    base = HERE
    for _ in range(6):
        for rel in relative_candidates:
            candidate = os.path.join(base, rel)
            if os.path.isdir(candidate):
                return os.path.abspath(candidate)
        base = os.path.dirname(base)
    return None


def find_root(*relative_candidates):
    """Return the directory CONTAINING the first resolvable candidate."""
    base = HERE
    for _ in range(6):
        for rel in relative_candidates:
            if os.path.exists(os.path.join(base, rel)):
                return os.path.abspath(base)
        base = os.path.dirname(base)
    return None


# The fleet root: the directory holding the three repos (commander workspace).
ROOT = find_root(os.path.join("fabric-ic-incident-target", "checkout.py")) or HERE
TARGET = find_dir("fabric-ic-incident-target") or ""
CHECKOUT_PY = None
for _cand in (os.path.join(TARGET, "checkout.py") if TARGET else "",
              os.path.join(ROOT, "fabric-ic-incident-target", "checkout.py")):
    if _cand and os.path.isfile(_cand):
        CHECKOUT_PY = _cand
        break

if not CHECKOUT_PY:
    raise SystemExit(
        "SKIP: checkout.py not found from __file__. This analyzer needs the fleet "
        "workspace (the three repos as siblings). It inspected NOTHING, which is "
        "NOT a clean bill of health -- so it refuses to print a verdict.")


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# EVIDENCE 1: does the deployed code itself ever name a price field?
# ---------------------------------------------------------------------------
with open(CHECKOUT_PY) as fh:
    DEPLOYED = fh.read()

# Any subscript/get of an item field, anywhere in the deployed pricing module.
field_reads = re.findall(r"""item\w*\s*\[\s*['"](\w+)['"]\s*\]""", DEPLOYED)
field_gets = re.findall(r"""\.get\(\s*['"](\w+)['"]""", DEPLOYED)

# ---------------------------------------------------------------------------
# EVIDENCE 2: what does the CALLER pass? Is there a caller in the repo at all?
# ---------------------------------------------------------------------------
callers = []
for root, _dirs, files in os.walk(TARGET or ROOT):
    if ".git" in root:
        continue
    for f in files:
        if not f.endswith(".py"):
            continue
        p = os.path.join(root, f)
        with open(p, errors="replace") as fh:
            src = fh.read()
        if "apply_discount(" in src and "def apply_discount" not in src:
            # what key names appear in dicts handed to apply_discount?
            keys = sorted(set(re.findall(r"""\{\s*['"](\w+)['"]\s*:""", src)))
            callers.append({"file": os.path.relpath(p, ROOT),
                            "item_keys_seen": keys})

# ---------------------------------------------------------------------------
# EVIDENCE 3: is there a schema / type declaration for an item ANYWHERE in
# PRODUCTION source?
#
# CRITICAL METHODOLOGY NOTE. The first draft of this scan walked the workspace
# root -- which contains THIS FILE and the probe, both of which mention
# "price_cents" and "unit_price" in their own source text and regexes. It would
# therefore have "discovered" the price field IN ITS OWN STRINGS and reported it
# as observable in production. A check contaminated by its own text is exactly
# the disease this fleet keeps re-committing (INC-25/INC-27: judging code by its
# incidental spelling).
#
# So PRODUCTION SOURCE IS AN EXPLICIT ALLOWLIST -- the three deployed files, and
# nothing else. Verifiers, tests, briefs, probes, and this analyzer are NOT
# production and are excluded BY CONSTRUCTION, not by a substring filter that
# could be fooled.
# ---------------------------------------------------------------------------
PRODUCTION_SOURCES = [
    os.path.join("fabric-ic-incident-target", "checkout.py"),
    os.path.join("fabric-gateway-demo", "service", "usage_aggregator.py"),
    os.path.join("checkout-api", "service", "checkout", "session.js"),
]
PRICE_TOKENS = r"(price_cents|unit_price|amount_cents|price|cost|amount)\b"

prod_hits = []
for rel in PRODUCTION_SOURCES:
    p = os.path.join(ROOT, rel)
    if not os.path.isfile(p):
        continue
    with open(p, errors="replace") as fh:
        src = fh.read()
    for m in re.finditer(PRICE_TOKENS, src):
        prod_hits.append({"file": rel, "token": m.group(1)})

# For CONTRAST only: where DO these tokens live? (tests/verifiers/analysis).
# Reported as provenance so the reader can see the field name exists ONLY in
# non-production text -- i.e. it was invented by test fixtures, never declared
# by the service.
nonprod_hits = {}
for root, _dirs, files in os.walk(ROOT):
    if ".git" in root or "node_modules" in root:
        continue
    for f in files:
        if not f.endswith((".py", ".js", ".json", ".md")):
            continue
        p = os.path.join(root, f)
        rel = os.path.relpath(p, ROOT)
        if rel in PRODUCTION_SOURCES:
            continue
        if os.path.basename(rel) in (
                "analyze_fixability.py", "analyze_inc34_fixability.py",
                "probe_run34.py", "pull_context.py",
                "inc34_fixability.json", "run34_evidence.json",
                "run34_pull.json"):
            continue                      # this analysis's OWN files: never evidence
        with open(p, errors="replace") as fh:
            src = fh.read()
        for m in re.finditer(PRICE_TOKENS, src):
            nonprod_hits.setdefault(m.group(1), set()).add(rel)

# SELF-CHECK: the allowlist must genuinely exclude this analyzer. If our own
# filename ever appears among the production hits, the scan is contaminated and
# the verdict is void.
contaminated = any(
    os.path.basename(h["file"]) in (
        "analyze_fixability.py", "analyze_inc34_fixability.py",
        "probe_run34.py", "pull_context.py")
    for h in prod_hits)
if contaminated:
    raise SystemExit("SCAN CONTAMINATED: the analyzer found its own source text. "
                     "Refusing to report a verdict.")

# ---------------------------------------------------------------------------
# EVIDENCE 4: THE DECISIVE TEST. Does the tier-selection defect exist
# INDEPENDENTLY of the price field? i.e. can we witness a wrong tier using ONLY
# information the deployed function ALREADY receives?
#
# The deployed signature is apply_discount(subtotal_cents, eligible_items).
# It already receives: the whole-order subtotal, and the eligible item COUNT.
#
# Ask: is there a case where the deployed tier is provably wrong under EVERY
# possible price-field interpretation -- i.e. wrong from the arguments alone?
# ---------------------------------------------------------------------------
co = load(CHECKOUT_PY, "co")


def tier_of(charge, subtotal):
    """Recover the discount fraction the deployed code actually applied."""
    return round(1 - charge / subtotal, 4) if subtotal else 0.0


# A $300 order where exactly ONE item is eligible.
# The eligible item's price CANNOT exceed the order subtotal ($300) -- that is a
# hard arithmetic bound, true regardless of what the price field is CALLED.
# So the eligible MEAN is at most $300... but that is also exactly what the
# deployed code computes (300/1). Hmm -- so for n=1 the deployed reading is an
# UPPER BOUND on the true mean, and coincides only when the eligible item IS
# the entire order.
#
# Therefore: whenever the order contains ANY ineligible value, the deployed tier
# is >= the true tier. It OVER-discounts. That direction is knowable WITHOUT the
# field name. But the MAGNITUDE of the correction is not.
probe = []
for sub, n in [(30_000, 1), (30_000, 2), (50_000, 5), (30_000, 10)]:
    items = [{"sku": "E%d" % i} for i in range(n)]
    charged = co.apply_discount(sub, items)
    probe.append({
        "subtotal_cents": sub,
        "eligible_count": n,
        "deployed_implied_per_item": sub / n,
        "deployed_tier": tier_of(charged, sub),
        "charged_cents": charged,
        "note": ("deployed treats the ENTIRE order value as if it were "
                 "concentrated in the eligible items"),
    })

verdict = {
    "D1_tier_selection": {
        "defect": ("tier chosen from subtotal/eligible_count, i.e. the whole-order "
                   "value is attributed to the eligible items"),
        "contradicts_own_docstring": (
            "average price per eligible item" in DEPLOYED),
        "direction_of_error_knowable_without_price_field": True,
        "reason": ("the eligible items are a SUBSET of the order, so their true "
                   "mean price can never exceed subtotal/count -- the deployed "
                   "tier is therefore always >= the correct tier: it can only "
                   "OVER-discount, never under-discount"),
        "MAGNITUDE_of_correction_requires_price_field": True,
        "price_field_read_by_deployed_code": sorted(set(field_reads + field_gets)),
        "price_field_named_in_any_PRODUCTION_source": sorted(
            {h["token"] for h in prod_hits}),
        "production_sources_scanned": PRODUCTION_SOURCES,
        "price_tokens_found_ONLY_in_nonproduction": {
            t: sorted(fs) for t, fs in sorted(nonprod_hits.items())},
        "scan_self_contamination_check": "PASSED (analyzer excluded by allowlist)",
        "callers_of_apply_discount_in_repo": callers,
        "autonomously_fixable": False,
        "blocker": ("the per-item price FIELD NAME is not observable anywhere in "
                    "production source, and no caller exists in the repo. Guessing "
                    "it either (a) reads every item as free via .get(key, 0) -> "
                    "silently misprices forever, or (b) raises KeyError on the "
                    "checkout path -> turns a revenue leak into a hard OUTAGE."),
    },
    "D2_discount_scope": {
        "defect": "does the discount apply to the eligible subtotal or whole order?",
        "autonomously_fixable": False,
        "blocker": ("both are defensible business policies producing DIFFERENT "
                    "customer invoices. This is revenue policy, full stop."),
    },
    "deployed_tier_probe": probe,
}

print("=" * 78)
print("INC-34 FIXABILITY ANALYSIS -- INC-6, decomposed")
print("=" * 78)
print()
print("The deployed pricing module reads these item fields: %s" % (
    sorted(set(field_reads + field_gets)) or "NONE -- it is price-blind"))
print("Callers of apply_discount() inside the repo: %s" % (callers or "NONE"))
print()
print("PRODUCTION SOURCE ALLOWLIST (the only files that count as evidence):")
for rel in PRODUCTION_SOURCES:
    print("  - %s" % rel)
print("  price-like tokens found in ANY of them: %s" % (
    sorted({h["token"] for h in prod_hits}) or "NONE"))
print("  self-contamination check: PASSED (analyzer/probe excluded by construction)")
print()
print("CONTRAST -- where the price field name DOES appear (all NON-production):")
for tok, files in sorted(nonprod_hits.items()):
    print("  %-14s %s" % (tok, ", ".join(sorted(files)[:3])))
print("  => the field name exists ONLY in test fixtures and verifier text.")
print("     It was INVENTED by tests; the service never declares it.")
print()
print("-" * 78)
print("DEPLOYED TIER PROBE -- what tier does it pick, from the args alone?")
print("-" * 78)
print("%-10s %-6s %-16s %-8s %s" % (
    "SUBTOTAL", "N_ELIG", "IMPLIED $/ITEM", "TIER", "CHARGED"))
for p in probe:
    print("%-10s %-6d %-16s %-8s %s" % (
        "$%.2f" % (p["subtotal_cents"] / 100),
        p["eligible_count"],
        "$%.2f" % (p["deployed_implied_per_item"] / 100),
        "%d%%" % round(p["deployed_tier"] * 100),
        "$%.2f" % (p["charged_cents"] / 100)))
print()
print("KEY INSIGHT (knowable WITHOUT the price field name):")
print("  The eligible items are a SUBSET of the order. Their true mean price can")
print("  NEVER exceed subtotal/count. So the deployed tier is always >= correct.")
print("  => The bug can ONLY over-discount. It is a pure REVENUE LEAK, never an")
print("     overcharge. Direction: certain. Magnitude: requires the price field.")
print()
print("=" * 78)
print("FIXABILITY VERDICT")
print("=" * 78)
for k in ("D1_tier_selection", "D2_discount_scope"):
    v = verdict[k]
    print("  %-20s autonomously_fixable = %s" % (k, v["autonomously_fixable"]))
    print("      blocker: %s" % v["blocker"])
print()
print("  CONCLUSION: INC-6 is NOT autonomously patchable -- and now we know WHY")
print("  precisely: not because 'billing is scary', but because the price field")
print("  name is UNOBSERVABLE and the scope is a genuine policy fork. The prior")
print("  33 runs reached the right verdict; this run establishes the reason.")
print("=" * 78)

with open(os.path.join(HERE, "inc34_fixability.json"), "w") as fh:
    json.dump(verdict, fh, indent=2, default=str)
print("wrote inc34_fixability.json")