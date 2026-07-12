#!/usr/bin/env python3
"""Fabric incident commander — INC-16 verifier (double-witness).

THE FINDING
-----------
`verify_inc15_cross_fleet_discovery.py` gates the cross-fleet discovery repair.
Two of its nine gates are mutation-style witnesses:

  G4 (WITNESS A)  the PRE-repair discovery is BLIND on this filesystem
  G5 (WITNESS B)  DIVERGENCE — OLD skips all 3 cross-fleet gates, NEW runs all 3

Both evaluated the OLD-vs-NEW comparison against the AMBIENT fleet roots. The
OLD discovery looks for directories literally named `incident-target/` and
`gateway/` — so when the sibling repos are cloned under exactly those LEGACY
names, the OLD lookup RESOLVES them, "the old discovery is blind" is FALSE, and
G4/G5 went RED:

    siblings as gateway/ + incident-target/          -> 7/9, exit 1  (G4,G5 FAIL)
    same tree as fabric-gateway-demo/ + fabric-ic-*  -> 9/9, exit 0

The verifier's exit code was a function of how somebody named their clone
directories, not of the property under test.

And it was self-contradictory: gate **G3 of that very file** asserts the legacy
names MUST still resolve ("the fix adds, never replaces"). So the verifier
declared the legacy layout supported and then hard-failed on it.

A gate whose colour depends on ambient directory naming is the fleet's signature
failure mode — *a gate that cannot be trusted is decoration* — recurring for the
fifth time (INC-9 / INC-11 / INC-12 / INC-15 / here).

THE REPAIR
----------
Witness the divergence on a tree that can actually HOST the witness:
  * ambient fleet under the REAL repo names -> witness there (behaviour unchanged)
  * ambient fleet under the LEGACY names    -> witness on a synthetic canonical
    real-name fleet, because a tree the OLD lookup can SEE cannot host a
    blindness witness
  * no fleet at all (bare CI checkout)      -> SKIP, as before

GATES
-----
  G1  WITNESS A (necessity) — the PRE-patch verifier FAILS on the legacy layout
  G2  WITNESS B (sufficiency) — the POST-patch verifier PASSES on the legacy layout
  G3  DIVERGENCE — same tree, opposite outcomes: the repair is NOT a no-op
  G4  no regression — POST-patch still passes under the REAL repo names
  G5  bare checkout still SKIPs G4/G5 and exits 0 (CI not permanently red)
  G6  a skip is never laundered into a pass (un-run gates stay out of the
      denominator)
  G7  NO PRODUCTION DRIFT — every deployed source is byte-for-byte unchanged

G1 and G3 are load-bearing. Without them a "green" patch proves nothing: a gate
that was already green cannot demonstrate that the fix was needed, and a fix
whose before/after behave alike is a no-op dressed up as a repair.

Exit: 0 = every gate passed.
"""
from __future__ import annotations

import hashlib
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve()
CHECKOUT_API = HERE.parents[2]
FLEET = CHECKOUT_API.parent
REL = "artifacts/incident/verify_inc15_cross_fleet_discovery.py"
PATCHED = CHECKOUT_API / REL

# The sibling fleet repos, under whatever names this workspace cloned them.
TARGET_NAMES = ("fabric-ic-incident-target", "incident-target")
GATEWAY_NAMES = ("fabric-gateway-demo", "gateway")

REAL_TARGET_DIR = "fabric-ic-incident-target"
REAL_GATEWAY_DIR = "fabric-gateway-demo"

# Deployed production revisions. FULL sha256. None of these may change.
BASELINES = {
    "session.js": (
        CHECKOUT_API / "service" / "checkout" / "session.js",
        "b45a8eeceaa142dd70aea4182930d02edb2d23ce90f0f02527910abb5f18d7e8",
    ),
}

RESULTS: list[tuple[str, bool, str]] = []


def gate(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n         {detail}" if detail else ""))


def _find_sibling(names: tuple[str, ...], *parts: str) -> pathlib.Path | None:
    for n in names:
        p = FLEET / n
        if (p / pathlib.Path(*parts)).is_file():
            return p
    return None


# ---------------------------------------------------------------------------
# The PRE-PATCH witness logic, reproduced verbatim from the code this patch
# replaces. Reproducing it (rather than diffing text) lets us EXECUTE the old
# behaviour and watch it fail — the only evidence that the repair was necessary.
# ---------------------------------------------------------------------------
PRE_PATCH_G4_G5 = '''
    # ------------------------------------------- G4 · WITNESS A (blindness) --
    o_target, o_gateway = old_discovery(roots)
    if siblings_present:
        gate(
            "G4 WITNESS A — the PRE-REPAIR discovery is BLIND on this very filesystem",
            o_target is None and o_gateway is None,
            f"old logic finds NEITHER sibling despite both being present: "
            f"incident-target={o_target} gateway={o_gateway}. "
            f"This is why G6a/G6b/G6c never executed, in ANY environment.",
        )
    else:
'''


def _stage(root: pathlib.Path, target_dir: str | None, gateway_dir: str | None) -> pathlib.Path:
    """Copy checkout-api (+ optionally the siblings, under the given names) to root."""
    root.mkdir(parents=True, exist_ok=True)
    dst = root / "checkout-api"
    shutil.copytree(CHECKOUT_API, dst)

    src_target = _find_sibling(TARGET_NAMES, "checkout.py")
    src_gateway = _find_sibling(GATEWAY_NAMES, "service", "usage_aggregator.py")
    if target_dir and src_target:
        shutil.copytree(src_target, root / target_dir)
    if gateway_dir and src_gateway:
        shutil.copytree(src_gateway, root / gateway_dir)
    return dst


def _revert_to_pre_patch(checkout: pathlib.Path) -> bool:
    """Undo the INC-16 repair in a THROWAWAY copy, restoring the ambient-roots witness.

    Returns False if the expected post-patch shape is not found, or if the result
    does not compile — either would mean this verifier has drifted away from the
    code it is meant to guard, which is a hard fail, never a silent skip.

    The revert is done by targeted substitution rather than index slicing: it puts
    G4/G5 back on the AMBIENT fleet roots, which is exactly the pre-patch
    behaviour, and asserts each substitution actually applied.
    """
    path = checkout / REL
    src = path.read_text()
    if "_witness_roots(" not in src:
        return False

    # 1. G4/G5 evaluate the OLD/NEW comparison on the AMBIENT roots (pre-patch).
    before = src
    src = src.replace(
        "    witness_roots, witness_mode = _witness_roots(roots, siblings_present)\n"
        "    if witness_roots is not None:\n"
        "        o_target, o_gateway = old_discovery(witness_roots)\n"
        "        w_target, w_gateway = new_discovery(witness_roots)\n",
        "    witness_roots = roots if siblings_present else None\n"
        "    witness_mode = 'ambient roots (pre-patch behaviour)'\n"
        "    if siblings_present:\n"
        "        o_target, o_gateway = old_discovery(roots)\n"
        "        w_target, w_gateway = n_target, n_gateway\n",
        1,
    )
    if src == before:
        return False

    # 2. G5's guard keyed off siblings_present, not the witness roots.
    before = src
    src = src.replace(
        "    if witness_roots is not None:\n"
        "        old_would_skip = o_target is None or o_gateway is None\n"
        "        new_would_run = w_target is not None and w_gateway is not None",
        "    if siblings_present:\n"
        "        old_would_skip = o_target is None or o_gateway is None\n"
        "        new_would_run = n_target is not None and n_gateway is not None",
        1,
    )
    if src == before:
        return False

    path.write_text(src)

    # The reverted file must still be valid Python, and must no longer contain the
    # repair. If either is false, we are not witnessing the pre-patch behaviour.
    try:
        compile(src, str(path), "exec")
    except SyntaxError:
        return False
    return "_witness_roots(roots, siblings_present)" not in src


def _run(checkout: pathlib.Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, REL],
        cwd=str(checkout),
        capture_output=True,
        text=True,
        timeout=900,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _tally(blob: str) -> tuple[int, int] | None:
    m = re.search(r"^INC-15 GATES: (\d+)/(\d+) passed", blob, re.M)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _red(blob: str, g: str) -> bool:
    return bool(re.search(rf"^\[FAIL\] {g} ", blob, re.M))


def _green(blob: str, g: str) -> bool:
    return bool(re.search(rf"^\[PASS\] {g} ", blob, re.M))


def _skipped(blob: str, g: str) -> bool:
    return bool(re.search(rf"^\[SKIP\] {g} ", blob, re.M))


def _fmt(t: tuple[int, int] | None) -> str:
    return f"{t[0]}/{t[1]}" if t else "none"


def main() -> int:
    print("Fabric incident commander — INC-16 verification gates\n")

    have_target = _find_sibling(TARGET_NAMES, "checkout.py") is not None
    have_gateway = _find_sibling(GATEWAY_NAMES, "service", "usage_aggregator.py") is not None
    if not (have_target and have_gateway):
        gate(
            "G0 the sibling fleet repos are present (required to witness a LAYOUT bug)",
            False,
            "FATAL: this verifier's entire subject is how discovery behaves across "
            "directory LAYOUTS, so it structurally requires the sibling repos. "
            "Run it from the incident-commander workspace, not a bare CI checkout. "
            "Refusing to report passes for gates that cannot execute.",
        )
        return _summary()

    with tempfile.TemporaryDirectory() as tmp:
        tmpd = pathlib.Path(tmp)

        # ---- legacy layout, PRE-patch (necessity) -------------------------- G1
        legacy_old = _stage(tmpd / "legacy_old", "incident-target", "gateway")
        reverted = _revert_to_pre_patch(legacy_old)
        old_rc, old_blob = _run(legacy_old) if reverted else (0, "")
        old_t = _tally(old_blob)
        gate(
            "G1 WITNESS A (necessity) — the PRE-patch verifier FAILS on the legacy layout",
            reverted
            and old_rc == 1
            and _red(old_blob, "G4")
            and _red(old_blob, "G5")
            and _green(old_blob, "G3"),
            f"pre-patch on siblings named gateway/ + incident-target/: exit={old_rc} "
            f"tally={_fmt(old_t)} G4=RED G5=RED — while G3 of the "
            f"same run PASSES, asserting that exact layout is supported. The verifier "
            f"contradicted itself. (revert applied={reverted})",
        )

        # ---- legacy layout, POST-patch (sufficiency) ----------------------- G2
        legacy_new = _stage(tmpd / "legacy_new", "incident-target", "gateway")
        new_rc, new_blob = _run(legacy_new)
        new_t = _tally(new_blob)
        gate(
            "G2 WITNESS B (sufficiency) — the POST-patch verifier PASSES on the legacy layout",
            new_rc == 0
            and new_t == (9, 9)
            and _green(new_blob, "G4")
            and _green(new_blob, "G5"),
            f"post-patch, same layout: exit={new_rc} tally={_fmt(new_t)} "
            f"G4=PASS G5=PASS — the witness is now evaluated on a tree that can host it "
            f"instead of on whatever the clone dirs happen to be called.",
        )

        # ---- the divergence is the proof of value -------------------------- G3
        gate(
            "G3 DIVERGENCE — same tree, opposite outcomes: the repair is NOT a no-op",
            old_rc == 1 and new_rc == 0,
            f"identical filesystem, identical siblings: PRE-patch exit={old_rc} [RED] · "
            f"POST-patch exit={new_rc} [GREEN]. Had both agreed, the patch would be "
            f"decoration and this gate would say so.",
        )

        # ---- real names must keep working --------------------------------- G4
        real = _stage(tmpd / "real", REAL_TARGET_DIR, REAL_GATEWAY_DIR)
        r_rc, r_blob = _run(real)
        r_t = _tally(r_blob)
        gate(
            "G4 NO REGRESSION — POST-patch still passes under the REAL repo names",
            r_rc == 0 and r_t == (9, 9) and _green(r_blob, "G4") and _green(r_blob, "G5"),
            f"siblings named {REAL_GATEWAY_DIR}/ + {REAL_TARGET_DIR}/: exit={r_rc} "
            f"tally={_fmt(r_t)} — the path that already worked is untouched.",
        )

        # ---- bare checkout: SKIP, never red, never a phantom pass ---- G5 / G6
        bare = _stage(tmpd / "bare", None, None)
        b_rc, b_blob = _run(bare)
        b_t = _tally(b_blob)
        gate(
            "G5 BARE CHECKOUT — G4/G5 SKIP and CI is not left permanently red",
            b_rc == 0 and _skipped(b_blob, "G4") and _skipped(b_blob, "G5"),
            f"no siblings (exactly what checkout-api CI clones): exit={b_rc} "
            f"G4=SKIP G5=SKIP. Making these fatal would re-commit the INC-11 "
            f"expired-precondition bug inside the fix for it.",
        )
        gate(
            "G6 a SKIP is never laundered into a PASS (un-run gates stay out of the denominator)",
            b_t is not None
            and b_t[0] == b_t[1]
            and b_t[1] == 5
            and "SKIPPED — NOT counted as passes" in b_blob,
            f"bare checkout tally={_fmt(b_t)} with 4 gates reported SKIPPED — the "
            f"INC-15 finding (a skip folded into a confident pass count) has NOT "
            f"regressed.",
        )

    # ---- production is untouched ------------------------------------------ G7
    drift = []
    for label, (path, expected) in BASELINES.items():
        if not path.is_file():
            drift.append(f"{label}: MISSING")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            drift.append(f"{label}: {actual} != {expected}")
    for names, parts, label, expected in (
        (
            GATEWAY_NAMES,
            ("service", "usage_aggregator.py"),
            "usage_aggregator.py",
            "bb21e50f7b5dab4463b71984bbe86a5df2b6ba442ffeff84d9b70815781750e5",
        ),
        (
            TARGET_NAMES,
            ("checkout.py",),
            "checkout.py",
            "da2a02fd87aec668467114e0bc30ff7c2fe7fd3d8f105f5f156361b9c87c5c5e",
        ),
    ):
        base = _find_sibling(names, *parts)
        if base is None:
            drift.append(f"{label}: MISSING")
            continue
        actual = hashlib.sha256((base / pathlib.Path(*parts)).read_bytes()).hexdigest()
        if actual != expected:
            drift.append(f"{label}: {actual} != {expected}")

    gate(
        "G7 NO PRODUCTION DRIFT — every deployed source is byte-for-byte unchanged",
        not drift,
        "session.js, usage_aggregator.py and checkout.py all match their deployed "
        "sha256. This patch touches ONLY a verifier under artifacts/incident/. "
        "The three owner-blocked billing defects (INC-3/INC-6/INC-8) remain live and "
        "UNPATCHED — deliberately: their correct behaviour is a billing-policy "
        "decision, not a mechanical repair."
        if not drift
        else "; ".join(drift),
    )

    return _summary()


def _summary() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{'=' * 74}\nINC-16 GATES: {passed}/{total} passed\n{'=' * 74}")
    if passed != total:
        for name, ok, _ in RESULTS:
            if not ok:
                print(f"  FAILED: {name}")
        return 1
    print("All gates green. The INC-15 witness gates no longer depend on how the")
    print("fleet's clone directories are NAMED: they are red when the discovery is")
    print("broken and green when it is not — which is the only thing a gate is for.")
    print("No production source was modified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
