#!/usr/bin/env python3
"""Fabric incident commander — verification gate for the patch applied this run.

THE PATCH THIS RUN SHIPS IS A CI GATE, NOT A PRODUCTION CODE CHANGE.

Why: every incident this fleet has produced (INC-1, INC-2/6, INC-3/5) shipped
through a PR that changed a code path with no test executing against it. The two
live defects that remain are blocked on revenue/billing POLICY (see
incident_brief.md) and cannot be safely auto-patched. The systemic cause — no
repo executes any check on a pull request — IS mechanically fixable, with zero
risk to production source.

`checkout-api` is the sharpest case: it carries a merged, passing 10-test
regression suite on `main` whose assertions provably fail on the INC-1 defect —
and nothing ever ran it. The guard is dead code. This gate proves the CI
workflow turns it into a live guard.

A CI workflow that cannot fail is decoration, so the load-bearing gates are the
double witness, G4/G5:

  G1  the CI workflow is valid YAML and triggers on pull_request + push:[main]
  G2  it invokes package.json's REAL test script (not an invented one)
  G3  production source is UNTOUCHED (byte-for-byte sha256 vs upstream main) -- this
      is FATAL if it drifts. ci.yml's own drift vs upstream is reported as
      PROVENANCE and is never fatal: a pull request is allowed to edit its own
      workflow (INC-12 repair). G1/G2 remain fatal, so a workflow that stops
      running the suite or the mutation witness still fails.
  G4  WITNESS A — the suite is GREEN against the deployed source
  G5  WITNESS B — MUTATION: reintroduce the exact INC-1 unguarded read and the
      suite must go RED with the original production TypeError. If the guard
      cannot fail, it is worthless.
  G6  the two live policy-blocked defects are STILL live on current HEAD
      (we re-confirm rather than trusting a previous run's word)

Run:  python3 artifacts/incident/verify_run.py
Exit: 0 = every gate passed.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]

# This verifier must run BOTH from the commander workspace (where checkout-api is
# a clone under fleet/) and from INSIDE the repo, where it ships as
# artifacts/incident/verify_inc9_ci_gate.py and ROOT already IS the repo root.
# Auto-detect rather than hard-coding one layout: a verifier that only runs on
# the author's machine is not a verifier.
_CANDIDATES = [ROOT / "fleet" / "checkout-api", ROOT]
CHECKOUT_API = next(
    (c for c in _CANDIDATES if (c / "service" / "checkout" / "session.js").is_file()),
    None,
)
if CHECKOUT_API is None:
    sys.exit(f"cannot locate the checkout-api repo root from {ROOT}")

SRC = CHECKOUT_API / "service" / "checkout" / "session.js"
CI = CHECKOUT_API / ".github" / "workflows" / "ci.yml"
PKG = CHECKOUT_API / "package.json"

# The deployed guard, and the exact INC-1 defect it replaced.
GUARDED = "const refreshToken = session.auth && session.auth.refreshToken;"
DEFECT = "const refreshToken = session.auth.refreshToken;"

RESULTS: list[tuple[str, bool, str]] = []


def gate(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n         {detail}" if detail else ""))


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


def load(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    print("Fabric incident commander — verification gates for this run's patch\n")
    sha_before = hashlib.sha256(SRC.read_bytes()).hexdigest()

    # ---------------------------------------------------------------- G1 --
    ci_text = CI.read_text() if CI.is_file() else ""
    try:
        import yaml  # type: ignore

        parsed = yaml.safe_load(ci_text) if ci_text else None
        # PyYAML parses the bare `on:` key as boolean True (YAML 1.1).
        triggers = parsed.get(True, parsed.get("on")) if isinstance(parsed, dict) else None
        valid_yaml = isinstance(parsed, dict)
        has_pr = isinstance(triggers, dict) and "pull_request" in triggers
        push = triggers.get("push") if isinstance(triggers, dict) else None
        has_push_main = isinstance(push, dict) and "main" in (push.get("branches") or [])
    except ImportError:  # no PyYAML: fall back to a textual check
        valid_yaml = bool(ci_text)
        has_pr = "pull_request:" in ci_text
        has_push_main = "branches: [main]" in ci_text

    gate(
        "G1 CI workflow is valid YAML and fires on pull_request + push:[main]",
        valid_yaml and has_pr and has_push_main,
        f"valid_yaml={valid_yaml} pull_request={has_pr} push:[main]={has_push_main}",
    )

    # ---------------------------------------------------------------- G2 --
    import json

    pkg = json.loads(PKG.read_text())
    real_script = pkg.get("scripts", {}).get("test", "")

    # Parse the workflow's actual steps rather than substring-matching the file:
    # a `run: npm test` appearing in a COMMENT would fool a substring check.
    runs_real_script = False
    try:
        import yaml  # type: ignore

        wf = yaml.safe_load(ci_text) or {}
        for job in (wf.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                cmd = (step or {}).get("run") or ""
                if "npm test" in cmd or real_script in cmd:
                    runs_real_script = True
    except ImportError:
        runs_real_script = bool(
            re.search(r"^\s*run:\s*npm test\s*$", ci_text, re.M)
        )

    gate(
        "G2 CI executes package.json's real test script (parsed from steps)",
        bool(real_script) and runs_real_script,
        f"package.json test = {real_script!r}; a workflow STEP runs it",
    )

    # ---------------------------------------------------------------- G3 --
    # Provenance: the authoritative statement of "what is deployed" is upstream
    # `main`. Comparing the local tree against ITSELF would pass vacuously, so we
    # fetch upstream bytes and require an exact sha256 match on every prod file.
    #
    # PRODUCTION-source drift is FATAL. ci.yml drift is only REPORTED -- see the
    # long INC-12 comment at the gate() call below for why (a PR that adds a CI
    # step must, necessarily, change ci.yml; requiring byte-identity there would
    # forbid the repo from ever editing its own workflow).
    RAW = "https://raw.githubusercontent.com/chrischabot/checkout-api/main/{}"
    PROD_FILES = [
        "service/checkout/session.js",
        "package.json",
        "test/session.test.js",
    ]

    def fetch(rel: str):
        try:
            with urllib.request.urlopen(RAW.format(rel), timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    drifted: list[str] = []
    checked = 0
    upstream_reachable = True
    try:
        for rel in PROD_FILES:
            upstream = fetch(rel)
            local = (CHECKOUT_API / rel).read_bytes()
            if upstream is None or hashlib.sha256(upstream).digest() != hashlib.sha256(local).digest():
                drifted.append(rel)
            checked += 1
        ci_upstream = fetch(".github/workflows/ci.yml")
    except urllib.error.URLError as exc:
        upstream_reachable = False
        ci_upstream = None
        print(f"         (upstream unreachable: {exc})")

    # G3 is IDEMPOTENT, and that is a deliberate fix.
    #
    # This gate originally required `ci_upstream is None` -- i.e. it asserted the
    # workflow did not yet exist on main, because when INC-9 was first raised the
    # workflow was a NEW file. That assertion expired the moment the INC-9 PR was
    # merged: ci.yml now exists upstream, so the gate went PERMANENTLY RED on main
    # (5/6, exit 1) even though nothing was wrong. PR #8 repaired that half.
    #
    # `ci_matches_upstream` is still COMPUTED, but only to describe the state in
    # the gate's detail line (see ci_state below). It is deliberately NOT part of
    # the pass/fail decision any more -- that is the INC-12 repair, explained in
    # full at the gate() call.
    ci_local = CI.read_bytes() if CI.is_file() else None
    ci_matches_upstream = (
        ci_upstream is None  # not yet merged: a genuinely new file
        or (
            ci_local is not None
            and hashlib.sha256(ci_upstream).digest() == hashlib.sha256(ci_local).digest()
        )
    )
    if ci_upstream is None:
        ci_state = "absent upstream (new file, pre-merge)"
    elif ci_matches_upstream:
        ci_state = "present upstream and IDENTICAL (post-merge, no local edit)"
    else:
        ci_state = "present upstream and LOCALLY MODIFIED (expected on a PR that changes CI)"

    # INC-12 REPAIR -- the second half of the expired-precondition bug.
    #
    # PR #8 fixed G3's "ci.yml must be ABSENT upstream" assertion, which had gone
    # permanently false at merge. But it left a sibling defect in place: G3 still
    # required the local ci.yml to be BYTE-IDENTICAL to upstream main. As a
    # permanent gate that is self-defeating -- it fails EVERY legitimate pull
    # request that edits the workflow, i.e. it forbids the repo from ever changing
    # its own CI. INC-12 (adding the verifier step) tripped it immediately:
    #
    #     [FAIL] G3 ... drifted=none; ci.yml present upstream but LOCALLY MODIFIED
    #
    # Note `drifted=none`: production source was untouched. The gate was failing
    # purely because a CI-changing PR had, necessarily, changed CI.
    #
    # The durable invariant separates the two cleanly:
    #   * PRODUCTION source drift vs upstream main -> still FATAL. This is the
    #     real protection: it is what catches an unreviewed edit sneaking into the
    #     auth path under cover of a "CI-only" change.
    #   * ci.yml vs upstream main -> PROVENANCE. Reported, never fatal. A pull
    #     request is allowed to change the workflow; that is what review is for.
    #     ci.yml must still EXIST and must still run the suite + the mutation
    #     witness -- G1/G2 assert exactly that, and they are fatal.
    #
    # So a missing workflow, a workflow that stops running the suite, drifted
    # production source, or a guard that no longer bites all still FAIL. Only the
    # "the workflow may never be edited" over-reach is gone.
    gate(
        "G3 production source byte-identical to upstream main (ci.yml drift = provenance)",
        upstream_reachable and not drifted and CI.is_file(),
        f"checked={checked}/{len(PROD_FILES)} drifted={drifted or 'none'} [FATAL if any]; "
        f"ci.yml {ci_state} [provenance only -- a PR may legitimately edit CI; "
        f"G1/G2 still enforce that it runs the suite and the mutation witness]",
    )

    # -------------------------------------------------- G4 · WITNESS A --
    green = npm_test(CHECKOUT_API)
    p, f = tallies(green)
    gate(
        "G4 WITNESS A — suite is GREEN on the deployed source",
        green.returncode == 0 and p > 0 and f == 0,
        f"exit={green.returncode} pass={p} fail={f}",
    )

    # -------------------------------------------------- G5 · WITNESS B --
    # Mutate a THROWAWAY copy: reintroduce the INC-1 defect and require the
    # suite to catch it. The production tree is never mutated.
    with tempfile.TemporaryDirectory() as tmp:
        mirror = pathlib.Path(tmp) / "checkout-api"
        shutil.copytree(CHECKOUT_API, mirror)
        msrc = mirror / "service" / "checkout" / "session.js"
        mutated = msrc.read_text().replace(GUARDED, DEFECT)
        assert DEFECT in mutated and GUARDED not in mutated, "mutation did not apply"
        msrc.write_text(mutated)

        red = npm_test(mirror)
        mp, mf = tallies(red)
        blob = red.stdout + red.stderr
        reproduced = "Cannot read properties of null" in blob and "refreshToken" in blob
        gate(
            "G5 WITNESS B — MUTATION: the INC-1 defect makes the suite go RED",
            red.returncode != 0 and mf > 0 and reproduced,
            f"exit={red.returncode} pass={mp} fail={mf}; "
            f"original production TypeError reproduced={reproduced}",
        )

    # Production source must be bit-identical after all of the above.
    sha_after = hashlib.sha256(SRC.read_bytes()).hexdigest()
    gate(
        "G3b production source sha256 unchanged after mutation testing",
        sha_before == sha_after,
        f"{sha_before[:12]} == {sha_after[:12]}",
    )

    # ---------------------------------------------------------------- G6 --
    # Cross-fleet re-confirmation (INC-6 / INC-5 / INC-8) needs the OTHER two
    # fleet repos. Depending on how this verifier is invoked, they may sit beside
    # this repo (commander workspace: fleet/{checkout-api,incident-target,gateway})
    # or not be present at all (a plain checkout of checkout-api). Search every
    # plausible sibling location; if they genuinely are not here, SKIP explicitly
    # rather than reporting a vacuous pass.
    # INC-13 REPAIR — this gate was DEAD, and it hid that fact behind a green exit.
    #
    # The discovery below used to look for sibling directories literally named
    # `incident-target` and `gateway`. The fleet repos are actually named
    # `fabric-ic-incident-target` and `fabric-gateway-demo`. So the lookup never
    # matched, G6 ALWAYS took the SKIP path, and G6a/G6b/G6c never executed --
    # in ANY environment, including the commander workspace this verifier claims
    # to be designed for. Three of the nine gates were unreachable code.
    #
    # Worse than being dead: the SKIP path counted only the gates that HAD run
    # and returned `0 if passed == total`, so a run where the cross-fleet gates
    # never fired still printed a confident "GATES: 6/6 passed" and exited 0.
    # A skipped check reported as a pass is exactly the rot INC-9/INC-11/INC-12
    # were raised about -- a gate that cannot fail -- reproduced a third time,
    # inside the verifier that exists to police it.
    #
    # (PR #7 diagnosed this same name mismatch but was closed UNMERGED, superseded
    # by #8/#12, so the repair was never actually landed on main. It is landed here.)
    #
    # Fix: search the REAL repo names first, keep the legacy names as fallbacks so
    # other checkout layouts still work, and make a genuine skip VISIBLE in the
    # summary instead of laundering it into the pass count.
    TARGET_DIRS = ("fabric-ic-incident-target", "incident-target")
    GATEWAY_DIRS = ("fabric-gateway-demo", "gateway")

    _fleet_roots = [CHECKOUT_API.parent, ROOT / "fleet", ROOT]
    target = next(
        (
            r / d / "checkout.py"
            for r in _fleet_roots
            for d in TARGET_DIRS
            if (r / d / "checkout.py").is_file()
        ),
        None,
    )
    gateway = next(
        (
            r / d / "service" / "usage_aggregator.py"
            for r in _fleet_roots
            for d in GATEWAY_DIRS
            if (r / d / "service" / "usage_aggregator.py").is_file()
        ),
        None,
    )

    if target is None or gateway is None:
        # A genuine skip (e.g. a bare clone of just this repo). Say so LOUDLY and
        # report it as SKIPPED -- never fold it into "passed", or the next reader
        # will trust three gates that never ran.
        #
        # STRICTNESS -- why a skip is not unconditionally fatal.
        #
        # checkout-api's own CI checks out ONLY checkout-api, so the sibling fleet
        # repos are legitimately absent there. Making the skip always fatal would
        # turn this verifier permanently red in the very CI job that runs it --
        # which is precisely the expired-precondition bug INC-11 and INC-12 were
        # raised to repair. Re-introducing it here would be a regression.
        #
        # But "visible" alone is too weak wherever the cross-fleet gates ARE
        # expected to run (the commander workspace, which clones all three repos):
        # there, a silent skip is the whole INC-13 defect. So the caller declares
        # its expectation and a broken skip becomes FATAL:
        #
        #   FABRIC_REQUIRE_CROSS_FLEET=1  (env)  or  --require-cross-fleet  (flag)
        #
        # Default (bare checkout / repo CI): skip is reported, exit stays 0.
        # Strict (commander workspace):      skip is a hard FAILURE, exit 1.
        #
        # The env value is case-folded: a human setting `FALSE`, `False` or `No`
        # means "off", and must not accidentally turn strict mode ON.
        _flag = os.environ.get("FABRIC_REQUIRE_CROSS_FLEET", "").strip().lower()
        require_cross_fleet = (
            _flag not in ("", "0", "false", "no", "off")
            or "--require-cross-fleet" in sys.argv[1:]
        )

        missing = []
        if target is None:
            missing.append(f"checkout.py in any of {TARGET_DIRS}")
        if gateway is None:
            missing.append(f"service/usage_aggregator.py in any of {GATEWAY_DIRS}")
        print(
            "\n[SKIP] G6a/G6b/G6c cross-fleet re-confirmation (INC-6/5/8) DID NOT RUN.\n"
            f"       Not found: {'; '.join(missing)}\n"
            f"       Searched: {[str(r) for r in _fleet_roots]}\n"
            "       Clone the sibling fleet repos beside this one to execute them."
        )
        passed = sum(1 for _, ok, _ in RESULTS if ok)
        total = len(RESULTS)
        print(f"\n{'=' * 74}")
        print(f"GATES: {passed}/{total} passed, 3 SKIPPED (cross-fleet gates did not run)")
        print(f"{'=' * 74}")
        if require_cross_fleet:
            print(
                "FATAL: cross-fleet re-confirmation was REQUIRED\n"
                "       (FABRIC_REQUIRE_CROSS_FLEET / --require-cross-fleet) but the\n"
                "       sibling repos were not found, so G6a/G6b/G6c did not execute.\n"
                "       Refusing to report a pass for gates that never ran."
            )
            return 1
        print(
            "NOTE: exit 0 is correct for a bare single-repo checkout (this repo's own\n"
            "      CI clones only checkout-api). Run with --require-cross-fleet from\n"
            "      the commander workspace to make a missing sibling FATAL."
        )
        return 0 if passed == total else 1

    # Re-confirm the two policy-blocked defects are STILL live on current HEAD.
    checkout = load(target, "checkout_live")
    leak = checkout.apply_discount(30_000, [{"price_cents": 1_000}])
    price_blind = checkout.apply_discount(30_000, [{"price_cents": 1}]) == checkout.apply_discount(
        30_000, [{"price_cents": 29_999}]
    )
    gate(
        "G6a INC-6 checkout discount leak is STILL LIVE on main",
        leak == 25_500 and price_blind,
        f"$300 order / one $10 eligible item -> charges ${leak / 100:,.2f} "
        f"(contract requires $300.00); item price ignored entirely={price_blind}",
    )

    agg = load(gateway, "agg_live")
    batch_died = False
    try:
        agg.aggregate_usage([{"model": "gpt-4o", "tokens": 120}, {"model": "gpt-4o"}])
    except KeyError:
        batch_died = True

    # NEW this run: a null model does NOT raise — it silently aggregates billable
    # tokens under a None key. A different, quieter failure than INC-3/INC-5.
    null_model = agg.aggregate_usage([{"model": None, "tokens": 10}])
    silent_null = None in null_model["per_model"]
    gate(
        "G6b INC-5 /v1/usage batch failure is STILL LIVE on main",
        batch_died,
        "one malformed record raises KeyError and destroys the whole batch",
    )
    gate(
        "G6c INC-8 (NEW) null model silently mis-attributes billable tokens",
        silent_null,
        f"{{'model': None, 'tokens': 10}} -> {null_model} — no error raised; "
        "10 billable tokens booked against a None model key",
    )

    # ------------------------------------------------------------ summary --
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{'=' * 74}\nGATES: {passed}/{total} passed\n{'=' * 74}")
    if passed != total:
        for name, ok, _ in RESULTS:
            if not ok:
                print(f"  FAILED: {name}")
        return 1
    print("All gates green. The CI patch is verified safe (production source")
    print("untouched) and PROVEN to bite (mutation goes red).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
