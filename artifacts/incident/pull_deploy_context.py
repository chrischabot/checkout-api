#!/usr/bin/env python3
"""Fabric incident commander -- PR / DEPLOY CONTEXT.

Asserting "GitHub PR/deploy context: LIVE" without showing a single fetched PR,
run, or SHA is not provenance -- it is a claim. This module FETCHES the context and
writes it down, so the brief can cite specific evidence.

What it retrieves, per repo, from the authenticated GitHub REST API:

  * recent commits on the default branch  (the DEPLOYS)
  * recent CI workflow runs + conclusions (did the deploy go green?)
  * open pull requests                    (what is in flight)

Then it CORRELATES: for each known live defect, which deploy introduced the code
path it lives in? That correlation is the whole reason deploy context matters --
it turns "this function is wrong" into "this function became wrong at THIS commit".

Credentials come from the environment (GITHUB_TOKEN / GH_TOKEN). If none is
present, that is REPORTED with the exact HTTP status proving it -- never guessed
around, and never silently reported as "no deploys found".

Output: deploy_context.json
"""
from __future__ import annotations

import json
import os
import pathlib
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT / "deploy_context.json"

API = "https://api.github.com"
OWNER = "chrischabot"
REPOS = ["checkout-api", "fabric-gateway-demo", "fabric-ic-incident-target"]

# Each live defect, and the FILE its code path lives in. The correlator finds the
# commit that last touched that file -- i.e. the deploy that shipped the defect.
DEFECT_PATHS = {
    "INC-6 (checkout discount leak)": ("fabric-ic-incident-target", "checkout.py"),
    "INC-5 (malformed record kills batch)": ("fabric-gateway-demo", "service/usage_aggregator.py"),
    "INC-8 (null model -> None bucket)": ("fabric-gateway-demo", "service/usage_aggregator.py"),
}


def _token() -> str | None:
    for k in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT"):
        v = os.environ.get(k)
        if v:
            return v
    return None


def _get(path: str, token: str | None):
    url = f"{API}{path}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "fabric-incident-commander",
    })
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, json.loads(r.read(400_000).decode("utf-8", "replace")), None
    except urllib.error.HTTPError as e:
        return e.code, None, e.read(500).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return None, None, f"{type(e).__name__}: {e}"


def collect(repo: str, token: str | None) -> dict:
    rec: dict = {
        "repo": f"{OWNER}/{repo}",
        "attempted": [],
        "deploys": [],
        "workflow_runs": [],
        "open_prs": [],
        "status": "unavailable",
        "reason": None,
    }

    # --- deploys (commits on the default branch) ---
    p = f"/repos/{OWNER}/{repo}/commits?per_page=10"
    code, body, err = _get(p, token)
    rec["attempted"].append({"path": p, "status": code, "error": err})
    if code == 200 and isinstance(body, list):
        for c in body:
            rec["deploys"].append({
                "sha": (c.get("sha") or "")[:12],
                "message": ((c.get("commit") or {}).get("message") or "").splitlines()[0][:120],
                "date": ((c.get("commit") or {}).get("author") or {}).get("date"),
            })

    # --- CI runs: did the deploy actually go green? ---
    p = f"/repos/{OWNER}/{repo}/actions/runs?per_page=10"
    code, body, err = _get(p, token)
    rec["attempted"].append({"path": p, "status": code, "error": err})
    if code == 200 and isinstance(body, dict):
        for r in body.get("workflow_runs", []):
            rec["workflow_runs"].append({
                "name": r.get("name"),
                "branch": r.get("head_branch"),
                "sha": (r.get("head_sha") or "")[:12],
                "status": r.get("status"),
                "conclusion": r.get("conclusion"),
                "created_at": r.get("created_at"),
                "url": r.get("html_url"),
            })

    # --- open PRs: what is in flight ---
    p = f"/repos/{OWNER}/{repo}/pulls?state=open&per_page=20"
    code, body, err = _get(p, token)
    rec["attempted"].append({"path": p, "status": code, "error": err})
    if code == 200 and isinstance(body, list):
        for pr in body:
            rec["open_prs"].append({
                "number": pr.get("number"),
                "title": (pr.get("title") or "")[:110],
                "head": (pr.get("head") or {}).get("ref"),
                "created_at": pr.get("created_at"),
                "url": pr.get("html_url"),
            })

    got = rec["deploys"] or rec["workflow_runs"] or rec["open_prs"]
    statuses = [a["status"] for a in rec["attempted"]]
    if got:
        rec["status"] = "pulled"
        rec["reason"] = (f"{len(rec['deploys'])} deploy(s), {len(rec['workflow_runs'])} CI run(s), "
                         f"{len(rec['open_prs'])} open PR(s) retrieved")
    elif all(s is None for s in statuses):
        rec["reason"] = "network failure reaching the GitHub API"
    elif any(s in (401, 403) for s in statuses):
        rec["reason"] = (f"GitHub API returned {[s for s in statuses if s in (401, 403)]} "
                         f"-- credential missing or insufficient scope "
                         f"(token present={bool(token)}). Set GITHUB_TOKEN.")
    else:
        rec["status"] = "empty"
        rec["reason"] = f"API answered {statuses} but returned no deploys/runs/PRs"
    return rec


def _commit_history(repo: str, path: str, token: str | None, max_pages: int = 10):
    """Fetch the FULL commit history for a path, oldest-last, by PAGINATING.

    WHY PAGINATION IS LOAD-BEARING, NOT AN OPTIMISATION.

    An earlier draft fetched `per_page=5` and called the oldest commit in that page
    `introduced_by`. That is only the oldest commit I HAPPENED TO SEE -- if the file's
    history is longer than one page, the real origin is older, and the reported
    "exposure window opens" date would be WRONG in the direction that matters: it
    would tell the owner to reconcile a SHORTER window than the defect actually had.
    An incident brief that under-reports the exposure window is worse than one that
    admits ignorance.

    So: walk pages until GitHub returns a short page (= end of history). Report
    `exhaustive=True` only when we genuinely reached the end; otherwise the caller
    must downgrade its claim from "introduced by" to a bounded approximation.

    Returns (commits_newest_first, exhaustive).
    """
    per_page = 100
    all_commits: list[dict] = []
    for page in range(1, max_pages + 1):
        p = (f"/repos/{OWNER}/{repo}/commits"
             f"?path={path}&per_page={per_page}&page={page}")
        code, body, err = _get(p, token)
        if code != 200 or not isinstance(body, list):
            return all_commits, False
        all_commits.extend(body)
        if len(body) < per_page:
            return all_commits, True   # short page => we reached the end of history
    return all_commits, False          # hit the page cap: NOT exhaustive


def _fmt(c: dict) -> dict:
    return {
        "sha": (c.get("sha") or "")[:12],
        "message": ((c.get("commit") or {}).get("message") or "").splitlines()[0][:120],
        "date": ((c.get("commit") or {}).get("author") or {}).get("date"),
    }


def correlate(by_repo: dict[str, dict], token: str | None) -> list[dict]:
    """For each live defect, WHICH DEPLOY first shipped the file it lives in?

    This is the point of pulling deploy context. "apply_discount() is wrong" is a
    code review. "apply_discount() has been wrong since commit X on date Y" is an
    incident with a timeline -- and the timeline is what bounds the exposure window
    the owner has to reconcile.

    HONESTY ABOUT WHAT THIS IS AND IS NOT:

      * It is FILE-level, not line-level. It reports when the FILE first appeared,
        which is the EARLIEST the defect could possibly have shipped -- an upper
        bound on the exposure window. The defective LINE may have arrived later.
      * Where the full history was walked, that bound is exact and
        `history_exhaustive` is true. Where pagination was cut short, the field is
        reported as a BOUNDED APPROXIMATION and says so, rather than asserting a
        provenance it cannot support.

    Line-level attribution needs blame over the specific function, which the REST
    API does not expose (it is a GraphQL/`git blame` operation). Rather than fake
    it, this reports the file-level bound and labels it precisely.
    """
    out: list[dict] = []
    for defect, (repo, path) in DEFECT_PATHS.items():
        entry: dict = {
            "defect": defect,
            "repo": f"{OWNER}/{repo}",
            "file": path,
            "granularity": "file-level (not line-level blame)",
            "history_exhaustive": False,
            "commits_touching_file": 0,
            "file_first_appeared": None,
            "currently_deployed": None,
            "exposure_window_opens_at_or_after": None,
            "claim": None,
            "note": None,
        }

        commits, exhaustive = _commit_history(repo, path, token)
        entry["history_exhaustive"] = exhaustive
        entry["commits_touching_file"] = len(commits)

        if not commits:
            entry["note"] = f"could not fetch commit history for {path}"
            out.append(entry)
            continue

        newest = _fmt(commits[0])
        oldest = _fmt(commits[-1])
        entry["currently_deployed"] = newest
        entry["file_first_appeared"] = oldest

        if exhaustive:
            entry["exposure_window_opens_at_or_after"] = oldest["date"]
            entry["claim"] = (
                f"EXHAUSTIVE file history ({len(commits)} commit(s) walked to the end). "
                f"The file first appeared at {oldest['sha']} ({oldest['date']}), so the "
                f"defect CANNOT predate that commit -- it is a hard upper bound on the "
                f"exposure window. File-level, not line-level: the defective line may "
                f"have arrived in a later commit, so the true window is a SUBSET of this one."
            )
        else:
            entry["exposure_window_opens_at_or_after"] = None
            entry["claim"] = (
                f"BOUNDED APPROXIMATION -- history was NOT walked to the end "
                f"({len(commits)} commit(s) seen, more may exist). The oldest commit SEEN "
                f"is {oldest['sha']} ({oldest['date']}), but an older one may exist, so "
                f"this is NOT an introduced-by provenance and the exposure window is "
                f"deliberately left UNSET rather than under-reported."
            )

        entry["note"] = (
            "How many customers were affected inside this window CANNOT be derived from "
            "deploy context alone -- that requires production telemetry, which is dark "
            "this run. The window is reported; the impact is NOT estimated."
        )
        out.append(entry)
    return out


def main() -> int:
    token = _token()
    print("=" * 78)
    print("PR / DEPLOY CONTEXT (fetched from the GitHub API)")
    print("=" * 78)
    print(f"credential present: {bool(token)}\n")

    by_repo: dict[str, dict] = {}
    for repo in REPOS:
        rec = collect(repo, token)
        by_repo[repo] = rec
        print(f"--- {rec['repo']} -> {rec['status'].upper()}")
        print(f"    {rec['reason']}")
        for d in rec["deploys"][:4]:
            print(f"    deploy  {d['sha']}  {d['date']}  {d['message'][:70]}")
        for r in rec["workflow_runs"][:3]:
            print(f"    ci-run  {r['sha']}  {r['branch'][:28] if r['branch'] else '?':<28} "
                  f"{r['status']}/{r['conclusion']}")
        for pr in rec["open_prs"][:6]:
            print(f"    open PR #{pr['number']}  {pr['title'][:70]}")
        print()

    print("=" * 78)
    print("DEPLOY <-> DEFECT CORRELATION  (file-level; history paginated to the end)")
    print("=" * 78)
    corr = correlate(by_repo, token)
    for c in corr:
        print(f"\n  {c['defect']}")
        print(f"    file: {c['repo']}/{c['file']}")
        print(f"    commits touching file: {c['commits_touching_file']} "
              f"(history exhaustive: {c['history_exhaustive']})")
        if c["file_first_appeared"]:
            i = c["file_first_appeared"]
            d = c["currently_deployed"] or {}
            print(f"    file first appeared   : {i['sha']} ({i['date']}) {i['message'][:55]}")
            print(f"    currently deployed as : {d.get('sha')} ({d.get('date')})")
            win = c["exposure_window_opens_at_or_after"]
            print(f"    exposure window opens : {win if win else 'UNSET (bounded approximation)'}")
        print(f"    claim: {c['claim'] or c['note']}")

    n_pulled = sum(1 for r in by_repo.values() if r["status"] == "pulled")
    payload = {
        "run_utc": datetime.now(timezone.utc).isoformat(),
        "credential_present": bool(token),
        "repos": by_repo,
        "correlation": corr,
        "summary": {
            "repos_pulled": n_pulled,
            "repos_total": len(REPOS),
            "total_deploys": sum(len(r["deploys"]) for r in by_repo.values()),
            "total_ci_runs": sum(len(r["workflow_runs"]) for r in by_repo.values()),
            "total_open_prs": sum(len(r["open_prs"]) for r in by_repo.values()),
        },
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True))

    print("\n" + "=" * 78)
    print(f"  {n_pulled}/{len(REPOS)} repos pulled; "
          f"{payload['summary']['total_deploys']} deploys, "
          f"{payload['summary']['total_ci_runs']} CI runs, "
          f"{payload['summary']['total_open_prs']} open PRs")
    print(f"  provenance artifact written: {OUT.name}")

    # A collector that retrieves nothing from every repo is not "clean" -- it is
    # blind, and must say so with a non-zero exit rather than a confident silence.
    if n_pulled == 0:
        print("\n  FATAL: no deploy context could be retrieved from ANY repo.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
