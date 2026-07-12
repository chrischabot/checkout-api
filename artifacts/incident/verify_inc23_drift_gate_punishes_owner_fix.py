#!/usr/bin/env python3
"""Fabric incident commander — INC-23 verifier.

THE FINDING
-----------
`artifacts/incident/verify_inc15_cross_fleet_discovery.py` gate G8 ("NO PRODUCTION
DRIFT") required every deployed source to be byte-identical to a HARDCODED sha256
baseline, and was FATAL on any difference.

That is a MERGE-TIME FACT FROZEN INTO A PERMANENT GATE. It encodes the claim
"nobody has repaired the billing defects yet" — a statement about the CALENDAR,
not about correctness. Two consequences, both reproduced by execution:

  1. An owner landing the CORRECT INC-6 repair (the one this commander has
     escalated for many consecutive runs) changes `checkout.py`'s bytes, so G8
     goes hard RED on a repo where NOTHING IS WRONG. INC-19's G1 merely re-runs
     the INC-15 verifier, so it inherits the failure: one root cause, two red
     gates. The owner does exactly what we keep asking for, and CI punishes them.

  2. It padlocks this repo's OWN `session.js` — any legitimate future edit
     reddens CI. That is the INC-12 bug, re-committed.

THE REPAIR — assert the invariant, not the calendar
---------------------------------------------------
What G8 legitimately protects is the verifier's OWN side effects: it mutates files
during mutation testing and must restore every one. So G8 now compares a
start-of-run SNAPSHOT against the bytes on disk at the end:

  * bytes moved DURING OUR OWN RUN            -> FATAL (still bites)
  * differs from the historical reference but
    STABLE across our run  = an OWNER EDIT    -> PROVENANCE, never fatal

GATES
-----
  G0  STATIC: the shipped INC-15 verifier carries the repair (no siblings needed,
      so this is what guards it inside CI)
  G1  NO REGRESSION: the repaired verifier is still 9/9 on an untouched fleet
  G2  WITNESS A (necessity): the PRE-repair predicate REJECTS a correct owner fix
  G3  WITNESS B (sufficiency): the repaired verifier PASSES on that same tree
  G4  DIVERGENCE (load-bearing): identical tree, PRE = RED · POST = GREEN
  G5  ANTI-WEAKENING: a verifier that leaves production MUTATED across its own run
      is STILL rejected
  G6  the owner's CORRECT repair is genuinely correct (established by execution)
  G7  NO DRIFT: this verifier restores every file it touches

G5 is the gate that matters most. Simply DELETING G8 would have turned the red
gate green *and* satisfied G2/G3/G4 — and it FAILS G5. That is the difference
between a CORRECTION and a COVER-UP.

On G2's soundness: the necessity witness is anchored to the FROZEN HISTORICAL
CONSTANT (parsed from the verifier's own BASELINES dict), NOT to the bytes present
when the verifier starts. Anchoring to runtime bytes would be confounded on a tree
where the owner fix had already landed — the "baseline" would silently become the
repaired file, the old predicate would appear to accept it, and the gate would
prove nothing.

Exit: 0 = every executed gate passed.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
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

INC15_REL = "artifacts/incident/verify_inc15_cross_fleet_discovery.py"
INC19_REL = "artifacts/incident/verify_inc19_layout_and_count_invariance.py"
INC15 = CHECKOUT_API / INC15_REL

TARGET = FLEET / "fabric-ic-incident-target"
GATEWAY = FLEET / "fabric-gateway-demo"
CHECKOUT_PY = TARGET / "checkout.py"

# The deployed INC-6 defect, and the CORRECT owner repair. The deployed code
# divides the SUBTOTAL by the item count -- it never reads any item's price, which
# is why a $0.01 and a $299.99 eligible item produce an identical charge.
DEFECT_LINE = "    avg_cents = subtotal_cents / n"
REPAIR_LINE = '    avg_cents = sum(i["price_cents"] for i in eligible_items) / n'

RESULTS: list[tuple[str, bool, str]] = []
SKIPPED: list[tuple[str, str]] = []

# INC-30: strict cross-fleet mode is honoured through an environment variable, and
# a bare `subprocess.run()` (no `env=`) leaks it into child verifier processes.
# This file's witnesses run child verifiers against synthetic trees with the
# siblings deliberately absent -- an inherited strict flag forces them to
# hard-fail for a reason unrelated to the property under test.
STRICT_ENV_VAR = "FABRIC_REQUIRE_CROSS_FLEET"


def child_env(*, strict: bool = False) -> dict:
    env = dict(os.environ)
    env.pop(STRICT_ENV_VAR, None)   # ALWAYS scrubbed
    if strict:
        env[STRICT_ENV_VAR] = "1"   # ...re-set ONLY on request
    return env


def gate(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n         {detail}" if detail else ""))


def skip(name: str, detail: str = "") -> None:
    SKIPPED.append((name, detail))
    print(f"[SKIP] {name}" + (f"\n         {detail}" if detail else ""))


def sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run(script: pathlib.Path, cwd: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script)], cwd=str(cwd), env=child_env(),
        capture_output=True, text=True, timeout=900,
    )


def failed_gates(blob: str) -> list[str]:
    return re.findall(r"^\[FAIL\] (\S+)", blob, re.M)


def frozen_baselines() -> dict[str, str]:
    """Parse the FROZEN historical sha256 constants out of the shipped verifier.

    G2 must be anchored to these, not to runtime bytes -- see the module docstring.
    """
    src = INC15.read_text()
    names = re.findall(r'/\s*"([\w.]+)":\s*\n?\s*"([0-9a-f]{64})"', src)
    if names:
        return {n: h for n, h in names}
    # Fall back: pair each 64-hex literal with the filename mentioned just above it.
    out: dict[str, str] = {}
    for m in re.finditer(r'"([0-9a-f]{64})"', src):
        window = src[max(0, m.start() - 200):m.start()]
        fm = re.findall(r'"([\w.]+\.(?:js|py))"', window)
        if fm:
            out[fm[-1]] = m.group(1)
    return out


def land_owner_repair(checkout_py: pathlib.Path) -> str:
    """Ensure the tree carries the CORRECT owner INC-6 repair. IDEMPOTENT.

    This function must never raise merely because the owner already did the thing we
    have been asking them to do. An earlier draft did exactly that:

        if DEFECT_LINE not in src: raise AssertionError(...)

    ...which means that once an owner lands the INC-6 fix upstream, THIS VERIFIER
    CRASHES -- punishing the remediation it exists to request. That is the very
    disease INC-23 diagnoses, re-committed inside the verifier that diagnoses it.
    (Third time this fleet has done it. Hence this docstring.)

    Returns one of:
      "applied"          -- the defect was present; we landed the repair ourselves
      "already-repaired" -- the OWNER has already landed it. Provenance, not failure.
      "unrecognised"     -- neither pattern present; the source moved. Callers must
                            SKIP the witnesses rather than fail on it.
    """
    src = checkout_py.read_text()
    if DEFECT_LINE in src:
        checkout_py.write_text(src.replace(DEFECT_LINE, REPAIR_LINE, 1))
        return "applied"
    if REPAIR_LINE.strip() in src or 'sum(i["price_cents"]' in src:
        return "already-repaired"
    return "unrecognised"


def old_g8_predicate(fleet: pathlib.Path, baselines: dict[str, str]) -> tuple[bool, list[str]]:
    """The PRE-REPAIR G8, reproduced faithfully: fatal on ANY drift from the
    FROZEN historical constants."""
    paths = {
        "session.js": fleet / "checkout-api" / "service" / "checkout" / "session.js",
        "usage_aggregator.py": fleet / "fabric-gateway-demo" / "service" / "usage_aggregator.py",
        "checkout.py": fleet / "fabric-ic-incident-target" / "checkout.py",
    }
    drift = []
    checked = 0
    for name, p in paths.items():
        if not p.is_file() or name not in baselines:
            continue
        checked += 1
        if sha(p) != baselines[name]:
            drift.append(name)
    return (not drift and checked > 0), drift


def fleet_copy(tmp: pathlib.Path) -> pathlib.Path:
    fleet = tmp / "fleet"
    fleet.mkdir()
    for r in ("checkout-api", "fabric-gateway-demo", "fabric-ic-incident-target"):
        shutil.copytree(FLEET / r, fleet / r)
    return fleet


def g8_verdict_expr(src: str) -> tuple[str | None, list[str], bool]:
    """Extract G8's VERDICT predicate and check it SEMANTICALLY, not syntactically.

    Syntactic checks kept losing this arms race, and each loss was caught by the
    negative control -- worth recording, because it shaped this function:

      * A substring check for "not self_inflicted and checked > 0" is bypassed by
        WIDENING the verdict to "not owner_edits and not self_inflicted and
        checked > 0" -- the substring is still present, the bug is fully restored.
      * Requiring the identifier `checked` is bypassed by `checked >= 0`.
      * Requiring a `checked > 0` Compare node with no `Or` is bypassed by
        `not (checked > 0)`, which INVERTS the guard.

    Pattern-matching source text can always be out-spelled. So instead we EVALUATE
    the predicate as a function of its three inputs and require it to satisfy the
    exact truth table the repair demands:

      self_inflicted   owner_edits   checked   required verdict
      ---------------  -----------   -------   ----------------
      no               no            >0        PASS   (clean tree)
      no               YES           >0        PASS   (an OWNER EDIT is provenance,
                                                       never a failure -- the whole
                                                       point of INC-23)
      YES              no            >0        FAIL   (we mutated prod: still bites)
      YES              YES           >0        FAIL
      no               no            0         FAIL   (vacuous: nothing examined is
                                                       not a passing check)

    Any predicate satisfying every row IS the repair, however it is spelled; any
    predicate that punishes an owner edit, tolerates self-inflicted drift, or passes
    vacuously fails at least one row.

    The positive rows are sampled across MANY values of `checked` (1..8, plus large
    ones), not just one: a verdict special-cased to the sampled count -- say
    `checked == 3` -- would satisfy a single-sample table while still being wrong for
    every other tree. Only `checked > 0` (however spelled) satisfies all of them
    together with the `checked == 0` FAIL row.

    Returns (unparsed verdict, identifiers in it, satisfies_truth_table).
    """
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "gate" and len(node.args) >= 2):
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)
                and first.value.startswith("G8")):
            continue

        verdict = node.args[1]
        expr = ast.unparse(verdict)
        names = [n.id for n in ast.walk(verdict) if isinstance(n, ast.Name)]

        # The verdict must be a pure function of these three locals. If it reaches
        # for anything else, we cannot certify it and refuse to pass the gate.
        allowed = {"self_inflicted", "owner_edits", "checked"}
        if not set(names).issubset(allowed):
            return expr, names, False

        # (self_inflicted, owner_edits, checked) -> required verdict.
        # Every POSITIVE `checked` must behave identically -- so we sweep a range
        # rather than sampling one value that a `checked == N` special-case could
        # satisfy.
        POSITIVE_COUNTS = [1, 2, 3, 4, 5, 6, 7, 8, 17, 99, 1000]
        rows: list[tuple[list, list, int, bool]] = []
        for ch in POSITIVE_COUNTS:
            rows.append(([], [], ch, True))       # clean tree              -> PASS
            rows.append(([], ["x"], ch, True))    # owner edit = provenance -> PASS
            rows.append((["x"], [], ch, False))   # we left prod mutated    -> FAIL
            rows.append((["x"], ["x"], ch, False))
        # Nothing examined is NEVER a passing check, whatever else is true.
        rows.append(([], [], 0, False))
        rows.append(([], ["x"], 0, False))
        rows.append((["x"], [], 0, False))

        code = compile(ast.Expression(body=verdict), "<g8_verdict>", "eval")
        try:
            for si, oe, ch, required in rows:
                got = bool(eval(  # noqa: S307 -- evaluating an expression WE parsed
                    code,
                    {"__builtins__": {}},
                    {"self_inflicted": si, "owner_edits": oe, "checked": ch},
                ))
                if got is not required:
                    return expr, names, False
        except Exception:  # noqa: BLE001 -- an unevaluable verdict cannot be certified
            return expr, names, False

        return expr, names, True
    return None, [], False


def main() -> int:
    print("Fabric incident commander — INC-23 verification gates\n")

    pre_run_hashes = {
        p: sha(p)
        for p in (
            CHECKOUT_API / "service" / "checkout" / "session.js",
            GATEWAY / "service" / "usage_aggregator.py",
            CHECKOUT_PY,
        )
        if p.is_file()
    }

    src = INC15.read_text()

    # ------------------------------------------------------------------ G0 --
    # STATIC, and it needs no siblings -- so THIS is the gate that guards the
    # repair inside `checkout-api` CI, which clones only this repo.
    #
    # The verdict is inspected through the AST, not grepped: see g8_verdict_expr.
    # A substring check on "not self_inflicted and checked > 0" is BYPASSABLE --
    # widening the verdict back to "not owner_edits and not self_inflicted and
    # checked > 0" still contains that substring while fully restoring the bug.
    # The negative control caught exactly that, which is why this gate uses the AST.
    has_snapshot = "RUN_SNAPSHOT" in src and "_snapshot_now" in src
    compares_snapshot = "snapshot is not None and actual != snapshot" in src
    verdict_expr, verdict_names, truth_table_ok = g8_verdict_expr(src)
    # The verdict is certified SEMANTICALLY: g8_verdict_expr evaluates it against the
    # required truth table (owner edit -> PASS; self-inflicted drift -> FAIL; nothing
    # examined -> FAIL). Syntactic checks lost this arms race three times -- widening
    # the conjunction, `checked >= 0`, and `not (checked > 0)` each defeated a
    # pattern-match while restoring the bug. Evaluating the predicate closes every
    # spelling at once.
    verdict_ignores_owner_edits = (
        verdict_expr is not None
        and "owner_edits" not in verdict_names
        and truth_table_ok
    )
    gate(
        "G0 STATIC — the shipped INC-15 verifier carries the INC-23 repair (no siblings needed)",
        has_snapshot and compares_snapshot and verdict_ignores_owner_edits,
        f"RUN_SNAPSHOT anchor present={has_snapshot}; G8 compares against the "
        f"start-of-run snapshot={compares_snapshot}; G8 verdict (parsed from the AST) "
        f"= `{verdict_expr}` -> references owner_edits="
        f"{'owner_edits' in verdict_names} (must be False); "
        f"satisfies the required TRUTH TABLE (owner edit=PASS, self-inflicted "
        f"drift=FAIL, nothing-examined=FAIL)={truth_table_ok} (must be True). "
        f"Strip, widen, invert, or vacuously weaken the repair and this gate goes RED in CI.",
    )

    siblings = TARGET.is_file() or (TARGET.is_dir() and GATEWAY.is_dir())
    if not (TARGET.is_dir() and GATEWAY.is_dir()):
        skip(
            "G1-G7 behavioural witnesses",
            "the sibling fleet repos are not in this checkout (CI clones only this "
            "repo), and every witness structurally needs their SOURCES. Reported as "
            "SKIPPED -- never a pass, never a hard failure, so this step cannot become "
            "the permanently-red INC-11 bug it exists to prevent. G0 above still "
            "guards the repair here.",
        )
        return _summary()

    baselines = frozen_baselines()
    if len(baselines) < 3:
        gate(
            "G2 WITNESS A (necessity)",
            False,
            f"could not parse the frozen baseline constants (found {baselines}). "
            "The necessity witness MUST anchor to them, not to runtime bytes.",
        )
        return _summary()

    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)

        # -------------------------------------------------------------- G1 --
        clean = fleet_copy(tmp / "a") if False else None  # placeholder avoided
        base = tmp / "clean"
        base.mkdir()
        fleet_clean = base / "fleet"
        fleet_clean.mkdir()
        for r in ("checkout-api", "fabric-gateway-demo", "fabric-ic-incident-target"):
            shutil.copytree(FLEET / r, fleet_clean / r)
        res = run(fleet_clean / "checkout-api" / INC15_REL, fleet_clean / "checkout-api")
        blob = res.stdout + res.stderr
        m = re.search(r"^INC-15 GATES: (\d+)/(\d+) passed", blob, re.M)
        gate(
            "G1 NO REGRESSION — the repaired verifier still passes on an untouched fleet",
            res.returncode == 0,
            f"exit={res.returncode} tally={m.group(0) if m else 'none'} "
            f"failed={failed_gates(blob) or 'none'}",
        )

        # ---- a fleet where the OWNER HAS LANDED THE CORRECT INC-6 REPAIR ----
        rep = tmp / "repaired"
        rep.mkdir()
        fleet_rep = rep / "fleet"
        fleet_rep.mkdir()
        for r in ("checkout-api", "fabric-gateway-demo", "fabric-ic-incident-target"):
            shutil.copytree(FLEET / r, fleet_rep / r)
        repaired_checkout = fleet_rep / "fabric-ic-incident-target" / "checkout.py"
        repair_state = land_owner_repair(repaired_checkout)

        # If the OWNER has ALREADY landed the INC-6 fix upstream, there is no defect
        # left to revert -- so this tree cannot host the necessity/divergence
        # witnesses. The correct answer is SKIP, never a hard failure: failing here
        # would mean this verifier punishes the owner for doing exactly what we asked,
        # which is the entire defect INC-23 exists to cure.
        #
        # G0 (static) and G1 (no regression) still ran above and still guard the
        # repair. G3/G7 below still run: the repaired verifier must PASS on the
        # owner-repaired tree and must leave production untouched -- which is precisely
        # the property that matters most once the owner HAS acted.
        if repair_state != "applied":
            note = (
                "the OWNER has already landed the INC-6 repair upstream"
                if repair_state == "already-repaired"
                else "checkout.py no longer matches either the known defect or the known "
                     "repair (the source moved)"
            )
            skip(
                "G2/G4/G6 necessity + divergence witnesses",
                f"{note}, so this tree cannot host a witness that REVERTS the defect "
                f"(there is nothing left to revert). Reported as SKIPPED -- never a "
                f"pass, and never a hard failure. Failing here would punish the owner "
                f"for landing the very repair this commander has been requesting, "
                f"i.e. the exact defect INC-23 diagnoses. G0/G1 above and G3/G7 below "
                f"still execute and still guard the repair.",
            )

        # -------------------------------------------------------------- G6 --
        # The witness is only meaningful if the repair is GENUINELY CORRECT.
        probe = tmp / "probe.py"
        probe.write_text(
            "import importlib.util, pathlib, sys\n"
            "spec = importlib.util.spec_from_file_location('c', pathlib.Path(sys.argv[1]))\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "print(m.apply_discount(30000, [{'price_cents': 1000}]),\n"
            "      m.apply_discount(50000, [{'price_cents': 10000}] * 5),\n"
            "      m.apply_discount(30000, []))\n"
        )
        pr = subprocess.run(
            [sys.executable, str(probe), str(repaired_checkout)],
            env=child_env(),
            capture_output=True, text=True, timeout=120,
        )
        vals = pr.stdout.split()
        correct = vals == ["30000", "42500", "30000"]
        if repair_state == "applied":
            gate(
                "G6 the owner repair used as the witness is GENUINELY CORRECT (by execution)",
                correct,
                f"$300 order / one $10 eligible item -> ${int(vals[0])/100:,.2f} (deployed leaks "
                f"$255.00); 5 x $100 -> ${int(vals[1])/100:,.2f} (contract $425.00, 15% tier); "
                f"zero-item guard -> ${int(vals[2])/100:,.2f}"
                if len(vals) == 3 else f"probe failed: {pr.stderr[:200]}",
            )

        # -------------------------------------------- G2 · WITNESS A (necessity) --
        old_ok, old_drift = old_g8_predicate(fleet_rep, baselines)
        if repair_state == "applied":
            gate(
                "G2 WITNESS A (necessity) — the PRE-REPAIR G8 predicate REJECTS a correct owner fix",
                (not old_ok) and "checkout.py" in old_drift,
                f"anchored to the FROZEN historical constants (not runtime bytes): "
                f"old predicate verdict={'PASS' if old_ok else 'REJECT'} drift={old_drift}. "
                f"The owner lands the repair we asked for, and the old gate hard-fails.",
            )

        # ------------------------------------------ G3 · WITNESS B (sufficiency) --
        post = run(fleet_rep / "checkout-api" / INC15_REL, fleet_rep / "checkout-api")
        pblob = post.stdout + post.stderr
        p_failed = failed_gates(pblob)
        provenance_reported = "OWNER EDIT" in pblob or "PROVENANCE" in pblob
        gate(
            "G3 WITNESS B (sufficiency) — the REPAIRED verifier PASSES on that same tree",
            post.returncode == 0 and not p_failed,
            f"exit={post.returncode} failed={p_failed or 'none'}; "
            f"owner edit surfaced as provenance={provenance_reported}",
        )

        # ------------------------------------------------- G4 · DIVERGENCE ------
        # Load-bearing. Same tree: the OLD predicate rejects, the NEW one passes.
        # And INC-19 -- which merely re-runs the INC-15 verifier -- recovers with it.
        inc19 = run(fleet_rep / "checkout-api" / INC19_REL, fleet_rep / "checkout-api")
        if repair_state == "applied":
            gate(
                "G4 DIVERGENCE (load-bearing) — identical tree: PRE = REJECT [RED] · POST = GREEN",
                (not old_ok) and post.returncode == 0 and inc19.returncode == 0,
                f"PRE-repair predicate={'PASS' if old_ok else 'REJECT [RED]'} · "
                f"POST-repair INC-15 exit={post.returncode} [GREEN] · "
                f"INC-19 (re-runs INC-15, inherited the failure) exit={inc19.returncode}. "
                f"Had both behaved alike the repair would be a no-op.",
            )

        # --------------------------------------------- G5 · ANTI-WEAKENING ------
        # THE GATE THAT MATTERS MOST. Deleting G8 outright would ALSO have turned
        # the red gate green and satisfied G2/G3/G4. It must still be FATAL when the
        # verifier leaves production MUTATED across its own run. We simulate that by
        # making the verifier corrupt a source and not restore it.
        weak = tmp / "weak"
        weak.mkdir()
        fleet_weak = weak / "fleet"
        fleet_weak.mkdir()
        for r in ("checkout-api", "fabric-gateway-demo", "fabric-ic-incident-target"):
            shutil.copytree(FLEET / r, fleet_weak / r)
        v = fleet_weak / "checkout-api" / INC15_REL
        vsrc = v.read_text()
        # Inject a sabotage line into main(): mutate production and never restore it.
        anchor = '    print("Fabric incident commander — INC-15 verification gates\\n")'
        assert anchor in vsrc, "could not locate main()'s banner to inject sabotage"
        sabotage = (
            anchor
            + "\n    # SIMULATED BUG: mutate production and fail to restore it.\n"
            + "    _sab = CHECKOUT_API / 'service' / 'checkout' / 'session.js'\n"
            + "    _sab.write_text(_sab.read_text() + '\\n// left mutated by the verifier\\n')\n"
        )
        v.write_text(vsrc.replace(anchor, sabotage, 1))
        wres = run(v, fleet_weak / "checkout-api")
        wblob = wres.stdout + wres.stderr
        w_failed = failed_gates(wblob)
        gate(
            "G5 ANTI-WEAKENING — a verifier that leaves production MUTATED is STILL rejected",
            wres.returncode == 1 and any(g.startswith("G8") for g in w_failed),
            f"verifier sabotaged to mutate session.js and not restore it: "
            f"exit={wres.returncode} failed={w_failed or 'none'}. "
            f"This is why the repair is a CORRECTION, not a COVER-UP: deleting G8 "
            f"would have satisfied G2/G3/G4 and FAILED here.",
        )

    # ------------------------------------------------------------------ G7 --
    post_run_hashes = {p: sha(p) for p in pre_run_hashes if p.is_file()}
    unchanged = [p.name for p in pre_run_hashes if post_run_hashes.get(p) == pre_run_hashes[p]]
    moved = [p.name for p in pre_run_hashes if post_run_hashes.get(p) != pre_run_hashes[p]]
    gate(
        "G7 NO DRIFT FROM THIS VERIFIER — every source byte-identical before/after",
        not moved,
        f"{len(unchanged)}/{len(pre_run_hashes)} byte-identical; moved={moved or 'none'}. "
        f"All mutation testing happened in throwaway copies.",
    )

    return _summary()


def _summary() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{'=' * 74}\nINC-23 GATES: {passed}/{total} passed", end="")
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
    print("All executed gates green. The drift gate no longer punishes an owner for")
    print("landing the billing repair this commander has been asking for -- and it")
    print("STILL fails a verifier that leaves production mutated. Correction, not cover-up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
