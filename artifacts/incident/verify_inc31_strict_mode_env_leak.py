#!/usr/bin/env python3
"""Fabric incident commander -- INC-31 verifier.

THE FINDING
-----------
Strict cross-fleet mode can be requested two ways, and they are supposed to mean
the SAME thing:

    python3 verify_x.py --require-cross-fleet          # argv
    FABRIC_REQUIRE_CROSS_FLEET=1 python3 verify_x.py   # environment

An independent detector ran every verifier in all three invocation modes, in both
the fleet workspace and a synthetic bare checkout. FIVE verifiers returned a
DIFFERENT VERDICT depending on which spelling was used -- on the identical tree:

  | environment  | verifier      | argv    | env       |
  |--------------|---------------|---------|-----------|
  | fleet        | verify_inc15  | exit 0  | exit 1 !! |
  | fleet        | verify_inc19  | exit 0  | exit 1 !! |
  | fleet        | verify_inc23  | exit 0  | exit 1 !! |
  | bare (=CI)   | verify_inc12  | exit 0  | exit 1 !! |
  | bare (=CI)   | verify_inc18  | exit 0  | exit 1 !! |

    A VERDICT THAT DEPENDS ON HOW THE REQUEST WAS SPELLED IS NOT A VERDICT.

ROOT CAUSE (proven, not assumed)
--------------------------------
NOT ONE verifier-launching `subprocess.run()` passed `env=`. Python hands the
child the parent's ENTIRE environment, so the strict flag LEAKED into children
that must not receive it.

Causation was DEMONSTRATED before any patch was written: a shim that scrubs the
variable from CHILD environments only (leaving the parent's own view intact)
turned all five divergent verifiers green. 5/5. Only then was the repair written.

  * verify_inc12 / verify_inc18 spawn verify_inc9_ci_gate.py to ask exactly one
    question -- "does the shipped INC-9 verifier pass on this tree?" In a BARE
    CHECKOUT (what CI clones) INC-9 has no siblings, so it correctly SKIPs its
    cross-fleet gates and exits 0. With the flag leaked in, the child inherits
    it, is forced into strict mode, and HARD-FAILS for want of siblings that are
    legitimately absent. The parent then reports "INC-9 does not pass" -- which
    is FALSE, and has nothing to do with the property being tested.
  * verify_inc15 / verify_inc19 / verify_inc23 spawn children as NEGATIVE
    CONTROLS against synthetic bare trees, and the control REQUIRES the child to
    SKIP and exit 0.

    A NEGATIVE CONTROL THAT INHERITS THE VERY FLAG IT IS CONTROLLING FOR IS NOT
    A CONTROL.

THE RULE
--------
    An intent must be PASSED to the child that should receive it,
    never INHERITED by a child that must not.

THE REPAIR
----------
    def child_env(*, strict=False):
        env = dict(os.environ)
        env.pop(STRICT_ENV_VAR, None)   # ALWAYS scrubbed
        if strict:
            env[STRICT_ENV_VAR] = "1"   # ...re-set ONLY on explicit request
        return env

Threaded through every python-launching spawn across the five files. The
strict-mode FEATURE IS UNTOUCHED at the top level -- it still works via argv AND
via the environment, and still hard-fails when legitimately asked. This stops it
LEAKING; it does not remove it.

GATES
-----
  G0  STATIC/AST -- every python-launching spawn in the patched set carries an
      explicit `env=`, and `child_env` genuinely pops the variable. The
      denominator is ASSERTED EXACTLY, so a blind audit can never masquerade as
      a clean one (see the note below -- this bit its own author).
  G1  NECESSITY   -- restore the leak, and the two strict modes DIVERGE again.
  G2  SUFFICIENCY -- as shipped, the two strict modes AGREE.
  G3  DIVERGENCE (load-bearing) -- identical tree: leaked = divergent,
      scrubbed = clean. Proves the repair is not a no-op.
  G4  ANTI-WEAKENING -- strict mode STILL HARD-FAILS when legitimately requested.
      This is what makes the change a CORRECTION and not a COVER-UP: simply
      DELETING strict mode would also have turned all five reds green and would
      have satisfied G1-G3. It fails G4.
  G5  SELF-REGRESSION -- reverting the scrub is REJECTED by G0's own AST audit.
  G6  NO DRIFT -- every deployed production source byte-identical before/after.

A NOTE ON G0, BECAUSE THE GATE CAUGHT ITS OWN AUTHOR
----------------------------------------------------
The detector that found this incident initially discovered repos by testing for a
`.git` directory. The clone had none, so it discovered ZERO verifiers -- AND
REPORTED "NO DIVERGENCES" ANYWAY. A check that examined nothing announced a clean
bill of health.

That is the same disease as the incident itself, one level up. So two rules are
encoded here permanently:

  1. discovery is STRUCTURAL (glob for the spawn shape), never keyed on an
     incidental artifact like `.git`; and
  2. AN EMPTY DENOMINATOR IS A HARD FAILURE. A gate that inspected 0 things is
     not passing -- it is blind, and it must say so.

G0 therefore asserts the spawn count EXACTLY rather than "all spawns I happened
to find", and a file audited 0/0 is itself a failure.

Runs on a BARE CHECKOUT: the behavioural witnesses need sibling repos, so where
they are absent they SKIP -- reported, never counted as a pass, never permanently
red. G0 (static) still guards the repair inside CI.

Exit: 0 = every executed gate passed.
"""
from __future__ import annotations

import ast
import hashlib
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve()
CHECKOUT_API = HERE.parents[2]
FLEET = CHECKOUT_API.parent
INCIDENT = CHECKOUT_API / "artifacts" / "incident"

STRICT_ENV_VAR = "FABRIC_REQUIRE_CROSS_FLEET"
STRICT_ARGV = "--require-cross-fleet"

# The five files the independent detector implicated, and the EXACT number of
# PYTHON-LAUNCHING spawns each must scrub. The denominator is LOAD-BEARING: an
# audit that finds 0 spawns in a file that has one is BLIND, and a blind audit
# must never be able to report a pass. See the module docstring.
#
# SCOPE, stated precisely, because "which spawns count" is exactly where a lazy
# audit hides. These counts were established by an INDEPENDENT AST recount of
# every subprocess.run/Popen in the incident directory:
#
#   verify_inc12: 2 python spawns (the INC-9 child, and the nested verifier in
#                 the simulated tree). It ALSO spawns `npm test` -- a NODE child,
#                 which cannot read a Python env flag. Not counted here (it is
#                 nonetheless given an explicit env= for uniformity).
#   verify_inc15: 1 python spawn (run_verifier -> verify_inc9_ci_gate.py)
#   verify_inc18: 1 python spawn (the shipped INC-9 verifier)
#   verify_inc19: 1 python spawn (target arrives as a FUNCTION PARAMETER, which
#                 is precisely why a name-resolving audit went blind to it)
#   verify_inc23: 2 python spawns -- the child verifier AND the inert pricing
#                 probe. The probe reads no env var, so scrubbing it changes
#                 nothing behaviourally; it is included anyway because a UNIFORM
#                 STRUCTURAL RULE ("every python-launching spawn carries an
#                 explicit env=") is mechanically auditable, whereas a per-spawn
#                 judgement call about which children "might" read the flag is
#                 the sort of reasoning that lets the next leak through.
#
# Total: 7. Asserted exactly.
PATCHED = {
    "verify_inc12_ci_runs_verifier.py": 2,
    "verify_inc15_cross_fleet_discovery.py": 1,
    "verify_inc18_gate_punishes_remediation.py": 1,
    "verify_inc19_layout_and_count_invariance.py": 1,
    "verify_inc23_drift_gate_punishes_owner_fix.py": 2,
}

DEPLOYED = [
    CHECKOUT_API / "service" / "checkout" / "session.js",
    FLEET / "fabric-gateway-demo" / "service" / "usage_aggregator.py",
    FLEET / "fabric-ic-incident-target" / "checkout.py",
]

RESULTS: list[tuple[str, bool, str]] = []
SKIPPED: list[tuple[str, str]] = []


def gate(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n         {detail}" if detail else ""))


def skip(name: str, detail: str = "") -> None:
    SKIPPED.append((name, detail))
    print(f"[SKIP] {name}" + (f"\n         {detail}" if detail else ""))


def sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def child_env(*, strict: bool = False) -> dict:
    env = dict(os.environ)
    env.pop(STRICT_ENV_VAR, None)
    if strict:
        env[STRICT_ENV_VAR] = "1"
    return env


def run(script: pathlib.Path, cwd: pathlib.Path, *, argv_strict=False, env_strict=False):
    cmd = [sys.executable, str(script)]
    if argv_strict:
        cmd.append(STRICT_ARGV)
    p = subprocess.run(cmd, cwd=str(cwd), env=child_env(strict=env_strict),
                       capture_output=True, text=True, timeout=900)
    return p.returncode


# --------------------------------------------------------------------- G0 ----
def launches_python(call: ast.Call) -> bool:
    """Does this subprocess call launch a PYTHON child?

    STRUCTURAL, deliberately. An earlier draft resolved local NAMES bound to a
    verifier path and confidently reported "4/4 spawns scrubbed". Four was WRONG
    -- the true total is higher, because two files pass their spawn target as a
    FUNCTION PARAMETER, bound to no resolvable name at all. The audit was blind to
    them and would have certified two fully-unscrubbed files as clean.

    So stop inferring WHICH script is launched -- an unwinnable name-resolution
    arms race -- and ask a structural question instead: does the argv list start
    with `sys.executable`? Every child that could read the flag is a Python
    process, and no parameter, alias, or f-string can hide that.
    """
    if not call.args:
        return False
    first = call.args[0]
    if not isinstance(first, (ast.List, ast.Tuple)) or not first.elts:
        return False
    head = first.elts[0]
    return (
        isinstance(head, ast.Attribute)
        and head.attr == "executable"
        and isinstance(head.value, ast.Name)
        and head.value.id == "sys"
    )


def audit_spawns(src: str) -> tuple[int, int]:
    """(python-launching spawns, how many carry an explicit env=)."""
    tree = ast.parse(src)
    total = scrubbed = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        is_sp_run = (
            isinstance(fn, ast.Attribute)
            and fn.attr in ("run", "Popen")
            and isinstance(fn.value, ast.Name)
            and fn.value.id == "subprocess"
        )
        if not is_sp_run or not launches_python(node):
            continue
        total += 1
        if any(kw.arg == "env" for kw in node.keywords):
            scrubbed += 1
    return total, scrubbed


def child_env_really_pops(src: str) -> bool:
    """Does child_env actually POP the variable? Matched on the CALL NODE.

    Not a substring check: the body pops a module CONSTANT, so the literal string
    "FABRIC_REQUIRE_CROSS_FLEET" never appears inside the function. Judging code by
    its incidental spelling is precisely the disease this fleet keeps relapsing on.
    """
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "child_env"):
            continue
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "pop"
                and sub.args
            ):
                arg = sub.args[0]
                if isinstance(arg, ast.Name) and arg.id == "STRICT_ENV_VAR":
                    return True
                if isinstance(arg, ast.Constant) and arg.value == STRICT_ENV_VAR:
                    return True
    return False


def g0_static(root: pathlib.Path = INCIDENT) -> tuple[bool, str]:
    total_spawns = total_scrubbed = 0
    problems: list[str] = []
    for fname, expected in PATCHED.items():
        path = root / fname
        if not path.is_file():
            problems.append(f"{fname}: MISSING")
            continue
        src = path.read_text()
        n, scrubbed = audit_spawns(src)
        total_spawns += n
        total_scrubbed += scrubbed

        # A 0-spawn file in the PATCHED set is itself a hard failure: it means the
        # audit went blind, and a blind audit must never report clean.
        if n == 0:
            problems.append(f"{fname}: audited 0 spawns -- BLIND (expected {expected})")
        elif n != expected:
            problems.append(f"{fname}: found {n} python spawns, expected exactly {expected}")
        if scrubbed != n:
            problems.append(f"{fname}: {scrubbed}/{n} spawns carry env=")
        if not child_env_really_pops(src):
            problems.append(f"{fname}: child_env does not pop {STRICT_ENV_VAR}")

    expected_total = sum(PATCHED.values())
    ok = (
        not problems
        and total_spawns == expected_total
        and total_scrubbed == expected_total
    )
    detail = (
        f"{total_scrubbed}/{total_spawns} python-launching spawns carry an explicit "
        f"env= across {len(PATCHED)} files (denominator asserted EXACTLY at "
        f"{expected_total}; a file audited 0/0 is a hard failure, so a blind audit "
        f"cannot masquerade as a clean one)"
    )
    if problems:
        detail += "; PROBLEMS: " + "; ".join(problems)
    return ok, detail


# ---------------------------------------------------------- leak restoration --
def restore_the_leak(tree_root: pathlib.Path) -> int:
    """Strip every `env=child_env(...)` from the spawns -- i.e. re-break it.

    Used by the NECESSITY and ANTI-WEAKENING witnesses, in throwaway copies only.
    """
    n = 0
    for fname in PATCHED:
        path = tree_root / "artifacts" / "incident" / fname
        if not path.is_file():
            continue
        src = path.read_text()
        for pattern in (
            "            env=child_env(),\n",
            "        env=child_env(),\n",
            "        timeout=900, env=child_env(),\n",
            "            timeout=120,\n            env=child_env(),\n",
        ):
            if pattern in src:
                src = src.replace(pattern, "" if "timeout" not in pattern
                                  else pattern.replace(" env=child_env(),", ""))
                n += 1
        # Generic sweep for any remaining spellings.
        src = src.replace("env=child_env(),", "")
        src = src.replace("env=child_env()", "")
        path.write_text(src)
    return n


def bare_copy(repo: pathlib.Path, dest: pathlib.Path) -> pathlib.Path:
    out = dest / repo.name
    shutil.copytree(repo, out, ignore=shutil.ignore_patterns(".git", "node_modules"))
    return out


def modes_agree(script: pathlib.Path, cwd: pathlib.Path) -> tuple[int, int, bool]:
    a = run(script, cwd, argv_strict=True)
    e = run(script, cwd, env_strict=True)
    return a, e, a == e


def main() -> int:
    print("Fabric incident commander -- INC-31 verification gates")
    print("(a strict-mode flag leaked into child verifiers, so a verdict depended")
    print(" on HOW the flag was spelled)\n")

    before = {p: sha(p) for p in DEPLOYED if p.is_file()}

    # ------------------------------------------------------------------ G0 --
    ok, detail = g0_static()
    gate("G0 STATIC/AST -- every python-launching spawn carries an explicit env=", ok, detail)

    siblings = all(p.is_file() for p in DEPLOYED)
    if not siblings:
        skip(
            "G1-G5 behavioural witnesses",
            "the sibling fleet repos are not in this checkout (CI clones only this "
            "repo). The witnesses need a full fleet to exhibit the divergence. "
            "Reported as SKIPPED -- never a pass, never a hard failure, so this step "
            "cannot become the permanently-red bug this fleet keeps re-committing. "
            "G0 above is STATIC and still guards the repair here in CI.",
        )
        return _summary(before)

    inc12 = INCIDENT / "verify_inc12_ci_runs_verifier.py"
    inc18 = INCIDENT / "verify_inc18_gate_punishes_remediation.py"

    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)

        # ---------------------------------------------- G1 · NECESSITY -------
        # Restore the leak in a throwaway BARE copy and show the two strict modes
        # diverge again. If they did not, the leak was not the cause and the patch
        # would be aimed at the wrong thing.
        leaked = bare_copy(CHECKOUT_API, tmp / "leaked")
        n_stripped = restore_the_leak(leaked)
        l12 = modes_agree(leaked / "artifacts" / "incident" / inc12.name, leaked)
        l18 = modes_agree(leaked / "artifacts" / "incident" / inc18.name, leaked)
        necessity = (not l12[2]) and (not l18[2])
        gate(
            "G1 NECESSITY -- with the leak restored, the two strict modes DIVERGE again",
            necessity,
            f"leak re-introduced ({n_stripped} env= scrubs stripped), bare checkout: "
            f"inc12 argv={l12[0]}/env={l12[1]} agree={l12[2]} · "
            f"inc18 argv={l18[0]}/env={l18[1]} agree={l18[2]}. "
            f"The divergence returns, so the leak IS the cause.",
        )

        # -------------------------------------------- G2 · SUFFICIENCY -------
        shipped = bare_copy(CHECKOUT_API, tmp / "shipped")
        s12 = modes_agree(shipped / "artifacts" / "incident" / inc12.name, shipped)
        s18 = modes_agree(shipped / "artifacts" / "incident" / inc18.name, shipped)
        sufficiency = s12[2] and s18[2] and s12[0] == 0 and s18[0] == 0
        gate(
            "G2 SUFFICIENCY -- as shipped, the two strict modes AGREE",
            sufficiency,
            f"bare checkout, as shipped: inc12 argv={s12[0]}/env={s12[1]} agree={s12[2]} · "
            f"inc18 argv={s18[0]}/env={s18[1]} agree={s18[2]}",
        )

        # --------------------------------------------- G3 · DIVERGENCE -------
        gate(
            "G3 DIVERGENCE (load-bearing) -- identical tree: leaked = divergent, scrubbed = clean",
            necessity and sufficiency,
            "the SAME bare checkout, the same intent delivered two ways: with the leak "
            "the verdict FLIPS on spelling; with the scrub it does not. The repair is "
            "therefore NOT a no-op.",
        )

        # ----------------------------------------- G4 · ANTI-WEAKENING -------
        # THE GATE THAT MATTERS MOST.
        #
        # Simply DELETING strict mode would ALSO have turned all five reds green,
        # and would have satisfied G1/G2/G3. It must fail here. So: strict mode,
        # when LEGITIMATELY requested against a tree that genuinely lacks the
        # siblings, must STILL hard-fail -- via argv AND via the environment.
        inc15_bare = shipped / "artifacts" / "incident" / "verify_inc15_cross_fleet_discovery.py"
        default_exit = run(inc15_bare, shipped)
        argv_exit = run(inc15_bare, shipped, argv_strict=True)
        env_exit = run(inc15_bare, shipped, env_strict=True)
        still_bites = default_exit == 0 and argv_exit == 1 and env_exit == 1
        gate(
            "G4 ANTI-WEAKENING -- strict mode STILL hard-fails when legitimately requested",
            still_bites,
            f"bare checkout (siblings genuinely absent): default exit={default_exit} "
            f"(correctly SKIPs) · argv-strict exit={argv_exit} (FATAL) · env-strict "
            f"exit={env_exit} (FATAL). The feature is intact -- the LEAK is gone. "
            f"Deleting strict mode would have satisfied G1-G3 and FAILED HERE: that is "
            f"the difference between a CORRECTION and a COVER-UP.",
        )

        # --------------------------------------- G5 · SELF-REGRESSION --------
        # Reverting the scrub must be REJECTED by G0's own AST audit -- the gate
        # detects its own regression.
        reverted_ok, reverted_detail = g0_static(leaked / "artifacts" / "incident")
        gate(
            "G5 SELF-REGRESSION -- reverting the scrub is REJECTED by G0's AST audit",
            not reverted_ok,
            f"G0 re-run against the leak-restored tree -> "
            f"{'ACCEPT (BAD!)' if reverted_ok else 'REJECT (correct)'}. "
            f"Strip the repair and CI reddens: {reverted_detail[:160]}",
        )

    return _summary(before)


def _summary(before: dict) -> int:
    # ------------------------------------------------------------------ G6 --
    after = {p: sha(p) for p in before if p.is_file()}
    moved = [p.name for p in before if after.get(p) != before[p]]
    gate(
        "G6 NO PRODUCTION DRIFT -- every deployed source byte-identical before/after",
        not moved,
        f"{len(before) - len(moved)}/{len(before)} byte-identical on the FULL sha256; "
        f"moved={moved or 'none'}. All witnesses ran in throwaway copies. "
        f"This run changed NO production code.",
    )

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{'=' * 74}\nINC-31 GATES: {passed}/{total} passed", end="")
    if SKIPPED:
        print(f"  ({len(SKIPPED)} SKIPPED -- NOT counted as passes)", end="")
    print(f"\n{'=' * 74}")
    for name, _ in SKIPPED:
        print(f"  SKIPPED: {name}")
    if passed != total:
        for name, ok, _ in RESULTS:
            if not ok:
                print(f"  FAILED: {name}")
        return 1
    print("All executed gates green. A verifier's verdict no longer depends on HOW")
    print("strict mode was spelled -- and strict mode still bites when asked for.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
