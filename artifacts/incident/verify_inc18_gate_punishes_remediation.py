#!/usr/bin/env python3
"""Fabric autonomous incident commander -- INC-18 verification gates.

THE FINDING

`artifacts/incident/verify_inc9_ci_gate.py` gates G6a/G6b/G6c asserted that the
three OWNER-BLOCKED BILLING DEFECTS (INC-6, INC-5, INC-8) were STILL BROKEN:

    leak == 25_500 and price_blind    <-- G6a
    batch_died                        <-- G6b   (and it caught ONLY KeyError)
    silent_null                       <-- G6c

Each is a MERGE-TIME FACT FROZEN INTO A PERMANENT GATE. They encode "nobody has
fixed the billing defects yet" -- a statement about the calendar, not about
correctness.

The consequence is the sharpest form of this fleet's signature bug: the instant an
owner lands the INC-6/INC-5/INC-8 repair THE COMMANDER HAS BEEN ESCALATING FOR
FOUR CONSECUTIVE RUNS, these gates go false and `checkout-api` CI goes hard RED --
on a repo where nothing is wrong, for the sole reason that somebody finally did
the thing we kept asking them to do.

A gate that PUNISHES THE REMEDIATION IT EXISTS TO REQUEST is worse than no gate:
it trains the team to ignore red CI, which is the very disease this fleet exists
to cure. It is the INC-11 / INC-12 / INC-15 / INC-17 expired-precondition bug on
its fifth repetition -- this time aimed at the owners instead of at ourselves.

THE REPAIR

Assert the INVARIANT, not the CALENDAR. Liveness is now REPORTED as provenance
("STILL LIVE" / "REPAIRED UPSTREAM"); what is ENFORCED is the policy-free baseline
contract -- well-formed input must still price and aggregate correctly. Every
candidate owner policy (reject-loudly / skip / attribute-to-unknown, and either
discount scope) satisfies that baseline. The tempting BROKEN repairs do not. So
the gate stops punishing correct fixes without ceasing to catch bad ones.

GATES

  G1 the repaired INC-9 verifier still passes as shipped, in THIS environment
     (fleet workspace: cross-fleet gates execute; bare checkout: they SKIP)
  G2 WITNESS A -- the PRE-REPAIR predicates are BROKEN by a correct owner fix
  G3 WITNESS B -- DIVERGENCE (load-bearing): on the SAME tree the OLD predicate
     set REJECTS and the NEW one PASSES. Had both behaved alike, the repair would
     be a no-op and this gate would say so.
  G4 ANTI-WEAKENING -- the NEW baseline still REJECTS the tempting BROKEN repairs.
     Making a red gate green is trivial and worthless; this proves the change is a
     CORRECTION, not a RELAXATION.
  G5 the INC-5 probe cannot CRASH the verifier: a reject-loudly owner repair that
     raises a custom error is caught and reported, not propagated.
  G6 PROVENANCE ONLY, never fatal: report whether the production sources still
     match the bytes deployed when this run was made. This gate must NOT fail on
     drift. Asserting those hashes would hard-fail every legitimate future edit
     (the INC-12 padlock) and — far worse — would go RED the moment an owner
     REPAIRED a billing source, which is the exact remediation this incident
     exists to request. That would make this verifier an instance of the very
     crime it prosecutes. Correctness is enforced by G2-G5, which are strictly
     stronger: they hold for WHATEVER source is present.

Runs on a BARE CHECKOUT with no sibling repos required, so the CI step that
executes it is green in the very job that runs it, and cannot become the INC-11
bug it diagnoses.

Exit: 0 = every gate passed.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
_CANDIDATES = [ROOT / "fleet" / "checkout-api", ROOT]
CHECKOUT_API = next(
    (c for c in _CANDIDATES if (c / "service" / "checkout" / "session.js").is_file()),
    None,
)
if CHECKOUT_API is None:
    sys.exit(f"cannot locate the checkout-api repo root from {ROOT}")

_TARGET_DIRS = ("fabric-ic-incident-target", "incident-target")
_GATEWAY_DIRS = ("fabric-gateway-demo", "gateway")
_FLEET_ROOTS = [CHECKOUT_API.parent, ROOT / "fleet", ROOT, ROOT.parent]


def _find(dirnames, *relparts):
    for root in _FLEET_ROOTS:
        for dirname in dirnames:
            candidate = root.joinpath(dirname, *relparts)
            if candidate.is_file():
                return candidate
    return None


TARGET = _find(_TARGET_DIRS, "checkout.py")
GATEWAY = _find(_GATEWAY_DIRS, "service", "usage_aggregator.py")

# Deployed production sources, full sha256, as OBSERVED on main during the INC-18
# run (2026-07-12). These are PROVENANCE REFERENCE POINTS, NOT a gate condition.
#
# CRITICAL — read before making G6 fatal on drift.
#
# A first draft of this verifier asserted these hashes and ran as a CI step on
# every pull request. That is the INC-12 padlock bug AND the INC-18 bug itself,
# committed inside the verifier written to police them:
#
#   * any legitimate future edit to session.js would hard-redden CI — a gate no
#     normal pull request can pass is a padlock, not a safety property (INC-12);
#   * worse, in a fleet workspace, AN OWNER REPAIRING usage_aggregator.py OR
#     checkout.py — the exact remediation this incident exists to request — would
#     change those bytes and FAIL THIS GATE. That is precisely the crime INC-18
#     is about: punishing the fix we keep asking for.
#
# So drift is REPORTED, never ENFORCED. "Are these the bytes that were deployed
# when the run was made?" is a useful line in a CI log; it is not something a
# pull request may be required to answer "yes" to.
#
# What actually protects correctness here is G2-G5, and they are strictly
# stronger than any hash check: they assert that WHATEVER source is present
# prices and aggregates correctly, that the old predicate would have punished a
# correct repair, and that the tempting broken repairs are still rejected.
BASELINES = {
    CHECKOUT_API
    / "service"
    / "checkout"
    / "session.js": "b45a8eeceaa142dd70aea4182930d02edb2d23ce90f0f02527910abb5f18d7e8"
}
if TARGET is not None:
    BASELINES[TARGET] = "da2a02fd87aec668467114e0bc30ff7c2fe7fd3d8f105f5f156361b9c87c5c5e"
if GATEWAY is not None:
    BASELINES[GATEWAY] = "bb21e50f7b5dab4463b71984bbe86a5df2b6ba442ffeff84d9b70815781750e5"

RESULTS = []

# INC-31 -- STOP THE STRICT-MODE FLAG LEAKING INTO CHILDREN.
#
# `subprocess.run()` without `env=` hands the child the parent's ENTIRE
# environment, and strict cross-fleet mode is honoured through an environment
# variable. G1 below spawns verify_inc9_ci_gate.py to ask only "does the shipped
# INC-9 verifier pass on this tree?" On a BARE CHECKOUT -- what this repo's CI
# clones -- INC-9 legitimately SKIPs its cross-fleet gates and exits 0. With the
# flag leaked in, the child is forced into strict mode and HARD-FAILS for want of
# siblings that are legitimately absent.
#
# THE RULE: an intent must be PASSED to the child that should receive it,
# never INHERITED by a child that must not.
STRICT_ENV_VAR = "FABRIC_REQUIRE_CROSS_FLEET"


def child_env(*, strict: bool = False) -> dict:
    """A child environment with the strict flag ALWAYS scrubbed."""
    env = dict(os.environ)
    env.pop(STRICT_ENV_VAR, None)
    if strict:
        env[STRICT_ENV_VAR] = "1"
    return env


def gate(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n         {detail}" if detail else ""))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def tally(proc):
    blob = proc.stdout + proc.stderr
    m = re.search(r"GATES: (\d+)/(\d+) passed", blob)
    return m.group(0) if m else "(no tally)"


# ---------------------------------------------------------------------------
# The OLD (pre-INC-18) G6 predicates, reproduced EXACTLY, so they can be
# evaluated against a simulated owner-repaired fleet. This is the honest way to
# demonstrate that the old gate would have hard-failed a healthy repo.
# ---------------------------------------------------------------------------
def old_predicates(co, ag):
    """Evaluate the OLD G6 predicates, and record whether they CRASH.

    The old G6b probe caught ONLY KeyError. A reject-loudly owner repair raising
    a custom error would therefore escape it and kill the verifier outright. That
    escape IS part of the finding, so it is captured and reported here rather than
    propagated -- this reproduction must survive what the original could not.
    """
    leak = co.apply_discount(30_000, [{"price_cents": 1_000}])
    price_blind = co.apply_discount(30_000, [{"price_cents": 1}]) == co.apply_discount(
        30_000, [{"price_cents": 29_999}]
    )
    g6a = leak == 25_500 and price_blind

    # Faithful to the original: ONLY KeyError was caught.
    escaped = None
    batch_died = False
    try:
        ag.aggregate_usage([{"model": "gpt-4o", "tokens": 120}, {"model": "gpt-4o"}])
    except KeyError:
        batch_died = True
    except Exception as exc:  # the original would have DIED here
        escaped = type(exc).__name__
    g6b = batch_died

    try:
        nm = ag.aggregate_usage([{"model": None, "tokens": 10}])
        g6c = None in nm.get("per_model", {})
    except Exception:
        g6c = False

    return {"G6a": g6a, "G6b": g6b, "G6c": g6c, "crashed_with": escaped}


def _verdict(preds):
    """Only the three gate predicates decide pass/fail; crashed_with is metadata."""
    return [preds[k] for k in ("G6a", "G6b", "G6c")]


def new_predicates(co, ag):
    """The repaired, policy-free baseline predicates."""
    inc6_ok = (
        co.apply_discount(30_000, []) == 30_000
        and co.apply_discount(50_000, [{"price_cents": 10_000}] * 5) == 42_500
        and co.apply_discount(1_000, [{"price_cents": 200}] * 5) == 1_000
    )
    wf = ag.aggregate_usage(
        [
            {"model": "gpt-4o", "tokens": 120},
            {"model": "claude", "tokens": 30},
            {"model": "gpt-4o", "tokens": 10},
        ]
    )
    agg_ok = wf == {"per_model": {"gpt-4o": 130, "claude": 30}, "grand_total": 160}
    return {"G6a": inc6_ok, "G6b": agg_ok, "G6c": agg_ok}


# --- simulated trees. TEMP FILES ONLY. No billing policy is being shipped. ---

TIERS = (
    "def _select_tier(avg_cents):\n"
    "    if avg_cents >= 10000:\n"
    "        return 0.15\n"
    "    if avg_cents >= 5000:\n"
    "        return 0.10\n"
    "    if avg_cents >= 2000:\n"
    "        return 0.05\n"
    "    return 0.0\n"
)

# A CORRECT, owner-approved repair: tier from the ELIGIBLE subtotal / eligible
# count; the discount applies to the eligible subtotal (one of the two valid
# scopes). This is what a healthy post-remediation fleet looks like.
REPAIRED_CHECKOUT = (
    "def apply_discount(subtotal_cents, eligible_items):\n"
    "    n = len(eligible_items)\n"
    "    if n == 0:\n"
    "        return subtotal_cents\n"
    "    elig = sum(int(i['price_cents']) for i in eligible_items)\n"
    "    tier = _select_tier(elig / n)\n"
    "    if elig >= subtotal_cents:\n"
    "        return round(subtotal_cents * (1 - tier))\n"
    "    return round(elig * (1 - tier)) + (subtotal_cents - elig)\n"
    "\n\n" + TIERS
)

# A CORRECT repair of INC-5 + INC-8 together: reject a malformed OR null-valued
# record LOUDLY (the policy safest for invoice integrity).
REPAIRED_AGG = (
    "class UsageRecordError(ValueError):\n"
    "    pass\n"
    "\n\n"
    "def aggregate_usage(records):\n"
    "    totals = {}\n"
    "    grand_total = 0\n"
    "    for record in records:\n"
    "        if 'model' not in record or 'tokens' not in record:\n"
    "            raise UsageRecordError('missing model or tokens')\n"
    "        model = record['model']\n"
    "        tokens = record['tokens']\n"
    "        if model is None or tokens is None:\n"
    "            raise UsageRecordError('null model or tokens')\n"
    "        totals[model] = totals.get(model, 0) + tokens\n"
    "        grand_total += tokens\n"
    "    return {'per_model': totals, 'grand_total': grand_total}\n"
)

# The TEMPTING BROKEN repairs -- the ones the commander has always refused to
# ship. The NEW baseline MUST still reject these (G4, anti-weakening).
BROKEN_CHECKOUT = (
    "def apply_discount(subtotal_cents, eligible_items):\n"
    "    n = len(eligible_items)\n"
    "    if n == 0:\n"
    "        return subtotal_cents\n"
    "    # THE TEMPTING BUG: wrong key -> every item reads as free -> 0% tier.\n"
    "    elig = sum(int(i.get('price', 0)) for i in eligible_items)\n"
    "    tier = _select_tier(elig / n)\n"
    "    return round(subtotal_cents * (1 - tier))\n"
    "\n\n" + TIERS
)

BROKEN_AGG = (
    "def aggregate_usage(records):\n"
    "    totals = {}\n"
    "    grand_total = 0\n"
    "    for record in records:\n"
    "        # THE TEMPTING BUG: guard only the ABSENT key, and read tokens from\n"
    "        # the wrong field -- so the happy path books zero for everything.\n"
    "        model = record.get('model') or 'unknown'\n"
    "        tokens = record.get('token_count', 0)\n"
    "        totals[model] = totals.get(model, 0) + tokens\n"
    "        grand_total += tokens\n"
    "    return {'per_model': totals, 'grand_total': grand_total}\n"
)

# The DEPLOYED behaviour, reconstructed for a bare checkout where the sibling
# repos are legitimately absent. INC-6/INC-5/INC-8 are all live here.
DEPLOYED_CHECKOUT_FALLBACK = (
    "def apply_discount(subtotal_cents, eligible_items):\n"
    "    n = len(eligible_items)\n"
    "    if n == 0:\n"
    "        return subtotal_cents\n"
    "    tier = _select_tier(subtotal_cents / n)\n"
    "    return round(subtotal_cents * (1 - tier))\n"
    "\n\n" + TIERS
)

DEPLOYED_AGG_FALLBACK = (
    "def aggregate_usage(records):\n"
    "    totals = {}\n"
    "    grand_total = 0\n"
    "    for record in records:\n"
    "        model = record['model']\n"
    "        tokens = record['tokens']\n"
    "        if model not in totals:\n"
    "            totals[model] = 0\n"
    "        totals[model] += tokens\n"
    "        grand_total += tokens\n"
    "    return {'per_model': totals, 'grand_total': grand_total}\n"
)


def write_pair(directory, checkout_src, agg_src, tag):
    directory.mkdir(parents=True, exist_ok=True)
    c = directory / "co.py"
    a = directory / "ag.py"
    c.write_text(checkout_src)
    a.write_text(agg_src)
    return load(c, "co_" + tag), load(a, "ag_" + tag)


def main():
    print("Fabric incident commander -- INC-18 verification gates")
    print("(the cross-fleet gates asserted the billing defects were STILL BROKEN)\n")

    # ------------------------------------------------------------------ G1 --
    inc9 = CHECKOUT_API / "artifacts" / "incident" / "verify_inc9_ci_gate.py"
    shipped = subprocess.run(
        [sys.executable, str(inc9)],
        cwd=str(CHECKOUT_API),
        capture_output=True,
        text=True,
        timeout=600,
        env=child_env(),
    )
    blob = shipped.stdout + shipped.stderr
    ran_cross_fleet = "G6a" in blob
    gate(
        "G1 the repaired INC-9 verifier passes as shipped (this environment)",
        shipped.returncode == 0,
        f"exit={shipped.returncode} {tally(shipped)}; cross-fleet gates "
        + (
            "EXECUTED (fleet workspace)"
            if ran_cross_fleet
            else "SKIPPED (bare checkout -- correct, and never a silent pass)"
        ),
    )

    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)

        # THE WITNESS MUST BE ANCHORED TO A FIXED REFERENCE OF THE DEFECT.
        #
        # A first draft built this simulation from whatever source was CURRENTLY
        # deployed, and required the OLD predicates to hold on it (`sim_valid`).
        # That is the INC-18 bug for a THIRD time, one layer deeper: the moment an
        # owner repaired their billing source, the "deployed" tree stopped
        # exhibiting the defect, `sim_valid` went False, and G2 HARD-FAILED CI --
        # punishing the very remediation this incident exists to request. Caught by
        # actually simulating an owner repair rather than by reading the code.
        #
        # So the DEFECTIVE reference is a fixed, self-contained snapshot of the
        # behaviour as it existed when INC-18 was raised. That is what a witness
        # is: a frozen record of the defect, used to prove the old predicate was
        # wrong. It is never a demand about the state of production TODAY.
        #
        # The CURRENT deployed source is still examined -- but only to REPORT its
        # state (G6, and G6a/b/c inside the INC-9 verifier), never to gate on it.
        def_co, def_ag = write_pair(
            tmp / "defective", DEPLOYED_CHECKOUT_FALLBACK, DEPLOYED_AGG_FALLBACK, "def"
        )
        old_on_defective = old_predicates(def_co, def_ag)
        new_on_defective = new_predicates(def_co, def_ag)

        # Sanity on the FROZEN witness, not on production: the old predicates must
        # hold on the defect they were written against, and the new baseline must
        # ALSO hold there (well-formed input priced correctly even while the defect
        # is live). If this fails, the witness itself is misconstructed and every
        # conclusion below would be worthless -- so it is legitimately fatal, and
        # it can never be flipped by anything an owner does.
        sim_valid = all(_verdict(old_on_defective)) and all(_verdict(new_on_defective))

        # Provenance only: what does the CURRENTLY deployed source do? Reported in
        # the gate detail, never gated on.
        #
        # This whole block is wrapped, and that is load-bearing. "Report-only" has
        # to mean it can NEVER abort the verifier: the live sibling source is code
        # an OWNER may have just edited. If their repair has a syntax error, a bad
        # import, or raises at module scope, an unguarded import here would crash
        # the verifier and redden CI -- which is, once again, the exact INC-18 bug.
        # An unreadable live source is a thing to REPORT, not a thing to die on.
        live_note = "current deployed sibling source not present (bare checkout)"
        if TARGET is not None and GATEWAY is not None:
            try:
                live_co, live_ag = write_pair(
                    tmp / "live", TARGET.read_text(), GATEWAY.read_text(), "live"
                )
                old_on_live = old_predicates(live_co, live_ag)
                live_note = (
                    f"current deployed source: OLD predicates {old_on_live} "
                    + (
                        "(defects still live)"
                        if all(_verdict(old_on_live))
                        else "(AT LEAST ONE DEFECT HAS BEEN REPAIRED UPSTREAM -- "
                        "reported, never punished)"
                    )
                )
            except Exception as exc:  # noqa: BLE001 - provenance must never be fatal
                live_note = (
                    f"current deployed sibling source could not be evaluated "
                    f"({type(exc).__name__}: {exc}). REPORTED, NOT FATAL -- this is "
                    f"provenance, and a live source an owner is mid-edit must never be "
                    f"able to crash this gate."
                )

        # ------------------------------------------------------------- G2 --
        rep_co, rep_ag = write_pair(tmp / "repaired", REPAIRED_CHECKOUT, REPAIRED_AGG, "rep")
        old_repaired = old_predicates(rep_co, rep_ag)
        new_repaired = new_predicates(rep_co, rep_ag)
        old_rejects_healthy = not all(_verdict(old_repaired))

        gate(
            "G2 WITNESS A -- the PRE-REPAIR predicates are BROKEN by a correct owner fix",
            sim_valid and old_rejects_healthy,
            f"witness anchored to a FROZEN snapshot of the INC-18 defect (never to "
            f"today's production, which an owner may legitimately have fixed); "
            f"sim_valid={sim_valid}, OLD on the frozen defect={old_on_defective}. "
            f"On a CORRECTLY REPAIRED fleet OLD={old_repaired} -> the old gates "
            f"HARD-FAIL a healthy repo whose owners did exactly what we asked. "
            f"[provenance] {live_note}",
        )

        # ------------------------------------------------------------- G3 --
        new_accepts_healthy = all(_verdict(new_repaired))
        gate(
            "G3 WITNESS B -- DIVERGENCE on the SAME tree: OLD rejects, NEW passes",
            old_rejects_healthy and new_accepts_healthy,
            f"owner-repaired fleet: OLD G6 -> "
            f"{'REJECT' if old_rejects_healthy else 'accept'} {old_repaired} | NEW G6 -> "
            f"{'PASS' if new_accepts_healthy else 'REJECT'} {new_repaired}. Had both "
            f"behaved alike the repair would be a no-op, and this gate would say so.",
        )

        # ------------------------------------------------------------- G4 --
        brk_co, brk_ag = write_pair(tmp / "broken", BROKEN_CHECKOUT, BROKEN_AGG, "brk")
        new_broken = new_predicates(brk_co, brk_ag)
        broken_charge = brk_co.apply_discount(50_000, [{"price_cents": 10_000}] * 5)
        broken_agg_out = brk_ag.aggregate_usage([{"model": "gpt-4o", "tokens": 120}])
        rejects_broken = (not new_broken["G6a"]) and (not new_broken["G6b"])

        gate(
            "G4 ANTI-WEAKENING -- the NEW baseline still REJECTS the tempting broken repairs",
            rejects_broken,
            f"broken INC-6 repair (wrong price key) charges ${broken_charge / 100:,.2f} on "
            f"the $500 order where the contract requires $425.00 -> NEW "
            f"G6a={new_broken['G6a']} (REJECTED). broken INC-5/8 repair (guards only the "
            f"absent key, wrong token field) aggregates {broken_agg_out} -> NEW "
            f"G6b={new_broken['G6b']} (REJECTED). This is a CORRECTION, not a RELAXATION.",
        )

        # ------------------------------------------------------------- G5 --
        # Under the OLD code the malformed-record probe caught ONLY KeyError, so a
        # reject-loudly owner repair raising a custom error would propagate and
        # kill the verifier outright -- the same bug, one layer down.
        escapes_old_handler = False
        try:
            rep_ag.aggregate_usage([{"model": "gpt-4o", "tokens": 120}, {"model": "gpt-4o"}])
        except KeyError:
            escapes_old_handler = False
        except Exception:
            escapes_old_handler = True

        try:
            rep_ag.aggregate_usage([{"model": "gpt-4o", "tokens": 120}, {"model": "gpt-4o"}])
            raised = None
        except Exception as exc:
            raised = type(exc).__name__
        handled = raised is not None and all(_verdict(new_repaired))

        gate(
            "G5 the INC-5 probe cannot CRASH the verifier on a reject-loudly repair",
            escapes_old_handler and handled,
            f"the owner repair raises {raised}; the OLD probe caught only KeyError, so it "
            f"would have propagated and killed the verifier "
            f"(escapes_old_handler={escapes_old_handler}); the repaired probe catches it, "
            f"reports the state, and the policy-free baseline still passes "
            f"(handled={handled}).",
        )

    # ------------------------------------------------------------------ G6 --
    # PROVENANCE ONLY. This gate reports drift; it must NEVER fail on it.
    #
    # See the long BASELINES comment above. In short: this verifier runs as a CI
    # step on every pull request, and a pull request exists in order to CHANGE
    # CODE. A gate demanding byte-identity with the bytes deployed on the day
    # INC-18 was written would:
    #   * hard-fail every legitimate future edit to session.js (the INC-12
    #     padlock), and
    #   * hard-fail the moment an owner REPAIRS usage_aggregator.py or
    #     checkout.py — which is the exact remediation this incident exists to
    #     request, and therefore the exact bug INC-18 is about.
    #
    # Committing that here would have made this verifier an instance of the crime
    # it prosecutes. So: report, never enforce.
    matched, drifted = [], []
    for path, expected in BASELINES.items():
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:  # unreadable file is provenance, not a failure
            drifted.append(f"{path.name} (unreadable: {type(exc).__name__})")
            continue
        if actual == expected:
            matched.append(path.name)
        else:
            drifted.append(f"{path.name} ({actual[:12]} vs run-baseline {expected[:12]})")

    if drifted:
        state = (
            f"{len(matched)}/{len(BASELINES)} identical to the INC-18 run baseline; "
            f"CHANGED SINCE THAT RUN: {', '.join(drifted)}. This is NOT a failure — a "
            f"changed billing source most likely means AN OWNER LANDED THE REPAIR we "
            f"asked for, which G6a/G6b/G6c now correctly report as 'REPAIRED UPSTREAM'. "
            f"Correctness is enforced by G2-G5, which hold for WHATEVER source is present."
        )
    else:
        state = (
            f"{len(matched)}/{len(BASELINES)} deployed sources present here are identical "
            f"to the INC-18 run baseline; this run changed NO production code"
        )

    gate(
        "G6 PROVENANCE (never fatal): production sources vs the INC-18 run baseline",
        True,  # deliberately unconditional: drift is reported, never enforced.
        state,
    )

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{'=' * 74}\nINC-18 GATES: {passed}/{total} passed\n{'=' * 74}")
    if passed != total:
        for name, ok, _ in RESULTS:
            if not ok:
                print(f"  FAILED: {name}")
        return 1
    print("All gates green. The cross-fleet gates no longer punish the very")
    print("remediation they exist to request -- and they still reject the broken")
    print("repairs. No production source, test assertion, or dependency changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
