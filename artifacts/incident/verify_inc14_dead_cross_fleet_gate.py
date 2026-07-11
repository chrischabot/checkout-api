#!/usr/bin/env python3
"""INC-14 verifier: the cross-fleet gates were unreachable dead code.

Raised by Fabric's autonomous incident commander.

THE FINDING
-----------
`verify_inc9_ci_gate.py` carries three cross-fleet gates (G6a/G6b/G6c) whose job
is to re-confirm, BY EXECUTING THE DEPLOYED SOURCE, that the three owner-blocked
billing defects (INC-6, INC-5, INC-8) are still live. That is the mechanism which
stops the commander from carrying findings forward on a previous run's word.

Its sibling-repo discovery searched for directories named:

    incident-target/        gateway/

The fleet repos are actually named:

    fabric-ic-incident-target/    fabric-gateway-demo/

The lookup NEVER matched -- in any environment, including the commander workspace
it was written for. G6 always took the SKIP path, so G6a/G6b/G6c never executed.
Worse, the skip path computed `passed == total` over only the gates that HAD run,
printing a confident "GATES: 6/6 passed" while a third of the verifier was
unreachable code.

That is this fleet's signature failure -- *a gate that cannot fail is decoration*
-- reproduced INSIDE the verifier whose only job is to police it. A skipped check
laundered into a pass count is worse than a missing check: it actively asserts
coverage it does not have.

GATES
-----
  G1  the repaired verifier passes AND the cross-fleet gates actually RAN
  G2  discovery resolves both real fleet repo names
  G3  legacy names still resolve (the fix ADDS names, never replaces them)
  G4  WITNESS A -- the OLD discovery logic is BLIND on this same filesystem
  G5  WITNESS B -- DIVERGENCE: old skips all 3 gates, new EXECUTES all 3
  G6  NEGATIVE CONTROL -- genuinely absent siblings report SKIPPED, never a pass
  G7  strict mode refuses to pass un-run gates (exit 1)
  G8  no production drift: all 3 deployed sources byte-identical to baseline

Exit 0 = every gate passed.
"""
from __future__ import annotations

import hashlib
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]

# This verifier must run from BOTH layouts:
#   * the commander workspace: <root>/{checkout-api, fabric-gateway-demo, ...}
#   * inside the repo, where it ships as artifacts/incident/... and the fleet
#     siblings may not be cloned at all (checkout-api CI clones only itself).
# Walk upward for the directory that actually holds the fleet, rather than
# hard-coding a parent depth -- a verifier that only runs on the author's
# machine is not a verifier.
_FLEET_MARKERS = (
    "checkout-api/service/checkout/session.js",
    "fabric-ic-incident-target/checkout.py",
    "fabric-gateway-demo/service/usage_aggregator.py",
)


def _find_fleet_root(start: pathlib.Path) -> pathlib.Path | None:
    for cand in (start, *start.parents):
        if all((cand / m).is_file() for m in _FLEET_MARKERS):
            return cand
    return None


FLEET_ROOT = _find_fleet_root(pathlib.Path(__file__).resolve())

if FLEET_ROOT is None:
    # The cross-fleet siblings are genuinely absent (e.g. checkout-api's own CI,
    # which clones only this repo). This verifier's gates STRUCTURALLY require
    # all three repos, so it cannot run here.
    #
    # Report an explicit SKIP and exit 0. It must NOT claim the gates passed --
    # that is the very INC-14 defect (a skip laundered into a pass). And it must
    # NOT hard-fail either, which would leave the check permanently red in the
    # job that runs it -- the INC-11 defect. Run it from the incident-commander
    # workspace, where all three repos are present, to execute the real gates.
    print("Fabric incident commander — INC-14 verification gates\n")
    print(
        "[SKIP] INC-14 cross-fleet gates: the sibling fleet repos\n"
        "       (fabric-ic-incident-target, fabric-gateway-demo) are not present\n"
        "       beside this checkout, and every gate here requires all three.\n"
        "       NOT counted as a pass. Run from the commander workspace to execute."
    )
    print("\n" + "=" * 74)
    print("GATES: 0/0 passed, ALL SKIPPED (not counted as passes)")
    print("=" * 74)
    sys.exit(0)

ROOT = FLEET_ROOT
VERIFIER = ROOT / "checkout-api" / "artifacts" / "incident" / "verify_inc9_ci_gate.py"

# The three deployed production sources. None may be touched by this run.
PROD = {
    "checkout-api/service/checkout/session.js": "b45a8eeceaa1",
    "fabric-ic-incident-target/checkout.py": "da2a02fd87ae",
    "fabric-gateway-demo/service/usage_aggregator.py": "bb21e50f7b5d",
}

RESULTS: list[tuple[str, bool, str]] = []


def gate(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n         {detail}" if detail else ""))


def sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12]


def run(args, cwd, env_extra=None):
    import os

    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, timeout=600, env=env
    )


# The OLD (defective) discovery logic, reproduced verbatim for WITNESS A.
def old_discovery(checkout_api: pathlib.Path, root: pathlib.Path):
    fleet_roots = [checkout_api.parent, root / "fleet", root]
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


# The NEW (repaired) discovery logic.
def new_discovery(checkout_api: pathlib.Path, root: pathlib.Path):
    fleet_roots = [checkout_api.parent, root / "fleet", root]
    target = next(
        (
            p
            for p in (
                r / name / "checkout.py"
                for r in fleet_roots
                for name in ("fabric-ic-incident-target", "incident-target")
            )
            if p.is_file()
        ),
        None,
    )
    gateway = next(
        (
            p
            for p in (
                r / name / "service" / "usage_aggregator.py"
                for r in fleet_roots
                for name in ("fabric-gateway-demo", "gateway")
            )
            if p.is_file()
        ),
        None,
    )
    return target, gateway


def main() -> int:
    print("Fabric incident commander — INC-14 verification gates\n")
    before = {rel: sha(ROOT / rel) for rel in PROD}

    checkout_api = ROOT / "checkout-api"

    # ------------------------------------------------------------------ G1 --
    # The repaired verifier must pass AND the cross-fleet gates must actually
    # have EXECUTED. "exit 0" alone is exactly the evidence that failed us before.
    proc = run([sys.executable, str(VERIFIER)], cwd=checkout_api)
    blob = proc.stdout + proc.stderr
    ran_g6 = all(f"G6{s}" in blob for s in ("a", "b", "c"))
    m = re.search(r"GATES: (\d+)/(\d+) passed", blob)
    tally = m.group(0) if m else "no tally"
    gate(
        "G1 repaired INC-9 verifier passes AND cross-fleet gates actually RAN",
        proc.returncode == 0 and ran_g6,
        f"exit={proc.returncode}; {tally}; G6a/G6b/G6c present in output={ran_g6}",
    )

    # ------------------------------------------------------------------ G2 --
    nt, ng = new_discovery(checkout_api, ROOT)
    gate(
        "G2 discovery resolves BOTH real fleet repo names",
        nt is not None and ng is not None,
        f"fabric-ic-incident-target -> {'FOUND' if nt else 'MISSING'}; "
        f"fabric-gateway-demo -> {'FOUND' if ng else 'MISSING'}",
    )

    # ------------------------------------------------------------------ G3 --
    # The fix must ADD names, not replace them: a legacy layout must still work.
    with tempfile.TemporaryDirectory() as tmp:
        legacy = pathlib.Path(tmp)
        (legacy / "checkout-api").mkdir()
        (legacy / "incident-target").mkdir()
        (legacy / "gateway" / "service").mkdir(parents=True)
        (legacy / "incident-target" / "checkout.py").write_text("# legacy\n")
        (legacy / "gateway" / "service" / "usage_aggregator.py").write_text("# legacy\n")
        lt, lg = new_discovery(legacy / "checkout-api", legacy)
        gate(
            "G3 legacy sibling names STILL resolve (fix adds, never replaces)",
            lt is not None and lg is not None,
            f"legacy incident-target -> {'FOUND' if lt else 'MISSING'}; "
            f"legacy gateway -> {'FOUND' if lg else 'MISSING'}",
        )

    # ------------------------------------------------- G4 · WITNESS A --
    # The old logic must be BLIND on the very filesystem where the siblings exist.
    ot, og = old_discovery(checkout_api, ROOT)
    gate(
        "G4 WITNESS A — the OLD discovery is BLIND despite both siblings being present",
        ot is None and og is None and nt is not None and ng is not None,
        f"OLD: target={ot} gateway={og} (both None => never matched)  |  "
        f"NEW: target={nt.name if nt else None}/... gateway=FOUND",
    )

    # ------------------------------------------------- G5 · WITNESS B --
    # DIVERGENCE, and this is the load-bearing gate. It is not enough that the new
    # code works; the old code must be shown to have SKIPPED the gates on the same
    # tree. Had both behaved alike, the repair would be a no-op and this must say so.
    old_would_skip = ot is None or og is None
    new_would_run = nt is not None and ng is not None
    gate(
        "G5 WITNESS B — DIVERGENCE: OLD skips all 3 cross-fleet gates, NEW executes them",
        old_would_skip and new_would_run and ran_g6,
        f"OLD -> SKIP path (G6a/b/c never execute, yet '6/6 passed' was printed)  |  "
        f"NEW -> all three gates EXECUTE and re-confirm INC-6/5/8 live",
    )

    # ------------------------------------------------------------------ G6 --
    # NEGATIVE CONTROL: with the siblings genuinely absent, the verifier must
    # report SKIPPED and must NOT count the skip as a pass. This is what makes the
    # fix correct rather than merely green.
    with tempfile.TemporaryDirectory() as tmp:
        bare = pathlib.Path(tmp) / "checkout-api"
        shutil.copytree(checkout_api, bare)
        bare_proc = run([sys.executable, "artifacts/incident/verify_inc9_ci_gate.py"], cwd=bare)
        bblob = bare_proc.stdout + bare_proc.stderr
        says_skipped = "SKIPPED (not counted as passes)" in bblob or "[SKIP]" in bblob
        # Crucially: the tally must NOT claim the skipped gates as passes.
        bm = re.search(r"GATES: (\d+)/(\d+) passed", bblob)
        no_phantom_pass = bool(bm) and int(bm.group(2)) == 6  # only the 6 real gates ran
        gate(
            "G6 NEGATIVE CONTROL — absent siblings report SKIPPED, never a silent pass",
            bare_proc.returncode == 0 and says_skipped and no_phantom_pass,
            f"bare checkout: exit={bare_proc.returncode} "
            f"tally='{bm.group(0) if bm else 'none'}' reports_skip={says_skipped} "
            f"(not permanently red -- checkout-api CI clones only itself)",
        )

        # -------------------------------------------------------------- G7 --
        # STRICT mode: where the siblings are EXPECTED, a skip must be FATAL.
        strict_proc = run(
            [sys.executable, "artifacts/incident/verify_inc9_ci_gate.py", "--require-cross-fleet"],
            cwd=bare,
        )
        gate(
            "G7 strict mode refuses to pass un-run gates",
            strict_proc.returncode != 0,
            f"--require-cross-fleet on a bare checkout -> exit={strict_proc.returncode} (FATAL, as required)",
        )

    # ------------------------------------------------------------------ G8 --
    after = {rel: sha(ROOT / rel) for rel in PROD}
    unchanged = [rel for rel in PROD if before[rel] == after[rel] == PROD[rel]]
    gate(
        "G8 NO PRODUCTION DRIFT — all 3 deployed sources byte-identical",
        len(unchanged) == len(PROD),
        "; ".join(f"{rel.split('/')[0]}={after[rel]}" for rel in PROD),
    )

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    bar = "=" * 74
    print(f"\n{bar}\nGATES: {passed}/{total} passed\n{bar}")
    if passed != total:
        for name, ok, _ in RESULTS:
            if not ok:
                print(f"  FAILED: {name}")
        return 1
    print("All gates green. The cross-fleet gates now EXECUTE; a skip can no longer")
    print("masquerade as a pass; no production source was touched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
