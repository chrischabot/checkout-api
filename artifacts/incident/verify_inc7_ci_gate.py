#!/usr/bin/env python3
"""INC-7 verifier -- Fabric autonomous incident commander (run 2026-07-14).

The patch this run applies is a CI workflow for `checkout-api`. That repo
carries a merged, passing 10-test regression suite on `main` -- and NOTHING
executes it. The guard is dead code. This verifier proves the patch turns it
into a live guard, and proves it is safe.

A CI workflow that cannot fail is decoration. So the load-bearing gates here
are G5/G6: the suite must go GREEN on the real code and RED on the exact
defect that caused INC-1. If it cannot fail, the patch is worthless.

  G1  production source is byte-for-byte IDENTICAL to upstream main, and
      ci.yml is genuinely absent upstream (verified against raw.githubusercontent,
      NOT against local git -- the clone carries no .git directory, so a
      `git status` check here would be vacuous and would silently "pass")
  G2  ci.yml is valid YAML
  G3  ci.yml triggers on pull_request AND push:[main]
  G4  ci.yml invokes package.json's REAL test script (not an invented one)
  G5  the suite is GREEN against deployed HEAD                  (guard works)
  G6  MUTATION: reintroduce the INC-1 unguarded read -> suite goes RED,
      reproducing the exact production error TypeError: Cannot read
      properties of null (reading 'refreshToken')                (guard bites)
  G7  production source sha256 is byte-for-byte UNCHANGED by this run

Run:  python3 artifacts/incident/verify_inc7_ci_gate.py
Exit: 0 = every gate passed
"""
import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
# The verifier must run BOTH from the commander workspace (where the repo is a
# clone under fleet/) and from inside the repo itself (where this file ships as
# artifacts/incident/verify_inc7_ci_gate.py and ROOT already IS the repo root).
# Auto-detect rather than hard-coding one layout -- a verifier that only runs on
# the author's machine is not a verifier.
_CANDIDATES = [ROOT / "fleet" / "checkout-api", ROOT]
REPO = next(
    (c for c in _CANDIDATES if (c / "service" / "checkout" / "session.js").is_file()),
    None,
)
if REPO is None:
    sys.exit("cannot locate checkout-api repo root from %s" % ROOT)

SRC = REPO / "service" / "checkout" / "session.js"
SUITE = REPO / "test" / "session.test.js"
CI = REPO / ".github" / "workflows" / "ci.yml"
PKG = REPO / "package.json"

# The deployed guard, and the INC-1 defect it replaced.
GUARDED = "const refreshToken = session.auth && session.auth.refreshToken;"
DEFECT = "const refreshToken = session.auth.refreshToken;"

results = []


def gate(name, ok, detail=""):
    results.append((name, ok))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, "  -- " + detail if detail else ""))


def npm_test(cwd):
    return subprocess.run(["npm", "test", "--silent"], cwd=str(cwd),
                          capture_output=True, text=True)


def tallies(proc):
    blob = proc.stdout + proc.stderr
    def grab(label):
        m = re.search(r"^# %s (\d+)" % label, blob, re.M)
        return int(m.group(1)) if m else 0
    return grab("pass"), grab("fail")


sha_before = hashlib.sha256(SRC.read_bytes()).hexdigest()

# ---------------------------------------------------------------- G1
# Provenance gate. The authoritative statement of "what is deployed" is upstream
# `main`, so compare against it directly. An earlier version of this gate shelled
# out to `git status`; the clone has no .git directory, so that check reported
# "nothing changed" and passed vacuously. A safety gate that cannot observe the
# thing it guards is worse than no gate, so it is now a real byte comparison.
RAW = "https://raw.githubusercontent.com/chrischabot/checkout-api/main/%s"
PROD_FILES = [
    "service/checkout/session.js",
    "package.json",
    "test/session.test.js",
]


def fetch(path):
    """Return upstream bytes for `path`, or None if it does not exist upstream."""
    try:
        with urllib.request.urlopen(RAW % path, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


identical, drifted = [], []
for rel in PROD_FILES:
    upstream = fetch(rel)
    local = (REPO / rel).read_bytes()
    if upstream is not None and hashlib.sha256(upstream).hexdigest() == hashlib.sha256(local).hexdigest():
        identical.append(rel)
    else:
        drifted.append(rel)

ci_upstream = fetch(".github/workflows/ci.yml")
gate("G1 production source byte-identical to upstream main; ci.yml is new",
     not drifted and ci_upstream is None and CI.is_file(),
     "identical=%d/%d drifted=%s | ci.yml upstream=%s local=%s" % (
         len(identical), len(PROD_FILES), drifted or "none",
         "absent" if ci_upstream is None else "ALREADY EXISTS",
         "present" if CI.is_file() else "MISSING"))

# ---------------------------------------------------------------- G2/G3/G4
try:
    import yaml
    ci = yaml.safe_load(CI.read_text())
    gate("G2 ci.yml is valid YAML", isinstance(ci, dict), "top-level keys=%s" % list(ci))
    # PyYAML parses the bare key `on:` as the boolean True -- handle both.
    triggers = ci.get("on", ci.get(True, {})) or {}
    push = triggers.get("push") or {}
    has_pr = "pull_request" in triggers
    has_main = "main" in (push.get("branches") or [])
    gate("G3 ci.yml triggers on pull_request AND push:[main]", has_pr and has_main,
         "pull_request=%s push.branches=%s" % (has_pr, push.get("branches")))
    runs = " ".join(
        s.get("run", "")
        for job in ci["jobs"].values()
        for s in job["steps"]
    )
    real_script = json.loads(PKG.read_text())["scripts"]["test"]
    gate("G4 ci.yml invokes package.json's real test script",
         "npm test" in runs and real_script.startswith("node --test"),
         "ci runs '%s' | package.json test = '%s'" % (runs.strip(), real_script))
except ImportError:
    gate("G2 ci.yml is valid YAML", False, "PyYAML unavailable")

# ---------------------------------------------------------------- G5
head = npm_test(REPO)
p, f = tallies(head)
gate("G5 suite GREEN against deployed HEAD",
     head.returncode == 0 and p > 0 and f == 0,
     "pass=%d fail=%d exit=%d" % (p, f, head.returncode))

# ---------------------------------------------------------------- G6
# Reintroduce the exact INC-1 defect in a throwaway copy and demand the suite
# fails. This is the gate that separates a real guard from decoration.
mut_root = pathlib.Path(tempfile.mkdtemp(prefix="inc7-mut-")) / "repo"
shutil.copytree(str(REPO), str(mut_root),
                ignore=shutil.ignore_patterns(".git", "node_modules"))
mut_src = mut_root / "service" / "checkout" / "session.js"
original = mut_src.read_text()
assert GUARDED in original, "deployed source no longer contains the expected guard"
mut_src.write_text(original.replace(GUARDED, DEFECT))

mut = npm_test(mut_root)
mp, mf = tallies(mut)
blob = mut.stdout + mut.stderr
reproduced = "Cannot read properties of null" in blob and "refreshToken" in blob
gate("G6 MUTATION: INC-1 defect reintroduced -> suite goes RED",
     mut.returncode != 0 and mf > 0,
     "pass=%d fail=%d exit=%d" % (mp, mf, mut.returncode))
gate("G6b MUTATION reproduces the exact production error",
     reproduced,
     "TypeError: Cannot read properties of null (reading 'refreshToken')"
     if reproduced else "expected TypeError not observed")
shutil.rmtree(str(mut_root.parent), ignore_errors=True)

# ---------------------------------------------------------------- G7
sha_after = hashlib.sha256(SRC.read_bytes()).hexdigest()
gate("G7 production source byte-for-byte UNCHANGED", sha_before == sha_after,
     "sha256=%s" % sha_after[:16])

# ---------------------------------------------------------------- verdict
passed = sum(1 for _, ok in results if ok)
print("\n%s\n%d/%d gates passed\n%s" % ("-" * 64, passed, len(results), "-" * 64))
if passed != len(results):
    print("VERDICT: NOT SAFE -- do not land.")
    sys.exit(1)
print("VERDICT: SAFE. The suite is a live guard: green on the real code, red on")
print("the INC-1 defect. It would have BLOCKED the PR that caused INC-1.")
sys.exit(0)
