#!/usr/bin/env python3
"""Fabric incident commander -- INC-30 verifier.

THE FINDING
-----------
Not one `subprocess.run` in this repo's verifier fleet passed `env=`. Every child
therefore inherited the parent's ENTIRE environment -- including
`FABRIC_REQUIRE_CROSS_FLEET`, the variable that forces STRICT cross-fleet mode.

Several of those children are spawned as NEGATIVE CONTROLS: they run against a
synthetic BARE CHECKOUT (sibling repos deliberately absent) and the gate requires
the child to report SKIP and exit 0. That is how the fleet proves its CI steps are
not permanently red in a repo whose CI clones only itself.

So the moment an operator or a CI job exports the strict flag, the control child
INHERITS it, is forced into strict mode, and HARD-FAILS for want of siblings that
are legitimately absent. The gate then reports a failure that has nothing to do
with the property under test.

    A negative control that inherits the very flag it is controlling for
    is not a control.

Measured on `main` this run -- the SAME TREE, the same intent, delivered two ways:

    verifier                              argv --require-cross-fleet | env var
    ------------------------------------- -------------------------- | -------
    verify_inc15_cross_fleet_discovery    exit 0  GREEN              | exit 1  RED
    verify_inc19_layout_and_count_invar.  exit 0  GREEN              | exit 1  RED
    verify_inc23_drift_gate_punishes_...  exit 0  GREEN              | exit 1  RED

    A verdict that depends on HOW strict mode was requested is not a verdict.

And the leak is wider than the three red verifiers: `verify_inc12` and
`verify_inc18` ALSO spawn `verify_inc9_ci_gate.py` (a strict-flag consumer) to
back a gate asking only "does the shipped INC-9 verifier pass on this tree?".
They are green today purely because nobody exported the flag.

THE REPAIR
----------
    An intent must be PASSED to the child that should receive it,
    never INHERITED by a child that must not.

A `child_env()` helper that ALWAYS pops the variable and re-sets it only on
explicit request, threaded through every verifier-launching spawn in the five
affected files. The strict-mode FEATURE is untouched at the top level: it still
works via argv AND via the environment, and still hard-fails when legitimately
asked. This stops it LEAKING; it does not remove it.

The inert pricing probe in verify_inc23 (which reads no environment variable) is
deliberately left alone. A gate that fires on things it does not care about is
noise, and noise teaches the team to ignore gates.

GATES
-----
  G0 STATIC/AST -- every verifier-launching spawn carries an explicit env=, and
     child_env genuinely POPS the variable. Needs no siblings, so this is the gate
     that guards the repair inside CI.
  G1 NECESSITY  -- with the leak restored, the env-var invocation goes RED.
  G2 SUFFICIENCY-- same tree, scrubbed: the env-var invocation goes GREEN.
  G3 DIVERGENCE (load-bearing) -- identical tree: leaked = RED, scrubbed = GREEN.
     Had both behaved alike the repair would be a no-op.
  G4 ANTI-WEAKENING -- strict mode STILL hard-fails when legitimately requested
     via argv on a bare checkout. THIS is what makes it a correction and not a
     cover-up: simply DELETING strict mode would also have turned the three reds
     green and would have satisfied G1-G3 -- and it FAILS G4.
  G5 MODE AGREEMENT -- argv and env produce the SAME verdict for every verifier.
  G6 NO PRODUCTION DRIFT -- the three deployed sources are byte-identical.

Exit: 0 = every executed gate passed.
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

HERE = pathlib.Path(__file__).resolve()
CHECKOUT_API = HERE.parents[2]
FLEET = CHECKOUT_API.parent

STRICT_ENV_VAR = "FABRIC_REQUIRE_CROSS_FLEET"
STRICT_FLAG = "--require-cross-fleet"

# The verifiers that spawn a child which HONOURS the strict flag.
PATCHED = [
    "artifacts/incident/verify_inc12_ci_runs_verifier.py",
    "artifacts/incident/verify_inc15_cross_fleet_discovery.py",
    "artifacts/incident/verify_inc18_gate_punishes_remediation.py",
    "artifacts/incident/verify_inc19_layout_and_count_invariance.py",
    "artifacts/incident/verify_inc23_drift_gate_punishes_owner_fix.py",
]
# The three that were measurably RED under the env var before the repair.
WERE_RED = [
    "artifacts/incident/verify_inc15_cross_fleet_discovery.py",
    "artifacts/incident/verify_inc19_layout_and_count_invariance.py",
    "artifacts/incident/verify_inc23_drift_gate_punishes_owner_fix.py",
]
INC9_REL = "artifacts/incident/verify_inc9_ci_gate.py"

DEPLOYED = {
    CHECKOUT_API / "service" / "checkout" / "session.js":
        "b45a8eeceaa142dd70aea4182930d02edb2d23ce90f0f02527910abb5f18d7e8",
    FLEET / "fabric-gateway-demo" / "service" / "usage_aggregator.py":
        "bb21e50f7b5dab4463b71984bbe86a5df2b6ba442ffeff84d9b70815781750e5",
    FLEET / "fabric-ic-incident-target" / "checkout.py":
        "da2a02fd87aec668467114e0bc30ff7c2fe7fd3d8f105f5f156361b9c87c5c5e",
}

RESULTS: list[tuple[str, bool, str]] = []
SKIPPED: list[tuple[str, str]] = []


def gate(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n         {detail}" if detail else ""))


def skip(name: str, detail: str = "") -> None:
    SKIPPED.append((name, detail))
    print(f"[SKIP] {name}" + (f"\n         {detail}" if detail else ""))


def base_env(**extra) -> dict:
    env = dict(os.environ)
    env.pop(STRICT_ENV_VAR, None)
    env.update(extra)
    return env


def run(cwd: pathlib.Path, rel: str, *args: str, env: dict | None = None) -> int:
    p = subprocess.run(
        [sys.executable, rel, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=900,
        env=env if env is not None else base_env(),
    )
    return p.returncode


# ---------------------------------------------------------------------------
# G0: the STATIC / AST audit.
#
# It must be STRUCTURAL, not a substring scan. Two traps, both real, both of which
# defeated an earlier draft of this kind of audit in this fleet:
#
#   1. A spawn may name its target through a VARIABLE -- `[sys.executable, str(inc9)]`
#      -- so scanning the argv for the literal text "verify_" reports 0 spawns in a
#      file that has one, and would certify an unscrubbed file as clean. So names
#      bound to a verifier path are resolved first.
#   2. child_env pops a module CONSTANT (STRICT_ENV_VAR), so the literal string
#      "FABRIC_REQUIRE_CROSS_FLEET" never appears inside the function body. An audit
#      demanding that literal reports False on a helper that demonstrably works.
#      So we match the .pop() CALL NODE and resolve its argument.
# ---------------------------------------------------------------------------
def audit(path: pathlib.Path) -> dict:
    tree = ast.parse(path.read_text())

    # Module-level string constants, so a popped constant can be resolved.
    consts: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if isinstance(t, ast.Name) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                consts[t.id] = node.value.value

    # Names bound to a path that looks like a verifier script (trap #1).
    verifier_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if isinstance(t, ast.Name):
                blob = ast.unparse(node.value)
                if "verify_" in blob:
                    verifier_names.add(t.id)
    for name, val in consts.items():
        if "verify_" in val:
            verifier_names.add(name)

    # TRAP #3 -- the one that made this audit report 0/0 on two files that each
    # have a real spawn, i.e. a BLIND GATE, i.e. the exact disease this fleet
    # exists to cure. Caught by the negative control (a 0/0 audit is not a clean
    # bill of health; it is a check that saw nothing).
    #
    # A generic runner takes the script as a PARAMETER:
    #
    #     def run(cwd, rel, *args):                 # inc19
    #         subprocess.run([sys.executable, rel, *args], ...)
    #     def run(script, cwd):                     # inc23
    #         subprocess.run([sys.executable, str(script)], ...)
    #
    # `rel` / `script` are bound to nothing at module scope, so no amount of
    # constant-resolution finds "verify_" in the argv. But the function is a
    # SUBPROCESS LAUNCHER whose target is caller-supplied, so it can launch a
    # verifier and MUST be scrubbed. Treat any subprocess call whose argv starts
    # with sys.executable as a verifier launcher UNLESS its target resolves to
    # something we can positively identify as an inert probe (a local temp file
    # written by this verifier, e.g. verify_inc23's pricing probe).
    #
    # This errs on the side of DEMANDING a scrub -- the safe direction. A gate
    # that cannot see a spawn is worthless; a gate that asks for one scrub too
    # many is merely strict.
    probe_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if isinstance(t, ast.Name):
                blob = ast.unparse(node.value)
                # a scratch file this verifier writes itself, then executes
                if "probe" in t.id.lower() or "probe.py" in blob:
                    probe_names.add(t.id)

    def spawn_kind(call: ast.Call) -> str | None:
        """'verifier' | 'probe' | None (not a python-script spawn at all)."""
        if not call.args:
            return None
        first = call.args[0]
        if not isinstance(first, (ast.List, ast.Tuple)) or not first.elts:
            return None
        argv = [ast.unparse(e) for e in first.elts]
        if not any("sys.executable" in a for a in argv):
            return None  # e.g. ["npm", "test"] -- not a python child

        rest = argv[1:]
        # positively identified inert probe?
        for a in rest:
            bare = a.replace("str(", "").replace(")", "").strip()
            if bare in probe_names or "probe" in bare.lower():
                return "probe"
        # a verifier by literal name, by a module-level name, or by a PARAMETER
        # whose value the caller supplies -- all must be scrubbed.
        return "verifier"

    spawns = 0
    scrubbed = 0
    probes = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = ast.unparse(node.func)
        if fn not in ("subprocess.run", "subprocess.Popen"):
            continue
        kind = spawn_kind(node)
        if kind is None:
            continue
        if kind == "probe":
            probes += 1
            continue  # inert: reads no environment variable, deliberately out of scope
        spawns += 1
        if any(kw.arg == "env" for kw in node.keywords):
            scrubbed += 1

    # Does child_env genuinely POP the strict variable? Match the call node.
    pops = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "child_env":
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "pop"
                    and inner.args
                ):
                    arg = inner.args[0]
                    resolved = None
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        resolved = arg.value
                    elif isinstance(arg, ast.Name):
                        resolved = consts.get(arg.id)
                    if resolved == STRICT_ENV_VAR:
                        pops = True
    return {"spawns": spawns, "scrubbed": scrubbed, "probes": probes, "pops": pops}


def strip_repair(root: pathlib.Path, rel: str) -> bool:
    """Re-introduce the LEAK in a throwaway copy: drop `env=child_env()` from spawns."""
    p = root / rel
    src = p.read_text()
    n = src.count("env=child_env(),")
    if n == 0:
        return False
    p.write_text(src.replace("env=child_env(),", "", 1) if False else src.replace("env=child_env(),", ""))
    import py_compile
    try:
        py_compile.compile(str(p), doraise=True)
    except py_compile.PyCompileError:
        return False
    return True


def fleet_copy(dst: pathlib.Path) -> pathlib.Path:
    fleet = dst / "fleet"
    fleet.mkdir(parents=True)
    for r in ("checkout-api", "fabric-gateway-demo", "fabric-ic-incident-target"):
        src = FLEET / r
        if src.is_dir():
            shutil.copytree(src, fleet / r, ignore=shutil.ignore_patterns("node_modules", "__pycache__"))
    return fleet / "checkout-api"


def main() -> int:
    print("Fabric incident commander — INC-30 verification gates")
    print("(a strict-mode flag leaked into child verifiers through the environment)\n")

    pre = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in DEPLOYED if p.is_file()}

    # ------------------------------------------------------------------ G0 --
    rows = []
    total_spawns = total_scrubbed = 0
    all_pop = True
    for rel in PATCHED:
        path = CHECKOUT_API / rel
        if not path.is_file():
            all_pop = False
            rows.append(f"{pathlib.Path(rel).name}: MISSING")
            continue
        a = audit(path)
        total_spawns += a["spawns"]
        total_scrubbed += a["scrubbed"]
        all_pop = all_pop and a["pops"]
        rows.append(
            f"{pathlib.Path(rel).name}: {a['scrubbed']}/{a['spawns']} verifier spawns "
            f"carry env= (+{a['probes']} inert probe(s) out of scope), "
            f"child_env pops the var={a['pops']}"
        )
    # EVERY patched file must expose at least one verifier spawn. A file audited as
    # 0/0 is not clean -- it is a check that SAW NOTHING, which is precisely the
    # blind-gate failure this fleet keeps re-committing. So a zero-spawn file in the
    # patched set is a HARD FAILURE of the audit itself.
    per_file_spawns = {
        pathlib.Path(rel).name: audit(CHECKOUT_API / rel)["spawns"]
        for rel in PATCHED
        if (CHECKOUT_API / rel).is_file()
    }
    blind = [n for n, c in per_file_spawns.items() if c == 0]
    gate(
        "G0 STATIC/AST — every verifier-launching spawn is scrubbed; child_env really pops",
        total_spawns > 0
        and total_scrubbed == total_spawns
        and all_pop
        and not blind,
        "; ".join(rows)
        + f" | TOTAL {total_scrubbed}/{total_spawns}"
        + (f" | !! AUDIT IS BLIND on {blind} (0 spawns found in a file that has one) "
           if blind else " | no file audited 0/0 — the audit can SEE every spawn")
        + ". Structural (AST): a target behind a variable, or arriving as a function "
          "PARAMETER, still counts — a spawn the audit cannot see would make this gate "
          "worthless.",
    )

    siblings = (FLEET / "fabric-gateway-demo").is_dir() and (
        FLEET / "fabric-ic-incident-target"
    ).is_dir()

    if not siblings:
        skip(
            "G1-G5 behavioural witnesses",
            "the sibling fleet repos are not in this checkout (CI clones only this repo), "
            "and the witnesses need the full fleet to reproduce the strict-mode divergence. "
            "Reported as SKIPPED — never a pass, never a hard failure, so this step cannot "
            "become the permanently-red INC-11 bug. G0 above and G6 below still execute and "
            "still guard the repair here.",
        )
        return drift_gate_then_summary(pre)

    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)

        # ---- G1 NECESSITY: restore the leak, and the env invocation reddens ----
        leaked_api = fleet_copy(tmp / "leaked")
        stripped = [rel for rel in WERE_RED if strip_repair(leaked_api, rel)]
        leak_env = base_env(**{STRICT_ENV_VAR: "1"})
        leaked_exits = {rel: run(leaked_api, rel, env=leak_env) for rel in WERE_RED}
        gate(
            "G1 NECESSITY — with the leak restored, the env-var invocation goes RED",
            len(stripped) == len(WERE_RED) and all(e != 0 for e in leaked_exits.values()),
            f"stripped env=child_env() from {len(stripped)}/{len(WERE_RED)} files; "
            + ", ".join(f"{pathlib.Path(r).name}={e}" for r, e in leaked_exits.items())
            + " — the child inherits the flag and hard-fails for want of siblings it was "
              "never meant to have.",
        )

        # ---- G2 SUFFICIENCY: the SHIPPED (scrubbed) tree, same invocation ----
        clean_api = fleet_copy(tmp / "clean")
        clean_exits = {rel: run(clean_api, rel, env=base_env(**{STRICT_ENV_VAR: "1"}))
                       for rel in WERE_RED}
        gate(
            "G2 SUFFICIENCY — same tree, scrubbed: the env-var invocation goes GREEN",
            all(e == 0 for e in clean_exits.values()),
            ", ".join(f"{pathlib.Path(r).name}={e}" for r, e in clean_exits.items()),
        )

        # ---- G3 DIVERGENCE (load-bearing) ----
        diverged = all(
            leaked_exits[r] != 0 and clean_exits[r] == 0 for r in WERE_RED
        )
        gate(
            "G3 DIVERGENCE (load-bearing) — identical tree: leaked = RED · scrubbed = GREEN",
            diverged,
            "; ".join(
                f"{pathlib.Path(r).name}: leaked={leaked_exits[r]} scrubbed={clean_exits[r]}"
                for r in WERE_RED
            )
            + " — had both behaved alike, the repair would be a no-op.",
        )

        # ---- G4 ANTI-WEAKENING: strict mode must STILL bite when asked properly ----
        # A bare checkout with NO siblings + an explicit argv request = a caller who
        # KNOWS the siblings should be there. That MUST still hard-fail. Deleting
        # strict mode would have satisfied G1-G3 and fails here.
        bare = tmp / "bare" / "checkout-api"
        bare.parent.mkdir(parents=True)
        shutil.copytree(
            CHECKOUT_API, bare, ignore=shutil.ignore_patterns("node_modules", "__pycache__")
        )
        strict_argv = run(bare, INC9_REL, STRICT_FLAG)
        default_bare = run(bare, INC9_REL)
        strict_env_bare = run(bare, INC9_REL, env=base_env(**{STRICT_ENV_VAR: "1"}))
        gate(
            "G4 ANTI-WEAKENING — strict mode STILL hard-fails when legitimately requested",
            strict_argv != 0 and strict_env_bare != 0 and default_bare == 0,
            f"bare checkout, INC-9: default exit={default_bare} (GREEN — gates correctly SKIP) · "
            f"argv {STRICT_FLAG} exit={strict_argv} (RED — correct) · "
            f"env {STRICT_ENV_VAR}=1 exit={strict_env_bare} (RED — correct). "
            "The FEATURE is intact at the top level: this patch stops the flag LEAKING into "
            "children, it does not remove it. DELETING strict mode would have satisfied "
            "G1-G3 and FAILED here — that is the difference between a correction and a cover-up.",
        )

        # ---- G5 MODE AGREEMENT across the whole patched set ----
        disagree = []
        for rel in PATCHED:
            a = run(clean_api, rel, STRICT_FLAG)
            e = run(clean_api, rel, env=base_env(**{STRICT_ENV_VAR: "1"}))
            if a != e:
                disagree.append(f"{pathlib.Path(rel).name}: argv={a} env={e}")
        gate(
            "G5 MODE AGREEMENT — argv and env produce the SAME verdict for every verifier",
            not disagree,
            "a verdict that depends on HOW strict mode was requested is not a verdict; "
            + (f"disagreements: {disagree}" if disagree else
               f"all {len(PATCHED)} patched verifiers agree across both strict modes"),
        )

    return drift_gate_then_summary(pre)


def drift_gate_then_summary(pre: dict) -> int:
    """G6 + summary. Factored out so the drift gate ALWAYS runs.

    An earlier draft returned early on a bare checkout and skipped G6 entirely --
    so the one gate that proves this verifier does not corrupt production was
    unreachable in exactly the environment (CI) where it runs most often. A gate
    that does not execute is not a gate.
    """
    post = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in DEPLOYED if p.is_file()}
    moved = [p.name for p in pre if post.get(p) != pre[p]]
    wrong = [p.name for p, h in DEPLOYED.items() if p.is_file() and post.get(p) != h]
    gate(
        "G6 NO PRODUCTION DRIFT — deployed sources byte-identical, before and after",
        not moved and not wrong and len(post) > 0,
        f"{len(post)}/{len(DEPLOYED)} deployed sources present here and byte-identical on the "
        f"FULL sha256; moved by this run={moved or 'none'}; differ from deployed={wrong or 'none'}"
        + ("" if len(post) == len(DEPLOYED)
           else " (siblings absent in this checkout: not drift, just a different environment)")
        + ". All mutation testing happened in throwaway copies.",
    )
    return summary()


def summary() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{'=' * 74}\nINC-30 GATES: {passed}/{total} passed", end="")
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
    print("All executed gates green. An intent is now PASSED to the child that should")
    print("receive it, never INHERITED by a child that must not -- and strict mode still")
    print("hard-fails when it is legitimately requested. Production source untouched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
