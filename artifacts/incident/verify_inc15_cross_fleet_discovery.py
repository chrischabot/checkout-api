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
  G8  no production drift CAUSED BY THIS RUN: every deployed source is handed back
      byte-identical to the start-of-run snapshot. An owner edit (e.g. landing the
      INC-6 billing repair) is REPORTED as provenance and is never a failure --
      a gate that punishes the remediation it exists to request is worse than none.

G5 is the load-bearing gate. It does not merely assert the new code works -- it
proves the OLD code was blind on the SAME filesystem. Had both implementations
behaved alike, the repair would be a no-op and G5 would say so.

Exit: 0 = every gate passed.
"""
from __future__ import annotations

import contextlib
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

# Deployed revisions as recorded when this verifier was written. FULL sha256.
#
# PROVENANCE REFERENCE VALUES ONLY — read this before making G8 fatal on them.
#
# These hashes are a MERGE-TIME FACT. Asserting them as a permanent gate encodes
# the statement "nobody has repaired the billing defects yet" — a claim about the
# CALENDAR, not about correctness. The instant an owner lands the INC-6 repair this
# commander has escalated for many consecutive runs, `checkout.py` changes bytes
# and a fatal comparison here goes hard RED on a repo where NOTHING IS WRONG. It
# also padlocks this repo's own `session.js`: any legitimate future edit reddens
# CI (the INC-12 bug). A gate that punishes the remediation it exists to request is
# worse than no gate at all — it teaches the team to ignore the red.
#
# So drift from THESE values is REPORTED as provenance and is NEVER fatal.
# What G8 legitimately protects is this verifier's OWN side effects: it mutates
# files during mutation testing and MUST restore every one. That is a property of
# THIS PROCESS, not of the fleet's bug backlog — and it is enforced against the
# start-of-run snapshot below, which remains FATAL.
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

# INC-30: strict cross-fleet mode is honoured through an environment variable, and
# a bare `subprocess.run()` (no `env=`) leaks it into children. Several children
# here are NEGATIVE CONTROLS: they run against a synthetic bare checkout and the
# control REQUIRES the child to SKIP its cross-fleet gates and exit 0. A control
# that inherits the very flag it is controlling for is not a control -- the child
# is forced into strict mode and hard-fails exactly where the control demands a
# skip. ALWAYS scrub; re-set only on explicit request.
STRICT_ENV_VAR = "FABRIC_REQUIRE_CROSS_FLEET"


def child_env(*, strict: bool = False) -> dict:
    env = dict(os.environ)
    env.pop(STRICT_ENV_VAR, None)   # ALWAYS scrubbed
    if strict:
        env[STRICT_ENV_VAR] = "1"   # ...re-set ONLY on request
    return env


def _snapshot_now() -> dict[pathlib.Path, str]:
    """Hash the deployed sources AS THEY ARE RIGHT NOW (verifier start).

    This is the anchor G8 enforces, and it is the whole INC-23 repair: whatever
    state the billing path is in when we start — pristine, or already repaired by
    an owner — this verifier must hand it back UNCHANGED. That invariant never
    expires, where a hardcoded merge-time hash does.
    """
    snap: dict[pathlib.Path, str] = {}
    for path in BASELINES:
        if path.is_file():
            snap[path] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snap


RUN_SNAPSHOT = _snapshot_now()


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
    # INC-30: strict mode is passed EXPLICITLY via argv (`*args`) when the caller
    # wants it. It must never arrive by environment inheritance -- otherwise a
    # negative control that requires a SKIP inherits the flag and hard-fails.
    return subprocess.run(
        [sys.executable, "artifacts/incident/verify_inc9_ci_gate.py", *args],
        cwd=str(cwd),
        env=child_env(),
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
    # INC-19 REPAIR (layout independence).
    #
    # G4/G5 used to witness the OLD-vs-NEW divergence against the AMBIENT fleet
    # roots. But the OLD lookup searches for directories literally named
    # `incident-target/` and `gateway/`. So when the siblings happen to be cloned
    # under exactly those LEGACY names, the OLD lookup RESOLVES them, "the old
    # discovery is blind" is genuinely false, and G4/G5 both go RED -- on a tree
    # where nothing is wrong. Measured: legacy layout -> 7/9, exit 1.
    #
    # Worse, it was self-contradictory: G3 above certifies the legacy layout as
    # SUPPORTED ("the fix adds, never replaces"), and then G4/G5 hard-failed on it.
    # The verifier's exit code became a function of how somebody named their clone
    # directories, not of the property under test.
    #
    # A tree the OLD lookup can SEE cannot host a blindness witness. So witness on
    # a tree that CAN host it: if the ambient layout uses the real repo names, use
    # the ambient roots (today's passing path, preserved bit-for-bit). If the
    # ambient layout is legacy, build a synthetic canonical real-name fleet and
    # witness there. If there is no fleet at all, SKIP exactly as before.
    o_target, o_gateway = old_discovery(roots)
    _witness_stack = contextlib.ExitStack()
    with _witness_stack:
        witness_roots = roots
        witness_note = "ambient fleet roots (real repo names)"
        # "Can the OLD lookup see this tree?" If yes, it cannot demonstrate
        # blindness here, so relocate the witness onto a canonical real-name fleet.
        if siblings_present and not (o_target is None and o_gateway is None):
            synth = pathlib.Path(_witness_stack.enter_context(tempfile.TemporaryDirectory()))
            (synth / "fabric-ic-incident-target").mkdir(parents=True)
            (synth / "fabric-ic-incident-target" / "checkout.py").write_text("# synthetic\n")
            (synth / "fabric-gateway-demo" / "service").mkdir(parents=True)
            (synth / "fabric-gateway-demo" / "service" / "usage_aggregator.py").write_text(
                "# synthetic\n"
            )
            witness_roots = [synth]
            witness_note = (
                "synthetic canonical real-name fleet — the AMBIENT layout uses the "
                "LEGACY directory names, which the OLD lookup can SEE, so the ambient "
                "tree cannot host a blindness witness (that is the INC-19 defect)"
            )

        w_o_target, w_o_gateway = old_discovery(witness_roots)
        w_n_target, w_n_gateway = new_discovery(witness_roots)

        if siblings_present:
            gate(
                "G4 WITNESS A — the PRE-REPAIR discovery is BLIND on this very filesystem",
                w_o_target is None and w_o_gateway is None,
                f"old logic finds NEITHER sibling despite both being present: "
                f"incident-target={w_o_target} gateway={w_o_gateway}. "
                f"Witnessed against {witness_note}. "
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
        #
        # `ran` is deliberately NOT recomputed here: it still comes from invoking the
        # repaired verifier against the AMBIENT roots, which is the only thing that
        # proves the actually-deployed discovery works on this real filesystem.
        # Moving it onto the synthetic fleet would make G5 a tautology about a temp dir.
        if siblings_present:
            old_would_skip = w_o_target is None or w_o_gateway is None
            new_would_run = w_n_target is not None and w_n_gateway is not None
            gate(
                "G5 WITNESS B — DIVERGENCE: OLD skips all 3 gates · NEW executes all 3",
                old_would_skip and new_would_run and ran == {"G6a", "G6b", "G6c"},
                f"OLD -> skips G6a/G6b/G6c (and printed a confident '6/6 passed') · "
                f"NEW -> executes {sorted(ran)}. Witnessed against {witness_note}; "
                f"`ran` measured against the AMBIENT fleet. Same filesystem, opposite "
                f"outcomes: the repair is NOT a no-op.",
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
        lm = re.search(r"^GATES: (\d+)/(\d+) passed", lblob, re.M)
        # INC-19 REPAIR (count independence).
        #
        # This predicate used to read:
        #
        #     no_phantom_passes = bool(lm) and int(lm.group(2)) == 6
        #
        # That is a MERGE-TIME FACT FROZEN INTO A PERMANENT GATE: it encodes "the
        # INC-9 verifier has exactly six gates", true only on the day it was
        # written. Add one perfectly ordinary new gate to that verifier -- exactly
        # the behaviour this fleet wants to ENCOURAGE -- and this gate hard-reddens
        # CI on a repo where nothing is wrong.
        #
        # Assert the INVARIANT, not the CONSTANT. What INC-15 actually exists to
        # enforce is: the denominator counts exactly the gates that EXECUTED, and a
        # SKIPPED gate is in neither the numerator nor the denominator. That holds
        # whether the verifier has 6 gates or 40 -- while a skip laundered back into
        # the tally is still caught.
        n_executed = len(re.findall(r"^\[(?:PASS|FAIL)\]", lblob, re.M))
        n_skipped = len(re.findall(r"^\[SKIP\]", lblob, re.M))
        if lm:
            numerator, denominator = int(lm.group(1)), int(lm.group(2))
            no_phantom_passes = (
                denominator == n_executed  # skips are NOT in the denominator
                and numerator <= n_executed  # and cannot inflate the numerator
                and denominator < n_executed + n_skipped  # a laundered skip pushes it higher
            )
        else:
            numerator = denominator = -1
            no_phantom_passes = False
        gate(
            "G6 NEGATIVE CONTROL — absent siblings SKIP (exit 0), never a silent pass",
            lone.returncode == 0
            and not lran
            and skipped_reported
            and no_phantom_passes,
            f"bare checkout: exit={lone.returncode} cross-fleet executed={sorted(lran) or 'none'} "
            f"SKIPPED reported={skipped_reported} tally={lm.group(0) if lm else 'none'} "
            f"executed={n_executed} skipped={n_skipped} "
            f"(denominator counts exactly the gates that RAN — count-independent, so "
            f"adding a gate cannot redden this; a laundered skip still would)",
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
    # NO PRODUCTION DRIFT *CAUSED BY THIS RUN*  (the INC-23 repair).
    #
    # Assert the INVARIANT, not the CALENDAR. Two distinct conditions, and only one
    # of them is a defect:
    #
    #   bytes moved DURING OUR OWN RUN   -> we mutated production during mutation
    #                                       testing and failed to restore it.
    #                                       FATAL. This gate still bites.
    #   differs from the recorded ref     -> an OWNER EDIT (e.g. landing the INC-6
    #     but STABLE across our run         billing repair we have asked for over
    #                                       many runs). PROVENANCE. Never fatal.
    #
    # Note that simply DELETING this gate would also have turned it green -- and
    # would have let a verifier that corrupts production sail through. That is the
    # difference between a CORRECTION and a COVER-UP. Comparing against the
    # start-of-run snapshot is what keeps the teeth while removing the padlock.
    #
    # Hash only the files that EXIST here: on a bare checkout the sibling sources
    # are absent, and an absent file is not drift -- it is a different environment.
    self_inflicted: list[str] = []
    owner_edits: list[str] = []
    checked = 0
    for path, expected in BASELINES.items():
        snapshot = RUN_SNAPSHOT.get(path)
        if not path.is_file():
            # Present when we started, gone now => WE deleted it. That is the worst
            # kind of self-inflicted drift, and it must never be skipped past.
            if snapshot is not None:
                self_inflicted.append(
                    f"{path.name}: PRESENT at start-of-run ({snapshot[:12]}) but MISSING now"
                )
            # Absent at start AND absent now: not drift, just a different
            # environment (a bare checkout does not clone the siblings).
            continue
        checked += 1
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if snapshot is not None and actual != snapshot:
            # We changed these bytes and did not put them back. Our fault. FATAL.
            self_inflicted.append(
                f"{path.name}: {actual[:12]} != start-of-run {snapshot[:12]}"
            )
        elif snapshot is None:
            # Appeared during our run. We did not have it at start; treat the
            # sudden appearance of a production source as self-inflicted too.
            self_inflicted.append(
                f"{path.name}: ABSENT at start-of-run but PRESENT now ({actual[:12]})"
            )
        elif actual != expected:
            owner_edits.append(f"{path.name} ({actual[:12]} vs recorded {expected[:12]})")

    if self_inflicted:
        g8_detail = (
            "THIS VERIFIER LEFT PRODUCTION MUTATED: "
            + "; ".join(self_inflicted)
            + ". Mutation testing must restore every file it touches."
        )
    elif owner_edits:
        g8_detail = (
            f"{checked}/{len(BASELINES)} sources present and STABLE across this run "
            f"(we mutated nothing). PROVENANCE — differs from the revision recorded when "
            f"this verifier was written: {', '.join(owner_edits)}. That is an OWNER EDIT, "
            f"very likely the billing repair this commander has been requesting. It is NOT "
            f"a failure and must never be treated as one."
        )
    else:
        g8_detail = (
            f"{checked}/{len(BASELINES)} sources present, byte-identical on the FULL sha256 "
            f"to both the start-of-run snapshot and the recorded revision"
            + ("" if siblings_present else " (siblings absent in this checkout: not drift)")
        )

    gate(
        "G8 NO PRODUCTION DRIFT CAUSED BY THIS RUN — an owner's repair is provenance, not failure",
        not self_inflicted and checked > 0,
        g8_detail,
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
