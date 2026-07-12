#!/usr/bin/env python3
"""Fabric incident commander — INC-17 verifier.

THE FINDING
-----------
`verify_inc15_cross_fleet_discovery.py` gate **G6** (the NEGATIVE CONTROL) asserted
the nested INC-9 verifier's pass tally had a denominator of literally six:

    no_phantom_passes = bool(lm) and int(lm.group(2)) == 6   # <-- THE DEFECT

That is a MERGE-TIME FACT frozen into a PERMANENT GATE. It encodes "the INC-9
verifier has exactly six gates" -- true only on the day it was written. Add one
ordinary gate to `verify_inc9_ci_gate.py` and the tally becomes 7/7, so G6 goes
RED in CI while nothing is actually wrong.

This is the fleet's most persistent pathology, and it is now on its fourth
repetition:

  * INC-11  G3 asserted "ci.yml is NEW" -- permanently false once it merged.
  * INC-12  G3 required ci.yml byte-identical to main -- forbade editing CI.
  * INC-15  the cross-fleet gates were unreachable, and the skip was laundered
            into a "6/6 passed" tally.
  * INC-17  the gate that POLICES that laundering hardcoded the very number it
            was policing -- so it punishes the next contributor for adding a gate.

A gate that can never pass is exactly as worthless as one that can never fail:
both teach the team to ignore the red. INC-15's verifier committed the INC-11 bug
inside the file written to stop it.

THE REPAIR
----------
Assert the INVARIANT, not the CONSTANT. What INC-15 actually exists to enforce is:

    the denominator counts exactly the gates that EXECUTED, and a SKIPPED gate is
    in NEITHER the numerator NOR the denominator.

That is count-independent: the INC-9 verifier may grow to 7, 9 or 40 gates and G6
keeps working -- while a skip folded back into the tally is still caught.

GATES
-----
  G1  the repaired INC-15 verifier passes as shipped (fleet workspace + bare)
  G2  WITNESS A -- the PRE-REPAIR G6 predicate is BROKEN by a legitimate new gate
  G3  WITNESS B -- DIVERGENCE (load-bearing): on the SAME tree, with one ordinary
      gate added to the INC-9 verifier, OLD G6 -> FAIL, NEW G6 -> PASS
  G4  the repaired G6 STILL CATCHES the original INC-15 bug (a skip laundered
      into the pass tally) -- the fix must not be a weakening
  G5  the repaired G6 still catches a genuinely FAILING nested verifier
  G6  no production drift: every deployed source present matches its baseline

G3 is the load-bearing gate. It does not merely assert the new predicate works --
it proves the OLD predicate FAILED on the same tree where the new one passes. Had
both behaved alike, the repair would be a no-op and G3 would say so.

G4 is the anti-weakening gate. Making a red gate green is trivial and worthless;
G4 proves the repaired predicate still reddens on the exact defect INC-15 was
raised to eliminate.

Exit: 0 = every gate passed.
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

INC9 = "artifacts/incident/verify_inc9_ci_gate.py"
INC15 = "artifacts/incident/verify_inc15_cross_fleet_discovery.py"

TARGET = FLEET / "fabric-ic-incident-target"
GATEWAY = FLEET / "fabric-gateway-demo"

BASELINES = {
    CHECKOUT_API / "service" / "checkout" / "session.js":
        "b45a8eeceaa142dd70aea4182930d02edb2d23ce90f0f02527910abb5f18d7e8",
    GATEWAY / "service" / "usage_aggregator.py":
        "bb21e50f7b5dab4463b71984bbe86a5df2b6ba442ffeff84d9b70815781750e5",
    TARGET / "checkout.py":
        "da2a02fd87aec668467114e0bc30ff7c2fe7fd3d8f105f5f156361b9c87c5c5e",
}

# The pre-repair G6 predicate, reproduced verbatim from what shipped on `main`.
# This is Witness A: we evaluate the OLD rule against a tally it must not reject,
# and show that it does.
OLD_G6_SOURCE = "no_phantom_passes = bool(lm) and int(lm.group(2)) == 6"

RESULTS: list[tuple[str, bool, str]] = []


def gate(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n         {detail}" if detail else ""))


def old_g6_predicate(tally_denominator: int) -> bool:
    """The PRE-REPAIR rule: the nested denominator must be exactly 6."""
    return tally_denominator == 6


def new_g6_predicate(numerator: int, denominator: int, executed: int, skipped: int) -> bool:
    """The REPAIRED rule -- the durable invariant, count-independent."""
    return (
        denominator == executed
        and numerator <= executed
        and denominator < executed + skipped
    )


def run(cwd: pathlib.Path, script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, script, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=900,
    )


def tally_of(blob: str) -> tuple[int, int]:
    """The nested INC-9 verifier's tally (`GATES: n/m passed`)."""
    m = re.search(r"^GATES: (\d+)/(\d+) passed", blob, re.M)
    return (int(m.group(1)), int(m.group(2))) if m else (-1, -1)


def inc15_tally_of(blob: str) -> tuple[int, int]:
    """The INC-15 verifier's OWN tally (`INC-15 GATES: n/m passed`).

    A separate parser because the two lines differ, and reporting `-1/-1` in a
    gate's evidence line would be its own little provenance defect -- a report
    whose numbers do not match the artifact it describes is the class of failure
    this fleet keeps producing.
    """
    m = re.search(r"^INC-15 GATES: (\d+)/(\d+) passed", blob, re.M)
    return (int(m.group(1)), int(m.group(2))) if m else (-1, -1)


def executed_count(blob: str) -> int:
    return len(re.findall(r"^\[(?:PASS|FAIL)\] ", blob, re.M))


def skipped_count(blob: str) -> int:
    return len(re.findall(r"^\[SKIP\] ", blob, re.M))


def inc15_g6_ok(blob: str) -> bool:
    """Did the INC-15 verifier's G6 gate PASS in this output?"""
    return bool(re.search(r"^\[PASS\] G6 NEGATIVE CONTROL", blob, re.M))


def clone_fleet(dst: pathlib.Path) -> pathlib.Path:
    """A full fleet workspace (all three repos as siblings)."""
    for repo in (CHECKOUT_API, GATEWAY, TARGET):
        if repo.is_dir():
            shutil.copytree(repo, dst / repo.name)
    return dst / CHECKOUT_API.name


def add_ordinary_gate(verifier: pathlib.Path) -> None:
    """Simulate a perfectly legitimate future PR: add one more gate to INC-9.

    This is the change that MUST NOT break anything. Extending the gate surface is
    the behaviour the fleet wants to encourage; a meta-gate that punishes it is
    the defect.
    """
    src = verifier.read_text()
    m = re.search(r"\n(\s*)return _summary\(\)", src)
    assert m, "could not locate the summary return in the INC-9 verifier"
    indent = m.group(1)
    injected = (
        f"\n{indent}gate(\n"
        f"{indent}    \"G9 an ordinary new assertion added by a legitimate future PR\",\n"
        f"{indent}    True,\n"
        f"{indent}    \"nothing is wrong here; the gate surface simply grew\",\n"
        f"{indent})\n"
        f"{indent}return _summary()"
    )
    verifier.write_text(src[: m.start()] + injected + src[m.end() :])


def launder_skip_into_pass(verifier: pathlib.Path) -> None:
    """Re-introduce the ORIGINAL INC-15 defect in the INC-9 verifier's summary.

    The historical bug: skipped gates folded into the pass tally, so the verifier
    printed a confident `6/6 passed` while a third of it never executed. Here we
    make the summary count the SKIPPED gates as passes in BOTH numerator and
    denominator -- exactly the laundering -- and require the repaired G6 to catch
    it. If G6 still passes under this mutation, the INC-17 repair weakened the
    gate and this verifier must fail.
    """
    src = verifier.read_text()
    old = "    passed = sum(1 for _, ok, _ in RESULTS if ok)\n    total = len(RESULTS)"
    assert old in src, "could not locate the INC-9 summary tally"
    new = (
        "    passed = sum(1 for _, ok, _ in RESULTS if ok) + len(SKIPPED)  # LAUNDERED\n"
        "    total = len(RESULTS) + len(SKIPPED)  # LAUNDERED"
    )
    verifier.write_text(src.replace(old, new, 1))


def break_a_gate(verifier: pathlib.Path) -> None:
    """Make the nested INC-9 verifier genuinely FAIL a gate (not skip -- fail)."""
    src = verifier.read_text()
    m = re.search(r"\n(\s*)return _summary\(\)", src)
    assert m
    indent = m.group(1)
    injected = (
        f"\n{indent}gate(\"GX deliberately failing gate\", False, \"injected by INC-17 G5\")\n"
        f"{indent}return _summary()"
    )
    verifier.write_text(src[: m.start()] + injected + src[m.end() :])


def main() -> int:
    print("Fabric incident commander — INC-17 verification gates\n")

    # ------------------------------------------------------------------ G1 --
    # The repaired INC-15 verifier passes as shipped, in BOTH environments that
    # matter: the commander workspace (siblings present) and a bare checkout
    # (= exactly what `checkout-api` CI clones).
    ws = run(CHECKOUT_API, INC15)
    ws_blob = ws.stdout + ws.stderr
    with tempfile.TemporaryDirectory() as tmp:
        bare = pathlib.Path(tmp) / "checkout-api"
        shutil.copytree(CHECKOUT_API, bare)
        br = run(bare, INC15)
        br_blob = br.stdout + br.stderr
    ws_num, ws_den = inc15_tally_of(ws_blob)
    br_num, br_den = inc15_tally_of(br_blob)
    gate(
        "G1 the repaired INC-15 verifier passes as shipped (fleet workspace AND bare checkout)",
        ws.returncode == 0
        and br.returncode == 0
        and inc15_g6_ok(ws_blob)
        and inc15_g6_ok(br_blob)
        and ws_num == ws_den
        and ws_den > 0
        and br_num == br_den
        and br_den > 0,
        f"fleet workspace: exit={ws.returncode} INC-15 tally={ws_num}/{ws_den} G6=PASS · "
        f"bare checkout (= checkout-api CI): exit={br.returncode} "
        f"INC-15 tally={br_num}/{br_den} G6=PASS "
        "(green in the very job that runs it — not permanently red)",
    )

    # ------------------------------------------------------------------ G2 --
    # WITNESS A: the pre-repair predicate is BROKEN by an ordinary new gate.
    # We measure the real tally an extended INC-9 verifier produces on a bare
    # checkout, then evaluate the OLD rule against it.
    with tempfile.TemporaryDirectory() as tmp:
        ext = pathlib.Path(tmp) / "checkout-api"
        shutil.copytree(CHECKOUT_API, ext)
        add_ordinary_gate(ext / INC9)
        nested = run(ext, INC9)
        nblob = nested.stdout + nested.stderr
        num, den = tally_of(nblob)
        n_exec, n_skip = executed_count(nblob), skipped_count(nblob)

        old_verdict = old_g6_predicate(den)
        new_verdict = new_g6_predicate(num, den, n_exec, n_skip)

        gate(
            "G2 WITNESS A — the PRE-REPAIR G6 predicate is BROKEN by a legitimate new gate",
            nested.returncode == 0 and not old_verdict and new_verdict,
            f"the extended INC-9 verifier is itself HEALTHY (exit={nested.returncode}, "
            f"tally={num}/{den}, executed={n_exec}, skipped={n_skip}) — yet the OLD rule "
            f"`denominator == 6` returns {old_verdict}, i.e. it REJECTS a perfectly good "
            f"tally purely because the gate surface grew. The repaired invariant returns "
            f"{new_verdict}. Shipped as `{OLD_G6_SOURCE}`.",
        )

    # ------------------------------------------------------------------ G3 --
    # WITNESS B · DIVERGENCE (load-bearing). Same tree, one ordinary gate added to
    # the INC-9 verifier. Run the SHIPPED (repaired) INC-15 verifier against it,
    # and the PRE-REPAIR one against it, and show they DISAGREE.
    with tempfile.TemporaryDirectory() as tmp:
        div = pathlib.Path(tmp) / "checkout-api"
        shutil.copytree(CHECKOUT_API, div)
        add_ordinary_gate(div / INC9)

        new_run = run(div, INC15)
        new_blob = new_run.stdout + new_run.stderr
        new_g6_passed = inc15_g6_ok(new_blob)

        # Now regress ONLY the G6 predicate in the INC-15 verifier back to the
        # hardcoded count, on the same tree, and re-run.
        inc15_path = div / INC15
        src = inc15_path.read_text()
        pre_repair = re.sub(
            r"        no_phantom_passes = \(\n(?:.*\n)*?        \)\n",
            "        no_phantom_passes = bool(lm) and int(lm.group(2)) == 6\n",
            src,
            count=1,
        )
        regressed = pre_repair != src
        inc15_path.write_text(pre_repair)

        old_run = run(div, INC15)
        old_blob = old_run.stdout + old_run.stderr
        old_g6_passed = inc15_g6_ok(old_blob)

        gate(
            "G3 WITNESS B — DIVERGENCE: with one gate added, OLD G6 FAILS · NEW G6 PASSES",
            regressed
            and old_g6_passed is False
            and old_run.returncode == 1
            and new_g6_passed is True
            and new_run.returncode == 0,
            f"SAME tree, one ordinary gate added to the INC-9 verifier. "
            f"PRE-REPAIR INC-15 -> G6 PASS={old_g6_passed}, exit={old_run.returncode} "
            f"(a healthy repo hard-fails CI) · REPAIRED INC-15 -> G6 PASS={new_g6_passed}, "
            f"exit={new_run.returncode}. Opposite outcomes on identical input: the repair "
            f"is NOT a no-op.",
        )

    # ------------------------------------------------------------------ G4 --
    # ANTI-WEAKENING. The repaired G6 must STILL catch the original INC-15 defect:
    # a skip laundered into the pass tally. Making a gate green is trivial; making
    # it CORRECT means it still reddens on the thing it was built to catch.
    with tempfile.TemporaryDirectory() as tmp:
        laundered = pathlib.Path(tmp) / "checkout-api"
        shutil.copytree(CHECKOUT_API, laundered)
        launder_skip_into_pass(laundered / INC9)

        nested = run(laundered, INC9)
        nblob = nested.stdout + nested.stderr
        num, den = tally_of(nblob)
        n_exec, n_skip = executed_count(nblob), skipped_count(nblob)

        caught = run(laundered, INC15)
        cblob = caught.stdout + caught.stderr

        gate(
            "G4 ANTI-WEAKENING — the repaired G6 STILL catches a skip laundered into a pass",
            not inc15_g6_ok(cblob)
            and caught.returncode == 1
            and not new_g6_predicate(num, den, n_exec, n_skip),
            f"INC-9 summary mutated to fold its {n_skip} SKIPPED gate(s) into the tally "
            f"(prints a confident {num}/{den} while only {n_exec} gates executed) -> "
            f"repaired INC-15 G6 FAILS, exit={caught.returncode}. This is the ORIGINAL "
            f"INC-15 defect, and the count-independent invariant still reddens on it. "
            f"The INC-17 fix is a correction, NOT a weakening.",
        )

    # ------------------------------------------------------------------ G5 --
    # The repaired G6 must also still notice a nested verifier that genuinely
    # FAILS a gate -- G6 requires exit 0 from the bare nested run.
    with tempfile.TemporaryDirectory() as tmp:
        broken = pathlib.Path(tmp) / "checkout-api"
        shutil.copytree(CHECKOUT_API, broken)
        break_a_gate(broken / INC9)
        res = run(broken, INC15)
        rblob = res.stdout + res.stderr
        gate(
            "G5 the repaired G6 still catches a genuinely FAILING nested verifier",
            not inc15_g6_ok(rblob) and res.returncode == 1,
            f"a deliberately failing gate injected into the INC-9 verifier -> "
            f"INC-15 G6 FAILS, exit={res.returncode} (the invariant did not make G6 blind "
            f"to real failures)",
        )

    # ------------------------------------------------------------------ G6 --
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
        "G6 NO PRODUCTION DRIFT — every deployed source present matches its baseline",
        not drift and checked > 0,
        f"{checked}/{len(BASELINES)} deployed sources byte-identical on the FULL sha256 "
        "(this incident touches only verifier code)"
        if not drift
        else "; ".join(drift),
    )

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{'=' * 74}\nINC-17 GATES: {passed}/{total} passed\n{'=' * 74}")
    if passed != total:
        for name, ok, _ in RESULTS:
            if not ok:
                print(f"  FAILED: {name}")
        return 1
    print("All gates green. G6's expired precondition is gone: the meta-gate now")
    print("asserts the INVARIANT (skips in neither numerator nor denominator) rather")
    print("than a hardcoded gate count -- so extending the gate surface no longer")
    print("reddens CI, and a laundered skip is still caught (G4).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
