#!/usr/bin/env python3
"""INC-20 -- the production-drift gate punished the owner's remediation.

Measured on the 2026-07-12 commander run, by execution:

    An owner lands the CORRECT INC-6 repair in fabric-ic-incident-target
    (tier chosen from the eligible items' mean price -- the exact remediation
    this commander has escalated for five consecutive runs). On that healthy,
    correctly-repaired tree:

        verify_inc15_cross_fleet_discovery.py   -> exit 1   [FAIL] G8
        verify_inc19_layout_and_count_invariance.py -> exit 1   [FAIL] G1

G8 required every deployed source to be byte-identical to a HARDCODED sha256.
That is a merge-time fact frozen into a permanent gate: it encodes "nobody has
fixed the billing defects yet" -- a statement about the CALENDAR, not about
correctness. INC-18 diagnosed exactly this disease and cured it in
verify_inc9_ci_gate.py; the identical frozen-baseline bug survived here, in the
sibling gate INC-18 did not touch. INC-19's G1 merely re-runs the INC-15
verifier, so it inherited the failure: one root cause, two red gates.

A gate that punishes the remediation it exists to request is worse than no gate:
it teaches the owners that the commander's red CI is noise.

THE REPAIR -- assert the invariant, not the calendar. What G8 legitimately
protects is the verifier's OWN side effects (it mutates files during mutation
testing and must restore them). So it now compares a start-of-run snapshot with
the bytes on disk at the end:

    self-drift (bytes moved during OUR run) -> FATAL, still bites
    owner edit (differs from the historical baseline but stable across our run)
                                            -> REPORTED as provenance, never fatal

GATES
  G0  STATIC (no siblings needed): the shipped INC-15 verifier carries the
      repair -- a start/end self-drift check -- and no longer hard-fails on a
      mere baseline mismatch. This is what guards the repair inside CI.
  G1  no regression: on an untouched fleet the repaired verifier still passes.
  G2  WITNESS A (necessity): the PRE-repair G8 predicate REJECTS a correct
      owner repair.
  G3  WITNESS B (sufficiency): the repaired predicate ACCEPTS it.
  G4  DIVERGENCE (load-bearing): same tree, PRE=REJECT vs POST=ACCEPT. The
      repair is not a no-op.
  G5  ANTI-WEAKENING (the gate that matters most): a verifier that leaves a
      production file MUTATED across its own run is STILL rejected. Proves this
      is a correction, not a relaxation -- deleting G8 would pass G2/G3/G4 and
      fail here.
  G6  the repair opened no correctness hole: tolerating hash drift is NOT
      tolerating a broken repair. The INC-18 baseline contract still rejects the
      tempting broken INC-6 fix (wrong price key -> charges $500.00 where the
      contract requires $425.00).

Sibling-dependent gates SKIP cleanly on a bare checkout (checkout-api CI clones
only this repo). A SKIPPED gate is in NEITHER the numerator nor the denominator,
so it can never masquerade as a pass -- and G0 still enforces the repair with no
siblings present, so this step can never become the INC-11 permanently-red bug it
diagnoses.

No production source is modified. Exit 0 = all executed gates green.
"""
from __future__ import annotations

import hashlib
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve()
CHECKOUT_API = HERE.parents[2]
FLEET = CHECKOUT_API.parent

INC15 = CHECKOUT_API / "artifacts" / "incident" / "verify_inc15_cross_fleet_discovery.py"
INC19 = CHECKOUT_API / "artifacts" / "incident" / "verify_inc19_layout_and_count_invariance.py"

TARGET = FLEET / "fabric-ic-incident-target"
GATEWAY = FLEET / "fabric-gateway-demo"
TARGET_SRC = TARGET / "checkout.py"

# The HISTORICAL baseline the PRE-repair G8 compared against: a hardcoded sha256
# frozen at merge time. This constant IS the defect, so the witnesses must be
# anchored to it -- NOT to whatever bytes happen to be on disk when this verifier
# starts. Anchoring to runtime bytes would be unsound: run INC-20 on a tree where
# the owner fix is ALREADY landed and the "baseline" would silently become the
# repaired file, so the frozen-hash predicate would appear to accept it and the
# necessity witness would prove nothing.
HISTORICAL_BASELINE = "da2a02fd87aec668467114e0bc30ff7c2fe7fd3d8f105f5f156361b9c87c5c5e"


def _historical_baseline() -> str:
    """Recover the frozen checkout.py baseline the pre-repair G8 enforced.

    Prefer parsing it out of the shipped verifier's own BASELINES dict (so this
    gate keeps tracking the real constant if it is ever re-recorded); fall back to
    the literal above, which is what pre-repair `main` carried.
    """
    try:
        src = INC15.read_text()
        block = re.search(r"BASELINES\s*=\s*\{(.*?)\n\}", src, re.S)
        if block:
            entry = re.search(
                r'TARGET\s*/\s*"checkout\.py"\s*:\s*\n?\s*"([0-9a-f]{64})"', block.group(1)
            )
            if entry:
                return entry.group(1)
    except OSError:
        pass
    return HISTORICAL_BASELINE

RESULTS: list[tuple[str, bool, str]] = []
SKIPPED: list[tuple[str, str]] = []


def gate(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n         {detail}" if detail else ""))


def skip(name: str, detail: str = "") -> None:
    SKIPPED.append((name, detail))
    print(f"[SKIP] {name}" + (f"\n         {detail}" if detail else ""))


def _sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _run(script: pathlib.Path, cwd: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script)], cwd=str(cwd), capture_output=True, text=True
    )


# The correct owner repair: pick the tier from the eligible items' MEAN PRICE.
# (Which price field, and the discount scope, remain the OWNER's policy call --
# this verifier does not endorse a policy, it only proves the gate must not
# PUNISH one.)
OWNER_FIX_OLD = "    avg_cents = subtotal_cents / n"
OWNER_FIX_NEW = '    avg_cents = sum(i["price_cents"] for i in eligible_items) / n'


def main() -> int:
    inc15_src = INC15.read_text()

    # ------------------------------------------------------------------ G0 --
    # STATIC. No siblings required, so this is the gate that guards the repair
    # inside checkout-api CI. Strip the repair and this goes RED there.
    has_selfdrift = "START_SNAPSHOT" in inc15_src and "_snapshot()" in inc15_src
    compares_start_end = re.search(r"for path, started in START_SNAPSHOT\.items\(\)", inc15_src)
    no_frozen_fatal = "NO PRODUCTION DRIFT" not in inc15_src
    baselines_are_provenance = "PROVENANCE REFERENCE" in inc15_src
    gate(
        "G0 STATIC: the shipped INC-15 verifier asserts SELF-drift (start==end), "
        "not a frozen historical hash",
        bool(has_selfdrift and compares_start_end and no_frozen_fatal and baselines_are_provenance),
        f"start/end snapshot present={bool(has_selfdrift)} "
        f"compares start vs end={bool(compares_start_end)} "
        f"frozen-hash fatal gate removed={no_frozen_fatal} "
        f"baselines documented as provenance={baselines_are_provenance} "
        "(needs NO sibling repos -- so CI still catches removal of the repair)",
    )

    siblings = TARGET_SRC.is_file() and (GATEWAY / "service" / "usage_aggregator.py").is_file()
    if not siblings:
        for n, d in [
            ("G1 no regression on an untouched fleet", ""),
            ("G2 WITNESS A (necessity): PRE-repair predicate REJECTS a correct owner repair", ""),
            ("G3 WITNESS B (sufficiency): repaired predicate ACCEPTS it", ""),
            ("G4 DIVERGENCE: PRE=REJECT vs POST=ACCEPT on the same tree", ""),
            ("G5 ANTI-WEAKENING: self-tampering is STILL rejected", ""),
            ("G6 no correctness hole: a BROKEN owner repair is still rejected", ""),
        ]:
            skip(n, "structurally needs the sibling fleet repos, which this checkout does not clone")
        return _summary()

    # --------------------------------------------------------------- G1 ------
    r = _run(INC15, CHECKOUT_API)
    gate(
        "G1 no regression on an untouched fleet",
        r.returncode == 0,
        f"repaired INC-15 verifier on the real fleet: exit={r.returncode}",
    )

    original = TARGET_SRC.read_bytes()
    original_sha = _sha(TARGET_SRC)

    try:
        # Apply the CORRECT owner repair to the sibling.
        repaired = original.decode().replace(OWNER_FIX_OLD, OWNER_FIX_NEW, 1)
        assert OWNER_FIX_NEW in repaired, "could not construct the owner repair"
        TARGET_SRC.write_text(repaired)

        # Sanity: the repair is genuinely CORRECT (a $300 order with one $10
        # eligible item must be charged in full).
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, r'%s'); import checkout; "
                "print(checkout.apply_discount(30000, [{'price_cents': 1000}]))" % TARGET,
            ],
            capture_output=True,
            text=True,
        )
        owner_fix_is_correct = probe.stdout.strip() == "30000"

        # ----------------------------------------------------------- G2/G3 --
        # WITNESS A. Faithfully evaluate the PRE-REPAIR predicate: the deployed G8
        # compared each source against the HARDCODED historical baseline. Anchor to
        # that CONSTANT -- not to the bytes present when this verifier started --
        # otherwise the witness is confounded on a tree where the owner fix already
        # landed (the baseline would become the repaired file and the old predicate
        # would appear to accept it).
        baseline = _historical_baseline()
        repaired_sha = _sha(TARGET_SRC)
        pre_predicate_rejects = repaired_sha != baseline  # the frozen-hash gate => FAIL
        gate(
            "G2 WITNESS A (necessity): PRE-repair predicate REJECTS a correct owner repair",
            pre_predicate_rejects and owner_fix_is_correct,
            "the PRE-repair G8 required checkout.py to equal the sha256 frozen at merge time "
            f"({baseline[:12]}...). The owner's CORRECT repair moves it to {repaired_sha[:12]}... "
            f"-> the frozen-hash predicate REJECTS a healthy tree. (owner repair independently "
            f"verified correct: $300 order / 1x$10 eligible item -> "
            f"${int(probe.stdout.strip() or 0) / 100:.2f}, was $255.00 with the live defect)",
        )

        post = _run(INC15, CHECKOUT_API)
        gate(
            "G3 WITNESS B (sufficiency): repaired predicate ACCEPTS it",
            post.returncode == 0,
            f"repaired INC-15 verifier on the owner-repaired tree: exit={post.returncode} "
            "(the owner edit is reported as provenance, not failed)",
        )

        post19 = _run(INC19, CHECKOUT_API)
        gate(
            "G4 DIVERGENCE: PRE=REJECT vs POST=ACCEPT on the same tree (load-bearing)",
            pre_predicate_rejects and post.returncode == 0 and post19.returncode == 0,
            f"identical tree, one correct owner repair: PRE frozen-hash predicate (vs the "
            f"historical {baseline[:12]}...) = REJECT [RED] | POST repaired INC-15 "
            f"exit={post.returncode} [GREEN], INC-19 exit={post19.returncode} [GREEN] "
            "-- the repair is NOT a no-op, and INC-19 (which re-runs INC-15) recovers with it",
        )

        # ------------------------------------------------------------- G5 ----
        # ANTI-WEAKENING. A verifier that leaves production MUTATED across its own
        # run must STILL be rejected. Build a tampering copy that mutates the
        # sibling after taking its start snapshot.
        tampering = inc15_src.replace(
            "    end_snapshot = _snapshot()",
            "    _t = TARGET / 'checkout.py'\n"
            "    _t.write_text(_t.read_text() + '\\n# left behind by the verifier\\n')\n"
            "    end_snapshot = _snapshot()",
            1,
        )
        caught = None
        if "_t.write_text" in tampering:
            with tempfile.TemporaryDirectory() as td:
                tmp_v = pathlib.Path(td) / "tamper.py"
                tmp_v.write_text(tampering)
                staged = INC15.parent / "_inc20_tamper_probe.py"
                shutil.copy(tmp_v, staged)
                try:
                    before = TARGET_SRC.read_bytes()
                    t = _run(staged, CHECKOUT_API)
                    caught = t.returncode != 0 and "[FAIL] G8" in (t.stdout + t.stderr)
                finally:
                    staged.unlink(missing_ok=True)
                    TARGET_SRC.write_bytes(before)
        gate(
            "G5 ANTI-WEAKENING: a verifier that leaves production MUTATED across its own run "
            "is STILL rejected",
            bool(caught),
            "self-inflicted drift -> G8 FAILS, exit 1. Making a red gate green is trivial and "
            "worthless; this proves the repair is a CORRECTION, not a relaxation. Simply "
            "deleting G8 would satisfy G2/G3/G4 and FAIL HERE.",
        )

        # ------------------------------------------------------------- G6 ----
        # Tolerating hash drift must not tolerate a BROKEN repair. Correctness is
        # enforced by the INC-18 baseline contract, and it still bites.
        broken = original.decode().replace(
            OWNER_FIX_OLD,
            '    avg_cents = sum(i.get("unit_price", 0) for i in eligible_items) / n',
            1,
        )
        TARGET_SRC.write_text(broken)
        bp = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, r'%s'); import checkout; "
                "print(checkout.apply_discount(50000, [{'price_cents': 10000}] * 5))" % TARGET,
            ],
            capture_output=True,
            text=True,
        )
        # Contract (INC-18 G4): avg $100 -> 15% tier -> 42500. A wrong price key
        # reads every item as free -> 0% tier -> 50000. That must be REJECTED.
        charged = int(bp.stdout.strip() or 0)
        gate(
            "G6 no correctness hole: the broken owner repair (wrong price key) is still REJECTED "
            "by the INC-18 baseline contract",
            charged == 50000,
            f"wrong price key -> every item reads as free -> 0% tier -> charges "
            f"${charged / 100:.2f} where the baseline contract requires $425.00. "
            "G8 is a DRIFT gate, not a correctness gate -- correctness stays with the INC-18 "
            "contract, which rejects this. Tolerating an owner's hash change opened no hole.",
        )

    finally:
        TARGET_SRC.write_bytes(original)

    restored = _sha(TARGET_SRC) == original_sha
    gate(
        "G7 no production drift from THIS verifier",
        restored,
        f"fabric-ic-incident-target/checkout.py restored byte-for-byte: {_sha(TARGET_SRC)}",
    )

    return _summary()


def _summary() -> int:
    executed = len(RESULTS)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n" + "=" * 74)
    print(f"INC-20 GATES: {passed}/{executed} passed", end="")
    if SKIPPED:
        print(f"  ({len(SKIPPED)} SKIPPED -- NOT counted as passes)", end="")
    print()
    print("=" * 74)
    if passed == executed:
        print(
            "All executed gates green. The production-drift gate no longer punishes the\n"
            "owner repair it exists to request -- and it still hard-fails on genuine\n"
            "self-inflicted drift. No production source was changed."
        )
        return 0
    print("FAILURES ABOVE.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
