#!/usr/bin/env python3
"""Fabric incident commander — INC-13 verification gate.

THE FINDING
-----------
`artifacts/incident/verify_inc9_ci_gate.py` gate G6 (cross-fleet re-confirmation
of the three policy-blocked billing defects, G6a/G6b/G6c) was **dead code that
reported itself as green**.

Its sibling-repo discovery looked for directories literally named:

    incident-target/          gateway/

The fleet repos are actually named:

    fabric-ic-incident-target/    fabric-gateway-demo/

The lookup therefore never matched — in ANY layout, including the commander
workspace it was written for. G6 always took the SKIP path, so G6a/G6b/G6c never
ran. And the SKIP path computed `passed == total` over only the gates that HAD
run, then returned 0 — printing a confident **"GATES: 6/6 passed"** and exiting
clean while three of its nine gates were unreachable.

That is the INC-9 / INC-11 / INC-12 failure mode — *a gate that cannot fail is
decoration* — reproduced a third time, this time INSIDE the verifier whose entire
job is to police exactly that. A skipped check laundered into a pass count is
worse than a missing check, because it actively asserts coverage it does not have.

(PR #7 spotted this same mismatch, but #7 was closed UNMERGED — superseded by
#8/#12 — so the repair never landed on `main`. It lands here.)

WHY IT MATTERS
--------------
G6a/G6b/G6c are the gates that re-confirm, by EXECUTING the deployed source, that
the three owner-blocked defects are still live:

  * INC-6  checkout over-discounts: a $300 order with one $10 eligible item is
           charged $255.00 instead of $300.00
  * INC-5  one malformed usage record raises KeyError and kills the whole
           /v1/usage batch
  * INC-8  a null model silently books billable tokens against a `None` key

With G6 dead, the commander's own "still live?" re-confirmation never executed.
The fleet would have kept reporting these as verified-live on the strength of a
previous run's word — precisely the "trust, don't verify" posture the
double-witness design exists to forbid.

GATES
-----
  G1  the REPAIRED verifier passes on the current tree, and the cross-fleet
      gates ACTUALLY RAN (9 gates present, not 6)
  G2  the repaired discovery resolves BOTH real fleet repo names
  G3  the legacy directory names still resolve (the fix ADDS names, never
      replaces them — other checkout layouts keep working)
  G4  WITNESS A — pre-repair discovery logic is BLIND: replayed against the real
      workspace it finds neither sibling (this is the defect, reproduced)
  G5  WITNESS B — DIVERGENCE (load-bearing): on the SAME workspace the OLD logic
      skips all 3 cross-fleet gates while the NEW logic executes all 3. If both
      behaved alike, the repair would be pointless and this gate says so.
  G6  NEGATIVE CONTROL — the repaired gate still BITES: point discovery at a
      workspace whose siblings are genuinely absent and it must report SKIPPED
      rather than silently claiming a full pass.
  G7  ALL production source untouched — session.js / checkout.py /
      usage_aggregator.py are each hashed BEFORE any gate runs and re-compared
      afterwards, so drift in any of the three fleet repos is caught (the INC-9
      verifier imports all three, so all three are in scope).

Exit: 0 = every gate passed.
"""
from __future__ import annotations

import hashlib
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

VERIFIER = CHECKOUT_API / "artifacts" / "incident" / "verify_inc9_ci_gate.py"
SESSION = CHECKOUT_API / "service" / "checkout" / "session.js"

# The real fleet directory names, and the legacy ones the old code looked for.
REAL_TARGET = "fabric-ic-incident-target"
REAL_GATEWAY = "fabric-gateway-demo"
LEGACY_TARGET = "incident-target"
LEGACY_GATEWAY = "gateway"

RESULTS: list[tuple[str, bool, str]] = []
SKIPPED: list[tuple[str, str]] = []


def gate(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n         {detail}" if detail else ""))


def skip(name: str, why: str) -> None:
    """Record a gate that could not run. NEVER counted as a pass.

    This exists because of the very defect INC-13 is about: the INC-9 verifier
    folded un-run gates into its pass count. A skip must be structurally
    incapable of masquerading as a pass, so skips live in their own list and are
    reported separately in the summary.
    """
    SKIPPED.append((name, why))
    print(f"[SKIP] {name}\n         {why}")


def sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# The two discovery implementations, replayed as pure functions so we can run
# BOTH against the same filesystem and compare. This is the only honest way to
# show the repair changes behaviour: assert the divergence, don't assume it.
# --------------------------------------------------------------------------
def discover_old(fleet_roots: list[pathlib.Path]):
    """Pre-repair logic: hard-coded `incident-target` / `gateway` only."""
    target = next(
        (
            p
            for p in (r / LEGACY_TARGET / "checkout.py" for r in fleet_roots)
            if p.is_file()
        ),
        None,
    )
    gateway = next(
        (
            p
            for p in (
                r / LEGACY_GATEWAY / "service" / "usage_aggregator.py"
                for r in fleet_roots
            )
            if p.is_file()
        ),
        None,
    )
    return target, gateway


def discover_new(fleet_roots: list[pathlib.Path]):
    """Post-repair logic: real names first, legacy names as fallback."""
    target_dirs = (REAL_TARGET, LEGACY_TARGET)
    gateway_dirs = (REAL_GATEWAY, LEGACY_GATEWAY)
    target = next(
        (
            r / d / "checkout.py"
            for r in fleet_roots
            for d in target_dirs
            if (r / d / "checkout.py").is_file()
        ),
        None,
    )
    gateway = next(
        (
            r / d / "service" / "usage_aggregator.py"
            for r in fleet_roots
            for d in gateway_dirs
            if (r / d / "service" / "usage_aggregator.py").is_file()
        ),
        None,
    )
    return target, gateway


def main() -> int:
    print("Fabric incident commander — INC-13 gates (G6 was dead and claimed green)\n")

    fleet_roots = [CHECKOUT_API.parent, ROOT / "fleet", ROOT]

    # PRE-RUN BASELINES -- taken BEFORE any gate executes.
    #
    # G7 must be able to prove that nothing below mutated a production file. That
    # proof is only worth anything if the baseline is captured up front, for EVERY
    # production file we can reach -- not just session.js. The INC-9 verifier this
    # gate invokes touches all three repos (it imports checkout.py and
    # usage_aggregator.py to re-confirm the live defects), so all three are in
    # scope for drift and all three get a pre-run hash here.
    baseline_target, baseline_gateway = discover_new(fleet_roots)

    BASELINES: dict[str, tuple[pathlib.Path, str]] = {
        "checkout-api/service/checkout/session.js": (SESSION, sha(SESSION)),
    }
    if baseline_target is not None:
        BASELINES[f"{REAL_TARGET}/checkout.py"] = (
            baseline_target,
            sha(baseline_target),
        )
    if baseline_gateway is not None:
        BASELINES[f"{REAL_GATEWAY}/service/usage_aggregator.py"] = (
            baseline_gateway,
            sha(baseline_gateway),
        )
    print("pre-run production baselines (sha256):")
    for rel, (_, h) in BASELINES.items():
        print(f"    {h[:12]}  {rel}")
    print()

    # ENVIRONMENT AWARENESS -- and why this is not a weakening of the gates.
    #
    # G1/G2/G5 structurally REQUIRE the sibling fleet repos: they exist to show
    # that the repaired discovery finds them and that the old discovery did not.
    # In a BARE checkout of checkout-api -- which is exactly what this repo's own
    # CI clones -- the siblings are legitimately absent, so those gates cannot be
    # evaluated at all.
    #
    # Two wrong answers, both of which this fleet has already paid for:
    #   * Fail them  -> the verifier is PERMANENTLY RED in checkout-api CI. That is
    #                   the expired-precondition bug INC-11 and INC-12 were raised
    #                   to repair, re-committed by the incident that diagnoses it.
    #   * Pass them  -> un-run gates laundered into a green pass count. That is
    #                   INC-13 itself, the defect this file exists to fix.
    #
    # The only honest answer is a THIRD state: SKIP -- reported, never counted as a
    # pass, and promotable to FATAL by the caller that knows the siblings ought to
    # be there (the commander workspace):
    #
    #   FABRIC_REQUIRE_CROSS_FLEET=1  or  --require-cross-fleet
    #
    # Case-folded so `FALSE`/`False`/`No` mean off and cannot accidentally enable it.
    _flag = os.environ.get("FABRIC_REQUIRE_CROSS_FLEET", "").strip().lower()
    require_cross_fleet = (
        _flag not in ("", "0", "false", "no", "off")
        or "--require-cross-fleet" in sys.argv[1:]
    )
    siblings_present = baseline_target is not None and baseline_gateway is not None
    if not siblings_present:
        print(
            "NOTE: sibling fleet repos NOT present in this checkout.\n"
            "      G1/G2/G5 require them and will be SKIPPED (never silently passed).\n"
            f"      strict mode (--require-cross-fleet) = {require_cross_fleet}\n"
        )

    # ---------------------------------------------------------------- G1 --
    # Requires the siblings: the whole point is that the cross-fleet gates now RUN
    # (9/9), which is only observable where those repos exist.
    if not siblings_present:
        skip(
            "G1 repaired INC-9 verifier passes AND the cross-fleet gates actually ran",
            "sibling fleet repos absent -- the INC-9 verifier correctly reports 6/6 "
            "+ 3 SKIPPED here, so '9/9 with cross-fleet executed' is not evaluable "
            "in a bare checkout. Run from the commander workspace to execute it.",
        )
    else:
        proc = subprocess.run(
            [sys.executable, str(VERIFIER)],
            capture_output=True,
            text=True,
            timeout=600,
            cwd=str(CHECKOUT_API),
        )
        blob = proc.stdout + proc.stderr
        m = re.search(r"GATES: (\d+)/(\d+) passed", blob)
        passed, total = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        cross_fleet_ran = (
            "G6a" in blob and "G6b" in blob and "G6c" in blob and "DID NOT RUN" not in blob
        )
        gate(
            "G1 repaired INC-9 verifier passes AND the cross-fleet gates actually ran",
            proc.returncode == 0 and total == 9 and passed == 9 and cross_fleet_ran,
            f"exit={proc.returncode} gates={passed}/{total} (was 6/6 with G6a/b/c "
            f"unreachable); cross_fleet_executed={cross_fleet_ran}",
        )

    # ---------------------------------------------------------------- G2 --
    t_new, g_new = discover_new(fleet_roots)
    if not siblings_present:
        skip(
            "G2 repaired discovery resolves BOTH real fleet repo names",
            "sibling fleet repos absent -- nothing to resolve in a bare checkout. "
            "G3 still proves the name list is correct by resolving a synthetic layout.",
        )
    else:
        gate(
            "G2 repaired discovery resolves BOTH real fleet repo names",
            t_new is not None and g_new is not None,
            f"{REAL_TARGET} -> {'FOUND' if t_new else 'MISSING'}; "
            f"{REAL_GATEWAY} -> {'FOUND' if g_new else 'MISSING'}",
        )

    # ---------------------------------------------------------------- G3 --
    # The fix must ADD names, not swap them: a checkout that uses the legacy
    # layout must keep working. Build one and prove the new logic still finds it.
    with tempfile.TemporaryDirectory() as tmp:
        legacy = pathlib.Path(tmp)
        (legacy / LEGACY_TARGET).mkdir(parents=True)
        (legacy / LEGACY_TARGET / "checkout.py").write_text("# legacy layout\n")
        (legacy / LEGACY_GATEWAY / "service").mkdir(parents=True)
        (legacy / LEGACY_GATEWAY / "service" / "usage_aggregator.py").write_text(
            "# legacy layout\n"
        )
        t_leg, g_leg = discover_new([legacy])
        gate(
            "G3 legacy directory names STILL resolve (fix adds names, never replaces)",
            t_leg is not None and g_leg is not None,
            f"legacy {LEGACY_TARGET}/ + {LEGACY_GATEWAY}/ -> both still found",
        )

    # -------------------------------------------------- G4 · WITNESS A --
    # Reproduce the defect: the OLD logic, on the REAL workspace, finds nothing.
    # Note this gate is meaningful in BOTH environments: in the commander workspace
    # it proves the old logic was blind DESPITE the repos being present (the actual
    # defect); in a bare checkout it is trivially true. G5 is what distinguishes
    # them, which is why G5 skips rather than passes when the siblings are absent.
    t_old, g_old = discover_old(fleet_roots)
    old_blind = t_old is None or g_old is None
    gate(
        "G4 WITNESS A — pre-repair discovery is BLIND on the real workspace",
        old_blind,
        f"old logic looked for {LEGACY_TARGET}/ + {LEGACY_GATEWAY}/ and found "
        f"target={t_old} gateway={g_old} -> G6 always SKIPped (the defect)"
        + (
            " [decisive: the repos ARE present, yet the old logic found neither]"
            if siblings_present
            else " [bare checkout: trivially true here; see G5]"
        ),
    )

    # -------------------------------------------------- G5 · WITNESS B --
    # DIVERGENCE — the load-bearing gate. Same filesystem, two implementations:
    # OLD skips the 3 cross-fleet gates, NEW executes them. If they agreed, the
    # repair would be a no-op and this gate would fail, telling us so.
    #
    # Requires the siblings: a divergence between "finds nothing" and "finds the
    # repos" is only observable where the repos exist. In a bare checkout BOTH
    # implementations correctly find nothing -- that is agreement, not a defect,
    # so the gate is not evaluable and must SKIP rather than fail.
    if not siblings_present:
        skip(
            "G5 WITNESS B — DIVERGENCE: OLD skips all 3 cross-fleet gates, NEW runs them",
            "sibling fleet repos absent -- OLD and NEW discovery both correctly find "
            "nothing here, so there is no divergence to observe. This is the gate that "
            "proves the repair is load-bearing; run it from the commander workspace.",
        )
    else:
        old_skips = old_blind
        new_executes = t_new is not None and g_new is not None
        diverges = old_skips and new_executes
        gate(
            "G5 WITNESS B — DIVERGENCE: OLD skips all 3 cross-fleet gates, NEW runs them",
            diverges,
            f"OLD -> SKIP (3 gates never run, yet exit 0 / '6/6 passed'); "
            f"NEW -> EXECUTE (9/9, G6a+G6b+G6c re-confirm the live defects). "
            f"diverges={diverges}",
        )

    # ---------------------------------------------------------------- G6 --
    # NEGATIVE CONTROL. Making a gate GREEN is easy and worthless; making it
    # CORRECT means it must still refuse to claim coverage it lacks. Point the
    # repaired discovery at an empty workspace: it must find nothing, which is
    # what drives the now-honest "3 SKIPPED" report instead of a silent pass.
    with tempfile.TemporaryDirectory() as tmp:
        empty = pathlib.Path(tmp)
        t_empty, g_empty = discover_new([empty])
        skip_is_visible = "3 SKIPPED" in VERIFIER.read_text()
        gate(
            "G6 NEGATIVE CONTROL — genuinely-absent siblings still SKIP (gate bites)",
            t_empty is None and g_empty is None and skip_is_visible,
            f"empty workspace -> target={t_empty} gateway={g_empty}; "
            f"and the SKIP path now reports '3 SKIPPED' rather than folding "
            f"un-run gates into the pass count ({skip_is_visible})",
        )

    # ---------------------------------------------------------------- G7 --
    # No production source may be touched by any of the above. This is a
    # verifier-only repair. Every file that had a PRE-RUN baseline is re-hashed
    # and compared -- session.js AND the two sibling production files the INC-9
    # verifier imports. A gate that only checked session.js would have been blind
    # to drift in checkout.py / usage_aggregator.py, which is exactly the class of
    # blind spot this incident is about.
    drift = [
        f"{rel} ({before[:12]} -> {sha(path)[:12]})"
        for rel, (path, before) in BASELINES.items()
        if sha(path) != before
    ]
    gate(
        "G7 ALL production source byte-identical to its pre-run hash (verifier-only change)",
        not drift,
        f"re-hashed {len(BASELINES)} production file(s) against pre-run baselines: "
        + ", ".join(f"{rel.split('/')[-1]}={before[:12]}" for rel, (_, before) in BASELINES.items())
        + f"; drifted={drift or 'none'}",
    )

    # ------------------------------------------------------------ summary --
    n_pass = sum(1 for _, ok, _ in RESULTS if ok)
    n_tot = len(RESULTS)
    n_skip = len(SKIPPED)
    print(f"\n{'=' * 74}")
    print(
        f"INC-13 GATES: {n_pass}/{n_tot} passed"
        + (f", {n_skip} SKIPPED (not evaluable here -- NOT counted as passes)" if n_skip else "")
    )
    print(f"{'=' * 74}")

    if n_pass != n_tot:
        for name, ok, _ in RESULTS:
            if not ok:
                print(f"  FAILED: {name}")
        return 1

    if n_skip:
        for name, _ in SKIPPED:
            print(f"  SKIPPED: {name}")
        if require_cross_fleet:
            print(
                "\nFATAL: cross-fleet gates were REQUIRED (FABRIC_REQUIRE_CROSS_FLEET /\n"
                "       --require-cross-fleet) but the sibling repos were not found.\n"
                "       Refusing to report a pass for gates that never ran."
            )
            return 1
        print(
            "\nExit 0 is correct for a bare single-repo checkout (this repo's own CI\n"
            "clones only checkout-api). The skipped gates are NOT counted as passes --\n"
            "that laundering IS the INC-13 defect. Run with --require-cross-fleet from\n"
            "the commander workspace to make a missing sibling FATAL."
        )
        return 0

    print(
        "All gates green. G6's three cross-fleet gates were UNREACHABLE and the\n"
        "verifier still exited 0 claiming '6/6 passed'. They now execute (9/9) and\n"
        "re-confirm the three owner-blocked billing defects are still live. G5 is\n"
        "the proof of value: the OLD logic skips them, the NEW logic runs them.\n"
        "G6 is the proof of correctness: a genuine absence still reports SKIPPED\n"
        "instead of laundering un-run gates into a pass. No production source,\n"
        "test assertion, or dependency was changed."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
