#!/usr/bin/env python3
"""Fabric incident commander -- telemetry provenance probe.

Each probe carries a NEGATIVE CONTROL: it must first prove it can detect a
thing it creates itself. A probe that cannot see a port it opens, or a log
file it plants, is a BROKEN CHECK -- and "nothing found" from a broken check
is not a measurement. Provenance without a control is decoration.

Exit code is always 0: this reports provenance, it is not a gate.
"""
import json
import os
import re
import socket
import threading
import tempfile
import pathlib
import urllib.request
import urllib.error

RESULT = {}


# ---------------------------------------------------------------- Sentry
SENTRY_HOSTS = ["https://sentry.io"]
if os.environ.get("SENTRY_URL"):
    SENTRY_HOSTS.insert(0, os.environ["SENTRY_URL"].rstrip("/"))


def probe_sentry():
    """Attempt a REAL pull of fresh ISSUES -- not just an auth ping.

    The incident loop needs issue payloads (culprit, count, last_seen), so the
    probe must walk orgs -> projects -> /issues/. Counts are reported per stage,
    and `issues_pulled` counts ISSUES ONLY (never organizations), so a partial
    pull can never be mistaken for a full one.
    """
    out = {"source": "sentry", "credential_env": None, "egress": None,
           "authenticated": False, "orgs_pulled": 0, "projects_pulled": 0,
           "issues_pulled": 0, "issues": [], "detail": ""}

    for var in ("SENTRY_DSN", "SENTRY_AUTH_TOKEN", "SENTRY_TOKEN", "SENTRY_API_KEY"):
        if os.environ.get(var):
            out["credential_env"] = var
            break

    base = SENTRY_HOSTS[0]
    tok = os.environ.get("SENTRY_AUTH_TOKEN") or os.environ.get("SENTRY_TOKEN")
    hdr = {"User-Agent": "fabric-ic"}
    if tok:
        hdr["Authorization"] = f"Bearer {tok}"

    def _get(url):
        req = urllib.request.Request(url, headers=hdr)
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))

    # Egress: can we reach Sentry at all? (200 here + 401 below == missing
    # CREDENTIAL, not a network block. That distinction drives the runbook.)
    try:
        st, body = _get(f"{base}/api/0/")
        out["egress"] = st
        out["detail"] = f"/api/0/ -> {st} {json.dumps(body)[:80]}"
    except urllib.error.HTTPError as e:
        out["egress"] = e.code
        out["detail"] = f"/api/0/ -> HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        out["detail"] = f"egress FAILED: {type(e).__name__}: {e}"
        return out

    # STAGE 1: organizations.
    try:
        st, orgs = _get(f"{base}/api/0/organizations/")
        out["authenticated"] = True
        out["orgs_pulled"] = len(orgs) if isinstance(orgs, list) else 0
    except urllib.error.HTTPError as e:
        out["detail"] += f" | /organizations/ -> HTTP {e.code}"
        if e.code in (401, 403):
            out["detail"] += " -- NO CREDENTIAL: cannot pull issues"
        return out
    except Exception as e:  # noqa: BLE001
        out["detail"] += f" | /organizations/ FAILED: {type(e).__name__}"
        return out

    # STAGE 2+3: projects, then FRESH ISSUES per project. This is the payload the
    # commander actually needs to cluster symptoms.
    for org in (orgs or [])[:5]:
        slug = org.get("slug")
        try:
            _st, projs = _get(f"{base}/api/0/organizations/{slug}/projects/")
        except Exception:  # noqa: BLE001
            continue
        out["projects_pulled"] += len(projs) if isinstance(projs, list) else 0
        for p in (projs or [])[:10]:
            pslug = p.get("slug")
            try:
                _st, issues = _get(
                    f"{base}/api/0/projects/{slug}/{pslug}/issues/"
                    "?statsPeriod=24h&query=is:unresolved")
            except Exception:  # noqa: BLE001
                continue
            for iss in (issues or []):
                out["issues_pulled"] += 1          # ISSUES only -- never orgs
                out["issues"].append({
                    "project": pslug,
                    "title": iss.get("title"),
                    "culprit": iss.get("culprit"),
                    "level": iss.get("level"),
                    "count": iss.get("count"),
                    "userCount": iss.get("userCount"),
                    "firstSeen": iss.get("firstSeen"),
                    "lastSeen": iss.get("lastSeen"),
                    "permalink": iss.get("permalink"),
                })

    return out


# ------------------------------------------------------------------ OTEL
OTEL_PORTS = [4317, 4318, 9411, 16686, 14268, 55681, 8126, 13133, 3100, 3200, 9090]


def _port_open(host, port, timeout=0.75):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:  # noqa: BLE001
        return False


def probe_otel():
    """Attempt a REAL trace pull, not merely a port scan.

    A port scan tells you a collector is absent; it does not tell you there are
    no traces. So where a port IS open, this actually queries the trace API
    (Jaeger/Tempo/Zipkin) and counts TRACES RETURNED. `traces_pulled` is only
    non-zero if trace payloads actually came back.
    """
    out = {"source": "otel", "endpoint_env": None, "ports_probed": len(OTEL_PORTS),
           "ports_open": [], "trace_apis_queried": [], "traces_pulled": 0,
           "traces": [], "negative_control": None}

    for var in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                "OTLP_ENDPOINT", "OTEL_COLLECTOR", "JAEGER_ENDPOINT", "TEMPO_ENDPOINT"):
        if os.environ.get(var):
            out["endpoint_env"] = var
            break

    # NEGATIVE CONTROL: open a socket ourselves and require the probe to SEE it.
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    ctl_port = srv.getsockname()[1]
    threading.Thread(target=lambda: srv.accept(), daemon=True).start()
    ctl_ok = _port_open("127.0.0.1", ctl_port)
    out["negative_control"] = (
        "PASS: probe detected a port it opened itself"
        if ctl_ok
        else "FAIL: probe is BLIND -- cannot see its own open port"
    )
    out["negative_control_passed"] = ctl_ok
    srv.close()

    for p in OTEL_PORTS:
        if _port_open("127.0.0.1", p):
            out["ports_open"].append(p)

    # Where something IS listening, ACTUALLY ASK IT FOR TRACES.
    TRACE_APIS = {
        16686: "/api/traces?service=checkout&limit=20",   # Jaeger
        3200: "/api/search?limit=20",                     # Tempo
        9411: "/api/v2/traces?limit=20",                  # Zipkin
    }
    for port in out["ports_open"]:
        path = TRACE_APIS.get(port)
        if not path:
            continue
        url = f"http://127.0.0.1:{port}{path}"
        out["trace_apis_queried"].append(url)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "fabric-ic"})
            with urllib.request.urlopen(req, timeout=10) as r:
                body = json.loads(r.read().decode("utf-8", "replace"))
            traces = body
            if isinstance(body, dict):
                # Jaeger -> {"data": [...]}, Tempo -> {"traces": [...]},
                # Zipkin -> [[span,...],...]. Accept each rather than assuming one.
                for key in ("data", "traces", "results", "spans"):
                    if isinstance(body.get(key), list):
                        traces = body[key]
                        break
            if isinstance(traces, list):
                out["traces_pulled"] += len(traces)
                out["traces"].extend(traces[:5])
        except Exception as e:  # noqa: BLE001
            out.setdefault("trace_api_errors", []).append(
                f"{url} -> {type(e).__name__}")

    return out


# ---------------------------------------------------------- Gateway logs
LOG_PATHS = [
    "/var/log/gateway", "/var/log/gateway.log", "/var/log/fabric",
    "/var/log/fabric-gateway.log", "/var/log/nginx/access.log",
    "./logs", "./gateway.log", "./var/log", "/tmp/gateway.log",
    "/mnt/logs", "/opt/fabric/logs",
]


def _scan_log_paths(paths):
    """THE scanner. Both the real probe and its negative control drive this exact
    function -- a control that exercises a different code path than the probe
    proves nothing about the probe.

    It does not merely COUNT lines: it PARSES them and extracts incident
    SYMPTOMS (5xx/4xx rates, error signatures, affected routes), because a line
    count cannot be clustered and is therefore useless to triage.
    """
    found = []
    lines = 0
    symptoms = {"status_counts": {}, "error_signatures": {}, "routes": {},
                "server_errors": 0, "client_errors": 0, "samples": []}

    ERR_PAT = re.compile(
        r"(KeyError|TypeError|ValueError|NullPointer|Timeout|ECONNREFUSED|"
        r"Traceback|5\d{2}\s|error|exception)", re.I)
    STATUS_PAT = re.compile(r'"?status(?:_code)?"?\s*[:=]\s*"?(\d{3})')
    ROUTE_PAT = re.compile(r'"?(?:route|path|url)"?\s*[:=]\s*"?(/[^\s",}]*)')

    def _consume(fh):
        nonlocal lines
        for raw in fh:
            lines += 1
            ln = raw.strip()
            if not ln:
                continue

            status = None
            route = None
            # Prefer structured JSON; fall back to regex for plain text.
            try:
                rec = json.loads(ln)
                if isinstance(rec, dict):
                    status = rec.get("status") or rec.get("status_code")
                    route = rec.get("route") or rec.get("path") or rec.get("url")
                    msg = str(rec.get("error") or rec.get("message") or "")
                else:
                    msg = ln
            except (ValueError, TypeError):
                msg = ln
                m = STATUS_PAT.search(ln)
                if m:
                    status = m.group(1)
                m = ROUTE_PAT.search(ln)
                if m:
                    route = m.group(1)

            if status is not None:
                s = str(status)
                symptoms["status_counts"][s] = symptoms["status_counts"].get(s, 0) + 1
                if s.startswith("5"):
                    symptoms["server_errors"] += 1
                elif s.startswith("4"):
                    symptoms["client_errors"] += 1
            if route:
                symptoms["routes"][route] = symptoms["routes"].get(route, 0) + 1

            hit = ERR_PAT.search(msg or ln)
            if hit:
                sig = hit.group(1)
                symptoms["error_signatures"][sig] = \
                    symptoms["error_signatures"].get(sig, 0) + 1
                if len(symptoms["samples"]) < 5:
                    symptoms["samples"].append(ln[:160])

    for p in paths:
        path = pathlib.Path(p)
        if not path.exists():
            continue
        found.append(str(path))
        try:
            if path.is_file():
                with path.open("r", errors="replace") as fh:
                    _consume(fh)
            else:
                for child in path.rglob("*"):
                    if child.is_file():
                        with child.open("r", errors="replace") as fh:
                            _consume(fh)
        except OSError:
            pass
    return found, lines, symptoms


def probe_gateway_logs():
    out = {"source": "gateway_logs", "paths_checked": len(LOG_PATHS),
           "sources_found": [], "lines_pulled": 0, "symptoms": {},
           "negative_control": None}

    # NEGATIVE CONTROL: plant a log with a KNOWN symptom mix and require THE
    # SCANNER ITSELF -- the same _scan_log_paths the real probe uses -- to find
    # the file, read every line, AND correctly extract the symptoms. A scanner
    # that reads lines but cannot classify a 502 is still blind for triage
    # purposes, so the control asserts the ANALYSIS, not just the line count.
    with tempfile.TemporaryDirectory() as td:
        planted = pathlib.Path(td) / "gateway.log"
        planted.write_text(
            '{"status":502,"route":"/v1/usage","error":"KeyError: tokens"}\n'
            '{"status":200,"route":"/v1/usage"}\n'
            '{"status":500,"route":"/v1/checkout","error":"Timeout"}\n'
        )
        cf, cl, cs = _scan_log_paths([str(planted)])
        ok = (len(cf) == 1 and cl == 3
              and cs["server_errors"] == 2
              and cs["status_counts"].get("502") == 1
              and cs["error_signatures"].get("KeyError") == 1
              and cs["routes"].get("/v1/usage") == 2)
        out["negative_control"] = (
            "PASS: scanner found its own planted log, read all 3 lines, and "
            "correctly extracted the symptoms (2 server errors, 1x502, "
            "KeyError signature, /v1/usage x2)"
            if ok else
            f"FAIL: scanner is BLIND -- planted 3 lines/2 server errors, got "
            f"{cl} lines / {cs['server_errors']} server errors / "
            f"sigs={cs['error_signatures']}"
        )
        out["negative_control_passed"] = ok

    out["sources_found"], out["lines_pulled"], out["symptoms"] = \
        _scan_log_paths(LOG_PATHS)
    return out


# ---------------------------------------------------------------- GitHub
FLEET = ["checkout-api", "fabric-gateway-demo", "fabric-ic-incident-target"]
OWNER = "chrischabot"


def probe_github():
    """ACTUALLY CALL GitHub, for EVERY repo in the fleet.

    A probe that hardcodes 'LIVE' is a check that cannot fail -- and a probe that
    inspects ONE repo cannot speak for a three-repo fleet. Both are the same
    disease: a check whose scope is narrower than its claim.
    """
    out = {"source": "github", "authenticated": False, "repos_pulled": 0,
           "prs_pulled": 0, "graphql": None, "fleet": {},
           "negative_control": None, "detail": ""}

    tok = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "")
    hdr = {"User-Agent": "fabric-ic", "Accept": "application/vnd.github+json"}
    if tok:
        hdr["Authorization"] = f"Bearer {tok}"

    def _get(url):
        req = urllib.request.Request(url, headers=hdr)
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))

    # Recent PR context for EVERY fleet repo -- open AND recently closed, since a
    # just-merged PR is exactly the kind of change that introduces an incident.
    for repo in FLEET:
        entry = {}
        try:
            st, meta = _get(f"https://api.github.com/repos/{OWNER}/{repo}")
            entry["repo_status"] = st
            out["repos_pulled"] += 1
            out["authenticated"] = True
        except urllib.error.HTTPError as e:
            entry["repo_status"] = e.code
            out["fleet"][repo] = entry
            continue
        except Exception as e:  # noqa: BLE001
            entry["error"] = f"{type(e).__name__}"
            out["fleet"][repo] = entry
            continue

        for state in ("open", "closed"):
            try:
                _st, prs = _get(
                    f"https://api.github.com/repos/{OWNER}/{repo}"
                    f"/pulls?state={state}&sort=updated&direction=desc&per_page=10")
            except Exception:  # noqa: BLE001
                entry[f"{state}_prs_error"] = True
                continue
            recs = [{"number": p.get("number"), "title": (p.get("title") or "")[:80],
                     "merged_at": p.get("merged_at"),
                     "updated_at": p.get("updated_at"),
                     "head": (p.get("head", {}).get("sha") or "")[:12]}
                    for p in (prs or [])]
            entry[f"{state}_prs"] = len(recs)
            entry[f"recent_{state}_prs"] = recs[:5]
            out["prs_pulled"] += len(recs)

        out["fleet"][repo] = entry

    out["detail"] = (f"{out['repos_pulled']}/{len(FLEET)} fleet repos read; "
                     f"{out['prs_pulled']} PRs pulled (open + recently closed)")

    # NEGATIVE CONTROL: a repo that cannot exist must come back EXACTLY 404.
    try:
        _get(f"https://api.github.com/repos/{OWNER}/fabric-ic-no-such-repo-"
             "negative-control-000")
        out["negative_control"] = ("FAIL: a nonexistent repo returned success -- "
                                   "this probe is not reading the real API")
        out["negative_control_passed"] = False
    except urllib.error.HTTPError as e:
        if e.code == 404:
            out["negative_control"] = "PASS: nonexistent repo correctly 404'd"
            out["negative_control_passed"] = True
        elif e.code == 403:
            out["negative_control"] = (
                "INCONCLUSIVE: HTTP 403 (forbidden/rate-limited) -- cannot prove "
                "the probe distinguishes a real repo from a fake one")
            out["negative_control_passed"] = False
        else:
            out["negative_control"] = f"FAIL: unexpected HTTP {e.code}"
            out["negative_control_passed"] = False
    except Exception as e:  # noqa: BLE001
        out["negative_control"] = f"INCONCLUSIVE: {type(e).__name__}"
        out["negative_control_passed"] = False

    # A fleet-wide claim requires a fleet-wide read.
    out["fleet_coverage_complete"] = out["repos_pulled"] == len(FLEET)

    try:
        req = urllib.request.Request(
            "https://api.github.com/graphql", data=b'{"query":"query{viewer{login}}"}',
            headers={**hdr, "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            out["graphql"] = r.status
    except urllib.error.HTTPError as e:
        out["graphql"] = e.code
    except Exception:  # noqa: BLE001
        out["graphql"] = None

    return out


if __name__ == "__main__":
    RESULT["sentry"] = probe_sentry()
    RESULT["otel"] = probe_otel()
    RESULT["gateway_logs"] = probe_gateway_logs()
    RESULT["github"] = probe_github()

    # PULLABLE means DATA WAS ACTUALLY PULLED -- not that a port happened to be
    # open, and not that an endpoint merely answered. An open OTEL port with zero
    # traces returned is NOT a usable telemetry source for triage, and calling it
    # one would be the same self-flattering error as a gate that cannot fail.
    pullable = 0
    RESULT["sentry"]["pullable"] = RESULT["sentry"]["issues_pulled"] > 0
    RESULT["otel"]["pullable"] = RESULT["otel"]["traces_pulled"] > 0
    RESULT["gateway_logs"]["pullable"] = RESULT["gateway_logs"]["lines_pulled"] > 0
    for key in ("sentry", "otel", "gateway_logs"):
        pullable += int(RESULT[key]["pullable"])

    gh = RESULT["github"]
    # GitHub counts as pullable only on a COMPLETE fleet read with real PR data.
    gh["pullable"] = bool(gh.get("fleet_coverage_complete") and gh.get("prs_pulled", 0) > 0)

    # A probe whose own control failed is a BROKEN CHECK. Say so loudly rather
    # than reporting a confident "nothing found".
    broken = [k for k in ("otel", "gateway_logs", "github")
              if RESULT[k].get("negative_control_passed") is False]

    RESULT["summary"] = {
        "telemetry_sources_pullable": f"{pullable}/3",
        "issues_pulled": RESULT["sentry"]["issues_pulled"],
        "traces_pulled": RESULT["otel"]["traces_pulled"],
        "log_lines_pulled": RESULT["gateway_logs"]["lines_pulled"],
        "github_pullable": gh["pullable"],
        "github_fleet_coverage": f"{gh.get('repos_pulled', 0)}/{len(FLEET)}",
        "github_prs_pulled": gh.get("prs_pulled", 0),
        "broken_probes": broken,
        "verdict": ("MEASURED" if not broken else
                    f"UNTRUSTWORTHY -- controls failed in: {broken}"),
        "note": ("'pullable' requires PAYLOADS pulled (issues / traces / log lines), "
                 "never merely an open port or a reachable endpoint."),
    }
    print(json.dumps(RESULT, indent=2, default=str))
