#!/usr/bin/env python3
"""Confirm the three owner-blocked defects by EXECUTING the deployed source.

No grepping. No trusting a prior run's write-up. Import the real modules the
fleet ships and observe what they do.
"""
import importlib.util
import json
import hashlib
import pathlib


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sha(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()

# Locate the sibling repos STRUCTURALLY, from this file's own position, so the
# probe does not depend on the working directory it was launched from.
_FLEET = pathlib.Path(__file__).resolve().parents[3]
CHECKOUT = _FLEET / "fabric-ic-incident-target" / "checkout.py"
AGG = _FLEET / "fabric-gateway-demo" / "service" / "usage_aggregator.py"

# On a BARE CHECKOUT (what CI clones) the siblings are legitimately absent. SKIP
# honestly rather than crashing -- but never report "no defects found", which
# would be a blind check masquerading as a clean bill of health.
if not CHECKOUT.exists() or not AGG.exists():
    print(json.dumps({
        "status": "SKIPPED",
        "reason": "sibling fleet repos not present (bare checkout) -- cannot execute "
                  "the deployed billing sources from here",
        "looked_for": [str(CHECKOUT), str(AGG)],
        "NOTE": "This is a SKIP, not a pass. It does NOT mean the defects are gone.",
    }, indent=2))
    raise SystemExit(0)

out = {}
checkout = load(CHECKOUT, "checkout")
agg = load(AGG, "usage_aggregator")

out["sha256"] = {"checkout.py": sha(CHECKOUT), "usage_aggregator.py": sha(AGG)}

# ---- INC-6: discount tier keyed off subtotal/len, ignoring item price.
# $300 order, ONE $10 eligible item. avg = 30000/1 = 30000 -> top 15% tier.
leak = checkout.apply_discount(30_000, [{"price_cents": 1_000}])
# Price-blindness: swap the item's price wildly; charge must not move.
cheap = checkout.apply_discount(30_000, [{"price_cents": 1}])
rich = checkout.apply_discount(30_000, [{"price_cents": 29_999}])
out["INC-6"] = {
    "$300 order, one $10 eligible item, charged": f"${leak/100:.2f}",
    "contract (no volume earned)": "$300.00",
    "leak": f"${(30_000-leak)/100:.2f}",
    "price_blind": cheap == rich == leak,
    "proof": f"$0.01 item -> ${cheap/100:.2f}; $299.99 item -> ${rich/100:.2f} (identical)",
    "scales_inversely_with_count": {
        f"{n} eligible items": f"${checkout.apply_discount(30_000, [{'price_cents': 1_000}]*n)/100:.2f}"
        for n in (1, 5, 20)
    },
    "zero_item_guard_holds": checkout.apply_discount(30_000, []) == 30_000,
    "LIVE": leak != 30_000,
}

# ---- INC-5: a record missing 'tokens' destroys the whole batch.
batch = [
    {"model": "gpt-4", "tokens": 100},
    {"model": "gpt-4"},                 # malformed: no tokens
    {"model": "claude", "tokens": 40},
]
valid_tokens = 140
try:
    agg.aggregate_usage(batch)
    out["INC-5"] = {"LIVE": False, "note": "batch survived -- repaired upstream?"}
except BaseException as e:  # noqa: BLE001 -- a custom owner exception must not crash us
    out["INC-5"] = {
        "raised": f"{type(e).__name__}({e})",
        "billable_tokens_destroyed": valid_tokens,
        "impact": "one malformed record kills the ENTIRE /v1/usage batch",
        "LIVE": True,
    }

# ---- INC-8: a null model books billable tokens against a None key, silently.
try:
    res = agg.aggregate_usage([{"model": "gpt-4", "tokens": 100},
                               {"model": None, "tokens": 40}])
    reconciles = res["grand_total"] == sum(res["per_model"].values())
    out["INC-8"] = {
        "result": {str(k): v for k, v in res["per_model"].items()},
        "grand_total": res["grand_total"],
        "tokens_on_None_key": res["per_model"].get(None),
        "raised": None,
        "grand_total_reconciles": reconciles,
        "why_undetectable": ("grand_total reconciles perfectly, so no downstream "
                             "invoice check can catch it"),
        "LIVE": None in res["per_model"],
    }
except BaseException as e:  # noqa: BLE001
    out["INC-8"] = {"raised": f"{type(e).__name__}({e})", "LIVE": False}

# ---- The coupling the fleet keeps missing: .get('model','unknown') does NOT
# fix INC-8, because the key IS present -- its value is null.
probe = {"model": None, "tokens": 40}
out["INC-5_and_INC-8_are_ONE_decision"] = {
    "record.get('model','unknown')": repr(probe.get("model", "unknown")),
    "lesson": ("a repair guarding only ABSENT keys passes a None straight "
               "through -- fixing INC-5 that way leaves INC-8 fully live"),
}

print(json.dumps(out, indent=2, default=str))
