#!/usr/bin/env python3
"""Fabric incident commander — INC-16 verification gates.

THE FINDING
-----------
`artifacts/incident/verify_inc9_ci_gate.py` on `main` carried three cross-fleet
gates (G6a/G6b/G6c) whose only job is to re-confirm -- BY EXECUTING THE DEPLOYED
SOURCE -- that the three owner-blocked billing defects (INC-6, INC-5, INC-8) are
still live. That is the mechanism that stops the commander from carrying findings
forward on a previous run's word.

Its sibling discovery searched for directories named `incident-target` and
`gateway`. The fleet repos are actually named `fabric-ic-incident-target` and
`fabric-gateway-demo`. The lookup NEVER matched -- in ANY environment, including
the commander workspace it was written for -- so G6 always took the SKIP path and
G6a/G6b/G6c never executed. Worse, the skip path computed `passed == total` over
only the gates that HAD run, printing a confident "GATES: 6/6 passed" while a
third of the verifier was unreachable code.

That is this fleet's signature failure -- *a gate that cannot fail is decoration*
-- reproduced INSIDE the verifier whose only job is to police it. A skipped check
laundered into a pass count is worse than a missing check: it actively asserts
coverage it does not have.

WHAT THIS VERIFIER PROVES
-------------------------
  G1  the repaired INC-9 verifier passes AND the cross-fleet gates actually RAN
  G2  the SHIPPED discovery resolves both real fleet repo names
  G3  legacy names still resolve (the fix ADDS names, never replaces them)
  G4  WITNESS A  — the pre-repair discovery is BLIND on this very filesystem
  G5  WITNESS B  — DIVERGENCE: OLD skips all 3 gates · NEW executes all 3
  G6  NEGATIVE CONTROL — genuinely-absent siblings still report SKIPPED, exit 0
  G7  strict mode refuses to pass un-run gates (exit 1, FATAL)
  G8  no production drift: 3/3 deployed sources byte-identical (full sha256)

G5 IS THE WHOLE ARGUMENT. It does not merely assert the new code works -- it
proves the OLD code was blind on the SAME filesystem where the new code succeeds.
Had both implementations behaved alike, the repair would be a no-op and G5 would
say so.

CRITICALLY, G2/G3/G5 drive the *SHIPPED* `_find` / `_TARGET_DIRS` / `_GATEWAY_DIRS`
symbols, imported from the real verifier module. An earlier draft re-implemented
the repaired lookup locally and asserted against that copy -- which would have
passed even if the lookup actually shipped in verify_inc9_ci_gate.py were still
blind. A gate that tests a duplicate of the code instead of the code is exactly
the "proves the wrong thing" failure this fleet keeps producing.

THIS VERIFIER IS ENVIRONMENT-AWARE, AND THAT IS LOAD-BEARING
------------------------------------------------------------
G1/G2/G4/G5/G8 structurally REQUIRE the sibling fleet repos: they execute the
deployed billing sources and hash them. `checkout-api` CI runs `actions/checkout`,
which clones ONLY this repo -- the siblings are legitimately absent there.

An earlier draft of the CI step invoked this verifier unconditionally. Against a
bare checkout it reported 4/8, exit 1. Left in, that would have shipped a
PERMANENTLY-RED gate -- the exact INC-11/INC-12 expired-precondition bug,
re-committed by the very incident that diagnoses it. A gate that can never pass is
exactly as worthless as a gate that can never fail: both teach the team to ignore
the red.

So the sibling-dependent gates SKIP when the siblings are absent -- reported,
never counted as passes, never in the denominator -- and `--require-cross-fleet` /
FABRIC_REQUIRE_CROSS_FLEET=1 promotes a missing sibling to FATAL for a caller
(like the commander workspace) that knows they ought to be there.

Run:  python3 artifacts/incident/verify_inc16_cross_fleet_gate.py
      python3 artifacts/incident/verify_inc16_cross_fleet_gate.py --require-cross-fleet
Exit: 0 = every gate that COULD run passed.
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

HERE = pathlib.Path(__file__).resolve()
REPO = HERE.parents[2]
VERIFIER = REPO / "artifacts" / "incident" / "verify_inc9_ci_gate.py"
FLEET = REPO.parent

# The deployed revisions, asserted on the FULL sha256 (not a truncated prefix).
DEPLOYED = {
    REPO / "service" / "checkout" / "session.js":
        "b45a8eeceaa142dd70aea4182930d02edb2d23ce90f0f02527910abb5f18d7e8",
    FLEET / "fabric-ic-incident-target" / "checkout.py":
        "da2a02fd87aec668467114e0bc30ff7c2fe7fd3d8f105f5f156361b9c87c5c5e",
    FLEET / "fabric-gateway-demo" / "service" / "usage_aggregator.py":
        "bb21e50f7b5dab4463b71984bbe86a5df2b6ba442ffeff84d9b70815781750e5",
}

RESULTS: list[tuple[str, bool, str]] = []
# Skips live in their OWN list: reported, but structurally incapable of entering
# the pass tally OR the denominator. That is the very defect INC-16 repairs; this
# verifier must not commit it itself.
SKIPPED: list[tuple[str, str]] = []


def gate(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n         {detail}" if detail else ""))


def skip(name: str, detail: str = "") -> None:
    SKIPPED.append((name, detail))
    print(f"[SKIPPED] {name}" + (f"\n         {detail}" if detail else ""))


def strict_cross_fleet() -> bool:
    if "--require-cross-fleet" in sys.argv:
        return True
    return os.environ.get("FABRIC_REQUIRE_CROSS_FLEET", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def load_shipped():
    """Import the SHIPPED verifier module, so we test the code that actually runs."""
    spec = importlib.util.spec_from_file_location("shipped_inc9", VERIFIER)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def run_verifier(cwd: pathlib.Path, *args: str, env: dict | None = None):
    import os

    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run(
        [sys.executable, str(cwd / "artifacts" / "incident" / "verify_inc9_ci_gate.py"), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=600,
        env=e,
    )


# The PRE-REPAIR discovery, reproduced verbatim from `main` for the witness.
def old_discovery(fleet_roots):
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


def _find_any(roots, dirnames, *relparts):
    """Sibling lookup INDEPENDENT of the shipped code, for environment detection.

    Deliberately NOT shipped._find: if the shipped lookup were regressed to the
    blind names, using it here would make a fully-populated fleet workspace look
    like a bare checkout, and the sibling-dependent gates would SKIP their way to
    a green exit -- laundering a regression into a pass, which is the exact defect
    INC-16 exists to repair.
    """
    for root in roots:
        for dirname in dirnames:
            cand = root.joinpath(dirname, *relparts)
            if cand.is_file():
                return cand
    return None


def summarize() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{'=' * 74}")
    print(
        f"INC-16 GATES: {passed}/{total} passed"
        + (f" · {len(SKIPPED)} SKIPPED" if SKIPPED else "")
    )
    for name, _ in SKIPPED:
        print(f"  SKIPPED (NOT counted as a pass, NOT in the denominator): {name}")
    print("=" * 74)
    if passed != total:
        for name, ok, _ in RESULTS:
            if not ok:
                print(f"  FAILED: {name}")
        return 1
    print("All gates that could run passed. The dead cross-fleet gates now EXECUTE;")
    print("a skip can no longer masquerade as a pass; no production source touched.")
    return 0


def run_sibling_free_gates(shipped, only: str | None = None) -> int:
    """G3, G6, G7 — these need NO sibling repos, so they run in every environment.

    They enforce the two properties that must hold everywhere: legacy names still
    resolve, and a skip can never masquerade as a pass (in either direction).
    """
    # ---------------------------------------------------------------- G3 --
    if only in (None, "G3"):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = pathlib.Path(tmp)
            (legacy / "incident-target").mkdir(parents=True)
            (legacy / "incident-target" / "checkout.py").write_text("# legacy layout\n")
            (legacy / "gateway" / "service").mkdir(parents=True)
            (legacy / "gateway" / "service" / "usage_aggregator.py").write_text("# legacy\n")
            t_leg = shipped._find([legacy], shipped._TARGET_DIRS, "checkout.py")
            g_leg = shipped._find(
                [legacy], shipped._GATEWAY_DIRS, "service", "usage_aggregator.py"
            )
            gate(
                "G3 legacy names STILL resolve (the fix adds names, never replaces)",
                t_leg is not None and g_leg is not None,
                f"synthetic legacy layout: target={'FOUND' if t_leg else 'NOT FOUND'}, "
                f"gateway={'FOUND' if g_leg else 'NOT FOUND'}",
            )

    if only == "G3":
        return 0

    # ------------------------------------- G6 · NEGATIVE CONTROL, and G7 --
    # A genuinely-absent sibling must still report SKIPPED and exit 0 -- never a
    # silent pass, and never permanently red.
    with tempfile.TemporaryDirectory() as tmp:
        bare = pathlib.Path(tmp) / "checkout-api"
        subprocess.run(
            ["cp", "-r", str(REPO), str(bare)],
            check=True, capture_output=True, timeout=300,
        )
        nproc = run_verifier(bare)
        nblob = nproc.stdout + nproc.stderr
        nm = re.search(r"^GATES: (\d+)/(\d+) passed(?: · (\d+) SKIPPED)?", nblob, re.M)
        np_, nt = (int(nm.group(1)), int(nm.group(2))) if nm else (0, 0)
        reported_skip = "SKIPPED (NOT counted as a pass)" in nblob
        # The un-run gates must be in NEITHER the numerator NOR the denominator.
        no_launder = nt == 6 and np_ == 6 and not re.search(r"^\[PASS\] G6[abc]\b", nblob, re.M)
        gate(
            "G6 NEGATIVE CONTROL — absent siblings report SKIPPED, exit 0, never a pass",
            nproc.returncode == 0 and reported_skip and no_launder,
            f"bare checkout: exit={nproc.returncode} GATES={np_}/{nt} "
            f"skip_reported={reported_skip}; un-run gates absent from BOTH the pass "
            f"tally and the denominator={no_launder}. Not permanently red, not a silent pass.",
        )

        sproc = run_verifier(bare, "--require-cross-fleet")
        sblob = sproc.stdout + sproc.stderr
        fatal = "REQUIRED but unavailable" in sblob
        gate(
            "G7 strict mode REFUSES to pass un-run gates (exit 1, FATAL)",
            sproc.returncode == 1 and fatal,
            f"--require-cross-fleet on a bare checkout: exit={sproc.returncode} "
            f"fatal_gate_reported={fatal}",
        )
    return 0


def main() -> int:
    print("Fabric incident commander — INC-16 gates (dead cross-fleet gates)\n")
    shipped = load_shipped()
    fleet_roots = [REPO.parent, REPO / "fleet", REPO]

    # Are the sibling fleet repos actually present? G1/G2/G4/G5/G8 structurally
    # require them. In `checkout-api` CI they are legitimately absent, and a gate
    # that cannot pass there is the INC-11 permanently-red bug.
    t_new = shipped._find(fleet_roots, shipped._TARGET_DIRS, "checkout.py")
    g_new = shipped._find(fleet_roots, shipped._GATEWAY_DIRS, "service", "usage_aggregator.py")
    # Look for the siblings under ANY of their known names, so "absent" means
    # genuinely absent -- not merely "the shipped lookup failed to see them".
    # Otherwise a regressed (blind) lookup would masquerade as a bare checkout and
    # SKIP its way to a green exit, which is precisely the laundering INC-16 fixes.
    siblings_on_disk = bool(
        _find_any(fleet_roots, ("fabric-ic-incident-target", "incident-target"), "checkout.py")
        and _find_any(
            fleet_roots, ("fabric-gateway-demo", "gateway"), "service", "usage_aggregator.py"
        )
    )

    if not siblings_on_disk:
        detail = (
            "the sibling fleet repos (fabric-ic-incident-target, fabric-gateway-demo) are "
            "not present in this checkout, under ANY known name. G1/G2/G4/G5/G8 execute and "
            "hash those deployed sources, so they CANNOT run here. This is the normal, "
            "expected state in `checkout-api` CI, which clones only this repo."
        )
        if strict_cross_fleet():
            gate(
                "G1/G2/G4/G5/G8 cross-fleet gates — REQUIRED but the siblings are absent",
                False,
                detail + " FATAL: --require-cross-fleet / FABRIC_REQUIRE_CROSS_FLEET is set.",
            )
        else:
            skip(
                "G1/G2/G4/G5/G8 cross-fleet gates (need the sibling fleet repos)",
                detail
                + " Reported as SKIPPED -- never counted as passes, never in the denominator. "
                + "Pass --require-cross-fleet to make their absence FATAL.",
            )
        # G3/G6/G7 need no siblings, so they still run below and still enforce the
        # two properties that must hold in EVERY environment: legacy names resolve,
        # and a skip can never masquerade as a pass.
        return run_sibling_free_gates(shipped) or summarize()

    # ---------------------------------------------------------------- G1 --
    proc = run_verifier(REPO)
    blob = proc.stdout + proc.stderr
    m = re.search(r"^GATES: (\d+)/(\d+) passed", blob, re.M)
    passed, total = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    ran = [g for g in ("G6a", "G6b", "G6c") if re.search(rf"^\[PASS\] {g}\b", blob, re.M)]
    gate(
        "G1 repaired INC-9 verifier passes AND the cross-fleet gates actually RAN",
        proc.returncode == 0 and passed == total and total == 9 and len(ran) == 3,
        f"exit={proc.returncode} GATES={passed}/{total}; cross-fleet gates executed={ran} "
        f"(pre-repair: 6/6 with all three UNREACHABLE)",
    )

    # ------------------------------------------- G2 · the SHIPPED discovery --
    gate(
        "G2 the SHIPPED discovery resolves both REAL fleet repo names",
        t_new is not None and g_new is not None,
        f"checkout target={'FOUND' if t_new else 'NOT FOUND'} "
        f"({t_new}); gateway={'FOUND' if g_new else 'NOT FOUND'} ({g_new}). "
        f"Driving the shipped _find/_TARGET_DIRS/_GATEWAY_DIRS symbols, not a local copy.",
    )

    # ---------------------------------------------------------------- G3 --
    run_sibling_free_gates(shipped, only="G3")

    # ------------------------------------------------- G4 · WITNESS A --
    # The pre-repair discovery is BLIND -- on this very filesystem, where both
    # siblings demonstrably exist (G2 just found them).
    t_old, g_old = old_discovery(fleet_roots)
    siblings_really_present = t_new is not None and g_new is not None
    gate(
        "G4 WITNESS A — the PRE-REPAIR discovery is BLIND",
        siblings_really_present and t_old is None and g_old is None,
        f"old logic finds target={t_old} gateway={g_old} — i.e. NEITHER — "
        f"despite both being present on this same filesystem (siblings_present="
        f"{siblings_really_present}). The lookup could never match, in any environment.",
    )

    # ------------------------------------------------- G5 · WITNESS B --
    # DIVERGENCE, and it is load-bearing: OLD skips all three gates, NEW executes
    # all three. If both behaved alike, the repair would be a no-op.
    old_would_skip = t_old is None or g_old is None
    new_would_run = t_new is not None and g_new is not None
    gate(
        "G5 WITNESS B — DIVERGENCE (load-bearing): OLD skips 3 gates · NEW executes 3",
        old_would_skip and new_would_run and len(ran) == 3,
        f"OLD discovery -> SKIPS G6a/G6b/G6c (and laundered the skip into "
        f"'6/6 passed'); NEW discovery -> EXECUTES {ran}. Same filesystem, "
        f"opposite outcomes: the repair is not a no-op.",
    )

    # --------------------------------------- G6/G7 · no siblings required --
    run_sibling_free_gates(shipped, only="G6G7")

    # ---------------------------------------------------------------- G8 --
    drift = []
    for path, expected in DEPLOYED.items():
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "MISSING"
        if actual != expected:
            drift.append(f"{path.name}: {actual[:12]} != {expected[:12]}")
    gate(
        "G8 NO PRODUCTION DRIFT — 3/3 deployed sources byte-identical (full sha256)",
        not drift,
        "session.js, checkout.py, usage_aggregator.py all hash-match their deployed "
        "revisions on the FULL sha256" if not drift else "DRIFT: " + "; ".join(drift),
    )

    return summarize()


if __name__ == "__main__":
    sys.exit(main())
