#!/usr/bin/env python3
"""Fabric incident commander — INC-15 verifier.

THE FINDING
-----------
`artifacts/incident/verify_inc9_ci_gate.py` carries three cross-fleet gates
(G6a/G6b/G6c) whose only job is to re-confirm -- BY EXECUTING THE DEPLOYED
SOURCE -- that the three owner-blocked billing defects (INC-6, INC-5, INC-8) are
still live. That is the mechanism which stops the commander from carrying a
finding forward on a previous run's word.

Its sibling discovery searched for directories named `incident-target/` and
`gateway/`. The fleet repos are actually named `fabric-ic-incident-target` and
`fabric-gateway-demo`. The lookup therefore NEVER matched -- in any environment,
including the commander workspace it was written for. G6 always took the SKIP
path, so G6a/G6b/G6c never executed. Worse, the skip path computed
`passed == total` over only the gates that HAD run, and printed:

    GATES: 6/6 passed

...while a third of the verifier was unreachable dead code. A skip laundered into
a pass count is worse than a missing check: it actively asserts coverage it does
not have. This is the fleet's signature failure -- *a gate that cannot fail is
decoration* -- reproduced INSIDE the verifier whose only job is to police it.

PRs #11 and #13 both diagnosed this. NEITHER MERGED, so the repair never landed
on `main` and the blind discovery is still live there. This verifier gates the
repair that lands it.

GATES
-----
  G1  the repaired verifier passes AND the cross-fleet gates actually RAN
  G2  discovery resolves both REAL fleet repo names
  G3  legacy names still resolve (the fix ADDS names, never replaces them)
  G4  WITNESS A -- the pre-repair discovery is BLIND on this very filesystem
  G5  WITNESS B -- DIVERGENCE: OLD skips all 3 gates, NEW executes all 3
  G6  NEGATIVE CONTROL -- genuinely-absent siblings still SKIP (exit 0), so the
      repair does not leave `checkout-api` CI permanently red (the INC-11 bug)
  G7  strict mode refuses to pass un-run gates (exit 1, FATAL)
  G8  no production drift: all 3 sources byte-identical to their baselines

G5 is the load-bearing gate. It does not merely assert the new code works -- it
proves the OLD code was blind on the SAME filesystem. Had both implementations
behaved alike, the repair would be a no-op and G5 would say so.

Exit: 0 = every gate passed.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve()
CHECKOUT_API = HERE.parents[2]
FLEET = CHECKOUT_API.parent
VERIFIER = CHECKOUT_API / "artifacts" / "incident" / "verify_inc9_ci_gate.py"

TARGET = FLEET / "fabric-ic-incident-target"
GATEWAY = FLEET / "fabric-gateway-demo"

# Deployed revisions, recorded before this run touched anything. FULL sha256 --
# a truncated prefix is a weaker assertion than the one we can trivially make.
BASELINES = {
    CHECKOUT_API / "service" / "checkout" / "session.js":
        "b45a8eeceaa142dd70aea4182930d02edb2d23ce90f0f02527910abb5f18d7e8",
    GATEWAY / "service" / "usage_aggregator.py":
        "bb21e50f7b5dab4463b71984bbe86a5df2b6ba442ffeff84d9b70815781750e5",
    TARGET / "checkout.py":
        "da2a02fd87aec668467114e0bc30ff7c2fe7fd3d8f105f5f156361b9c87c5c5e",
}

RESULTS: list[tuple[str, bool, str]] = []
SKIPPED: list[tuple[str, str]] = []


def gate(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n         {detail}" if detail else ""))


def skip(name: str, detail: str = "") -> None:
    """A gate that COULD NOT RUN here. Never a pass, never a false failure.

    THE GATE CAUGHT ITS OWN AUTHOR -- worth recording, because it changed this
    file. The first version of this verifier ran G1/G2/G5 unconditionally. Those
    gates STRUCTURALLY REQUIRE the sibling fleet repos... which `checkout-api`
    CI does not clone. So the moment it was wired into CI it went red:

        [FAIL] G1 ... cross-fleet gates executed=[]
        [FAIL] G2 ... fabric-ic-incident-target=MISSING fabric-gateway-demo=MISSING
        [FAIL] G5 ... NEW -> executes []
        FileNotFoundError: .../fabric-gateway-demo/service/usage_aggregator.py

    That is a PERMANENTLY-RED GATE -- precisely the INC-11 expired-precondition
    bug, and I would have committed it inside the very incident that diagnoses
    it. A gate that can never pass is exactly as worthless as one that can never
    fail: both teach the team to ignore the red.

    So the sibling-dependent gates are environment-aware. Where the siblings are
    absent they report SKIPPED -- excluded from both the numerator AND the
    denominator -- and `--require-cross-fleet` promotes that to a hard failure for
    a caller (the commander workspace) that knows they ought to be there.
    """
    SKIPPED.append((name, detail))
    print(f"[SKIP] {name}" + (f"\n         {detail}" if detail else ""))


def run_verifier(cwd: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "artifacts/incident/verify_inc9_ci_gate.py", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=600,
    )


def _load_deployed_verifier():
    """Import the SHIPPED verifier and hand back its REAL discovery symbols.

    This matters more than it looks. An earlier draft of this file re-implemented
    the repaired lookup locally and asserted against that copy -- which would
    have passed even if the lookup actually shipped in verify_inc9_ci_gate.py
    were still blind. A gate that tests a duplicate of the code instead of the
    code is precisely the "proves the wrong thing" failure this fleet keeps
    producing (INC-9/11/12/14). So G2/G3/G5 below drive the DEPLOYED function.

    The module runs a little top-level setup on import (it locates the repo root)
    but does not execute any gates unless __main__ is invoked, so importing it is
    side-effect-safe.
    """
    spec = importlib.util.spec_from_file_location("inc9_deployed", VERIFIER)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    missing = [n for n in ("_find", "_TARGET_DIRS", "_GATEWAY_DIRS") if not hasattr(mod, n)]
    if missing:
        raise AssertionError(
            f"the deployed verifier is missing the repaired discovery symbols: {missing}"
        )
    return mod


# ---------------------------------------------------------------------------
# The PRE-REPAIR discovery logic, reproduced verbatim from the code that is on
# `main` today. This is Witness A: we run the OLD lookup against the SAME
# filesystem that the NEW lookup succeeds on, and show it finds nothing.
# ---------------------------------------------------------------------------
def old_discovery(fleet_roots: list[pathlib.Path]):
    target = next(
        (p for p in (r / "incident-target" / "checkout.py" for r in fleet_roots) if p.is_file()),
        None,
    )
    gateway = next(
        (
            p
            for p in (r / "gateway" / "service" / "usage_aggregator.py" for r in fleet_roots)
            if p.is_file()
        ),
        None,
    )
    return target, gateway


def new_discovery(fleet_roots: list[pathlib.Path]):
    """The REPAIRED lookup -- driven through the DEPLOYED verifier's own symbols.

    Not a local re-implementation: this calls `_find` with `_TARGET_DIRS` /
    `_GATEWAY_DIRS` as they are actually shipped in verify_inc9_ci_gate.py. If
    the shipped discovery regresses (a name removed, `_find` broken), these gates
    go RED -- which is the entire point.
    """
    mod = _load_deployed_verifier()
    return (
        mod._find(fleet_roots, mod._TARGET_DIRS, "checkout.py"),
        mod._find(fleet_roots, mod._GATEWAY_DIRS, "service", "usage_aggregator.py"),
    )


def gates_that_ran(blob: str) -> set[str]:
    """Which cross-fleet gates actually EXECUTED (appear as PASS/FAIL lines)."""
    return {g for g in ("G6a", "G6b", "G6c") if re.search(rf"^\[(PASS|FAIL)\] {g} ", blob, re.M)}


def main() -> int:
    print("Fabric incident commander — INC-15 verification gates\n")
    roots = [FLEET, CHECKOUT_API / "fleet", CHECKOUT_API, CHECKOUT_API.parent]

    # Are the sibling fleet repos actually here? `checkout-api` CI clones ONLY
    # this repo, so on a bare checkout they legitimately are not -- and the gates
    # that need them must SKIP rather than fail (see skip.__doc__).
    n_target, n_gateway = new_discovery(roots)
    siblings_present = n_target is not None and n_gateway is not None
    strict = "--require-cross-fleet" in sys.argv or os.environ.get(
        "FABRIC_REQUIRE_CROSS_FLEET", ""
    ).strip().lower() in {"1", "true", "yes", "on"}

    if not siblings_present and strict:
        gate(
            "G0 STRICT MODE — sibling fleet repos are required but MISSING",
            False,
            "FATAL: --require-cross-fleet / FABRIC_REQUIRE_CROSS_FLEET was set, but "
            f"fabric-ic-incident-target={'FOUND' if n_target else 'MISSING'} "
            f"fabric-gateway-demo={'FOUND' if n_gateway else 'MISSING'}. "
            "Refusing to pass gates that cannot execute.",
        )
        return _summary()

    # ------------------------------------------------------------------ G1 --
    if siblings_present:
        proc = run_verifier(CHECKOUT_API, "--require-cross-fleet")
        blob = proc.stdout + proc.stderr
        ran = gates_that_ran(blob)
        m = re.search(r"^GATES: (\d+)/(\d+) passed", blob, re.M)
        tally = f"{m.group(1)}/{m.group(2)}" if m else "none"
        gate(
            "G1 repaired INC-9 verifier passes AND the cross-fleet gates actually RAN",
            proc.returncode == 0 and ran == {"G6a", "G6b", "G6c"},
            f"exit={proc.returncode} tally={tally} cross-fleet gates executed={sorted(ran)} "
            f"(pre-repair: 6/6 with G6a/G6b/G6c unreachable)",
        )
    else:
        ran = set()
        skip(
            "G1 repaired INC-9 verifier passes AND the cross-fleet gates actually RAN",
            "requires the sibling fleet repos, which a bare checkout does not clone. "
            "Run from the incident-commander workspace to execute this gate.",
        )

    # ------------------------------------------------------------------ G2 --
    deployed = _load_deployed_verifier()
    if siblings_present:
        gate(
            "G2 the DEPLOYED verifier's discovery resolves both REAL fleet repo names",
            n_target is not None and n_gateway is not None,
            f"driving verify_inc9_ci_gate.py's own _find/_TARGET_DIRS/_GATEWAY_DIRS "
            f"(not a copy): fabric-ic-incident-target={'FOUND' if n_target else 'MISSING'} "
            f"fabric-gateway-demo={'FOUND' if n_gateway else 'MISSING'}; "
            f"shipped names={deployed._TARGET_DIRS} / {deployed._GATEWAY_DIRS}",
        )
    else:
        skip(
            "G2 the DEPLOYED verifier's discovery resolves both REAL fleet repo names",
            "no sibling repos on this filesystem to resolve. The shipped names are "
            f"{deployed._TARGET_DIRS} / {deployed._GATEWAY_DIRS} -- asserted statically "
            "by G2b below, which needs no siblings.",
        )

    # ----------------------------------------------------------------- G2b --
    # The static half of G2, and it runs EVERYWHERE -- including CI. It cannot
    # confirm a lookup succeeds without siblings, but it CAN confirm the shipped
    # verifier still carries the real repo names. That is the regression that
    # INC-15 exists to prevent, so it must be guarded even on a bare checkout.
    gate(
        "G2b the shipped verifier still carries the REAL fleet repo names (no siblings needed)",
        "fabric-ic-incident-target" in deployed._TARGET_DIRS
        and "fabric-gateway-demo" in deployed._GATEWAY_DIRS
        and "incident-target" in deployed._TARGET_DIRS
        and "gateway" in deployed._GATEWAY_DIRS,
        f"_TARGET_DIRS={deployed._TARGET_DIRS} _GATEWAY_DIRS={deployed._GATEWAY_DIRS} "
        "— real names present AND legacy names retained as fallbacks. Strip either "
        "real name and this gate goes RED, in CI, with no siblings required.",
    )

    # ------------------------------------------------------------------ G3 --
    # The fix must ADD names, never replace them: a checkout laid out with the
    # legacy directory names must keep working.
    with tempfile.TemporaryDirectory() as tmp:
        legacy = pathlib.Path(tmp)
        (legacy / "incident-target").mkdir(parents=True)
        (legacy / "incident-target" / "checkout.py").write_text("# legacy layout\n")
        (legacy / "gateway" / "service").mkdir(parents=True)
        (legacy / "gateway" / "service" / "usage_aggregator.py").write_text("# legacy layout\n")
        l_target, l_gateway = new_discovery([legacy])
        gate(
            "G3 legacy directory names STILL resolve (the fix adds, never replaces)",
            l_target is not None and l_gateway is not None,
            f"synthetic legacy layout: incident-target={'FOUND' if l_target else 'MISSING'} "
            f"gateway={'FOUND' if l_gateway else 'MISSING'}",
        )

    # ------------------------------------------- G4 · WITNESS A (blindness) --
    o_target, o_gateway = old_discovery(roots)
    if siblings_present:
        gate(
            "G4 WITNESS A — the PRE-REPAIR discovery is BLIND on this very filesystem",
            o_target is None and o_gateway is None,
            f"old logic finds NEITHER sibling despite both being present: "
            f"incident-target={o_target} gateway={o_gateway}. "
            f"This is why G6a/G6b/G6c never executed, in ANY environment.",
        )
    else:
        skip(
            "G4 WITNESS A — the PRE-REPAIR discovery is BLIND on this very filesystem",
            "needs the siblings present to demonstrate blindness IN SPITE of their "
            "presence; on a bare checkout finding nothing is the correct answer.",
        )

    # ---------------------------------------- G5 · WITNESS B (DIVERGENCE) --
    # The load-bearing gate. OLD skips all three; NEW executes all three, on the
    # same filesystem. If both behaved alike the repair would be a no-op.
    # Requires the siblings to be present -- there is no divergence to observe on
    # a filesystem where NEITHER implementation could find anything.
    if siblings_present:
        old_would_skip = o_target is None or o_gateway is None
        new_would_run = n_target is not None and n_gateway is not None
        gate(
            "G5 WITNESS B — DIVERGENCE: OLD skips all 3 gates · NEW executes all 3",
            old_would_skip and new_would_run and ran == {"G6a", "G6b", "G6c"},
            f"OLD -> skips G6a/G6b/G6c (and printed a confident '6/6 passed') · "
            f"NEW -> executes {sorted(ran)}. Same filesystem, opposite outcomes: "
            f"the repair is NOT a no-op.",
        )
    else:
        skip(
            "G5 WITNESS B — DIVERGENCE: OLD skips all 3 gates · NEW executes all 3",
            "needs the siblings present: with none on the filesystem, OLD and NEW "
            "both legitimately find nothing and there is no divergence to witness.",
        )

    # -------------------------------------------- G6 · NEGATIVE CONTROL --
    # A bare checkout (exactly what `checkout-api` CI clones) has no siblings.
    # There the gates CANNOT run -- and the honest answer is SKIP, reported and
    # never counted as a pass. It must NOT be fatal: making it fatal would leave
    # the verifier permanently red in the very CI job that runs it, which is the
    # INC-11 expired-precondition bug all over again.
    with tempfile.TemporaryDirectory() as tmp:
        bare = pathlib.Path(tmp) / "checkout-api"
        shutil.copytree(CHECKOUT_API, bare)
        lone = run_verifier(bare)
        lblob = lone.stdout + lone.stderr
        lran = gates_that_ran(lblob)
        skipped_reported = "SKIPPED" in lblob
        # The old bug: a skip folded into the pass tally.
        lm = re.search(r"^GATES: (\d+)/(\d+) passed", lblob, re.M)
        no_phantom_passes = bool(lm) and int(lm.group(2)) == 6  # only the 6 real gates counted
        gate(
            "G6 NEGATIVE CONTROL — absent siblings SKIP (exit 0), never a silent pass",
            lone.returncode == 0
            and not lran
            and skipped_reported
            and no_phantom_passes,
            f"bare checkout: exit={lone.returncode} cross-fleet executed={sorted(lran) or 'none'} "
            f"SKIPPED reported={skipped_reported} tally={lm.group(0) if lm else 'none'} "
            f"(un-run gates are NOT in the denominator, and CI is not permanently red)",
        )

        # -------------------------------------------------------------- G7 --
        strict = run_verifier(bare, "--require-cross-fleet")
        sblob = strict.stdout + strict.stderr
        gate(
            "G7 STRICT MODE refuses to pass gates that never executed",
            strict.returncode == 1 and "FATAL" in sblob,
            f"bare checkout + --require-cross-fleet: exit={strict.returncode}, FATAL reported="
            f"{'FATAL' in sblob} (a caller that KNOWS the siblings should be there gets a hard fail)",
        )

    # ------------------------------------------------------------------ G8 --
    # Hash only the files that EXIST here. On a bare checkout the sibling sources
    # are absent -- reading them raised FileNotFoundError and crashed the verifier
    # in CI. An absent file is not drift; it is a different environment.
    drift = []
    checked = 0
    for path, expected in BASELINES.items():
        if not path.is_file():
            continue
        checked += 1
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            drift.append(f"{path.name}: {actual} != {expected}")
    gate(
        "G8 NO PRODUCTION DRIFT — every deployed source PRESENT here matches its baseline",
        not drift and checked > 0,
        f"{checked}/{len(BASELINES)} sources present and byte-identical on the FULL sha256"
        + ("" if siblings_present else " (siblings absent in this checkout: not drift)")
        if not drift
        else "; ".join(drift),
    )

    # -------------------------------------------------------------- summary --
    return _summary()


def _summary() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{'=' * 74}\nINC-15 GATES: {passed}/{total} passed", end="")
    if SKIPPED:
        print(f"  ({len(SKIPPED)} SKIPPED — NOT counted as passes)", end="")
    print(f"\n{'=' * 74}")
    for name, _ in SKIPPED:
        print(f"  SKIPPED: {name}")
    if passed != total:
        for name, ok, _ in RESULTS:
            if not ok:
                print(f"  FAILED: {name}")
        return 1
    print("All executed gates green. The cross-fleet re-confirmation gates now")
    print("EXECUTE, a skip can never masquerade as a pass, and production is untouched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
