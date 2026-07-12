#!/usr/bin/env python3
"""Fabric incident commander — INC-30 verifier.

THE FINDING (established by an INDEPENDENT DETECTOR, not by reading a write-up)
------------------------------------------------------------------------------
Strict cross-fleet mode can be requested two ways, and they are supposed to MEAN
THE SAME THING:

    python3 verify_x.py --require-cross-fleet          (argv)
    FABRIC_REQUIRE_CROSS_FLEET=1 python3 verify_x.py   (environment)

A cross-mode sweep of every verifier in the fleet (10 verifiers x 3 invocation
modes, run in BOTH the full fleet workspace and a synthetic bare checkout) found
that five verifiers gave a DIFFERENT VERDICT depending on WHICH SPELLING was used:

    FULL FLEET WORKSPACE            argv-strict    env-strict
      verify_inc15                  exit 0         exit 1
      verify_inc19                  exit 0         exit 1
      verify_inc23                  exit 0         exit 1

    BARE CHECKOUT (= what CI clones)
      verify_inc12                  exit 0         exit 1
      verify_inc18                  exit 0         exit 1

> A verdict that depends on HOW the request was spelled is not a verdict.

ROOT CAUSE
----------
Not one of the verifier-launching `subprocess.run()` calls passed `env=`. Python
hands the child the parent's ENTIRE environment, so the strict flag LEAKS into
child verifier processes:

  * inc12/inc18 spawn `verify_inc9_ci_gate.py` to ask ONE question -- "does the
    shipped INC-9 verifier pass on this tree?" In a bare checkout INC-9 has no
    sibling repos, so it correctly SKIPs its cross-fleet gates and exits 0. With
    the flag leaked in, the child is FORCED into strict mode and hard-fails for
    want of siblings that are LEGITIMATELY ABSENT. The parent then reports
    "INC-9 does not pass" -- which is FALSE, and has nothing to do with the
    property being tested.

  * inc15/inc19/inc23 spawn children as NEGATIVE CONTROLS against synthetic bare
    trees, and the control REQUIRES the child to SKIP and exit 0. A negative
    control that inherits the very flag it is controlling for IS NOT A CONTROL.

THE RULE
--------
> An intent must be PASSED to the child that should receive it,
> never INHERITED by a child that must not.

THE REPAIR: a `child_env()` helper in each file that ALWAYS scrubs the variable
and re-sets it ONLY on explicit request, threaded through every verifier-launching
spawn. The strict-mode FEATURE is untouched at the top level -- this stops the flag
LEAKING, it does not remove it.

GATES
-----
  G0  STATIC/AST — every verifier-launching spawn carries an explicit `env=`, and
      each `child_env` genuinely POPS the variable. Resolved through the AST, and
      names bound to a verifier path are followed, so a target hidden behind a
      VARIABLE (e.g. `[sys.executable, str(inc9)]`) cannot escape the audit.
  G1  NECESSITY — with the leak restored, env-strict DIVERGES from argv-strict.
  G2  SUFFICIENCY — as shipped, the two strict modes AGREE everywhere.
  G3  DIVERGENCE (load-bearing) — same tree: leaked = divergent, scrubbed = clean.
  G4  ANTI-WEAKENING — strict mode STILL HARD-FAILS when legitimately requested
      via argv on a bare checkout. Deleting the feature would also have satisfied
      G1-G3; it fails HERE. This is what makes the change a CORRECTION, not a
      COVER-UP.
  G5  SELF-REGRESSION — reverting the scrub is REJECTED by G0's own AST audit.
  G6  NO DRIFT — the three deployed production sources are byte-identical
      before/after this verifier runs. All mutation happens in throwaway copies.

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

ROOT = pathlib.Path(__file__).resolve().parent
STRICT_VAR = "FABRIC_REQUIRE_CROSS_FLEET"
STRICT_FLAG = "--require-cross-fleet"


def _locate_checkout_api() -> pathlib.Path:
    """Find the checkout-api repo root from WHEREVER this file is executed.

    This verifier is shipped in TWO places -- the commander workspace root (where
    the fleet repos are siblings) and inside `checkout-api/artifacts/incident/`
    (where CI runs it against a BARE CHECKOUT of that repo alone). A verifier that
    only runs from its author's directory is not a verifier, so resolve
    structurally rather than assuming a layout.
    """
    here = pathlib.Path(__file__).resolve()
    marker = pathlib.Path("service") / "checkout" / "session.js"
    candidates = [
        here.parent / "checkout-api",        # commander workspace root
        here.parents[2] if len(here.parents) > 2 else here.parent,  # in-repo
        here.parent / "fleet" / "checkout-api",
    ]
    for c in candidates:
        if (c / marker).is_file():
            return c
    raise SystemExit(f"cannot locate the checkout-api repo root from {here}")


CHECKOUT_API = _locate_checkout_api()
FLEET = CHECKOUT_API.parent
GATEWAY = FLEET / "fabric-gateway-demo"
TARGET = FLEET / "fabric-ic-incident-target"

# The five files the detector implicated, and which the repair touches.
PATCHED = [
    "verify_inc12_ci_runs_verifier.py",
    "verify_inc15_cross_fleet_discovery.py",
    "verify_inc18_gate_punishes_remediation.py",
    "verify_inc19_layout_and_count_invariance.py",
    "verify_inc23_drift_gate_punishes_owner_fix.py",
]

# Deployed production sources. G6 asserts we hand these back untouched.
# On a bare checkout the sibling sources are legitimately absent -- an absent file
# is a different ENVIRONMENT, not drift, so only what exists here is hashed.
PROD = [
    CHECKOUT_API / "service" / "checkout" / "session.js",
    GATEWAY / "service" / "usage_aggregator.py",
    TARGET / "checkout.py",
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


def clean_env() -> dict:
    e = dict(os.environ)
    e.pop(STRICT_VAR, None)
    return e


def run(script: pathlib.Path, cwd: pathlib.Path, *, argv_strict=False,
        env_strict=False, timeout=900) -> tuple[int, str]:
    cmd = [sys.executable, str(script)]
    if argv_strict:
        cmd.append(STRICT_FLAG)
    env = clean_env()
    if env_strict:
        env[STRICT_VAR] = "1"
    try:
        p = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"


# ---------------------------------------------------------------------------
# G0 — the STATIC/AST audit.
#
# WHY THE AST, AND WHY THIS IS STRUCTURAL RATHER THAN NAME-BASED.
#
# THE GATE CAUGHT ITS OWN AUTHOR. The first draft of this audit resolved local
# NAMES bound to a verifier path (to catch `[sys.executable, str(inc9)]`, where the
# target hides behind a variable). It reported a confident
#
#     4/4 verifier spawns scrubbed across 5 files
#
# ...and 4 was WRONG. The true total is SEVEN (verify_inc12 alone has 2).
# The audit was BLIND to `verify_inc19` and `verify_inc23`, whose spawn targets
# arrive as FUNCTION PARAMETERS -- `run(cwd, rel, *args)` and `run(script, cwd)` --
# and are therefore bound to no resolvable name at all. It would have certified
# both files as clean while they were fully unscrubbed.
#
# A gate that sees nothing is not a passing gate; it is a blind one. That is this
# fleet's signature disease, and I had just re-committed it inside the verifier
# written to cure it. Caught only because the reported denominator (4) contradicted
# a count taken from a different gate (G5 saw 2 spawns in one file).
#
# THE FIX: stop trying to infer WHICH script is being launched, which is an
# unwinnable name-resolution arms race. Ask a STRUCTURAL question instead:
#
#     does this spawn launch a PYTHON INTERPRETER (`sys.executable`)?
#
# Every child that could possibly read FABRIC_REQUIRE_CROSS_FLEET is a Python
# process, and every Python process here is spawned via sys.executable. No
# parameter, alias, or f-string can hide that. `npm test` spawns are correctly
# excluded -- npm reads no strict flag.
# ---------------------------------------------------------------------------
def verifier_spawn_audit(src: str) -> tuple[int, int, list[str]]:
    """Return (python_spawns_found, spawns_with_env, notes).

    Any spawn of a Python interpreter must carry an explicit `env=`. That is the
    invariant: a child Python process must never INHERIT the strict flag.
    """
    tree = ast.parse(src)

    found = 0
    with_env = 0
    notes: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = ast.unparse(node.func)
        if fn not in ("subprocess.run", "subprocess.Popen", "subprocess.check_output"):
            continue
        if not node.args:
            continue

        argv_src = ast.unparse(node.args[0])

        # STRUCTURAL: is a Python interpreter being launched? Nothing about the
        # script's NAME is consulted, so a target behind a variable, a function
        # parameter, an alias, or an f-string cannot escape this audit.
        if "sys.executable" not in argv_src:
            continue

        found += 1
        has_env = any(kw.arg == "env" for kw in node.keywords)
        if has_env:
            with_env += 1
        else:
            notes.append(
                f"UNSCRUBBED python spawn (inherits the parent env): "
                f"subprocess.run({argv_src[:70]}...) has no env="
            )

    return found, with_env, notes


def child_env_pops_var(src: str) -> bool:
    """Does child_env genuinely POP the strict variable?

    Matched on the `.pop()` CALL NODE with its argument RESOLVED -- not on the
    literal string. The body pops the module constant STRICT_ENV_VAR, so the raw
    string "FABRIC_REQUIRE_CROSS_FLEET" never appears inside the function; a
    literal-substring check would report False on a helper that demonstrably
    works. Judging code by its incidental spelling is precisely the disease this
    fleet keeps rediscovering.
    """
    tree = ast.parse(src)

    # What is STRICT_ENV_VAR bound to at module level?
    const_names = {
        t.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and node.value.value == STRICT_VAR
        for t in node.targets
        if isinstance(t, ast.Name)
    }

    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "child_env"):
            continue
        for inner in ast.walk(node):
            if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "pop" and inner.args):
                continue
            arg = inner.args[0]
            if isinstance(arg, ast.Constant) and arg.value == STRICT_VAR:
                return True
            if isinstance(arg, ast.Name) and arg.id in const_names:
                return True
    return False


def make_bare(repo: pathlib.Path, parent: pathlib.Path) -> pathlib.Path:
    """A dir containing ONLY this repo -- what its CI actually clones.

    `parent` must be a DISTINCT directory per witness: the necessity witness and
    the sufficiency witness need INDEPENDENT copies (one gets the leak restored,
    one stays as shipped). Sharing a destination would have them overwrite each
    other and the "divergence" would be an artifact of the harness rather than of
    the code -- a witness that proves the wrong thing, which is the very failure
    class this fleet exists to hunt.
    """
    parent.mkdir(parents=True, exist_ok=True)
    dest = parent / repo.name
    shutil.copytree(repo, dest, ignore=shutil.ignore_patterns(".git", "node_modules"))
    return dest


def make_fleet(tmp: pathlib.Path) -> pathlib.Path:
    fleet = tmp / "fleet"
    fleet.mkdir()
    for r in (CHECKOUT_API, GATEWAY, TARGET):
        shutil.copytree(r, fleet / r.name,
                        ignore=shutil.ignore_patterns(".git", "node_modules"))
    return fleet


def strip_scrub(path: pathlib.Path) -> None:
    """Revert the INC-30 repair in a COPY: remove every `env=child_env(...)`."""
    src = path.read_text()
    src = src.replace("env=child_env(strict=True),", "")
    src = src.replace("env=child_env(),", "")
    src = src.replace("cwd=str(cwd), env=child_env(),", "cwd=str(cwd),")
    path.write_text(src)


def main() -> int:
    print("Fabric incident commander — INC-30 verification gates")
    print("(a strict-mode env var leaked into child verifiers: the verdict")
    print(" depended on HOW strict mode was requested)\n")

    pre = {p: sha(p) for p in PROD if p.is_file()}

    # ------------------------------------------------------------------ G0 --
    total_spawns = 0
    total_env = 0
    all_notes: list[str] = []
    pops_ok: list[str] = []
    for name in PATCHED:
        f = CHECKOUT_API / "artifacts" / "incident" / name
        if not f.is_file():
            all_notes.append(f"{name}: MISSING")
            continue
        src = f.read_text()
        found, with_env, notes = verifier_spawn_audit(src)
        total_spawns += found
        total_env += with_env
        all_notes.extend(f"{name}: {n}" for n in notes)
        if child_env_pops_var(src):
            pops_ok.append(name)
        else:
            all_notes.append(f"{name}: child_env does NOT pop {STRICT_VAR}")

    # A blind audit is worse than no audit: it certifies coverage it does not have.
    # The first draft of this gate reported "4/4" while being unable to SEE the
    # spawns in verify_inc19 and verify_inc23 (their targets arrive as function
    # parameters). So the expected spawn count is asserted EXACTLY here: if the
    # audit's denominator drops below what the fleet actually contains, THAT is a
    # failure, not a pass -- and if a NEW spawn appears, the gate reddens until it
    # is accounted for, rather than silently tolerating it.
    #
    # The count is MEASURED, not guessed: verify_inc12=2, verify_inc15=1,
    # verify_inc18=1, verify_inc19=1, verify_inc23=2  ->  7.
    EXPECTED_SPAWNS = 7
    gate(
        "G0 STATIC/AST — every python-launching spawn carries env=, and child_env pops the var",
        total_spawns == EXPECTED_SPAWNS
        and total_spawns == total_env
        and len(pops_ok) == len(PATCHED)
        and not all_notes,
        f"{total_env}/{total_spawns} python spawns scrubbed across {len(PATCHED)} files "
        f"(exactly {EXPECTED_SPAWNS} expected: inc12=2, inc15=1, inc18=1, inc19=1, "
        f"inc23=2 -- asserting the DENOMINATOR so a BLIND audit cannot masquerade as a "
        f"clean one, and a newly-added spawn cannot slip in unscrubbed); child_env pops "
        f"{STRICT_VAR} in {len(pops_ok)}/{len(PATCHED)} files. The audit is STRUCTURAL "
        f"(it asks 'does this spawn launch sys.executable?'), so a target behind a "
        f"variable, a function parameter, or an alias cannot hide from it. "
        + (f"PROBLEMS: {all_notes}" if all_notes else "No unscrubbed python spawns."),
    )

    detector = ROOT / "detect_mode_divergence.py"
    if not detector.is_file() and not (CHECKOUT_API / "artifacts" / "incident").is_dir():
        skip("G1-G5 behavioural witnesses", "cannot locate the verifiers to exercise")
        return _summary()

    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)

        # The two child verifiers that CI actually runs on a bare checkout, and that
        # the detector implicated there. Each witness gets its OWN isolated copy.
        bare = make_bare(CHECKOUT_API, tmp / "shipped")
        inc12 = bare / "artifacts" / "incident" / "verify_inc12_ci_runs_verifier.py"
        inc18 = bare / "artifacts" / "incident" / "verify_inc18_gate_punishes_remediation.py"

        # ----------------------------------------------- G2 · SUFFICIENCY ----
        # As shipped: on a bare checkout the two strict modes must AGREE.
        a12, _ = run(inc12, bare, argv_strict=True)
        e12, _ = run(inc12, bare, env_strict=True)
        a18, _ = run(inc18, bare, argv_strict=True)
        e18, _ = run(inc18, bare, env_strict=True)
        gate(
            "G2 SUFFICIENCY — as shipped, argv-strict and env-strict AGREE (bare checkout)",
            a12 == e12 and a18 == e18,
            f"inc12: argv={a12} env={e12} (agree={a12 == e12}) · "
            f"inc18: argv={a18} env={e18} (agree={a18 == e18}). "
            f"Pre-repair these were argv=0 / env=1 -- a verdict that changed with the spelling.",
        )

        # ------------------------------------------------- G1 · NECESSITY ----
        # Restore the leak in a THROWAWAY copy and watch the divergence come back.
        leaked = make_bare(CHECKOUT_API, tmp / "leaked")
        l12 = leaked / "artifacts" / "incident" / "verify_inc12_ci_runs_verifier.py"
        l18 = leaked / "artifacts" / "incident" / "verify_inc18_gate_punishes_remediation.py"
        strip_scrub(l12)
        strip_scrub(l18)
        la12, _ = run(l12, leaked, argv_strict=True)
        le12, _ = run(l12, leaked, env_strict=True)
        la18, _ = run(l18, leaked, argv_strict=True)
        le18, _ = run(l18, leaked, env_strict=True)
        necessity = (la12 != le12) and (la18 != le18)
        gate(
            "G1 NECESSITY — with the leak RESTORED, the two strict modes DIVERGE again",
            necessity,
            f"leak restored: inc12 argv={la12} env={le12} (diverge={la12 != le12}) · "
            f"inc18 argv={la18} env={le18} (diverge={la18 != le18}). "
            f"The bare-checkout gate hard-fails ONLY because the child inherited the flag "
            f"and demanded siblings that CI legitimately does not clone.",
        )

        # ------------------------------------- G3 · DIVERGENCE (load-bearing) -
        gate(
            "G3 DIVERGENCE (load-bearing) — identical tree: leaked = DIVERGENT · scrubbed = CLEAN",
            necessity and (a12 == e12) and (a18 == e18),
            f"same repo, same inputs. LEAKED: inc12 {la12}/{le12}, inc18 {la18}/{le18} "
            f"-> divergent. SCRUBBED: inc12 {a12}/{e12}, inc18 {a18}/{e18} -> agree. "
            f"Had both behaved alike, the repair would be a no-op and this gate would say so.",
        )

        # --------------------------------------------- G4 · ANTI-WEAKENING ----
        # THE GATE THAT MATTERS MOST.
        #
        # Simply DELETING strict mode would ALSO have made every red go green and
        # would have satisfied G1/G2/G3. It must FAIL here. Strict mode, when
        # LEGITIMATELY requested via argv on a tree with no siblings, MUST still
        # hard-fail -- that is the whole point of the feature.
        inc15 = bare / "artifacts" / "incident" / "verify_inc15_cross_fleet_discovery.py"
        rc_default, _ = run(inc15, bare)
        rc_strict, blob_strict = run(inc15, bare, argv_strict=True)
        rc_strict_env, blob_env = run(inc15, bare, env_strict=True)
        still_bites = (
            rc_default == 0
            and rc_strict == 1
            and rc_strict_env == 1
            and "FATAL" in blob_strict
            and "FATAL" in blob_env
        )
        gate(
            "G4 ANTI-WEAKENING — strict mode STILL hard-fails when legitimately requested",
            still_bites,
            f"bare checkout, INC-15: default exit={rc_default} (SKIPs cross-fleet gates, "
            f"correct) · argv-strict exit={rc_strict} FATAL={'FATAL' in blob_strict} · "
            f"env-strict exit={rc_strict_env} FATAL={'FATAL' in blob_env}. "
            f"BOTH spellings now hard-fail together -- the feature is intact and still "
            f"bites. DELETING strict mode would have satisfied G1-G3 and FAILED HERE. "
            f"That is the difference between a CORRECTION and a COVER-UP.",
        )

        # -------------------------------------------- G5 · SELF-REGRESSION ----
        # Reverting the scrub must be REJECTED by G0's own AST audit.
        reverted_src = l12.read_text()
        f_, e_, notes_ = verifier_spawn_audit(reverted_src)
        gate(
            "G5 SELF-REGRESSION — reverting the scrub is REJECTED by G0's AST audit",
            f_ > 0 and e_ < f_ and bool(notes_),
            f"scrub stripped from verify_inc12: audit sees {e_}/{f_} spawns scrubbed "
            f"-> verdict=REJECT ({len(notes_)} unscrubbed spawn(s) reported). "
            f"The gate detects its own regression.",
        )

    # ------------------------------------------------------------------ G6 --
    post = {p: sha(p) for p in PROD if p.is_file()}
    moved = [p.name for p in pre if post.get(p) != pre[p]]
    gate(
        "G6 NO PRODUCTION DRIFT — deployed sources byte-identical before/after",
        not moved and len(pre) > 0,
        f"{len(pre) - len(moved)}/{len(pre)} byte-identical on the FULL sha256; "
        f"moved={moved or 'none'}. All mutation testing happened in throwaway copies. "
        + "; ".join(f"{p.name}={post[p][:12]}" for p in pre),
    )

    return _summary()


def _summary() -> int:
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
    print("All executed gates green. A strict-mode intent is now PASSED to the child")
    print("that should receive it, never INHERITED by a child that must not -- and")
    print("strict mode still hard-fails when it is legitimately requested.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
