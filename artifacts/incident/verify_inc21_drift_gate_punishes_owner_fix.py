#!/usr/bin/env python3
"""Fabric incident commander — INC-21 verifier.

THE FINDING
-----------
`verify_inc15_cross_fleet_discovery.py` gate G8 ("NO PRODUCTION DRIFT") required
every deployed source to be byte-identical to a HARDCODED sha256 BASELINE.

That is a MERGE-TIME FACT FROZEN INTO A PERMANENT GATE. It encodes "nobody has
fixed the billing defects yet" -- a statement about the CALENDAR, not about
correctness.

Reproduced by EXECUTION before repairing it. I simulated the exact remediation
this commander has escalated for six consecutive runs: an owner landing the
correct INC-6 repair, choosing the discount tier from the eligible items' mean
price rather than dividing the whole subtotal by the item count.

    avg_cents = sum(i["price_cents"] for i in eligible_items) / n

That repair is genuinely CORRECT -- the $300 order with one $10 eligible item
goes from a leaking $255.00 to the contractual $300.00. On that healthy,
correctly-repaired tree:

    verify_inc15_cross_fleet_discovery.py     exit 1 -- [FAIL] G8   (8/9)
    verify_inc19_layout_and_count_invariance  exit 1 -- [FAIL] G1   (6/7)

ONE ROOT CAUSE, TWO RED GATES: INC-19's G1 merely re-runs the INC-15 verifier,
so it inherits the failure. The owner does precisely the thing we keep asking
for, and CI goes hard RED on a repo where nothing is wrong.

INC-18 diagnosed exactly this disease -- gates asserting the billing defects were
STILL BROKEN -- and cured it in verify_inc9_ci_gate.py. It left the identical
frozen-baseline bug alive in the sibling INC-15 gate. This is the fleet's
signature failure on its seventh repetition:

    INC-11  G3 asserted "ci.yml is NEW" -- permanently false the instant it merged
    INC-12  required ci.yml byte-identical to main -- forbade editing its own CI
    INC-15  the cross-fleet gates were dead code; the skip was laundered into 6/6
    INC-17  the gate policing that laundering hardcoded the count it policed (== 6)
    INC-18  the gates asserted the billing defects were STILL BROKEN
    INC-19  witnesses depended on ambient directory names
    INC-21  the drift gate hard-fails the moment an owner REPAIRS a billing defect

A gate that PUNISHES THE REMEDIATION IT EXISTS TO REQUEST is worse than no gate
at all. A gate that can never fail and a gate that can never pass teach the team
the same lesson: ignore the red.

THE REPAIR -- assert the invariant, not the calendar
----------------------------------------------------
What G8 legitimately protects is the verifier's OWN SIDE EFFECTS: it mutates
files during mutation testing and must restore every one. That is a property of
THIS PROCESS, not of the fleet's bug backlog. So G8 now compares a START-OF-RUN
SNAPSHOT against the bytes on disk at the end:

    bytes moved DURING OUR OWN RUN (we failed to restore)  -> FATAL, still bites
    differs from the historical baseline but STABLE across
      our run = an OWNER EDIT                              -> PROVENANCE, never fatal

The frozen hashes are kept as provenance reference values only. No new merge-time
constant is introduced -- re-committing that pattern is the very bug being fixed.

GATES
-----
  G0  STATIC (no siblings needed) -- the shipped INC-15 verifier carries the
      repair. THIS is what guards it inside CI, on a bare checkout.
  G1  no regression -- the repaired verifier is still 9/9 on an untouched fleet.
  G2  WITNESS A (necessity) -- the PRE-repair predicate REJECTS a correct owner fix.
  G3  WITNESS B (sufficiency) -- the repaired verifier PASSES on that same tree.
  G4  DIVERGENCE (load-bearing) -- identical tree: PRE=REJECT, POST=GREEN, and
      INC-19 recovers with it. The repair is NOT a no-op.
  G5  ANTI-WEAKENING -- a verifier that leaves production MUTATED ACROSS ITS OWN
      RUN is STILL rejected (exit 1).
  G6  no drift caused by THIS verifier.

G5 IS THE GATE THAT MATTERS MOST. Simply DELETING G8 would have turned the red
gate green *and* satisfied G2/G3/G4 -- and it FAILS G5. That is the difference
between a CORRECTION and a COVER-UP. This is not a relaxation.

On G2's soundness: the necessity witness is anchored to the FROZEN HISTORICAL
BASELINE CONSTANT (parsed from the verifier's own BASELINES dict), not to the
bytes present when the verifier starts. Anchoring to runtime bytes would be
confounded on a tree where the owner fix had already landed -- the "baseline"
would silently become the repaired file, the old predicate would appear to accept
it, and the gate would prove nothing.

Exit: 0 = every executed gate passed.
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

INC15_REL = "artifacts/incident/verify_inc15_cross_fleet_discovery.py"
INC19_REL = "artifacts/incident/verify_inc19_layout_and_count_invariance.py"
INC15 = CHECKOUT_API / INC15_REL

TARGET_SRC = FLEET / "fabric-ic-incident-target" / "checkout.py"
GATEWAY_SRC = FLEET / "fabric-gateway-demo" / "service" / "usage_aggregator.py"

# The deployed defect, and the correct owner repair for it. The defect divides the
# WHOLE ORDER SUBTOTAL by the eligible-item count -- it never reads any item's
# price, despite its own docstring promising "the average price per eligible item".
DEFECT_LINE = "    avg_cents = subtotal_cents / n\n"
OWNER_FIX_LINE = "    avg_cents = sum(i['price_cents'] for i in eligible_items) / n\n"

RESULTS: list[tuple[str, bool, str]] = []
SKIPPED: list[tuple[str, str]] = []


def gate(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n         {detail}" if detail else ""))


def skip(name: str, detail: str = "") -> None:
    SKIPPED.append((name, detail))
    print(f"[SKIP] {name}" + (f"\n         {detail}" if detail else ""))


def run(cwd: pathlib.Path, rel: str, *args: str) -> tuple[int, str]:
    p = subprocess.run(
        [sys.executable, rel, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=900,
    )
    return p.returncode, p.stdout + p.stderr


def failed_gates(blob: str) -> list[str]:
    return re.findall(r"^\[FAIL\] (\S+)", blob, re.M)


def parse_baselines(src: str) -> dict[str, str]:
    """Pull the frozen historical baseline hashes out of the verifier's own source.

    G2 (necessity) must be anchored to these CONSTANTS -- not to the bytes on disk
    when the verifier starts. On a tree where the owner fix already landed, a
    runtime-anchored "baseline" would silently BE the repaired file, so the old
    predicate would appear to accept it and the witness would prove nothing.
    """
    return {
        name: h
        for name, h in re.findall(r'"([\w.]+)":\s*\n?\s*"([0-9a-f]{64})"', src)
    }


def old_g8_predicate(tree_fleet: pathlib.Path, baselines: dict[str, str]) -> tuple[bool, list[str]]:
    """The PRE-REPAIR G8, reproduced exactly: disk vs the FROZEN CONSTANTS, fatal."""
    files = {
        "session.js": tree_fleet / "checkout-api" / "service" / "checkout" / "session.js",
        "usage_aggregator.py": tree_fleet / "fabric-gateway-demo" / "service" / "usage_aggregator.py",
        "checkout.py": tree_fleet / "fabric-ic-incident-target" / "checkout.py",
    }
    drift = []
    checked = 0
    for name, path in files.items():
        if not path.is_file() or name not in baselines:
            continue
        checked += 1
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != baselines[name]:
            drift.append(f"{name}: {actual[:16]} != {baselines[name][:16]}")
    # The old gate PASSED only when nothing drifted from the frozen constant.
    return (not drift and checked > 0), drift


def build_repaired_fleet(dst: pathlib.Path) -> bool:
    """Copy the fleet and land the CORRECT owner repair for INC-6. True on success."""
    for name in ("checkout-api", "fabric-gateway-demo", "fabric-ic-incident-target"):
        src = FLEET / name
        if not src.is_dir():
            return False
        shutil.copytree(src, dst / name, ignore=shutil.ignore_patterns(".git", "node_modules"))
    tgt = dst / "fabric-ic-incident-target" / "checkout.py"
    s = tgt.read_text()
    if DEFECT_LINE not in s:
        return False
    tgt.write_text(s.replace(DEFECT_LINE, OWNER_FIX_LINE))
    return True


def main() -> int:
    print("Fabric incident commander — INC-21 verification gates\n")

    inc15_src = INC15.read_text()
    baselines = parse_baselines(inc15_src)

    # ------------------------------------------------------------------ G0 --
    # STATIC, and it needs NO siblings -- so it runs in `checkout-api` CI, on a
    # bare checkout. This is the gate that actually guards the repair inside CI.
    # Strip the repair and G0 goes RED there, with no fleet present.
    has_snapshot = "def snapshot_sources(" in inc15_src
    compares_to_snapshot = "START_SNAPSHOT" in inc15_src and "end_snapshot" in inc15_src
    reports_owner_edit = "owner_edits" in inc15_src
    still_fatal_on_self = "self_inflicted" in inc15_src
    # And the frozen-constant comparison must no longer be the FATAL predicate.
    no_frozen_fatal = not re.search(r"drift\.append.*!=.*expected", inc15_src)
    gate(
        "G0 STATIC — the shipped INC-15 verifier carries the repair (no siblings needed)",
        has_snapshot
        and compares_to_snapshot
        and reports_owner_edit
        and still_fatal_on_self
        and no_frozen_fatal,
        f"snapshot_sources={has_snapshot} start/end compare={compares_to_snapshot} "
        f"owner-edit provenance={reports_owner_edit} self-inflicted still fatal="
        f"{still_fatal_on_self} frozen-constant no longer fatal={no_frozen_fatal}. "
        "This gate runs on a BARE CHECKOUT: revert the repair and CI goes RED.",
    )

    siblings = TARGET_SRC.is_file() and GATEWAY_SRC.is_file()
    if not siblings:
        for n in (
            "G1 no regression — the repaired verifier is still green on an untouched fleet",
            "G2 WITNESS A (necessity) — the PRE-repair predicate REJECTS a correct owner fix",
            "G3 WITNESS B (sufficiency) — the repaired verifier PASSES on that same tree",
            "G4 DIVERGENCE (load-bearing) — identical tree: PRE=REJECT · POST=GREEN",
            "G5 ANTI-WEAKENING — a verifier that leaves production MUTATED is STILL rejected",
            "G6 no drift caused by THIS verifier",
        ):
            skip(n, "structurally requires the sibling fleet repos, which a bare "
                    "checkout does not clone. G0 above guards the repair here.")
        return _summary()

    pre_hashes = {
        p: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (TARGET_SRC, GATEWAY_SRC, CHECKOUT_API / "service" / "checkout" / "session.js")
    }

    # ------------------------------------------------------------------ G1 --
    rc, blob = run(CHECKOUT_API, INC15_REL)
    m = re.search(r"^INC-15 GATES: (\d+)/(\d+) passed", blob, re.M)
    gate(
        "G1 no regression — the repaired verifier is still green on an untouched fleet",
        rc == 0 and not failed_gates(blob),
        f"exit={rc} tally={m.group(0) if m else 'none'} failed={failed_gates(blob) or 'none'}",
    )

    # ---------------------------------------------- G2 / G3 / G4 — witnesses --
    with tempfile.TemporaryDirectory() as tmp:
        rep = pathlib.Path(tmp) / "fleet"
        rep.mkdir(parents=True)
        if not build_repaired_fleet(rep):
            for n in (
                "G2 WITNESS A (necessity) — the PRE-repair predicate REJECTS a correct owner fix",
                "G3 WITNESS B (sufficiency) — the repaired verifier PASSES on that same tree",
                "G4 DIVERGENCE (load-bearing) — identical tree: PRE=REJECT · POST=GREEN",
            ):
                gate(n, False, "could not construct the owner-repaired fleet (defect line "
                               "not found in the deployed checkout.py -- has it changed?)")
            return _summary()

        # Sanity: the simulated repair must actually be CORRECT, otherwise the
        # witness is about a broken tree and proves nothing.
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys;sys.path.insert(0,'.');import checkout;"
                "print(checkout.apply_discount(30000,[{'price_cents':1000}]),"
                "checkout.apply_discount(50000,[{'price_cents':10000}]*5))",
            ],
            cwd=str(rep / "fabric-ic-incident-target"),
            capture_output=True,
            text=True,
            timeout=120,
        )
        vals = probe.stdout.split()
        repair_is_correct = vals == ["30000", "42500"]

        # G2 -- WITNESS A (necessity), anchored to the FROZEN CONSTANTS.
        old_pass, old_drift = old_g8_predicate(rep, baselines)
        gate(
            "G2 WITNESS A (necessity) — the PRE-repair predicate REJECTS a correct owner fix",
            repair_is_correct and not old_pass and bool(old_drift),
            f"owner repair is correct (=$300.00 not $255.00, and $425.00 on the 5x$100 "
            f"order): {repair_is_correct}. OLD G8 (disk vs FROZEN BASELINE CONSTANT) -> "
            f"{'PASS' if old_pass else 'REJECT'} {old_drift}. The old gate hard-failed "
            "the owner for doing exactly what we asked.",
        )

        # G3 -- WITNESS B (sufficiency): the SHIPPED verifier on that same tree.
        rc_new, blob_new = run(rep / "checkout-api", INC15_REL)
        f_new = failed_gates(blob_new)
        rc19, blob19 = run(rep / "checkout-api", INC19_REL)
        gate(
            "G3 WITNESS B (sufficiency) — the repaired verifier PASSES on that same tree",
            rc_new == 0 and not f_new,
            f"repaired INC-15 on the owner-fixed fleet: exit={rc_new} failed={f_new or 'none'}; "
            f"the owner's edit is reported as PROVENANCE="
            f"{'OWNER EDIT' in blob_new or 'owner' in blob_new.lower()}",
        )

        # G4 -- DIVERGENCE. Same tree, opposite verdicts. And INC-19 recovers too.
        gate(
            "G4 DIVERGENCE (load-bearing) — identical tree: PRE=REJECT · POST=GREEN",
            (not old_pass) and rc_new == 0 and rc19 == 0,
            f"identical owner-repaired tree: PRE-repair predicate=REJECT [RED] · "
            f"POST-repair INC-15=exit {rc_new} [GREEN] · INC-19=exit {rc19} [GREEN] "
            f"(INC-19's G1 re-runs INC-15, so it inherited the failure: one root "
            f"cause, two red gates). The repair is NOT a no-op.",
        )

    # ------------------------------------------------------------------ G5 --
    # ANTI-WEAKENING. THE GATE THAT MATTERS MOST.
    #
    # Deleting G8 outright would ALSO have turned the red gate green and satisfied
    # G2/G3/G4. The only thing separating a CORRECTION from a COVER-UP is that the
    # gate must STILL hard-fail when the verifier leaves production mutated across
    # its own run -- the real property G8 exists to protect.
    with tempfile.TemporaryDirectory() as tmp:
        sab = pathlib.Path(tmp) / "fleet"
        sab.mkdir(parents=True)
        ok = True
        for name in ("checkout-api", "fabric-gateway-demo", "fabric-ic-incident-target"):
            src = FLEET / name
            if not src.is_dir():
                ok = False
                break
            shutil.copytree(src, sab / name, ignore=shutil.ignore_patterns(".git", "node_modules"))

        if not ok:
            gate(
                "G5 ANTI-WEAKENING — a verifier that leaves production MUTATED is STILL rejected",
                False,
                "could not stage the sabotage fleet",
            )
        else:
            v = sab / "checkout-api" / INC15_REL
            s = v.read_text()
            anchor = "    # ------------------------------------------------------------------ G8 --"
            inject = (
                '    _sab = CHECKOUT_API / "service" / "checkout" / "session.js"\n'
                '    _sab.write_bytes(_sab.read_bytes() + b"\\n// UNRESTORED MUTATION\\n")\n\n'
            )
            if anchor not in s:
                gate(
                    "G5 ANTI-WEAKENING — a verifier that leaves production MUTATED is STILL rejected",
                    False,
                    "could not locate the G8 anchor to inject the unrestored mutation",
                )
            else:
                v.write_text(s.replace(anchor, inject + anchor, 1))
                rc_sab, blob_sab = run(sab / "checkout-api", INC15_REL)
                f_sab = failed_gates(blob_sab)
                caught = rc_sab == 1 and any(g.startswith("G8") for g in f_sab)
                gate(
                    "G5 ANTI-WEAKENING — a verifier that leaves production MUTATED is STILL rejected",
                    caught and "MUTATED ACROSS OUR OWN RUN" in blob_sab,
                    f"verifier mutates session.js and fails to restore it -> exit={rc_sab} "
                    f"failed={f_sab or 'none'}; 'MUTATED ACROSS OUR OWN RUN' reported="
                    f"{'MUTATED ACROSS OUR OWN RUN' in blob_sab}. Deleting G8 would have "
                    "passed G2/G3/G4 and FAILED HERE — this is a correction, not a cover-up.",
                )

    # ------------------------------------------------------------------ G6 --
    drift = []
    for p, before in pre_hashes.items():
        if not p.is_file():
            drift.append(f"{p.name}: DISAPPEARED")
            continue
        after = hashlib.sha256(p.read_bytes()).hexdigest()
        if after != before:
            drift.append(f"{p.name}: {before[:16]} -> {after[:16]}")
    gate(
        "G6 no drift caused by THIS verifier",
        not drift,
        f"{len(pre_hashes)}/{len(pre_hashes)} sources byte-identical before/after this run "
        "(all mutation testing happened in throwaway copies)"
        if not drift
        else "; ".join(drift),
    )

    return _summary()


def _summary() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{'=' * 74}\nINC-21 GATES: {passed}/{total} passed", end="")
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
    print("All executed gates green. The drift gate now reports an owner's repair as")
    print("provenance instead of hard-failing CI for it — while STILL going red on a")
    print("verifier that leaves production mutated across its own run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
