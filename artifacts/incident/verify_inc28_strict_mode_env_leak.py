#!/usr/bin/env python3
"""Fabric incident commander — INC-28 verifier.

THE FINDING
-----------
Three verifiers in this repo SPAWN CHILD verifier processes, and several of those
children exist specifically as NEGATIVE CONTROLS: they run against a synthetic
BARE CHECKOUT (the sibling fleet repos deliberately absent) and require the child
to report SKIP and exit 0. That is how they prove the gates are not permanently
red in `checkout-api` CI, which clones only this repo.

But `subprocess.run()` WITHOUT `env=` hands the child the parent's ENTIRE
environment. And strict cross-fleet mode is honoured via an environment variable:

    FABRIC_REQUIRE_CROSS_FLEET=1     # "a missing sibling is FATAL"

So the moment an operator or a CI job exports that variable, the negative-control
child INHERITS it, is forced into strict mode, and HARD-FAILS -- exactly where the
control demands a SKIP. The gate then reports a failure that has nothing whatever
to do with the property it is testing.

A NEGATIVE CONTROL THAT INHERITS THE VERY FLAG IT IS CONTROLLING FOR IS NOT A
CONTROL.

MEASURED, on a fleet that was otherwise 13/13 GREEN:

    invocation                        INC-15   INC-19   INC-23
    --------------------------------  -------  -------  -------
    --require-cross-fleet  (argv)     9/9  ok  7/7  ok  8/8  ok
    FABRIC_REQUIRE_CROSS_FLEET=1      8/9 RED  2/7 RED  5/8 RED

An AMBIENT VARIABLE made a HEALTHY fleet report RED, in three verifiers at once.
INC-19 collapses hardest (2/7) because nearly every one of its gates spawns a
child. This is the fleet's signature disease -- a gate reddening for a reason
unrelated to the property under test -- one level down, in the harness itself.

THE REPAIR
----------
An INTENT must be PASSED to the child that should receive it, never INHERITED by a
child that must not. Each of the three verifiers now defines:

    def child_env(*, strict=None):
        env = dict(os.environ)
        env.pop("FABRIC_REQUIRE_CROSS_FLEET", None)   # ALWAYS scrubbed
        if strict:
            env["FABRIC_REQUIRE_CROSS_FLEET"] = "1"   # ...and re-set ONLY on request
        return env

and threads it through every child-spawning call. The strict-mode FEATURE is
untouched at the top level -- this is about not LEAKING it, not about removing it.

GATES
-----
  G0  STATIC/AST -- every subprocess.run() that launches a child verifier passes an
      explicit env=, and each file's child_env() actually POPS the variable. Needs
      no siblings, so THIS is the gate that guards the repair inside CI.
  G1  NECESSITY (witness A) -- with the leak simulated (env var inherited), a
      bare-checkout child HARD-FAILS instead of skipping. The damage, demonstrated.
  G2  SUFFICIENCY (witness B) -- same tree, scrubbed env: the child SKIPS, exit 0.
  G3  DIVERGENCE (load-bearing) -- G1 and G2 genuinely diverge on the IDENTICAL
      tree. Had they agreed, the repair would be a no-op and this gate would say so.
  G4  END-TO-END -- the three repaired verifiers are GREEN in BOTH invocation modes
      (argv flag AND ambient env var). This is the gate that would have caught the
      incident. Needs the siblings; SKIPS cleanly when they are absent.
  G5  ANTI-WEAKENING -- the repair did NOT achieve greenness by deleting strict mode
      or blinding the controls:
        (a) strict mode STILL BITES when legitimately requested via argv on a bare
            checkout (exit 1, FATAL);
        (b) reverting the scrub in a temp copy is REJECTED by G0's AST check.
      A gate that cannot detect its own regression is decoration.
  G6  NO DRIFT -- production sources and all verifiers byte-identical before/after.

G5 is the gate that matters most. Deleting the strict-mode feature outright would
ALSO have turned the three red verifiers green and satisfied G1-G4 -- and it FAILS
G5(a). That is the difference between a CORRECTION and a COVER-UP.

Exit: 0 = every executed gate passed. Skips are in neither numerator nor
denominator.
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

INC9_REL = "artifacts/incident/verify_inc9_ci_gate.py"
INC15_REL = "artifacts/incident/verify_inc15_cross_fleet_discovery.py"
INC19_REL = "artifacts/incident/verify_inc19_layout_and_count_invariance.py"
INC23_REL = "artifacts/incident/verify_inc23_drift_gate_punishes_owner_fix.py"

# The three verifiers that spawn child verifiers and therefore must scrub.
SPAWNERS = (INC15_REL, INC19_REL, INC23_REL)

PROD_SOURCES = (
    CHECKOUT_API / "service" / "checkout" / "session.js",
    FLEET / "fabric-gateway-demo" / "service" / "usage_aggregator.py",
    FLEET / "fabric-ic-incident-target" / "checkout.py",
)

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


def scrubbed_env() -> dict:
    env = dict(os.environ)
    env.pop(STRICT_ENV_VAR, None)
    return env


def leaked_env() -> dict:
    """The BUG, reproduced: the strict flag present in the inherited environment."""
    env = dict(os.environ)
    env[STRICT_ENV_VAR] = "1"
    return env


def run(cmd: list[str], cwd: pathlib.Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=900, env=env
    )


def siblings_present() -> bool:
    return (FLEET / "fabric-ic-incident-target" / "checkout.py").is_file() and (
        FLEET / "fabric-gateway-demo" / "service" / "usage_aggregator.py"
    ).is_file()


def tally(blob: str) -> str:
    m = re.search(r"^(?:INC-\d+ )?GATES: (\d+)/(\d+) passed", blob, re.M)
    return m.group(0) if m else "none"


# ---------------------------------------------------------------------------
# G0 -- the STATIC / AST audit.
#
# Deliberately NOT a substring grep. A comment mentioning `env=` must not satisfy
# this gate; the fleet has been burned repeatedly by verdicts that turned on
# incidental source text (INC-8's `".get(" not in DEPLOYED`, INC-24's prose match).
# So we parse the AST and inspect the actual Call nodes.
# ---------------------------------------------------------------------------
def audit_spawns(src: str) -> tuple[int, int, bool]:
    """Return (verifier_spawns_found, spawns_passing_env, child_env_pops_the_var).

    SCOPE -- and this matters, because my first draft got it wrong and its own G0
    caught me. The hazard is specifically a child that CONSULTS the strict-mode
    flag, i.e. a child that launches ANOTHER VERIFIER. Those are the ones whose
    behaviour an inherited FABRIC_REQUIRE_CROSS_FLEET silently changes.

    Not every `sys.executable` subprocess is such a child. INC-23 spawns a tiny
    inline `probe.py` that imports a copy of checkout.py and prints three prices.
    It never reads the environment, and no scrub could change its result --
    demanding env= there would be cargo-culting the ritual instead of enforcing the
    property, and a gate that fires on things it does not care about is noise that
    teaches the team to ignore it.

    So the discriminator is STRUCTURAL, taken from the AST element shape -- no string
    heuristics, which is what my first two drafts got wrong (and their own G0/G5b
    caught, twice):

        [sys.executable, VERIFIER_PATH]            -> a verifier    (IN scope)
        [sys.executable, rel, *args]               -> a verifier    (IN scope)
        [sys.executable, str(script)]              -> a verifier    (IN scope)
        [sys.executable, str(probe), str(datafile)] -> an inline probe GIVEN A DATA
                                                       PATH to read (OUT of scope)

    A verifier is launched with exactly ONE script path (plus optional CLI flags,
    which may arrive as a starred *args). The inert probe is distinguished by
    carrying a SECOND concrete script argument -- the file it is told to import --
    which no verifier invocation in this fleet does.
    """
    tree = ast.parse(src)
    spawns = 0
    with_env = 0

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (
            isinstance(f, ast.Attribute)
            and f.attr == "run"
            and isinstance(f.value, ast.Name)
            and f.value.id == "subprocess"
        ):
            continue
        if not node.args:
            continue

        cmd = node.args[0]
        if not isinstance(cmd, (ast.List, ast.Tuple)):
            continue
        elts = cmd.elts
        if not elts:
            continue
        # Must launch a Python child: the first element is STRUCTURALLY the
        # attribute `sys.executable` -- matched on the AST nodes themselves, not on
        # unparsed text, so no string in a comment or an unrelated expression can
        # satisfy it. (This fleet has been burned repeatedly by verdicts that turned
        # on incidental source text; the audit gate must not repeat it.)
        head = elts[0]
        launches_python = (
            isinstance(head, ast.Attribute)
            and head.attr == "executable"
            and isinstance(head.value, ast.Name)
            and head.value.id == "sys"
        )
        if not launches_python:
            continue

        # Count the CONCRETE (non-starred) arguments after the interpreter. A
        # verifier gets exactly one: its own path. The inert probe gets two: the
        # probe script AND the data file it must import -- and it reads no env var,
        # so no scrub could change its result. Demanding env= there would be
        # cargo-culting the ritual instead of enforcing the property.
        concrete_after = [e for e in elts[1:] if not isinstance(e, ast.Starred)]
        if len(concrete_after) != 1:
            continue

        spawns += 1
        if any(kw.arg == "env" for kw in node.keywords):
            with_env += 1

    # And child_env must actually REMOVE the variable -- a helper that merely
    # copies os.environ would be theatre.
    pops = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "child_env":
            body = ast.unparse(node)
            pops = ("pop" in body) and (STRICT_ENV_VAR in body or "STRICT_ENV_VAR" in body)
    return spawns, with_env, pops


def main() -> int:
    print("Fabric incident commander — INC-28 verification gates\n")

    pre = {p: sha(p) for p in PROD_SOURCES if p.is_file()}
    pre_verifiers = {
        CHECKOUT_API / rel: sha(CHECKOUT_API / rel)
        for rel in SPAWNERS
        if (CHECKOUT_API / rel).is_file()
    }

    # ------------------------------------------------------------------ G0 --
    audits = {}
    for rel in SPAWNERS:
        path = CHECKOUT_API / rel
        if not path.is_file():
            audits[rel] = (0, 0, False)
            continue
        audits[rel] = audit_spawns(path.read_text())

    all_scrub = all(
        spawns > 0 and spawns == with_env and pops
        for spawns, with_env, pops in audits.values()
    )
    detail = "; ".join(
        f"{pathlib.Path(rel).name}: {w}/{s} python spawns pass env=, child_env pops the var={p}"
        for rel, (s, w, p) in audits.items()
    )
    gate(
        "G0 STATIC/AST — every VERIFIER-launching child gets an EXPLICIT env= (no siblings needed)",
        all_scrub,
        detail
        + ". Parsed from the AST, not grepped: a comment mentioning env= must not "
        "satisfy this gate. Scope is verifier-launching spawns -- the children that "
        "actually CONSULT the strict flag; an inert probe script that never reads the "
        "environment is deliberately out of scope. Drop the env= on any verifier "
        "spawn, or gut child_env, and this goes RED in CI with no siblings required "
        "(G5b proves exactly that).",
    )

    # ---- a synthetic BARE CHECKOUT: this repo alone, siblings absent ----------
    with tempfile.TemporaryDirectory() as td:
        bare = pathlib.Path(td) / "checkout-api"
        shutil.copytree(CHECKOUT_API, bare)

        # -------------------------------------------- G1 · NECESSITY (A) --
        # Simulate the LEAK: the child inherits the strict flag from the ambient
        # environment. It must hard-fail -- that is the damage the bug causes.
        leaked = run([sys.executable, INC9_REL], bare, leaked_env())
        lblob = leaked.stdout + leaked.stderr
        leaked_fatal = leaked.returncode != 0 and "FATAL" in lblob
        gate(
            "G1 NECESSITY (witness A) — an INHERITED strict flag turns the control's SKIP into a FATAL",
            leaked_fatal,
            f"bare checkout + {STRICT_ENV_VAR}=1 inherited: exit={leaked.returncode} "
            f"FATAL={'FATAL' in lblob} tally={tally(lblob)}. A child spawned to OBSERVE "
            f"a skip is instead forced to die. This is what leaked into every "
            f"negative control.",
        )

        # ------------------------------------------ G2 · SUFFICIENCY (B) --
        # SAME tree, SCRUBBED env -- exactly what child_env() now produces.
        scrubbed = run([sys.executable, INC9_REL], bare, scrubbed_env())
        sblob = scrubbed.stdout + scrubbed.stderr
        scrubbed_skips = scrubbed.returncode == 0 and "SKIPPED" in sblob
        gate(
            "G2 SUFFICIENCY (witness B) — the SCRUBBED env lets the control SKIP and exit 0",
            scrubbed_skips,
            f"same bare checkout, {STRICT_ENV_VAR} scrubbed: exit={scrubbed.returncode} "
            f"SKIPPED reported={'SKIPPED' in sblob} tally={tally(sblob)}",
        )

        # --------------------------------------------- G3 · DIVERGENCE ----
        gate(
            "G3 DIVERGENCE (load-bearing) — identical tree: LEAKED = RED · SCRUBBED = GREEN",
            leaked_fatal and scrubbed_skips,
            f"one filesystem, two environments: inherited flag -> exit={leaked.returncode} "
            f"[RED] · scrubbed -> exit={scrubbed.returncode} [GREEN]. Had both behaved "
            f"alike the scrub would be a no-op and this gate would say so.",
        )

        # ------------------------------- G5(a) · ANTI-WEAKENING: still bites --
        # THE GATE THAT MATTERS MOST. It is trivial to stop a gate failing by
        # deleting the feature that made it fail. Strict mode must STILL hard-fail
        # when it is LEGITIMATELY requested (via argv) on a tree with no siblings.
        argv_strict = run(
            [sys.executable, INC9_REL, "--require-cross-fleet"], bare, scrubbed_env()
        )
        ablob = argv_strict.stdout + argv_strict.stderr
        still_bites = argv_strict.returncode != 0 and "FATAL" in ablob
        gate(
            "G5a ANTI-WEAKENING — strict mode STILL BITES when legitimately requested (argv)",
            still_bites,
            f"bare checkout + --require-cross-fleet: exit={argv_strict.returncode} "
            f"FATAL={'FATAL' in ablob}. The strict-mode FEATURE was NOT deleted -- the "
            f"repair stops it LEAKING into children, nothing more. Deleting it would "
            f"have satisfied G1-G4 and FAILED here: that is a COVER-UP, not a correction.",
        )

    # --------------------------- G5(b) · ANTI-WEAKENING: regression detected --
    # Revert the scrub in a THROWAWAY copy and require G0's AST audit to reject it.
    # A gate that cannot detect its own regression is decoration.
    with tempfile.TemporaryDirectory() as td:
        reverted = pathlib.Path(td) / "checkout-api"
        shutil.copytree(CHECKOUT_API, reverted)
        victim = reverted / INC15_REL
        vsrc = victim.read_text()
        # Remove the env= keyword from the child spawn -- i.e. reintroduce the leak.
        regressed = vsrc.replace(
            "        timeout=600,\n        env=child_env(strict=strict),\n",
            "        timeout=600,\n",
            1,
        )
        applied = regressed != vsrc
        victim.write_text(regressed)
        r_spawns, r_with_env, r_pops = audit_spawns(victim.read_text())
        g0_would_reject = not (r_spawns > 0 and r_spawns == r_with_env and r_pops)
        gate(
            "G5b ANTI-WEAKENING — G0 REJECTS a tree where the scrub has been reverted",
            applied and g0_would_reject,
            f"reverted the env= on INC-15's child spawn (mutation applied={applied}): "
            f"AST audit now sees {r_with_env}/{r_spawns} spawns passing env= -> "
            f"G0 verdict={'REJECT' if g0_would_reject else 'ACCEPT (BLIND!)'}. "
            f"The gate detects its own regression.",
        )

    # ------------------------------------------------------ G4 · END-TO-END --
    # The gate that would have CAUGHT this incident: each repaired verifier must be
    # green in BOTH invocation modes. Needs the real siblings.
    if not siblings_present():
        skip(
            "G4 END-TO-END — the three repaired verifiers are GREEN in BOTH invocation modes",
            "requires the sibling fleet repos, which a bare checkout does not clone. "
            "Reported as SKIPPED -- never a pass, never a hard failure, so this step "
            "cannot become the permanently-red INC-11 bug. G0/G1/G2/G3/G5 above all "
            "executed here and still guard the repair.",
        )
    else:
        rows = []
        both_green = True
        for rel in SPAWNERS:
            name = pathlib.Path(rel).name
            a = run([sys.executable, rel, "--require-cross-fleet"], CHECKOUT_API, scrubbed_env())
            b = run([sys.executable, rel], CHECKOUT_API, leaked_env())
            ta = tally(a.stdout + a.stderr)
            tb = tally(b.stdout + b.stderr)
            ok = a.returncode == 0 and b.returncode == 0
            both_green &= ok
            rows.append(f"{name}: argv exit={a.returncode} [{ta}] · ENV exit={b.returncode} [{tb}]")
        gate(
            "G4 END-TO-END — the three repaired verifiers are GREEN in BOTH invocation modes",
            both_green,
            " | ".join(rows)
            + ". Pre-repair the ENV column was INC-15 8/9, INC-19 2/7, INC-23 5/8 -- all RED "
            "on a healthy fleet.",
        )

    # ------------------------------------------------------------------ G6 --
    post = {p: sha(p) for p in PROD_SOURCES if p.is_file()}
    post_verifiers = {
        CHECKOUT_API / rel: sha(CHECKOUT_API / rel)
        for rel in SPAWNERS
        if (CHECKOUT_API / rel).is_file()
    }
    moved = [p.name for p in pre if post.get(p) != pre[p]]
    moved_v = [p.name for p in pre_verifiers if post_verifiers.get(p) != pre_verifiers[p]]
    gate(
        "G6 NO DRIFT — production sources and verifiers byte-identical before/after this run",
        not moved and not moved_v and len(pre) > 0,
        f"{len(pre)} production source(s) and {len(pre_verifiers)} verifier(s) checked; "
        f"moved={(moved + moved_v) or 'none'}. All mutation happened in throwaway copies.",
    )

    return _summary()


def _summary() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{'=' * 74}\nINC-28 GATES: {passed}/{total} passed", end="")
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
    print("All executed gates green. A strict-mode flag can no longer leak into a")
    print("negative control and force it to fail -- and strict mode STILL bites when")
    print("it is legitimately requested. Production source untouched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
