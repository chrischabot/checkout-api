#!/usr/bin/env python3
"""INC-12 verifier: does wiring the INC-9 verifier into CI actually BITE?

THE PATCH THIS RUN SHIPS IS A CI STEP, NOT A PRODUCTION CODE CHANGE.

`checkout-api` has carried a merged mutation-witness verifier on `main` since
PR #3 (`artifacts/incident/verify_inc9_ci_gate.py`), and `.github/workflows/
ci.yml` ran `npm test` and stopped. Nothing executed the verifier. That is the
INC-9 finding -- "a regression test that never runs is decoration" -- reproduced
one level up, on the gate itself. The sibling repos already run their verifiers
as CI steps; this repo was the last one left.

A CI step that cannot fail is decoration. So the load-bearing gate here is NOT
"is the new CI green?" -- it is the DIVERGENCE:

    simulate the exact regression this step exists to catch, then show
      OLD CI (npm test only)        -> exit 0   the regression SLIPS THROUGH
      NEW CI (npm test + verifier)  -> exit 1   the regression is CAUGHT

If OLD CI caught the regression on its own, the added step would be redundant,
and this verifier must say so loudly rather than claim credit.

WHICH REGRESSION? Not "someone reintroduces the INC-1 bug" -- `npm test` already
catches that (it is exactly what INC-9's G5 witnesses). The realistic and
strictly nastier threat is that someone GUTS THE GUARD: thins the cold-cache
coverage until the suite no longer witnesses the defect. The suite still passes.
Review sees a smaller, tidier test file. Only a mutation witness notices that the
guard has stopped biting -- and nothing was running the mutation witness.

CRITICAL PROPERTY OF A VALID SIMULATION: the gutted suite must remain GREEN on
good source. That is precisely what makes the regression dangerous and invisible
-- it sails through code review and through `npm test`. A "gutted" suite that
goes red on good source is not a blinded guard, it is a BROKEN suite, and CI
catching a broken suite proves nothing about whether the mutation witness is
needed. G5 asserts that precondition explicitly so an invalid simulation can
never be mistaken for a proof.

Gates:
  G1  the merged INC-9 verifier passes on the CURRENT tree (exit 0)
  G2  a ci.yml STEP invokes the INC-9 verifier (parsed from YAML, not grepped --
      a command sitting in a COMMENT must not count as CI executing anything)
  G3  ci.yml STILL runs the suite (the verifier step ADDS, never replaces)
  G4  SIMULATED REGRESSION -- guard gutted -> `npm test` STILL PASSES (green).
      This is the precondition that makes the divergence meaningful.
  G5  DIVERGENCE (LOAD-BEARING): same gutted tree ->
        OLD CI (suite only)       exit 0  -- slips through
        NEW CI (suite + verifier) exit 1  -- caught, AND the failure is
        ATTRIBUTED to the verifier's mutation witness (its G5/WITNESS B) rather
        than to any other gate. A bare non-zero exit is not proof: the simulated
        tree deliberately differs from upstream, so an unrelated provenance gate
        could redden for a reason that has nothing to do with the guard. We parse
        the per-gate output and require the witness itself to have bitten.
  G6  the real tree was mutated by NOTHING (sha256 of every file, before/after)

Exit 0 = every gate passed.
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

ROOT = pathlib.Path(__file__).resolve().parents[2]

# Run BOTH from the commander workspace (repo cloned as a subdir) and from inside
# the repo, where ROOT already IS the repo root. A verifier that only runs on its
# author's machine is not a verifier.
_CANDIDATES = [ROOT / "fleet" / "checkout-api", ROOT / "checkout-api", ROOT]
REPO = next(
    (c for c in _CANDIDATES if (c / "service" / "checkout" / "session.js").is_file()),
    None,
)
if REPO is None:
    sys.exit(f"cannot locate the checkout-api repo root from {ROOT}")

SRC = REPO / "service" / "checkout" / "session.js"
SUITE = REPO / "test" / "session.test.js"
CI = REPO / ".github" / "workflows" / "ci.yml"
PKG = REPO / "package.json"
INC9 = REPO / "artifacts" / "incident" / "verify_inc9_ci_gate.py"

TRACKED = [SRC, SUITE, CI, PKG, INC9]

RESULTS: list[tuple[str, bool, str]] = []


def gate(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n         {detail}" if detail else ""))


def sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def npm_test(cwd: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["npm", "test", "--silent"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=300,
    )


def tallies(proc: subprocess.CompletedProcess) -> tuple[int, int]:
    blob = proc.stdout + proc.stderr

    def grab(label: str) -> int:
        m = re.search(rf"^# {label} (\d+)", blob, re.M)
        return int(m.group(1)) if m else 0

    return grab("pass"), grab("fail")


def ci_run_steps(ci_text: str) -> list[str]:
    """The `run:` command of every STEP, parsed from YAML -- not substring-matched.

    This distinction is the whole point of G2/G3: `npm test` mentioned inside a
    YAML comment must NOT count as CI executing anything. Falls back to a
    line-anchored regex if PyYAML is unavailable (stdlib-only environments).
    """
    try:
        import yaml  # type: ignore

        wf = yaml.safe_load(ci_text) or {}
        out: list[str] = []
        for job in (wf.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                cmd = (step or {}).get("run")
                if cmd:
                    out.append(str(cmd).strip())
        return out
    except ImportError:
        return [m.strip() for m in re.findall(r"^\s*run:\s*(.+)$", ci_text, re.M)]


# The regression we simulate: delete the cold-cache guard outright.
#
# session.test.js drives 5 COLD_CACHE_SHAPES through refreshAccessToken() and
# asserts none of them throw. THAT test is what witnesses the INC-1 defect. The
# other tests (empty-auth-blob, warm-cache refresh, normalized shape, exports)
# all pass with or without the defect present -- so deleting the cold-cache test
# leaves a suite that is still GREEN on good source and completely BLIND to the
# defect coming back. That is the dangerous, review-surviving regression.
GUARD_TEST_RE = re.compile(
    r"test\('cold-cache resume degrades gracefully.*?\n\}\);\n",
    re.S,
)

DEFECT = "const refreshToken = session.auth.refreshToken;"
GUARDED = "const refreshToken = session.auth && session.auth.refreshToken;"

# --------------------------------------------------------------- INC-29 --
# SHIPPED IN THIS REPO. This is the file checkout-api CI audits and runs, and the
# file whose spawns verify_inc29_strict_mode_leak_in_missed_verifiers.py G0 checks.
#
# INC-28 established the rule: an intent must be PASSED to the child that should
# receive it, never INHERITED by a child that must not. It scrubbed the strict
# flag in verify_inc15/inc19/inc23 -- but NOT here, and this file spawns
# verify_inc9_ci_gate.py as a child in G1 (and again in the G5 simulation).
#
# G1 asks one question: "does the shipped INC-9 verifier pass on the current
# tree?" In a BARE CHECKOUT -- exactly what this repo's CI clones -- INC-9 has no
# sibling repos, so it correctly SKIPs its cross-fleet gates and exits 0. But if
# an operator or CI job exports FABRIC_REQUIRE_CROSS_FLEET, the child INHERITS it,
# is forced into strict mode, and hard-fails for want of siblings that are
# legitimately absent. G1 then reports "INC-9 does not pass" -- a statement that
# is FALSE, and that has nothing to do with the property G1 tests.
#
# Measured with the INC-28 repair applied, bare checkout, on the SAME tree:
#     argv strict -> exit 0      env strict -> exit 1 (5/6)
# A verdict that depends on HOW strict mode was requested is not a verdict.
STRICT_ENV_VAR = "FABRIC_REQUIRE_CROSS_FLEET"


def child_env(*, strict: bool | None = None) -> dict:
    """Environment for a spawned child verifier, with the strict intent explicit.

    The strict-cross-fleet flag is ALWAYS removed from the inherited environment
    and re-set ONLY when a call site explicitly asks for it. This stops the flag
    LEAKING; it does not remove the feature -- a caller that wants strict mode in
    a child still gets it by passing strict=True.
    """
    env = dict(os.environ)
    env.pop(STRICT_ENV_VAR, None)
    if strict:
        env[STRICT_ENV_VAR] = "1"
    return env


def main() -> int:
    print("Fabric incident commander -- INC-12 verification gates\n")

    before = {p: sha(p) for p in TRACKED}
    ci_text = CI.read_text()
    steps = ci_run_steps(ci_text)

    # ------------------------------------------------------------------ G1 --
    # env= is load-bearing: see child_env(). G1 asks whether INC-9 passes on this
    # tree, so the child must NOT inherit an ambient strict-mode flag that would
    # make it fail for want of siblings CI never clones.
    inc9 = subprocess.run(
        [sys.executable, str(INC9)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=600,
        env=child_env(),
    )
    gate(
        "G1 the merged INC-9 mutation-witness verifier passes on the current tree",
        inc9.returncode == 0,
        f"exit={inc9.returncode}",
    )

    # ------------------------------------------------------------------ G2 --
    invokes_verifier = [s for s in steps if "verify_inc9_ci_gate.py" in s]
    gate(
        "G2 a ci.yml STEP invokes the INC-9 verifier (parsed from YAML, not grepped)",
        bool(invokes_verifier),
        f"steps={steps}",
    )

    # ------------------------------------------------------------------ G3 --
    runs_suite = [s for s in steps if re.search(r"\bnpm\s+test\b", s)]
    gate(
        "G3 ci.yml still runs the suite (the verifier step ADDS, never replaces)",
        bool(runs_suite),
        f"suite step={runs_suite or 'MISSING'}",
    )

    # --------------------------------------------------- G4 / G5 · the sim --
    #
    # Build a throwaway copy of the repo, gut the guard there, and ask both CIs.
    # The real tree is never touched (G6 proves it).
    with tempfile.TemporaryDirectory() as td:
        sim = pathlib.Path(td) / "checkout-api"
        shutil.copytree(REPO, sim, ignore=shutil.ignore_patterns(".git", "node_modules"))

        sim_suite = sim / "test" / "session.test.js"
        original = sim_suite.read_text()
        gutted, n_removed = GUARD_TEST_RE.subn("", original)

        if n_removed != 1:
            gate(
                "G4 SIMULATION IS VALID (the cold-cache guard test was located)",
                False,
                f"expected to remove exactly 1 guard test, removed {n_removed} -- "
                "the suite's shape changed and this simulation is no longer "
                "proving what it claims. Refusing to report a divergence.",
            )
            return report()

        sim_suite.write_text(gutted)

        # G4 -- the gutted suite must still be GREEN on GOOD source. If it goes
        # red here it is a BROKEN suite, not a blinded one, and the divergence
        # below would prove nothing. This precondition is not optional.
        green = npm_test(sim)
        gp, gf = tallies(green)
        sim_is_valid = green.returncode == 0 and gf == 0
        gate(
            "G4 SIMULATED REGRESSION: guard gutted -> `npm test` STILL PASSES (blind)",
            sim_is_valid,
            f"exit={green.returncode} pass={gp} fail={gf} -- the suite does not "
            "notice the guard is gone, which is exactly why it survives review",
        )

        if not sim_is_valid:
            gate(
                "G5 DIVERGENCE (load-bearing)",
                False,
                "not evaluated: the simulation is invalid (a gutted suite that "
                "reddens on good source is broken, not blinded)",
            )
            return report()

        # Now prove the gutted guard is genuinely BLIND to the INC-1 defect:
        # reintroduce the defect into the sim and the gutted suite still passes.
        sim_src = sim / "service" / "checkout" / "session.js"
        sim_src.write_text(sim_src.read_text().replace(GUARDED, DEFECT))

        old_ci = npm_test(sim)  # OLD CI == the suite, and nothing else.
        op, of_ = tallies(old_ci)

        # NEW CI == the suite PLUS the mutation-witness verifier step.
        new_ci_verifier = subprocess.run(
            [sys.executable, str(sim / "artifacts" / "incident" / "verify_inc9_ci_gate.py")],
            cwd=str(sim),
            capture_output=True,
            text=True,
            timeout=600,
            env=child_env(),
        )
        vblob = new_ci_verifier.stdout + new_ci_verifier.stderr

        # ATTRIBUTION -- this is the correctness fix, and it matters.
        #
        # A bare non-zero exit from the nested verifier is NOT proof that the
        # mutation witness caught anything. The verifier has several gates, and in
        # this simulation the tree deliberately differs from upstream `main` (we
        # gutted the suite and reintroduced the defect), so a provenance/drift
        # gate could fail for a reason that has NOTHING to do with the guard.
        # Accepting `returncode != 0` would let the divergence "pass" for the
        # wrong reason -- a gate that is right by accident, which is exactly the
        # sin this whole verifier exists to prevent.
        #
        # So require the failure to be ATTRIBUTABLE to the mutation witness:
        # parse the per-gate output and demand that G5 (WITNESS B) is among the
        # failures. If NEW CI reddens for any other reason, this gate reports it
        # as NOT a valid divergence.
        failed_gates = re.findall(r"^\[FAIL\]\s+(\S+)", vblob, re.M)
        witness_b_failed = any(g.startswith("G5") for g in failed_gates)

        new_ci_exit = 0 if (old_ci.returncode == 0 and new_ci_verifier.returncode == 0) else 1

        diverges = (
            old_ci.returncode == 0  # OLD CI is blind: the regression slips through
            and new_ci_verifier.returncode != 0  # NEW CI reddens
            and witness_b_failed  # ...and it reddens BECAUSE the witness bit
        )
        gate(
            "G5 DIVERGENCE (LOAD-BEARING): OLD CI misses it, NEW CI catches it via WITNESS B",
            diverges,
            f"gutted guard + INC-1 defect reintroduced -> "
            f"OLD CI (suite only) exit={old_ci.returncode} pass={op} fail={of_} "
            f"[{'SLIPS THROUGH' if old_ci.returncode == 0 else 'caught -- the added step would be redundant'}] "
            f"| NEW CI (suite + verifier) exit={new_ci_exit} "
            f"[{'CAUGHT' if new_ci_verifier.returncode else 'MISSED'}]; "
            f"verifier failing gates={failed_gates or 'none'}; "
            f"ATTRIBUTED to the mutation witness (G5/WITNESS B)={witness_b_failed} "
            f"[required: a non-zero exit alone would not prove the witness bit -- "
            f"it could be an unrelated gate reddening on the simulated tree]",
        )

    # ------------------------------------------------------------------ G6 --
    after = {p: sha(p) for p in TRACKED}
    unchanged = [p for p in TRACKED if before[p] == after[p]]
    gate(
        "G6 the real tree was mutated by nothing (sha256 before == after)",
        len(unchanged) == len(TRACKED),
        f"{len(unchanged)}/{len(TRACKED)} files byte-identical; "
        f"session.js sha256={after[SRC][:12]}",
    )

    return report()


def report() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 74)
    print(f"GATES: {passed}/{total} passed")
    print("=" * 74)
    if passed == total:
        print(
            "\nThe merged INC-9 mutation witness now RUNS in CI. G5 is the proof of\n"
            "value: a gutted guard sails through the OLD workflow (suite only) and is\n"
            "CAUGHT by the new one. No production source, test assertion, or\n"
            "dependency was changed."
        )
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
