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
  G3  PROVENANCE ONLY, never fatal: report whether the local tree differs from
      upstream `main`. This gate must NOT fail on drift. It runs as a CI step on
      every pull request, and a pull request's whole purpose is to change source.
      Requiring byte-identity would forbid the repo from ever being edited.
      What actually protects the auth path is G4/G5 (the guard is green on this
      source, and it still goes RED on the INC-1 defect) plus code review.
  G4  WITNESS A — the suite is GREEN against the source under test
  G5  WITNESS B — MUTATION: reintroduce the exact INC-1 unguarded read and the
      suite must go RED with the original production TypeError. If the guard
      cannot fail, it is worthless. THIS is the gate with teeth.
  G3b the verifier itself mutated nothing (sha256 before == after)
  G6  the two live policy-blocked defects are STILL live on current HEAD
      (we re-confirm rather than trusting a previous run's word)

Run:  python3 artifacts/incident/verify_run.py
Exit: 0 = every gate passed.
"""
from __future__ import annotations

import hashlib
import importlib.util
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
    # PROVENANCE ONLY. This gate reports drift; it must never fail on it.
    #
    # See the long INC-12 FOLLOW-UP comment at the gate() call below. In short:
    # this verifier is now a CI step that runs on every pull request, and a pull
    # request exists in order to change code. A gate demanding byte-identity with
    # upstream `main` would fail every legitimate PR -- it would forbid the repo
    # from ever being edited, which is not a safety property, it is a padlock.
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
    # the pass/fail decision any more.
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

    # INC-12 FOLLOW-UP REPAIR -- the expired-precondition bug, fixed at the root.
    #
    # History: G3 originally asserted "ci.yml is ABSENT upstream (a new file)".
    # True exactly once -- in the PR that introduced ci.yml -- and permanently
    # false the instant it merged, so the verifier went permanently RED on a
    # healthy `main`. PR #8 fixed that clause. But two sibling over-reaches
    # survived, and BOTH became fatal once this verifier was wired in as a CI
    # step (PR #9):
    #
    #   1. ci.yml had to be byte-identical to upstream  -> fails every PR that
    #      edits the workflow (including the one adding this very step).
    #   2. session.js / package.json / session.test.js had to be byte-identical
    #      to upstream -> fails EVERY PR that touches source, tests, or package
    #      metadata. That is every normal pull request this repo will ever see.
    #
    # A gate that no legitimate change can pass is not a safety property. It is a
    # padlock on the repo, and its only real effect is to teach the team that red
    # CI means nothing -- which is precisely the disease this fleet is trying to
    # cure. It is exactly as worthless as a gate that can never fail.
    #
    # So G3 is now PROVENANCE: it fetches upstream bytes, reports whether the
    # tree differs, and NEVER fails on the difference. "Is this source the
    # deployed source?" is a useful thing to print in a CI log; it is not a thing
    # a pull request can be required to answer "yes" to.
    #
    # WHAT STILL PROTECTS THE AUTH PATH -- and these remain fatal:
    #   * G1/G2  the workflow exists, fires on PRs, and actually runs the suite.
    #   * G4     WITNESS A: the suite is GREEN on the source under test.
    #   * G5     WITNESS B: the suite still goes RED on the INC-1 defect. This is
    #            the gate with teeth -- it is what catches the guard being gutted
    #            or the defect being reintroduced, on WHATEVER source is present.
    #   * G3b    this verifier mutates nothing.
    #
    # Note that G4/G5 are strictly STRONGER protection than the hash check ever
    # was: a hash gate only says "these bytes are not the deployed bytes", while
    # G4/G5 say "whatever these bytes are, the auth guard still works and the
    # suite still catches it breaking". That is the property we actually want
    # enforced on a pull request.
    gate(
        "G3 PROVENANCE (never fatal): tree vs upstream main, reported not enforced",
        CI.is_file() and bool(ci_text.strip()),
        f"checked={checked}/{len(PROD_FILES)} vs upstream main: "
        f"{'identical (this IS the deployed source)' if upstream_reachable and not drifted else ('differs in ' + ', '.join(drifted)) if upstream_reachable else 'upstream unreachable'}"
        f"; ci.yml {ci_state}. "
        f"NOT FATAL -- a pull request may legitimately change source, tests or CI. "
        f"Enforcement lives in G1/G2 (the guard is wired) and G4/G5 (the guard "
        f"still bites on whatever source is present).",
    )

    # -------------------------------------------------- G4 · WITNESS A --
    # The suite must be green on whatever source is present. On `main` that is
    # the deployed source; on a PR branch it is the proposed source. Either way,
    # a red suite is a hard fail.
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
    _fleet_roots = [CHECKOUT_API.parent, ROOT / "fleet", ROOT]
    target = next(
        (p for p in (r / "incident-target" / "checkout.py" for r in _fleet_roots) if p.is_file()),
        None,
    )
    gateway = next(
        (
            p
            for p in (
                r / "gateway" / "service" / "usage_aggregator.py" for r in _fleet_roots
            )
            if p.is_file()
        ),
        None,
    )

    if target is None or gateway is None:
        print(
            "\n[SKIP] G6 cross-fleet re-confirmation (INC-6/5/8): the other fleet\n"
            "       repos are not present in this checkout. Run this verifier from\n"
            "       the incident-commander workspace to execute those gates."
        )
        passed = sum(1 for _, ok, _ in RESULTS if ok)
        total = len(RESULTS)
        print(f"\n{'=' * 74}\nGATES: {passed}/{total} passed\n{'=' * 74}")
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
