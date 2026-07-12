#!/usr/bin/env python3
"""Fabric incident commander — INC-22 verifier.

THE FINDING
-----------
`artifacts/incident/verify_inc15_cross_fleet_discovery.py` gate G8 ("NO PRODUCTION
DRIFT") required every deployed source to be byte-identical to a HARDCODED sha256
baseline, and was FATAL on any difference.

That is a merge-time fact frozen into a permanent gate. It encodes "nobody has
fixed the billing defects yet" -- a statement about THE CALENDAR, not about
correctness.

The consequence points OUTWARD, at the owners. This commander has escalated the
INC-6 discount leak (a $45 leak on a $300 order) for seven consecutive runs. The
instant an owner complies and lands the correct repair, G8 goes hard RED -- and
INC-19's G1 reddens with it, because it re-runs this verifier. The owner does
precisely the thing we keep asking for, and CI goes red on a repo where nothing
is wrong.

Reproduced by execution BEFORE repairing (the correct owner repair -- tier chosen
from the eligible items' mean price):

    $300 order / one $10 eligible item : $255.00 (leaking) -> $300.00 (contractual)
    5 x $100 all eligible              : -> $425.00 (avg $100 -> 15% tier)

  verify_inc15_cross_fleet_discovery.py -> exit 1, [FAIL] G8   (8/9)
  verify_inc19_layout_and_count_invariance.py -> exit 1, [FAIL] G1 (6/7)

INC-21 (PR #24) diagnosed this and NEVER MERGED, so the defect is still live on
`main`. This verifier gates the repair that lands it.

THE REPAIR -- assert the invariant, not the calendar
----------------------------------------------------
What G8 legitimately protects is the verifier's OWN SIDE EFFECTS: it mutates files
during mutation testing and must restore every one. That is a property of THIS
PROCESS, not of the fleet's bug backlog. So G8 now compares a start-of-run
SNAPSHOT against the bytes on disk at the end:

  bytes moved DURING OUR OWN RUN (we failed to restore what we mutated) -> FATAL
  differs from the historical reference but STABLE across our run       -> an OWNER
                                                    EDIT: reported, never fatal

GATES
-----
  G0  STATIC -- the shipped INC-15 verifier carries the repair (no siblings needed,
      so this is what guards the repair INSIDE CI)
  G1  NO REGRESSION -- the repaired verifier is still fully green on an untouched fleet
  G2  WITNESS A (necessity) -- the PRE-repair predicate REJECTS a correct owner repair
  G3  WITNESS B (sufficiency) -- the repaired verifier PASSES on that same tree
  G4  DIVERGENCE (load-bearing) -- identical tree: PRE = RED, POST = GREEN
  G5  ANTI-WEAKENING -- a verifier that leaves production MUTATED across its own run
      is STILL rejected
  G6  NO DRIFT FROM THIS VERIFIER -- every source byte-identical before/after

G5 is the gate that matters most. Simply DELETING G8 would have turned the red
gate green AND satisfied G2/G3/G4 -- and it fails G5. That is the difference
between a CORRECTION and a COVER-UP. This is not a relaxation.

On G2's soundness: the necessity witness is anchored to the frozen historical
baseline constant (parsed from the verifier's own PROVENANCE_REFERENCE/BASELINES
dict), NOT to the bytes present when the verifier starts. Anchoring to runtime
bytes would be confounded on a tree where the owner fix had already landed -- the
"baseline" would silently become the repaired file, the old predicate would appear
to accept it, and the gate would prove nothing.

Exit: 0 = every executed gate passed. SKIPPED gates are in neither the numerator
nor the denominator (the INC-15/INC-19 lesson).
"""
from __future__ import annotations

import hashlib
import importlib.util
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

RESULTS: list[tuple[str, bool, str]] = []
SKIPPED: list[tuple[str, str]] = []


def gate(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n         {detail}" if detail else ""))


def skip(name: str, detail: str = "") -> None:
    SKIPPED.append((name, detail))
    print(f"[SKIP] {name}" + (f"\n         {detail}" if detail else ""))


def sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# The CORRECT owner repair for INC-6: choose the discount tier from the mean price
# of the ELIGIBLE items, instead of dividing the whole order subtotal by the
# eligible-item count. This is the remediation the commander has been requesting.
DEFECT_LINE = "    avg_cents = subtotal_cents / n"
OWNER_FIX = '    avg_cents = sum(i["price_cents"] for i in eligible_items) / n'


def _load(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _run(verifier: pathlib.Path, cwd: pathlib.Path) -> subprocess.CompletedProcess:
    rel = verifier.relative_to(verifier.parents[2])
    return subprocess.run(
        [sys.executable, str(rel)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=900,
    )


def _fleet_copy(dst: pathlib.Path) -> pathlib.Path:
    """A throwaway copy of the whole fleet. Production is never touched."""
    for repo in (CHECKOUT_API, TARGET, GATEWAY):
        shutil.copytree(repo, dst / repo.name)
    return dst


def _land_owner_fix(fleet: pathlib.Path) -> bool:
    """Simulate the owner landing the CORRECT INC-6 repair in a throwaway fleet."""
    p = fleet / "fabric-ic-incident-target" / "checkout.py"
    src = p.read_text()
    if DEFECT_LINE not in src:
        return False
    p.write_text(src.replace(DEFECT_LINE, OWNER_FIX))
    return True


def _old_g8_predicate(fleet: pathlib.Path) -> bool:
    """The PRE-REPAIR G8, reproduced faithfully: every present source must equal the
    FROZEN HISTORICAL BASELINE, and any difference is fatal.

    Anchored to the verifier's own historical constant -- NOT to the bytes on disk
    when we start. Anchoring to runtime bytes would be confounded on a tree where
    the owner fix had already landed: the "baseline" would silently become the
    repaired file, the old predicate would appear to accept it, and this witness
    would prove nothing.

    Returns True if the old gate would PASS.
    """
    mod = _load(INC15, "inc15_for_baselines")
    frozen = getattr(mod, "PROVENANCE_REFERENCE", None) or mod.BASELINES

    # Re-root the frozen constant's paths onto the throwaway fleet.
    checked = 0
    for path, expected in frozen.items():
        rel = path.relative_to(FLEET)
        candidate = fleet / rel
        if not candidate.is_file():
            continue
        checked += 1
        if sha(candidate) != expected:
            return False  # old G8: any difference from the frozen hash is FATAL
    return checked > 0


def main() -> int:
    print("Fabric incident commander — INC-22 verification gates\n")

    shipped = INC15.read_text()

    # ------------------------------------------------------------------ G0 --
    # STATIC. Needs no siblings, so it runs in checkout-api CI -- which is what
    # actually guards the repair. Strip the repair and this goes RED in CI.
    has_snapshot = "START_OF_RUN" in shipped and "_snapshot_sources" in shipped
    reports_owner_edit = "OWNER EDIT" in shipped or "owner_edits" in shipped
    no_fatal_baseline = "NO SELF-INFLICTED DRIFT" in shipped
    gate(
        "G0 STATIC — the shipped INC-15 verifier asserts the invariant, not the calendar",
        has_snapshot and reports_owner_edit and no_fatal_baseline,
        f"start-of-run snapshot={has_snapshot} owner-edit-as-provenance={reports_owner_edit} "
        f"G8 renamed to the invariant it actually protects={no_fatal_baseline}. "
        "No siblings required — this is the gate that guards the repair INSIDE CI.",
    )

    siblings = TARGET.is_file() or TARGET.is_dir()
    siblings = siblings and GATEWAY.is_dir()
    if not siblings:
        for n in (
            "G1 NO REGRESSION — repaired verifier still green on an untouched fleet",
            "G2 WITNESS A (necessity) — the PRE-repair predicate REJECTS a correct owner repair",
            "G3 WITNESS B (sufficiency) — the repaired verifier PASSES on that same tree",
            "G4 DIVERGENCE (load-bearing) — identical tree: PRE = RED · POST = GREEN",
            "G5 ANTI-WEAKENING — a verifier that leaves production MUTATED is STILL rejected",
            "G6 NO DRIFT FROM THIS VERIFIER — sources byte-identical before/after",
        ):
            skip(
                n,
                "structurally requires the sibling fleet repos, which a bare checkout "
                "does not clone. G0 above guards the repair here, statically, and is "
                "green on a bare checkout — so this step can never become the "
                "permanently-red gate (INC-11) that this incident family is about.",
            )
        return _summary()

    before = {p: sha(p) for p in (TARGET / "checkout.py", GATEWAY / "service" / "usage_aggregator.py", CHECKOUT_API / "service" / "checkout" / "session.js") if p.is_file()}

    # ------------------------------------------------------------------ G1 --
    with tempfile.TemporaryDirectory() as tmp:
        clean = _fleet_copy(pathlib.Path(tmp))
        proc = _run(INC15, clean / "checkout-api")
        blob = proc.stdout + proc.stderr
        m = re.search(r"^INC-15 GATES: (\d+)/(\d+) passed", blob, re.M)
        gate(
            "G1 NO REGRESSION — repaired verifier still green on an untouched fleet",
            proc.returncode == 0,
            f"exit={proc.returncode} tally={m.group(0) if m else 'none'} "
            "(the repair does not break the passing path)",
        )

    # ---------------------------------------------- G2 / G3 / G4 — witnesses --
    with tempfile.TemporaryDirectory() as tmp:
        fixed = _fleet_copy(pathlib.Path(tmp))
        landed = _land_owner_fix(fixed)

        if not landed:
            # The owner may have ALREADY landed the fix upstream. Then there is no
            # "pre-repair defect" to witness against -- report honestly, never fake it.
            for n in (
                "G2 WITNESS A (necessity) — the PRE-repair predicate REJECTS a correct owner repair",
                "G3 WITNESS B (sufficiency) — the repaired verifier PASSES on that same tree",
                "G4 DIVERGENCE (load-bearing) — identical tree: PRE = RED · POST = GREEN",
            ):
                skip(n, "the INC-6 defect line is absent from checkout.py — the owner has "
                        "evidently already landed a repair, so there is no pre-repair state "
                        "to witness. Reported, never faked.")
        else:
            # Prove the simulated repair is genuinely CORRECT -- otherwise the witness
            # would be testing a broken tree and would prove nothing.
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import checkout;"
                    "a=checkout.apply_discount(30000,[{'price_cents':1000}]);"
                    "b=checkout.apply_discount(50000,[{'price_cents':10000}]*5);"
                    "c=checkout.apply_discount(30000,[]);"
                    "print(a,b,c)",
                ],
                cwd=str(fixed / "fabric-ic-incident-target"),
                capture_output=True,
                text=True,
                timeout=120,
            )
            vals = probe.stdout.split()
            repair_is_correct = vals == ["30000", "42500", "30000"]

            old_passes = _old_g8_predicate(fixed)
            post = _run(INC15, fixed / "checkout-api")
            post_blob = post.stdout + post.stderr
            post_green = post.returncode == 0
            provenance_reported = "PROVENANCE" in post_blob or "OWNER EDIT" in post_blob

            gate(
                "G2 WITNESS A (necessity) — the PRE-repair predicate REJECTS a correct owner repair",
                repair_is_correct and not old_passes,
                f"owner repair verified CORRECT by execution (charges: $300 order/1 eligible "
                f"item -> ${int(vals[0])/100:.2f} was $255.00; 5x$100 -> ${int(vals[1])/100:.2f}; "
                f"zero-item guard -> ${int(vals[2])/100:.2f}) — and the OLD frozen-baseline G8 "
                f"predicate {'REJECTS' if not old_passes else 'ACCEPTS'} it. A gate that was "
                f"already green could not demonstrate that a fix was needed.",
            )

            gate(
                "G3 WITNESS B (sufficiency) — the repaired verifier PASSES on that same tree",
                post_green,
                f"repaired INC-15 on the correctly-repaired fleet: exit={post.returncode}, "
                f"owner edit surfaced as provenance={provenance_reported}",
            )

            # INC-19's G1 re-runs the INC-15 verifier, so it inherits the failure.
            # The repair must recover it too, or the fleet is still red.
            inc19 = _run(INC19, fixed / "checkout-api") if INC19.is_file() else None
            inc19_ok = inc19 is None or inc19.returncode == 0

            gate(
                "G4 DIVERGENCE (load-bearing) — identical tree: PRE = RED · POST = GREEN",
                (not old_passes) and post_green and inc19_ok,
                f"same correctly-repaired tree: PRE(frozen-baseline G8) = "
                f"{'ACCEPT' if old_passes else 'REJECT [RED]'} · POST(repaired G8) = "
                f"{'GREEN' if post_green else 'RED'} · INC-19 recovers="
                f"{'yes' if inc19_ok else 'NO'}"
                + (f" (exit={inc19.returncode})" if inc19 is not None else "")
                + ". Had both behaved alike the repair would be a no-op.",
            )

    # ------------------------------------------------------------------ G5 --
    # ANTI-WEAKENING. This is the gate that matters most.
    #
    # Deleting G8 outright would ALSO have turned the red gate green, and would have
    # satisfied G2/G3/G4 above. The difference between a CORRECTION and a COVER-UP is
    # whether the gate still bites when the thing it legitimately protects is violated.
    #
    # So: hand the repaired verifier a tree where a source is left MUTATED ACROSS ITS
    # OWN RUN (simulating a verifier that mutated production during mutation testing
    # and failed to restore it). It must STILL go red.
    with tempfile.TemporaryDirectory() as tmp:
        sab = _fleet_copy(pathlib.Path(tmp))
        inc15_copy = sab / "checkout-api" / "artifacts" / "incident" / "verify_inc15_cross_fleet_discovery.py"
        src = inc15_copy.read_text()

        # Inject a line that corrupts a deployed source AFTER the start-of-run snapshot
        # is taken and leaves it that way -- exactly the failure G8 exists to catch.
        anchor = "def main() -> int:"
        assert anchor in src, "could not find main() to inject the sabotage"
        sabotage = (
            "def main() -> int:\n"
            "    # INC-22 G5 sabotage: mutate a deployed source and never restore it.\n"
            "    _victim = TARGET / 'checkout.py'\n"
            "    if _victim.is_file():\n"
            "        _victim.write_text(_victim.read_text() + '\\n# unrestored mutation\\n')\n"
        )
        inc15_copy.write_text(src.replace(anchor, sabotage, 1))

        sab_proc = _run(inc15_copy, sab / "checkout-api")
        sab_blob = sab_proc.stdout + sab_proc.stderr
        caught = sab_proc.returncode != 0 and (
            "MUTATED ACROSS OUR OWN RUN" in sab_blob or "SELF-INFLICTED" in sab_blob
        )
        gate(
            "G5 ANTI-WEAKENING — a verifier that leaves production MUTATED is STILL rejected",
            caught,
            f"verifier sabotaged to corrupt checkout.py and not restore it: "
            f"exit={sab_proc.returncode} (must be non-zero), self-inflicted drift detected="
            f"{'MUTATED ACROSS OUR OWN RUN' in sab_blob or 'SELF-INFLICTED' in sab_blob}. "
            "Deleting G8 would have satisfied G2/G3/G4 and FAILED here — this is what "
            "makes the change a correction rather than a cover-up.",
        )

    # ------------------------------------------------------------------ G6 --
    after = {p: sha(p) for p in before}
    moved = [p.name for p in before if before[p] != after[p]]
    gate(
        "G6 NO DRIFT FROM THIS VERIFIER — deployed sources byte-identical before/after",
        not moved,
        f"{len(before)}/{len(before)} sources byte-identical on the FULL sha256 "
        f"(all mutation testing done in throwaway copies)"
        if not moved
        else f"DRIFTED: {moved}",
    )

    return _summary()


def _summary() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{'=' * 74}\nINC-22 GATES: {passed}/{total} passed", end="")
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
    print("All executed gates green. The drift gate now asserts the INVARIANT (we")
    print("restored what we mutated) instead of THE CALENDAR (nobody has fixed the")
    print("billing defects yet) — so landing the INC-6 repair no longer reddens CI.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
