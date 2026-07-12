#!/usr/bin/env python3
"""Fabric incident commander — INC-19 verifier.

THE FINDING (two expired preconditions in verify_inc15_cross_fleet_discovery.py)
-------------------------------------------------------------------------------
1. LAYOUT DEPENDENCE (gates G4/G5). Both witnessed the OLD-vs-NEW discovery
   divergence against the AMBIENT fleet roots. But the OLD lookup searches for
   directories literally named `incident-target/` and `gateway/`. So when the
   siblings are cloned under exactly those LEGACY names, the OLD lookup RESOLVES
   them, "the old discovery is blind" is genuinely FALSE, and G4/G5 both go RED.

   Measured on the same tree, only the directory names differing:

       fabric-ic-incident-target/ + fabric-gateway-demo/  ->  9/9, exit 0
       incident-target/           + gateway/              ->  7/9, exit 1  [!!]

   The verifier's exit code was a function of HOW SOMEBODY NAMED THEIR CLONE
   DIRECTORIES, not of the property under test. And it was self-contradictory:
   G3 of that very file certifies the legacy layout as SUPPORTED ("the fix adds,
   never replaces") -- then G4/G5 hard-failed on it.

2. COUNT DEPENDENCE (gate G6). The negative control asserted:

       no_phantom_passes = bool(lm) and int(lm.group(2)) == 6

   A merge-time fact frozen into a permanent gate. Add ONE ordinary new gate to
   verify_inc9_ci_gate.py -- exactly the behaviour this fleet wants to ENCOURAGE
   -- and the healthy verifier reports 7/7 while this predicate rejects it,
   hard-reddening CI on a repo where nothing is wrong.

Both are the fleet's signature failure: A GATE THAT CANNOT PASS IS EXACTLY AS
WORTHLESS AS ONE THAT CANNOT FAIL -- both teach the team to ignore the red.

THE REPAIR
----------
* Witness blindness on a tree that CAN HOST the witness. A tree the OLD lookup
  can SEE cannot demonstrate its blindness, so when the ambient layout is legacy
  the witness runs against a synthetic canonical real-name fleet. `ran` is still
  measured against the AMBIENT fleet -- moving it would make G5 a tautology.
* Assert the INVARIANT, not the CONSTANT: the denominator counts exactly the
  gates that EXECUTED, and a SKIPPED gate is in neither numerator nor
  denominator. Count-independent, while a laundered skip is STILL caught.

GATES
-----
  G1  the repaired INC-15 verifier passes under the REAL repo names (no regression)
  G2  WITNESS A -- the PRE-repair verifier FAILS on the LEGACY layout (the defect)
  G3  WITNESS B -- the repaired verifier PASSES on that same legacy layout
  G4  DIVERGENCE (load-bearing) -- identical tree, opposite verdicts
  G5  ANTI-WEAKENING -- a laundered skip is STILL rejected (correction, not relaxation)
  G6  count independence -- an ordinary added gate does NOT redden the negative control
  G7  no production drift -- every deployed source present matches its baseline

G4 and G5 are the load-bearing pair. A gate that was already green cannot show a
fix was needed; and a fix that makes a red gate green while also accepting the
defect it polices is a cover-up, not a repair. G5 is what rules that out.

Runs on a BARE CHECKOUT (the CI case): the layout gates need sibling repos, so
where they are absent they SKIP -- reported, never a pass, never permanently red.

Exit: 0 = every executed gate passed.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve()
CHECKOUT_API = HERE.parents[2]
INC15_REL = "artifacts/incident/verify_inc15_cross_fleet_discovery.py"
INC9_REL = "artifacts/incident/verify_inc9_ci_gate.py"

INC15 = CHECKOUT_API / INC15_REL

_TARGET_NAMES = ("fabric-ic-incident-target", "incident-target")
_GATEWAY_NAMES = ("fabric-gateway-demo", "gateway")

BASELINES = {
    CHECKOUT_API / "service" / "checkout" / "session.js":
        "b45a8eeceaa142dd70aea4182930d02edb2d23ce90f0f02527910abb5f18d7e8",
}

RESULTS: list[tuple[str, bool, str]] = []
SKIPPED: list[tuple[str, str]] = []


def gate(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n         {detail}" if detail else ""))


def skip(name: str, detail: str = "") -> None:
    SKIPPED.append((name, detail))
    print(f"[SKIP] {name}" + (f"\n         {detail}" if detail else ""))


# The PRE-REPAIR G4/G5 witness logic, reproduced verbatim: it compares the OLD
# and NEW discovery against the AMBIENT roots only.
PRE_REPAIR_WITNESS = """
        w_o_target, w_o_gateway = old_discovery(roots)
        w_n_target, w_n_gateway = new_discovery(roots)
"""
POST_REPAIR_MARKER = "witness_roots"


def _find_fleet(root: pathlib.Path):
    target = next(
        (root / n / "checkout.py" for n in _TARGET_NAMES if (root / n / "checkout.py").is_file()),
        None,
    )
    gateway = next(
        (
            root / n / "service" / "usage_aggregator.py"
            for n in _GATEWAY_NAMES
            if (root / n / "service" / "usage_aggregator.py").is_file()
        ),
        None,
    )
    return target, gateway


def siblings_here():
    for root in (CHECKOUT_API.parent, CHECKOUT_API / "fleet", CHECKOUT_API.parent.parent):
        t, g = _find_fleet(root)
        if t and g:
            return t, g
    return None, None


# INC-28 REPAIR -- the strict intent must be PASSED, never INHERITED.
#
# Nearly every gate below spawns a CHILD verifier. Several of those children are
# NEGATIVE CONTROLS: they run against a synthetic tree where the siblings are
# deliberately absent, and they require the child to report SKIP and exit 0.
#
# subprocess.run() without env= hands the child the parent's WHOLE environment.
# So an ambient FABRIC_REQUIRE_CROSS_FLEET=1 (set by an operator or a CI job that
# wants strict cross-fleet checking) is inherited by the control child, forces it
# into strict mode, and makes it HARD-FAIL where the control demands a SKIP. The
# gate then reports a failure that has nothing to do with the property it tests.
#
# Measured before this repair, on a HEALTHY fleet: env var set -> INC-19 2/7,
# exit 1. This verifier collapses hardest precisely because so many of its gates
# spawn children. A negative control that inherits the very flag it is
# controlling for is not a control.
STRICT_ENV_VAR = "FABRIC_REQUIRE_CROSS_FLEET"


def child_env(*, strict: bool | None = None) -> dict:
    """Environment for a spawned child verifier, with the strict intent explicit."""
    env = dict(os.environ)
    env.pop(STRICT_ENV_VAR, None)
    if strict:
        env[STRICT_ENV_VAR] = "1"
    return env


def run(cwd: pathlib.Path, rel: str, *args: str, strict: bool | None = None):
    p = subprocess.run(
        [sys.executable, rel, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=900,
        env=child_env(strict=strict),
    )
    return p.returncode, p.stdout + p.stderr


def failed_gates(blob: str) -> list[str]:
    return re.findall(r"^\[FAIL\] (\S+)", blob, re.M)


def build_fleet(tmp: pathlib.Path, target_name: str, gateway_name: str) -> pathlib.Path:
    """A fleet laid out under the GIVEN directory names, with the real sources."""
    t_src, g_src = siblings_here()
    fleet = tmp / "fleet"
    api = fleet / "checkout-api"
    shutil.copytree(CHECKOUT_API, api)
    (fleet / target_name).mkdir(parents=True)
    shutil.copy(t_src, fleet / target_name / "checkout.py")
    (fleet / gateway_name / "service").mkdir(parents=True)
    shutil.copy(g_src, fleet / gateway_name / "service" / "usage_aggregator.py")
    return api


def revert_to_pre_repair(api: pathlib.Path) -> bool:
    """Restore the LAYOUT-DEPENDENT witness in a throwaway copy of the verifier."""
    path = api / INC15_REL
    src = path.read_text()
    if POST_REPAIR_MARKER not in src:
        return False
    # Re-point the witness at the ambient roots -- the pre-repair behaviour.
    src = re.sub(
        r"\n        w_o_target, w_o_gateway = old_discovery\(witness_roots\)"
        r"\n        w_n_target, w_n_gateway = new_discovery\(witness_roots\)\n",
        PRE_REPAIR_WITNESS,
        src,
        count=1,
    )
    if "old_discovery(witness_roots)" in src:
        return False
    path.write_text(src)
    import py_compile

    try:
        py_compile.compile(str(path), doraise=True)
    except py_compile.PyCompileError:
        return False
    return True


def main() -> int:
    print("Fabric incident commander — INC-19 verification gates\n")
    t_src, g_src = siblings_here()
    have_siblings = t_src is not None and g_src is not None

    if not have_siblings:
        skip(
            "G1-G6 layout/count witnesses",
            "the sibling fleet repos are not in this checkout (CI clones only this "
            "repo), and every layout witness structurally needs their SOURCES to "
            "build a real-name and a legacy-name fleet. Reported as SKIPPED -- never "
            "a pass, and never a hard failure, so this step cannot become the "
            "permanently-red INC-11 bug it exists to prevent.",
        )
        # The static half still runs everywhere: the repair must be PRESENT.
        src = INC15.read_text()
        gate(
            "G0 the shipped INC-15 verifier carries BOTH repairs (static, no siblings needed)",
            "witness_roots" in src and "denominator == n_executed" in src,
            "layout-independent witness present="
            f"{'witness_roots' in src}; count-independent negative control present="
            f"{'denominator == n_executed' in src}. Strip either and this gate goes RED "
            "in CI, with no siblings required.",
        )
        return _summary()

    # ------------------------------------------------------------------ G1 --
    with tempfile.TemporaryDirectory() as td:
        api = build_fleet(pathlib.Path(td), "fabric-ic-incident-target", "fabric-gateway-demo")
        rc, blob = run(api, INC15_REL)
        gate(
            "G1 the repaired verifier passes under the REAL repo names (no regression)",
            rc == 0 and not failed_gates(blob),
            f"exit={rc} failed={failed_gates(blob) or 'none'}",
        )

    # ------------------------------------------------- G2/G3/G4 · LEGACY --
    with tempfile.TemporaryDirectory() as td:
        api_old = build_fleet(pathlib.Path(td), "incident-target", "gateway")
        reverted = revert_to_pre_repair(api_old)
        rc_old, blob_old = run(api_old, INC15_REL)
        gate(
            "G2 WITNESS A — the PRE-REPAIR verifier FAILS on the LEGACY layout",
            reverted and rc_old != 0 and "G4" in " ".join(failed_gates(blob_old)),
            f"reverted={reverted} exit={rc_old} failed={failed_gates(blob_old) or 'none'} "
            "— the OLD lookup can SEE a legacy-named fleet, so 'the old discovery is "
            "blind' is false and its own witness gates go RED on a healthy tree",
        )

    with tempfile.TemporaryDirectory() as td:
        api_new = build_fleet(pathlib.Path(td), "incident-target", "gateway")
        rc_new, blob_new = run(api_new, INC15_REL)
        gate(
            "G3 WITNESS B — the REPAIRED verifier PASSES on that same legacy layout",
            rc_new == 0 and not failed_gates(blob_new),
            f"exit={rc_new} failed={failed_gates(blob_new) or 'none'} — the witness is "
            "relocated onto a synthetic canonical real-name fleet, which CAN host it",
        )

    gate(
        "G4 DIVERGENCE (load-bearing) — identical legacy tree, opposite verdicts",
        rc_old != 0 and rc_new == 0,
        f"same legacy-named fleet -> PRE-repair exit={rc_old} [RED] · POST-repair "
        f"exit={rc_new} [GREEN]. The repair is therefore NOT a no-op.",
    )

    # ---------------------------------------------------- G5 · ANTI-WEAKENING --
    # Launder a SKIP back into the pass tally -- the ORIGINAL INC-15 defect. The
    # repaired negative control must STILL catch it. Making a red gate green is
    # trivial; this is what proves the repair did not also blind the gate.
    with tempfile.TemporaryDirectory() as td:
        bare = pathlib.Path(td) / "checkout-api"
        shutil.copytree(CHECKOUT_API, bare)
        inc9 = bare / INC9_REL
        src = inc9.read_text()
        old_sum = '    print(f"\\n{\'=\' * 74}\\nGATES: {passed}/{total} passed", end="")'
        laundered_ok = old_sum in src
        if laundered_ok:
            inc9.write_text(
                src.replace(
                    old_sum,
                    "    passed += len(SKIPPED)\n    total += len(SKIPPED)\n" + old_sum,
                    1,
                )
            )
            rc_l, blob_l = run(bare, INC15_REL)
            gate(
                "G5 ANTI-WEAKENING — a laundered skip is STILL rejected",
                rc_l != 0 and any(g.startswith("G6") for g in failed_gates(blob_l)),
                f"skip folded into the tally -> exit={rc_l} failed={failed_gates(blob_l)} "
                "— a CORRECTION, not a relaxation",
            )
        else:
            gate("G5 ANTI-WEAKENING — a laundered skip is STILL rejected", False,
                 "could not locate the summary line to mutate")

    # ------------------------------------------------ G6 · COUNT INDEPENDENCE --
    with tempfile.TemporaryDirectory() as td:
        bare = pathlib.Path(td) / "checkout-api"
        shutil.copytree(CHECKOUT_API, bare)
        inc9 = bare / INC9_REL
        src = inc9.read_text()
        anchor = "    # ---------------------------------------------------------------- G6 --"
        extra = (
            '\n    gate(\n        "G9 an ordinary new gate (the kind any future PR might add)",\n'
            '        True,\n        "trivially true: its only job is to change the gate COUNT",\n'
            "    )\n"
        )
        if anchor in src:
            inc9.write_text(src.replace(anchor, extra + anchor, 1))
            rc_x, blob_x = run(bare, INC15_REL)
            old_pred = re.search(r"^GATES: (\d+)/(\d+) passed", blob_x, re.M)
            gate(
                "G6 COUNT INDEPENDENCE — an ordinary added gate does NOT redden the control",
                rc_x == 0 and not any(g.startswith("G6") for g in failed_gates(blob_x)),
                f"exit={rc_x} failed={failed_gates(blob_x) or 'none'} — the pre-repair "
                "predicate `denominator == 6` would have REJECTED this healthy repo",
            )
        else:
            gate(
                "G6 COUNT INDEPENDENCE — an ordinary added gate does NOT redden the control",
                False,
                "could not locate the G6 anchor to inject a gate",
            )

    # ------------------------------------------------------------------ G7 --
    drift = []
    checked = 0
    for path, expected in BASELINES.items():
        if not path.is_file():
            continue
        checked += 1
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            drift.append(f"{path.name}: {actual[:16]} != {expected[:16]}")
    gate(
        "G7 NO PRODUCTION DRIFT — deployed source present here matches its baseline",
        not drift and checked > 0,
        f"{checked}/{len(BASELINES)} byte-identical on the FULL sha256"
        if not drift
        else "; ".join(drift),
    )

    return _summary()


def _summary() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{'=' * 74}\nINC-19 GATES: {passed}/{total} passed", end="")
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
    print("All executed gates green. The INC-15 witness gates no longer depend on")
    print("ambient directory names or a hardcoded gate count -- and a laundered skip")
    print("is still caught. Production source untouched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
