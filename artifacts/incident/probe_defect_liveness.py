#!/usr/bin/env python3
"""Re-confirm the owner-blocked billing/revenue defects BY EXECUTING THE DEPLOYED
SOURCE -- never by trusting a previous run's write-up.

No telemetry source is reachable this run (Sentry 401 / no OTEL collector / no
gateway logs -- see probe_telemetry.py), so execution of the shipped code is the
ONLY authoritative evidence available. Every number this prints is produced by
calling the real deployed function.

This prober takes NO position on what the correct billing behaviour should be. It
reports what the code DOES. Choosing the semantics is a revenue-policy decision
and belongs to the owners.
"""
from __future__ import annotations

import hashlib
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent
CHECKOUT_PY = ROOT / "fabric-ic-incident-target" / "checkout.py"
AGG_PY = ROOT / "fabric-gateway-demo" / "service" / "usage_aggregator.py"


def load(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def money(cents) -> str:
    return f"${cents / 100:,.2f}"


def main() -> int:
    print("=" * 78)
    print("OWNER-BLOCKED DEFECT LIVENESS — established by EXECUTING the deployed source")
    print("=" * 78)
    print(f"\ncheckout.py          sha256={sha(CHECKOUT_PY)}")
    print(f"usage_aggregator.py  sha256={sha(AGG_PY)}\n")

    co = load(CHECKOUT_PY, "deployed_checkout")
    ag = load(AGG_PY, "deployed_agg")

    live = {}

    # ---------------------------------------------------------------- INC-6 --
    print("-" * 78)
    print("INC-6 — checkout volume-discount leak (fabric-ic-incident-target#6)")
    print("-" * 78)
    print("A $300 order. The discount TIER is chosen from subtotal/n, where the")
    print("subtotal is the WHOLE order but n counts only ELIGIBLE items.\n")
    print(f"{'eligible items':>15} {'charged':>12} {'leak':>10}   tier applied")
    rows = []
    for n in (1, 2, 5, 20):
        charged = co.apply_discount(30_000, [{"price_cents": 1_000}] * n)
        leak = 30_000 - charged
        pct = round(leak / 30_000 * 100)
        rows.append((n, charged, leak))
        print(f"{n:>15} {money(charged):>12} {money(leak):>10}   {pct}%")

    # Is the function price-blind? A $0.01 and a $299.99 eligible item must not
    # produce the same charge if the price is being read.
    cheap = co.apply_discount(30_000, [{"price_cents": 1}])
    dear = co.apply_discount(30_000, [{"price_cents": 29_999}])
    price_blind = cheap == dear
    print(f"\nprice-blindness check: a $0.01 eligible item charges {money(cheap)}; ")
    print(f"                      a $299.99 eligible item charges {money(dear)}")
    print(f"  -> apply_discount() reads NO item price: {price_blind}")

    zero_guard = co.apply_discount(30_000, []) == 30_000
    print(f"  -> zero-eligible-item guard holds (full subtotal charged): {zero_guard}")

    inc6_live = rows[0][2] > 0 and price_blind
    live["INC-6"] = inc6_live
    print(f"\n  VERDICT INC-6: {'LIVE' if inc6_live else 'NOT REPRODUCED'} "
          f"({money(rows[0][2])} leak at n=1; the leak scales INVERSELY with "
          f"eligible-item count)")

    # ---------------------------------------------------------------- INC-5 --
    print("\n" + "-" * 78)
    print("INC-5 — one malformed usage record destroys the whole batch "
          "(fabric-gateway-demo#2)")
    print("-" * 78)
    batch = [
        {"model": "gpt-4o", "tokens": 100},
        {"model": "claude", "tokens": 40},   # 140 valid billable tokens
        {"model": "gpt-4o"},                 # malformed: no `tokens`
    ]
    valid_tokens = sum(r["tokens"] for r in batch if "tokens" in r)
    try:
        out = ag.aggregate_usage(batch)
        inc5_live = False
        print(f"  batch aggregated: {out}")
    except BaseException as exc:  # noqa: BLE001 -- a custom owner exception must not crash us
        inc5_live = True
        print(f"  a record missing `tokens` raises {type(exc).__name__}({exc!r})")
        print(f"  -> the ENTIRE /v1/usage batch dies, taking {valid_tokens} valid "
              f"billable tokens with it")
    live["INC-5"] = inc5_live
    print(f"\n  VERDICT INC-5: {'LIVE' if inc5_live else 'NOT REPRODUCED'}")

    # ---------------------------------------------------------------- INC-8 --
    print("\n" + "-" * 78)
    print("INC-8 — null model books billable tokens to a `None` key "
          "(fabric-gateway-demo#5)")
    print("-" * 78)
    try:
        out = ag.aggregate_usage([
            {"model": "gpt-4o", "tokens": 100},
            {"model": None, "tokens": 40},
        ])
        null_bucket = None in out.get("per_model", {})
        reconciles = out.get("grand_total") == sum(out.get("per_model", {}).values())
        inc8_live = null_bucket
        print(f"  aggregate_usage([...{{'model': None, 'tokens': 40}}]) -> {out}")
        print(f"  -> 40 billable tokens booked against a `None` key: {null_bucket}")
        print(f"  -> no exception raised, and grand_total RECONCILES PERFECTLY: "
              f"{reconciles}")
        print(f"  -> so no downstream invoice check can detect it")
    except BaseException as exc:  # noqa: BLE001
        inc8_live = False
        print(f"  raised {type(exc).__name__}({exc!r}) -- the null case is rejected")
    live["INC-8"] = inc8_live
    print(f"\n  VERDICT INC-8: {'LIVE' if inc8_live else 'NOT REPRODUCED'}")

    # -------------------------------------------------------------- summary --
    print("\n" + "=" * 78)
    print("SUMMARY (all established by execution this run, not carried forward)")
    print("=" * 78)
    for k, v in live.items():
        print(f"  {k}: {'LIVE' if v else 'NOT REPRODUCED'}")
    n_live = sum(1 for v in live.values() if v)
    print(f"\n  {n_live}/{len(live)} owner-blocked defects confirmed LIVE.")
    print("\n  These are NOT auto-patched. Each candidate repair encodes a DIFFERENT")
    print("  billing/revenue policy, and guessing wrong mis-bills real customers with")
    print("  no error signal -- the same class of failure as the bug itself.")
    print("  Blast radius is UNKNOWN: no telemetry source was reachable this run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
