#!/usr/bin/env python3
"""Fabric incident commander -- DEPLOY CONTEXT probe.

The incident-response loop needs deploy context: what shipped, when, and did it
go red? A defect that appeared right after a deploy is a different incident from
one that has been latent for months.

This pulls, per fleet repo:
  * GET /repos/{o}/{r}/deployments            -- the deployment records
  * GET .../deployments/{id}/statuses         -- did each deploy succeed?
  * GET /repos/{o}/{r}/actions/runs           -- recent CI/CD runs (conclusions)
  * GET /repos/{o}/{r}/commits/{sha}/check-runs -- gate status on HEAD
  * GET /repos/{o}/{r}/releases               -- tagged releases

NEGATIVE CONTROL: a nonexistent repo's deployments endpoint must come back
EXACTLY 404. A 403 (forbidden / rate-limited) proves nothing about whether this
probe can distinguish a real deployment list from a fake one, so it is reported
INCONCLUSIVE -- never a pass. Without that, "no deployments found" could simply
mean the probe is blind, and a blind check that announces a clean bill of health
is the exact disease this fleet's incidents are about.

Exit code is always 0: this reports provenance, it is not a gate.
"""
import json
import os
import urllib.error
import urllib.request

OWNER = "chrischabot"
REPOS = ["checkout-api", "fabric-gateway-demo", "fabric-ic-incident-target"]

TOK = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
HDR = {"User-Agent": "fabric-ic", "Accept": "application/vnd.github+json"}
if TOK:
    HDR["Authorization"] = f"Bearer {TOK}"


def get(url):
    """Return (status, parsed_json_or_None, error_or_None)."""
    try:
        req = urllib.request.Request(url, headers=HDR)
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace")), None
    except urllib.error.HTTPError as e:
        return e.code, None, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return None, None, f"{type(e).__name__}: {e}"


API = "https://api.github.com"
out = {}

# ---------------------------------------------------------- negative control
st, _, err = get(f"{API}/repos/{OWNER}/fabric-ic-no-such-repo-neg-control-000/deployments")
if st == 404:
    ctl = "PASS: the deployments endpoint of a nonexistent repo correctly 404'd"
    ctl_ok = True
elif st == 403:
    ctl = ("INCONCLUSIVE: HTTP 403 (forbidden/rate-limited) -- cannot prove this "
           "probe distinguishes a real deployment list from a fake one")
    ctl_ok = False
else:
    ctl = f"FAIL: unexpected status {st} ({err}) for a nonexistent repo"
    ctl_ok = False

out["negative_control"] = {"result": ctl, "passed": ctl_ok}

# ----------------------------------------------------------------- per repo
total_deploys = 0
fleet = {}

for repo in REPOS:
    base = f"{API}/repos/{OWNER}/{repo}"
    entry = {}

    # 1. DEPLOYMENTS -- the thing the request actually asked for.
    st, deps, err = get(f"{base}/deployments")
    entry["deployments_status"] = st
    if isinstance(deps, list):
        entry["deployments_count"] = len(deps)
        total_deploys += len(deps)
        entry["deployments"] = []
        for d in deps[:5]:
            rec = {
                "id": d.get("id"),
                "sha": (d.get("sha") or "")[:12],
                "ref": d.get("ref"),
                "environment": d.get("environment"),
                "created_at": d.get("created_at"),
                "statuses": [],
            }
            # 2. STATUS of each deployment -- did it succeed or fail?
            sst, sts, _serr = get(f"{base}/deployments/{d.get('id')}/statuses")
            if isinstance(sts, list):
                rec["statuses"] = [
                    {"state": s.get("state"), "created_at": s.get("created_at")}
                    for s in sts[:5]
                ]
            else:
                rec["statuses_error"] = f"HTTP {sst}"
            rec["latest_state"] = (rec["statuses"][0]["state"]
                                   if rec["statuses"] else "NO STATUS RECORDED")
            entry["deployments"].append(rec)
    else:
        entry["deployments_count"] = 0
        entry["deployments_error"] = err

    # 3. WORKFLOW RUNS -- the de-facto deploy/CI signal for this fleet.
    st, runs, err = get(f"{base}/actions/runs?per_page=5")
    if isinstance(runs, dict):
        wr = runs.get("workflow_runs", []) or []
        entry["workflow_runs_total"] = runs.get("total_count", 0)
        entry["recent_runs"] = [
            {"name": r.get("name"), "event": r.get("event"),
             "head": (r.get("head_sha") or "")[:12],
             "status": r.get("status"), "conclusion": r.get("conclusion"),
             "created_at": r.get("created_at")}
            for r in wr
        ]
    else:
        entry["workflow_runs_total"] = 0
        entry["workflow_runs_error"] = err or f"HTTP {st}"

    # 4. RELEASES -- another shipping signal.
    st, rel, err = get(f"{base}/releases?per_page=5")
    entry["releases_count"] = len(rel) if isinstance(rel, list) else 0

    # 5. CHECK RUNS on the default branch HEAD -- is the shipped tree green?
    st, head, err = get(f"{base}/commits/HEAD")
    if isinstance(head, dict) and head.get("sha"):
        sha = head["sha"]
        entry["head_sha"] = sha[:12]
        entry["head_commit"] = (head.get("commit", {}).get("message") or "").split("\n")[0][:90]
        cst, checks, _cerr = get(f"{base}/commits/{sha}/check-runs")
        if isinstance(checks, dict):
            crs = checks.get("check_runs", []) or []
            entry["head_check_runs"] = [
                {"name": c.get("name"), "conclusion": c.get("conclusion")}
                for c in crs[:8]
            ]
            entry["head_check_run_count"] = checks.get("total_count", len(crs))
        else:
            entry["head_check_run_count"] = 0

    fleet[repo] = entry

out["fleet"] = fleet

# A passing negative control alone is NOT enough to interpret "zero deployments".
# If a REAL repo's deployments call came back 403/404/error, then "no deployments"
# is a FAILED READ, not an empty result. So the verdict requires that EVERY repo
# answered 200 with a parseable list.
reads_ok = [r for r, e in fleet.items() if e.get("deployments_status") == 200
            and "deployments_error" not in e]
reads_bad = [r for r in REPOS if r not in reads_ok]
all_read = len(reads_ok) == len(REPOS)
trustworthy = bool(ctl_ok and all_read)

out["read_coverage"] = {
    "repos_answering_200_list": reads_ok,
    "repos_failed_or_refused": reads_bad,
    "complete": all_read,
}

if trustworthy:
    if total_deploys:
        interp = (f"Deployments API answered 200 for all {len(REPOS)} repos; "
                  f"{total_deploys} deployment record(s) exist across the fleet.")
    else:
        interp = ("Deployments API answered 200 for ALL repos and returned EMPTY "
                  "lists -- so zero deployments is a REAL MEASUREMENT, not a failed "
                  "read. This fleet does not use the GitHub Deployments API, so "
                  "'which deploy introduced the defect?' cannot be answered from "
                  "deploy records; the shipping signal is workflow runs + merges.")
else:
    reasons = []
    if not ctl_ok:
        reasons.append("the negative control did not pass")
    if not all_read:
        reasons.append(f"deployments could not be read for: {reads_bad}")
    interp = ("CANNOT INTERPRET a deployment count -- " + "; ".join(reasons) +
              ". A 'no deployments' reading here would be a broken check, not a "
              "measurement.")

out["summary"] = {
    "deployment_records_across_fleet": total_deploys,
    "repos_read_successfully": f"{len(reads_ok)}/{len(REPOS)}",
    "negative_control_passed": ctl_ok,
    "deploy_context_pullable": trustworthy,
    "verdict": "MEASURED" if trustworthy else "UNTRUSTWORTHY",
    "interpretation": interp,
}

print(json.dumps(out, indent=2, default=str))
