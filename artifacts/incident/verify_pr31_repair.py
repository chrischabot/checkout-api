#!/usr/bin/env python3
"""INC-31 regression guard -- the strict-mode flag must not leak into children.

This verifier was written to check the INC-31 repair WITHOUT trusting the prose of
the PR that proposed it (#31). The repair has since MERGED, so this now runs as a
PERMANENT REGRESSION GUARD, and its gates are phrased accordingly:

  G0/G0b PRESENCE  -- every python-launching spawn on this tree passes `env=`.
                      Strip the scrub and this reddens. (An empty denominator is
                      a HARD FAILURE: a gate that inspected nothing is blind, not
                      passing.)
  G1  NECESSITY    -- REVERT the scrub in a throwaway copy, and the two spellings
                      of the strict flag DIVERGE again. This is how necessity
                      stays witnessable after the fix has landed -- otherwise the
                      gate would assert a property of a tree that no longer exists.
  G2  SUFFICIENCY  -- as shipped, the two spellings AGREE.
  G3  DIVERGENCE   -- identical tree: leaked = RED, scrubbed = GREEN. Proves the
                      repair is load-bearing and not decoration.
  G4  ANTI-WEAKENING -- strict mode STILL HARD-FAILS when legitimately requested.
                      This is what separates a CORRECTION from a COVER-UP: simply
                      DELETING strict mode would ALSO close the divergence and
                      would satisfy G1-G3. It must be rejected here.
  G4b THE COVER-UP IS REAL -- deleting strict mode is demonstrated to close the
                      divergence too, which is exactly why G4 must exist.

No production source is touched: every mutation happens in a temp copy.
Exit 0 only if every gate holds.
"""
import ast
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

STRICT = "FABRIC_REQUIRE_CROSS_FLEET"

# Locate the repo STRUCTURALLY, from this file's own position, rather than
# assuming a working directory. A verifier whose verdict depends on where it was
# launched from is the very class of defect INC-31 exists to cure.
REPO = pathlib.Path(__file__).resolve().parents[2]
INC = "artifacts/incident"

PATCHED = [
    "verify_inc12_ci_runs_verifier.py",
    "verify_inc15_cross_fleet_discovery.py",
    "verify_inc18_gate_punishes_remediation.py",
    "verify_inc19_layout_and_count_invariance.py",
    "verify_inc23_drift_gate_punishes_owner_fix.py",
]

# The verifiers that were observed to diverge in the fleet workspace.
DIVERGENT = [
    "verify_inc15_cross_fleet_discovery.py",
    "verify_inc19_layout_and_count_invariance.py",
    "verify_inc23_drift_gate_punishes_owner_fix.py",
]

RESULTS = []
SKIPPED = []


def gate(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    tag = "PASS" if ok else "FAIL"
    print("[" + tag + "] " + name + ("  -- " + detail if detail else ""))


def skip(name, why):
    """A gate that CANNOT run is SKIPPED -- never passed, never failed.

    A skip lands in neither the numerator nor the denominator. Laundering a skip
    into a pass is the INC-15 defect; hard-failing a gate that structurally
    cannot run on this tree is the INC-11 defect (a permanently-red gate). Both
    teach the team to ignore CI, so this reports the third, honest state.
    """
    SKIPPED.append((name, why))
    print("[SKIP] " + name + "  -- " + why)


# The reconstructed PR-31 repair, injected as a subprocess shim. It scrubs the
# strict flag from CHILD environments only; the parent's own view is untouched,
# so the strict-mode FEATURE keeps working.
SHIM_LINES = [
    "",
    "# --- injected: the PR #31 repair, reconstructed by the commander ---",
    "import os as _os_ic",
    "import subprocess as _sp_ic",
    "_STRICT_IC = " + repr(STRICT),
    "_real_run_ic = _sp_ic.run",
    "def _child_env_ic():",
    "    _e = dict(_os_ic.environ)",
    "    _e.pop(_STRICT_IC, None)",
    "    return _e",
    "def _run_ic(*_a, **_kw):",
    "    if 'env' not in _kw:",
    "        _kw['env'] = _child_env_ic()",
    "    return _real_run_ic(*_a, **_kw)",
    "_sp_ic.run = _run_ic",
    "# --- end injection ---",
    "",
]


def base_env():
    e = dict(os.environ)
    e.pop(STRICT, None)
    return e


def count_spawns(tree):
    """STRUCTURAL count of python-launching spawns, plus how many lack env=.

    An empty denominator is a HARD FAILURE: a gate that inspected nothing is
    blind, not passing.
    """
    total = 0
    unscrubbed = 0
    per = {}
    for name in PATCHED:
        tree_src = (tree / INC / name).read_text()
        node_tree = ast.parse(tree_src)
        n = 0
        for node in ast.walk(node_tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "run":
                if "sys.executable" in ast.unparse(node):
                    n += 1
                    if not any(k.arg == "env" for k in node.keywords):
                        unscrubbed += 1
        per[name] = n
        total += n
    return total, unscrubbed, per


def inject_scrub(tree):
    for name in PATCHED:
        p = tree / INC / name
        lines = p.read_text().split("\n")
        idx = 0
        for i, ln in enumerate(lines[:150]):
            if re.match(r"^(import |from )\w", ln):
                idx = i + 1
        p.write_text("\n".join(lines[:idx] + SHIM_LINES + lines[idx:]))


def strip_scrub(tree):
    """REVERT the landed repair in a throwaway copy: drop every `env=child_env()`
    keyword so the flag leaks again. This is how NECESSITY stays witnessable
    after the fix has merged -- otherwise the gate would be asserting a property
    of a tree that no longer exists."""
    for name in PATCHED:
        p = tree / INC / name
        src = p.read_text()
        src = src.replace("env=child_env(),", "")
        src = src.replace("env=child_env()", "")
        p.write_text(src)


def delete_strict_mode(tree):
    """THE COVER-UP: neuter strict mode. This ALSO closes the divergence -- which
    is precisely why closing the divergence cannot be the only evidence."""
    for name in PATCHED + ["verify_inc9_ci_gate.py"]:
        p = tree / INC / name
        if not p.exists():
            continue
        src = p.read_text()
        src = src.replace('os.environ.get("' + STRICT + '")', "None")
        src = src.replace("os.environ.get('" + STRICT + "')", "None")
        src = src.replace('"--require-cross-fleet" in sys.argv', "False")
        src = src.replace("'--require-cross-fleet' in sys.argv", "False")
        p.write_text(src)


def fresh_copy():
    td = pathlib.Path(tempfile.mkdtemp(prefix="ic-pr31-"))
    shutil.copytree(REPO, td / REPO.name, ignore=shutil.ignore_patterns(".git"))
    for sib in ("fabric-gateway-demo", "fabric-ic-incident-target"):
        s = REPO.parent / sib
        if s.exists():
            shutil.copytree(s, td / sib, ignore=shutil.ignore_patterns(".git"))
    return td, td / REPO.name


def run_modes(tree, verifier):
    """(argv_exit, env_exit) for the SAME verifier on the SAME tree."""
    cwd = tree / INC
    a = subprocess.run([sys.executable, verifier, "--require-cross-fleet"],
                       cwd=str(cwd), capture_output=True, text=True,
                       timeout=900, env=base_env())
    e = subprocess.run([sys.executable, verifier],
                       cwd=str(cwd), capture_output=True, text=True,
                       timeout=900, env=dict(base_env(), **{STRICT: "1"}))
    return a.returncode, e.returncode


# ------------------------------------------------------------- G0 static/AST
total, unscrubbed, per = count_spawns(REPO)
gate("G0 STATIC/AST -- there are python-launching spawns to audit "
     "(an empty denominator would be BLIND, not passing)",
     total > 0,
     str(total) + " spawns: " + ", ".join(k.split("_")[1] + "=" + str(v)
                                          for k, v in per.items()))

# POST-MERGE SEMANTICS: once the repair has landed, `main` must be FULLY
# scrubbed. Pre-merge this gate asserted the opposite (7/7 unscrubbed) to prove
# the repair was still needed. Now it guards against REGRESSION: if anyone strips
# the scrub, this goes red.
gate("G0b the repair is PRESENT on this tree -- every python spawn passes env= "
     "(strip the scrub and this reddens)",
     total > 0 and unscrubbed == 0,
     str(total - unscrubbed) + "/" + str(total) + " python spawns scrubbed")

# ------------------------------------------------------------- G1 necessity
# NECESSITY is proven against a tree with the repair DELIBERATELY REVERTED,
# because post-merge `main` is (correctly) no longer divergent.
#
# BUT: the divergence only EXISTS when the sibling repos are present -- strict
# mode diverges precisely because it demands siblings that a leaked flag makes
# the child require. On a BARE CHECKOUT (exactly what this repo's CI clones)
# there are no siblings, so the divergence CANNOT be reproduced. Hard-failing
# here would make this gate PERMANENTLY RED in CI on a perfectly healthy tree --
# the INC-11 disease, re-committed by the very verifier that polices it.
#
# So when the siblings are absent, G1/G3 SKIP. G0/G0b/G4/G4b still run and still
# bite: strip the scrub and G0b reddens IN CI, with no siblings required.
SIBLINGS = [REPO.parent / s for s in
            ("fabric-gateway-demo", "fabric-ic-incident-target")]
HAVE_SIBLINGS = all(s.exists() for s in SIBLINGS)

pre = []
if HAVE_SIBLINGS:
    td0, copy0 = fresh_copy()
    strip_scrub(copy0)
    for v in DIVERGENT:
        a, e = run_modes(copy0, v)
        if a != e:
            pre.append((v, a, e))
    gate("G1 NECESSITY -- revert the scrub and the same intent spelled two ways "
         "gives DIFFERENT verdicts again",
         len(pre) > 0,
         "; ".join(v.split("_")[1] + ": argv=" + str(a) + " env=" + str(e)
                  for v, a, e in pre) or "no divergence found")
    shutil.rmtree(td0, ignore_errors=True)
else:
    skip("G1 NECESSITY",
         "sibling fleet repos absent (bare checkout) -- the strict-mode "
         "divergence structurally cannot occur without them, so this witness "
         "cannot run here. NOT a pass: G0b still enforces the repair in CI.")

# --------------------------------------------- G2 sufficiency / G3 divergence
td1, copy1 = fresh_copy()
post = []
for v in DIVERGENT:
    a, e = run_modes(copy1, v)
    if a != e:
        post.append((v, a, e))

gate("G2 SUFFICIENCY -- with the child_env scrub in place, both spellings AGREE",
     len(post) == 0,
     "all " + str(len(DIVERGENT)) + " agree" if not post
     else str(len(post)) + " divergence(s) remain")

if HAVE_SIBLINGS:
    gate("G3 DIVERGENCE (load-bearing) -- identical tree: leaked=RED, scrubbed=GREEN",
         len(pre) > 0 and len(post) == 0,
         "PRE " + str(len(pre)) + " divergent -> POST " + str(len(post)) + " divergent")
else:
    skip("G3 DIVERGENCE",
         "depends on G1's witness, which needs the sibling repos (bare checkout)")

# ---------------------------------------------------------- G4 anti-weakening
# (a) the cover-up ALSO closes the divergence -> closing it is not sufficient proof
td2, copy2 = fresh_copy()
delete_strict_mode(copy2)
cover = []
for v in DIVERGENT:
    a, e = run_modes(copy2, v)
    if a != e:
        cover.append(v)

# (b) on the CORRECTLY scrubbed tree, strict mode must STILL BITE when asked for
td3, copy3 = fresh_copy()
for sib in ("fabric-gateway-demo", "fabric-ic-incident-target"):
    p = copy3.parent / sib
    if p.exists():
        shutil.rmtree(p)
bare = subprocess.run([sys.executable, "verify_inc15_cross_fleet_discovery.py",
                       "--require-cross-fleet"],
                      cwd=str(copy3 / INC), capture_output=True, text=True,
                      timeout=900, env=base_env())

gate("G4 ANTI-WEAKENING -- the scrub KEEPS strict mode's teeth: legitimately "
     "requested strict mode, siblings genuinely absent, STILL hard-fails",
     bare.returncode != 0,
     "scrubbed tree + argv strict + no siblings -> exit " + str(bare.returncode) +
     " (a cover-up that DELETED strict mode would exit 0 here)")

gate("G4b the COVER-UP is real -- deleting strict mode ALSO closes the divergence, "
     "which is exactly why G4 must exist",
     len(cover) == 0,
     "divergence closes under deletion too -> closing it alone proves nothing")

for td in (td1, td2, td3):
    shutil.rmtree(td, ignore_errors=True)

passed = sum(1 for _, ok, _ in RESULTS if ok)
total_run = len(RESULTS)
print("\nGATES: " + str(passed) + "/" + str(total_run) + " passed"
      + (", " + str(len(SKIPPED)) + " SKIPPED" if SKIPPED else ""))
if SKIPPED:
    print("       (a SKIP is in NEITHER the numerator nor the denominator)")
if passed == total_run:
    print("\nVERDICT: the INC-31 repair is PRESENT"
          + (", NECESSARY, SUFFICIENT, and NOT a cover-up." if HAVE_SIBLINGS
             else " and enforced. Strip the scrub and G0b reddens."))
    sys.exit(0)
print("\nVERDICT: verification FAILED")
sys.exit(1)
