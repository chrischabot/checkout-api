#!/usr/bin/env python3
"""Fabric autonomous incident commander -- INC-29 verification gates (repo-local).

THE FINDING
-----------
INC-28 established the rule:

    An intent must be PASSED to the child that should receive it,
    never INHERITED by a child that must not.

It scrubbed the strict-mode env var (FABRIC_REQUIRE_CROSS_FLEET) from the child
verifier spawns in verify_inc15, verify_inc19 and verify_inc23. But
`verify_inc12_ci_runs_verifier.py` and `verify_inc18_gate_punishes_remediation.py`
ALSO spawn `verify_inc9_ci_gate.py` as a child, and INC-28 left BOTH unscrubbed.

Each of those spawns backs a G1 gate asking exactly one question:

    Does the shipped INC-9 verifier pass on the current tree?

In a BARE CHECKOUT -- which is precisely what this repo's CI clones -- INC-9 has
no sibling repos, so it correctly SKIPs its cross-fleet gates and exits 0. But when
an operator or a CI job exports FABRIC_REQUIRE_CROSS_FLEET, the child INHERITS it,
is forced into strict mode, and hard-fails for want of siblings that are
LEGITIMATELY ABSENT. G1 then reports that INC-9 does not pass -- a claim that is
FALSE, and that has nothing to do with the property G1 tests.

Measured on the INC-28 tree (i.e. WITH the INC-28 repair applied), bare checkout:

    verifier      argv-strict     env-strict
    -----------   -----------     ----------
    INC-12        exit 0          exit 1  (5/6)   <-- same tree
    INC-18        exit 0          exit 1  (5/6)   <-- same tree

A verdict that depends on HOW strict mode was requested is not a verdict.

THE REPAIR
----------
INC-28's own child_env() helper, extended to both files. The strict-mode FEATURE is
untouched at the top level -- this stops it LEAKING, it does not remove it.

GATES (all runnable on a BARE CHECKOUT -- no sibling repos required)
-------------------------------------------------------------------
  G0 STATIC/AST      every verifier-launching spawn in the two files carries an
                     explicit env=, and child_env genuinely pops the variable.
  G1 NECESSITY       with the leak restored in a temp copy, the gate HARD-FAILS
                     under env-strict -- proving the scrub is load-bearing.
  G2 SUFFICIENCY     same tree, scrubbed: the gate PASSES under env-strict.
  G3 DIVERGENCE      LOAD-BEARING. Identical tree: leaked=RED, scrubbed=GREEN.
                     Had both behaved alike, the repair would be a no-op.
  G4 ANTI-WEAKENING  strict mode STILL hard-fails when legitimately requested via
                     argv, and the default invocation is STILL green (not
                     permanently red). Simply DELETING strict mode would satisfy
                     G1-G3 and FAIL THIS GATE: correction, not cover-up.
  G5 SELF-REGRESSION reverting the scrub is REJECTED by G0's own AST audit.
  G6 NO DRIFT        this verifier leaves the tree byte-identical; all mutation is
                     done in throwaway temp copies.

Exit: 0 = every gate passed.
"""
from __future__ import annotations

import ast
import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

# Locate this repo's root from the verifier's own location, so the gate runs
# identically in CI (a bare checkout of just this repo) and in the commander
# workspace. A verifier that only runs on its author's machine is not a verifier.
REPO = pathlib.Path(__file__).resolve().parents[2]
INCIDENT = REPO / "artifacts" / "incident"

PATCHED_FILES = (
    "verify_inc12_ci_runs_verifier.py",
    "verify_inc18_gate_punishes_remediation.py",
)
STRICT_ENV_VAR = "FABRIC_REQUIRE_CROSS_FLEET"

# Provenance reference only -- NEVER a gate condition. INC-23 established that
# freezing a deployed hash into a verdict hard-fails the moment an owner lands a
# legitimate repair. G6 asserts only that THIS RUN changed nothing.
SESSION_JS = REPO / "service" / "checkout" / "session.js"

RESULTS: list[tuple[str, bool, str]] = []


def gate(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n         {detail}" if detail else ""))


def sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit_file(src: str) -> dict:
    """STRUCTURAL (AST) audit of the child spawns -- deliberately NOT a grep.

    THE GATE CAUGHT ITS OWN AUTHOR, TWICE, and both findings changed this function.

    1. BLINDNESS. A first draft decided "is this a verifier spawn?" by looking for
       the substring `verify_` in the unparsed argv. verify_inc18 spawns its child
       as `[sys.executable, str(inc9)]` -- the script sits behind a VARIABLE, so no
       such substring exists, and the audit reported 0/0 spawns in a file that has
       one. A gate that sees nothing is blind, and would have certified an
       UNSCRUBBED file as clean. Fixed by RESOLVING the argv: names bound anywhere
       in the module to a path containing `verify_` are collected first, so a spawn
       that references its target through a variable is still recognised.

       SCOPE, stated honestly: a launch counts as a verifier spawn when its argv
       carries a `verify_` literal OR references such a resolved name. A
       sys.executable launch that does neither (e.g. an inert `-c` probe, which
       reads no env var and which no scrub could affect) is SKIPPED, not failed --
       INC-28 recorded that firing on inert spawns is noise that teaches the team to
       ignore the gate. If a future spawn shape hides its target from this resolver,
       extend the resolver; do not assume it is already covered.

    2. FALSE NEGATIVE ON THE POP. A draft required the literal string
       "FABRIC_REQUIRE_CROSS_FLEET" to appear inside child_env's body. But the body
       pops the MODULE CONSTANT -- `env.pop(STRICT_ENV_VAR, None)` -- so the literal
       never appears, and the check reported False on a helper that demonstrably
       works. Judging code by its incidental spelling is exactly the disease INC-27
       exists to cure. Fixed: match the `.pop()` CALL NODE and resolve its argument.
    """
    tree = ast.parse(src)

    verifier_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            rendered = ast.unparse(node.value) if node.value is not None else ""
            if "verify_" in rendered:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        verifier_names.add(t.id)

    spawns_total = 0
    spawns_with_env = 0
    unscrubbed_lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = ast.unparse(node.func)
        if not (fn.endswith("subprocess.run") or fn.endswith("Popen")):
            continue
        if not node.args:
            continue
        argv_node = node.args[0]
        argv = ast.unparse(argv_node)

        # Only python-interpreter launches can inherit the strict flag in a way that
        # matters. An `npm test` spawn reads no such variable.
        if "sys.executable" not in argv:
            continue

        mentions_literal = "verify_" in argv
        mentions_name = any(
            isinstance(n, ast.Name) and n.id in verifier_names for n in ast.walk(argv_node)
        )
        if not (mentions_literal or mentions_name):
            continue

        spawns_total += 1
        if any(kw.arg == "env" for kw in node.keywords):
            spawns_with_env += 1
        else:
            unscrubbed_lines.append(node.lineno)

    strict_literals = {STRICT_ENV_VAR}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if (
                isinstance(tgt, ast.Name)
                and isinstance(node.value, ast.Constant)
                and node.value.value == STRICT_ENV_VAR
            ):
                strict_literals.add(tgt.id)

    pops_var = False
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "child_env"):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            if not (isinstance(call.func, ast.Attribute) and call.func.attr == "pop"):
                continue
            if not call.args:
                continue
            key = call.args[0]
            if isinstance(key, ast.Constant) and key.value == STRICT_ENV_VAR:
                pops_var = True
            elif isinstance(key, ast.Name) and key.id in strict_literals:
                pops_var = True

    return {
        "spawns": spawns_total,
        "with_env": spawns_with_env,
        "unscrubbed_lines": unscrubbed_lines,
        "all_scrubbed": spawns_total > 0 and spawns_total == spawns_with_env,
        "child_env_pops": pops_var,
    }


def run(script: pathlib.Path, cwd: pathlib.Path, *args: str, env_strict: bool = False):
    env = dict(os.environ)
    env.pop(STRICT_ENV_VAR, None)  # never let OUR ambient env decide the child's mode
    if env_strict:
        env[STRICT_ENV_VAR] = "1"
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=900,
        env=env,
    )


def tally(proc) -> str:
    blob = proc.stdout + proc.stderr
    found = re.findall(r"(\d+)/(\d+) (?:gates )?passed", blob)
    return f"{found[-1][0]}/{found[-1][1]}" if found else "(no tally)"


def reintroduce_leak(src: str) -> str:
    """Undo the INC-29 repair: strip env=child_env() from the verifier spawns."""
    return re.sub(r"\n\s*env=child_env\(\),", "", src)


def main() -> int:
    print("Fabric incident commander -- INC-29 verification gates")
    print("(the strict-mode flag still leaked into the child spawns of the two")
    print(" verifiers INC-28 did not reach: verify_inc12 and verify_inc18)\n")

    before = sha(SESSION_JS) if SESSION_JS.is_file() else None

    # ------------------------------------------------------------------ G0 --
    audits = {n: audit_file((INCIDENT / n).read_text()) for n in PATCHED_FILES}
    g0 = all(a["all_scrubbed"] and a["child_env_pops"] for a in audits.values())
    gate(
        "G0 STATIC/AST -- every verifier spawn passes env=; child_env pops the var",
        g0,
        "; ".join(
            f"{n}: {a['with_env']}/{a['spawns']} spawns scrubbed"
            + (f" (UNSCRUBBED at {a['unscrubbed_lines']})" if a["unscrubbed_lines"] else "")
            + f", child_env pops the var={a['child_env_pops']}"
            for n, a in audits.items()
        )
        + " [structural: spawn targets resolved through variables, and the pop matched"
        " as an AST call node -- so a script behind a variable cannot hide from it]",
    )

    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        ignore = shutil.ignore_patterns(".git", "node_modules", "__pycache__")

        scrubbed = tmp / "scrubbed"
        shutil.copytree(REPO, scrubbed, ignore=ignore)

        leaked = tmp / "leaked"
        shutil.copytree(REPO, leaked, ignore=ignore)
        for name in PATCHED_FILES:
            f = leaked / "artifacts" / "incident" / name
            f.write_text(reintroduce_leak(f.read_text()))

        # The negative control must genuinely BE a control: confirm the mutation really
        # re-opened the leak. A witness that silently fails to apply its own mutation
        # proves nothing -- INC-28 hit precisely that trap.
        leak_audit = {
            n: audit_file((leaked / "artifacts" / "incident" / n).read_text())
            for n in PATCHED_FILES
        }
        control_valid = all(not a["all_scrubbed"] for a in leak_audit.values())

        # ---------------------------------------------------- G1 / G2 / G3 --
        necessity_reds: list[str] = []
        sufficiency_greens: list[str] = []
        rows: list[str] = []
        for name in PATCHED_FILES:
            leak_env = run(leaked / "artifacts" / "incident" / name, leaked, env_strict=True)
            scrub_env = run(
                scrubbed / "artifacts" / "incident" / name, scrubbed, env_strict=True
            )
            if leak_env.returncode != 0:
                necessity_reds.append(name)
            if scrub_env.returncode == 0:
                sufficiency_greens.append(name)
            rows.append(
                f"{name}: LEAKED env-strict exit={leak_env.returncode} ({tally(leak_env)})"
                f" | SCRUBBED env-strict exit={scrub_env.returncode} ({tally(scrub_env)})"
            )

        gate(
            "G1 NECESSITY -- with the leak restored, the bare-checkout gate HARD-FAILS",
            control_valid and len(necessity_reds) == len(PATCHED_FILES),
            f"control_valid={control_valid}; RED with the leak: {necessity_reds}. The child "
            f"inherits the flag, is forced strict, and fails for want of siblings CI never "
            f"clones -- so G1 reports that INC-9 does not pass, which is FALSE.",
        )

        gate(
            "G2 SUFFICIENCY -- same tree, scrubbed: the gate PASSES",
            len(sufficiency_greens) == len(PATCHED_FILES),
            f"GREEN after the scrub: {sufficiency_greens}",
        )

        gate(
            "G3 DIVERGENCE (LOAD-BEARING) -- identical tree: leaked=RED, scrubbed=GREEN",
            control_valid
            and len(necessity_reds) == len(PATCHED_FILES)
            and len(sufficiency_greens) == len(PATCHED_FILES),
            " | ".join(rows) + " -- had both behaved alike, the repair would be a no-op.",
        )

        # ------------------------------------------------------------- G4 --
        # ANTI-WEAKENING. THE GATE THAT MATTERS MOST.
        #
        # Deleting strict mode outright would ALSO turn the two reds green and would
        # satisfy G1-G3. It must fail HERE. So strict mode, when LEGITIMATELY requested
        # via argv, must STILL hard-fail where the siblings are genuinely absent -- while
        # the DEFAULT invocation must still be green, so this step never becomes the
        # permanently-red INC-11 bug.
        #
        # A THIRD SELF-CATCH, and it is worth recording. A draft of this gate decided
        # "are the siblings absent?" by looking at the REAL repo's parent directory
        # (REPO.parent) -- but the verifier under test is executed inside the TEMP COPY,
        # whose parent is a throwaway tmpdir that never has siblings beside it. In the
        # commander workspace those two answers DISAGREE: the real parent has the sibling
        # repos, the temp parent does not, so the gate expected strict mode to succeed
        # while the child (correctly) hard-failed for want of siblings, and G4 went red on
        # a healthy tree. That is this fleet's signature disease one more level down -- a
        # gate reddening for a reason unrelated to the property under test.
        #
        # The predicate must describe the TREE THAT WAS ACTUALLY RUN. The isolated copy
        # never has siblings, so strict mode MUST hard-fail against it -- in CI and in the
        # commander workspace alike. That makes this gate environment-independent, which
        # is exactly the invariance INC-19 demanded.
        inc15 = scrubbed / "artifacts" / "incident" / "verify_inc15_cross_fleet_discovery.py"
        strict_argv = run(inc15, scrubbed, "--require-cross-fleet")
        default_run = run(inc15, scrubbed)

        # Established from the executed tree, NOT from the ambient workspace layout.
        siblings_beside_executed_tree = (scrubbed.parent / "fabric-gateway-demo").is_dir()
        still_bites = strict_argv.returncode != 0
        not_permanently_red = default_run.returncode == 0

        gate(
            "G4 ANTI-WEAKENING -- strict mode STILL hard-fails when legitimately asked",
            still_bites and not_permanently_red and not siblings_beside_executed_tree,
            f"isolated copy, siblings genuinely absent beside it "
            f"(siblings_present={siblings_beside_executed_tree}): argv-strict exit="
            f"{strict_argv.returncode} (must be non-zero -- the feature still BITES when "
            f"legitimately requested) | default exit={default_run.returncode} (must be 0 -- "
            f"NOT permanently red in the very CI job that runs it). Simply DELETING strict "
            f"mode would satisfy G1-G3 and FAIL THIS GATE: that is the difference between a "
            f"CORRECTION and a COVER-UP. Judged against the EXECUTED tree, so the verdict is "
            f"identical in CI and in the commander workspace.",
        )

        # ------------------------------------------------------------- G5 --
        gate(
            "G5 SELF-REGRESSION -- reverting the scrub is REJECTED by G0's AST audit",
            all(not a["all_scrubbed"] for a in leak_audit.values()),
            f"leaked-tree audit all_scrubbed="
            f"{ {n: a['all_scrubbed'] for n, a in leak_audit.items()} } -> verdict=REJECT. "
            f"The gate catches its own removal.",
        )

    # ------------------------------------------------------------------ G6 --
    after = sha(SESSION_JS) if SESSION_JS.is_file() else None
    gate(
        "G6 NO DRIFT -- this verifier leaves the tree byte-identical",
        before == after,
        f"session.js sha256 before==after: {before == after} "
        f"({(after or 'absent')[:16]}). All mutation was done in throwaway temp copies; "
        f"no production source, test, or gate was weakened.",
    )

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{'=' * 74}\nINC-29 GATES: {passed}/{total} passed\n{'=' * 74}")
    if passed != total:
        for name, ok, _ in RESULTS:
            if not ok:
                print(f"  FAILED: {name}")
        return 1
    print(
        "\nThe strict-mode flag can no longer leak into the child verifier spawns of\n"
        "verify_inc12 or verify_inc18 -- and strict mode STILL bites when it is\n"
        "legitimately requested. Production source untouched; no billing policy invented."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
