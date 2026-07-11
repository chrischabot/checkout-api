#!/usr/bin/env python3
"""INC-11 verifier: does adding the verifier step to checkout-api's CI actually BITE?

The patch this run ships adds one step to `checkout-api/.github/workflows/ci.yml`
(run the merged INC-9 mutation-witness verifier) and repairs that verifier so it
can pass on `main` at all.

A CI step that cannot fail is decoration. So the load-bearing gate here is not
"is the new CI green?" — it is the DIVERGENCE:

    simulate the exact regression these steps exist to catch, then show
      OLD CI (npm test only)          -> exit 0   the regression SLIPS THROUGH
      NEW CI (npm test + verifier)    -> exit 1   the regression is CAUGHT

If OLD CI caught it on its own, the added step would be redundant and this
verifier should say so, loudly, rather than claim credit.

The regression simulated is the one the INC-10 postmortem identified as the
real-world threat: not "someone reintroduces the bug" (npm test catches that),
but "someone GUTS THE GUARD" — thins the cold-cache assertions until the suite
no longer witnesses the INC-1 defect. The suite still passes. Only a mutation
witness notices that the guard has stopped biting.

Gates:
  G1  the repaired INC-9 verifier passes on the CURRENT tree (exit 0)
  G2  ci.yml has a step that actually invokes the verifier (parsed, not grepped)
  G3  ci.yml still runs the suite too (the verifier does not replace it)
  G4  the INC-9 verifier's old merge-time-only assertion is GONE
      ("ci.yml is NEW"/absent-upstream can never be true post-merge)
  G5  SIMULATED REGRESSION: guard gutted -> `npm test` STILL PASSES (green)
      This is what makes the divergence meaningful: the suite is blind to it.
  G6  DIVERGENCE (load-bearing): same gutted tree ->
        OLD CI  exit 0  (slips through)
        NEW CI  exit 1  (caught by the verifier step)
  G7  no file in the real tree was modified by running this verifier (sha256)

Exit 0 = every gate passed.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
_CANDIDATES = [ROOT / "fleet" / "checkout-api", ROOT]
REPO = next(
    (c for c in _CANDIDATES if (c / "service" / "checkout" / "session.js").is_file()),
    None,
)
if REPO is None:
    sys.exit(f"cannot locate the checkout-api repo root from {ROOT}")

SUITE = REPO / "test" / "session.test.js"
CI = REPO / ".github" / "workflows" / "ci.yml"
VERIFIER = REPO / "artifacts" / "incident" / "verify_inc9_ci_gate.py"

RESULTS: list[tuple[str, bool, str]] = []


def gate(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n         {detail}" if detail else ""))


def sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run(cmd: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=600)


def ci_run_steps(ci_text: str) -> list[str]:
    """The `run:` commands of every step, parsed from YAML — not substring-matched.

    A `npm test` sitting in a COMMENT must not count as CI executing anything.
    """
    try:
        import yaml  # type: ignore

        wf = yaml.safe_load(ci_text) or {}
        out: list[str] = []
        for job in (wf.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                cmd = (step or {}).get("run")
                if cmd:
                    out.append(str(cmd))
        return out
    except ImportError:
        return re.findall(r"^\s*run:\s*(.+)$", ci_text, re.M)


# ---------------------------------------------------------------------------
# The simulated regression: gut the guard.
#
# `session.test.js` drives 5 cold-cache shapes through refreshAccessToken() and
# asserts none of them throw. THAT is what witnesses the INC-1 defect. Someone
# "simplifying" the suite by deleting those cases leaves a suite that still
# PASSES on good source — and no longer notices the defect coming back.
#
# CRITICAL PROPERTY OF A VALID SIMULATION (this bit me on the first attempt):
# the gutted suite must remain **GREEN on good source**. That is what makes the
# regression dangerous and invisible — it sails through code review and through
# `npm test`. A "gutted" suite that goes red on good source is not a gutted
# guard, it is a broken suite, and CI catching *that* proves nothing about
# whether the mutation witness is needed. (First attempt swapped the cold shapes
# for a warm record; the surviving assertions `out.ok === false` then failed on
# good source, G5 correctly rejected the simulation, and the divergence was
# unprovable.)
#
# So: DELETE the two COLD_CACHE_SHAPES-driven tests outright. What remains — the
# empty-auth-blob test, the warm-cache test, the exports test — passes on good
# source AND passes under the INC-1 defect (an empty auth *object* has no null to
# dereference, so the unguarded read survives it). That is precisely a suite that
# is green, plausible, and blind.
COLD_TEST = """test('cold-cache resume degrades gracefully instead of throwing (INC-1)', async (t) => {
  for (const [label, raw] of COLD_CACHE_SHAPES) {
    await t.test(label, () => {
      const session = resumeSession(raw);

      // The regression itself: this must not throw.
      const out = refreshAccessToken(session, tokenService);

      assert.strictEqual(out.ok, false, 'cold resume must not report success');
      assert.strictEqual(
        out.reason,
        'no_refresh_token',
        'cold resume must reach the no_refresh_token branch'
      );
      assert.ok(out.session, 'the session must still be returned to the caller');
    });
  }
});
"""

NORMALIZE_TEST = """test('resumeSession normalizes shape and never throws', () => {
  for (const [, raw] of COLD_CACHE_SHAPES) {
    const s = resumeSession(raw);
    assert.ok('id' in s && 'userId' in s && 'auth' in s, 'normalized shape must be complete');
  }
});
"""


def gut(repo: pathlib.Path) -> None:
    """Delete the cold-cache coverage that witnesses INC-1, leaving a green suite.

    Post-condition (asserted by G5): the resulting suite still passes on the
    deployed source. Only the mutation witness can tell that it has gone blind.
    """
    suite = repo / "test" / "session.test.js"
    text = suite.read_text()

    for block in (COLD_TEST, NORMALIZE_TEST):
        assert block in text, "suite shape not as expected — simulation would be invalid"
        text = text.replace(block, "")

    # The shape table is now unreferenced; drop it too, as a real "cleanup" would.
    text = re.sub(
        r"// Every raw record shape.*?^\];\n", "", text, flags=re.S | re.M
    )

    assert "COLD_CACHE_SHAPES" not in text, "gutting left a dangling reference"
    suite.write_text(text)


def old_ci(repo: pathlib.Path) -> subprocess.CompletedProcess:
    """OLD CI, as it exists on main today: the suite, and nothing else."""
    return run(["npm", "test", "--silent"], repo)


def new_ci(repo: pathlib.Path) -> tuple[int, str]:
    """NEW CI: the suite, then the mutation-witness verifier. First failure wins."""
    suite = old_ci(repo)
    if suite.returncode != 0:
        return suite.returncode, "npm test"
    ver = run([sys.executable, "artifacts/incident/verify_inc9_ci_gate.py"], repo)
    if ver.returncode != 0:
        return ver.returncode, "verify_inc9_ci_gate.py"
    return 0, "all steps green"


def main() -> int:
    print("INC-11 — does the added CI step actually catch anything?\n")

    tracked = [SUITE, CI, VERIFIER, REPO / "service" / "checkout" / "session.js"]
    before = {p: sha(p) for p in tracked}

    # ------------------------------------------------------------------ G1 --
    cur = run([sys.executable, str(VERIFIER)], REPO)
    gate(
        "G1 the repaired INC-9 verifier passes on the current tree",
        cur.returncode == 0,
        f"exit={cur.returncode} "
        f"({'was exit 1 on main before this patch — G3 asserted ci.yml did not exist upstream' if cur.returncode == 0 else 'still failing'})",
    )

    # --------------------------------------------------------------- G2/G3 --
    ci_text = CI.read_text()
    steps = ci_run_steps(ci_text)
    runs_verifier = any("verify_inc9_ci_gate.py" in s for s in steps)
    runs_suite = any("npm test" in s for s in steps)
    gate(
        "G2 a ci.yml STEP invokes the INC-9 verifier (parsed from YAML steps)",
        runs_verifier,
        f"steps={steps!r}",
    )
    gate(
        "G3 ci.yml still runs the regression suite (the gate ADDS, never replaces)",
        runs_suite,
        "both `npm test` and the verifier execute",
    )

    # ------------------------------------------------------------------ G4 --
    vtext = VERIFIER.read_text()
    stale = 'ci_upstream is None' in vtext or 'ci.yml is NEW' in vtext
    gate(
        "G4 the merge-time-only assertion is gone (it could never pass post-merge)",
        not stale,
        "the verifier no longer requires ci.yml to be ABSENT upstream, and no "
        "longer fails a PR merely for changing production source",
    )

    # --------------------------------------------------- G5/G6 · DIVERGENCE --
    with tempfile.TemporaryDirectory() as tmp:
        mirror = pathlib.Path(tmp) / "checkout-api"
        shutil.copytree(REPO, mirror)
        gut(mirror)

        # G5 — the gutted suite is STILL GREEN on the deployed source. This is the
        # whole danger, and it is the precondition that makes G6 meaningful: the
        # regression is completely invisible to `npm test`. If this gate fails,
        # the SIMULATION is invalid (it broke the suite rather than blinding it)
        # and no conclusion about the added CI step may be drawn.
        gutted_suite = old_ci(mirror)
        blob = gutted_suite.stdout + gutted_suite.stderr
        m = re.search(r"^# pass (\d+)", blob, re.M)
        mf = re.search(r"^# fail (\d+)", blob, re.M)
        passes = int(m.group(1)) if m else 0
        fails = int(mf.group(1)) if mf else 0
        gate(
            "G5 SIMULATED REGRESSION: the gutted guard STILL PASSES `npm test`",
            gutted_suite.returncode == 0 and passes > 0 and fails == 0,
            f"exit={gutted_suite.returncode} pass={passes} fail={fails} — the suite is "
            "BLIND to its own guard being deleted and stays green; that is exactly "
            "why a suite-only CI is not enough",
        )

        # G6 — the load-bearing divergence.
        old_code = gutted_suite.returncode
        new_code, culprit = new_ci(mirror)
        gate(
            "G6 DIVERGENCE: OLD CI passes the regression, NEW CI catches it",
            old_code == 0 and new_code != 0,
            f"OLD ci.yml (npm test only) exit={old_code} -> SLIPS THROUGH | "
            f"NEW ci.yml (suite + verifier) exit={new_code} -> CAUGHT by {culprit}. "
            "The divergence is the proof: the added step is not redundant.",
        )

    # ------------------------------------------------------------------ G7 --
    after = {p: sha(p) for p in tracked}
    unchanged = [p for p in tracked if before[p] == after[p]]
    gate(
        "G7 running this verifier mutated NOTHING in the real tree",
        len(unchanged) == len(tracked),
        f"{len(unchanged)}/{len(tracked)} files byte-identical before and after "
        "(all mutation happens in a throwaway copy)",
    )

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{'=' * 74}\nGATES: {passed}/{total} passed\n{'=' * 74}")
    if passed != total:
        for name, ok, _ in RESULTS:
            if not ok:
                print(f"  FAILED: {name}")
        return 1
    print("All gates green. The added CI step provably catches a regression that")
    print("the previous suite-only CI let straight through.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
