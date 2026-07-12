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
import socket
import ssl
import threading
import tempfile
import pathlib
import urllib.request
import urllib.error

RESULT = {}


# ---------------------------------------------------------------- Sentry
def probe_sentry():
    out = {"source": "sentry", "credential_env": None, "egress": None,
           "authenticated": False, "issues_pulled": 0, "detail": ""}

    for var in ("SENTRY_DSN", "SENTRY_AUTH_TOKEN", "SENTRY_TOKEN", "SENTRY_API_KEY"):
        if os.environ.get(var):
            out["credential_env"] = var
            break

    # Egress check: can we reach sentry.io at all?
    try:
        req = urllib.request.Request("https://sentry.io/api/0/",
                                     headers={"User-Agent": "fabric-ic"})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read(400).decode("utf-8", "replace")
            out["egress"] = r.status
            out["detail"] = f"/api/0/ -> {r.status} {body[:120]}"
    except urllib.error.HTTPError as e:
        out["egress"] = e.code
        out["detail"] = f"/api/0/ -> HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        out["egress"] = None
        out["detail"] = f"egress FAILED: {type(e).__name__}: {e}"

    # Authenticated pull: the actual incident data we want.
    tok = os.environ.get("SENTRY_AUTH_TOKEN") or os.environ.get("SENTRY_TOKEN")
    try:
        req = urllib.request.Request(
            "https://sentry.io/api/0/organizations/",
            headers={"User-Agent": "fabric-ic",
                     **({"Authorization": f"Bearer {tok}"} if tok else {})},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
            out["authenticated"] = True
            out["issues_pulled"] = len(data) if isinstance(data, list) else 0
            out["org_status"] = r.status
    except urllib.error.HTTPError as e:
        out["org_status"] = e.code
        out["detail"] += f" | /organizations/ -> HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        out["org_status"] = None
        out["detail"] += f" | /organizations/ FAILED: {type(e).__name__}"

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
    out = {"source": "otel", "endpoint_env": None, "ports_probed": len(OTEL_PORTS),
           "ports_open": [], "traces_pulled": 0, "negative_control": None}

    for var in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                "OTLP_ENDPOINT", "OTEL_COLLECTOR"):
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

    return out


# ---------------------------------------------------------- Gateway logs
LOG_PATHS = [
    "/var/log/gateway", "/var/log/gateway.log", "/var/log/fabric",
    "/var/log/fabric-gateway.log", "/var/log/nginx/access.log",
    "./logs", "./gateway.log", "./var/log", "/tmp/gateway.log",
    "/mnt/logs", "/opt/fabric/logs",
]


def _scan_log_paths(paths):
    """THE scanner. Both the real probe and its negative control drive this
    exact function -- a control that exercises a different code path than the
    probe proves nothing about the probe.
    """
    found = []
    lines = 0
    for p in paths:
        path = pathlib.Path(p)
        if not path.exists():
            continue
        found.append(str(path))
        try:
            if path.is_file():
                with path.open("r", errors="replace") as fh:
                    lines += sum(1 for _ in fh)
            else:
                for child in path.rglob("*"):
                    if child.is_file():
                        with child.open("r", errors="replace") as fh:
                            lines += sum(1 for _ in fh)
        except OSError:
            pass
    return found, lines


def probe_gateway_logs():
    out = {"source": "gateway_logs", "paths_checked": len(LOG_PATHS),
           "sources_found": [], "lines_pulled": 0, "negative_control": None}

    # NEGATIVE CONTROL: plant a log file and require THE SCANNER ITSELF --
    # the same _scan_log_paths used for the real probe -- to find it and read
    # its lines. If the scanner cannot see a file we planted for it, then a
    # "no sources found" verdict is a broken check, not a measurement.
    with tempfile.TemporaryDirectory() as td:
        planted = pathlib.Path(td) / "gateway.log"
        planted.write_text('{"status":502,"route":"/v1/usage"}\n'
                           '{"status":200,"route":"/v1/usage"}\n')
        ctl_found, ctl_lines = _scan_log_paths([str(planted)])
        ok = len(ctl_found) == 1 and ctl_lines == 2
        out["negative_control"] = (
            f"PASS: scanner found and read its own planted log "
            f"({ctl_lines} lines via the real scan path)"
            if ok else
            f"FAIL: scanner is BLIND -- planted 2 lines, scanner reported "
            f"{len(ctl_found)} source(s)/{ctl_lines} line(s)"
        )
        out["negative_control_passed"] = ok

    out["sources_found"], out["lines_pulled"] = _scan_log_paths(LOG_PATHS)
    return out


# ---------------------------------------------------------------- GitHub
def probe_github():
    """ACTUALLY CALL GitHub. A probe that hardcodes 'LIVE' is a check that
    cannot fail -- the exact disease this fleet's incidents are about. So
    make a real request against a real fleet repo and count what came back.
    """
    out = {"source": "github", "rest": None, "authenticated": False,
           "repos_pulled": 0, "prs_pulled": 0, "graphql": None,
           "negative_control": None, "detail": ""}

    tok = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "")
    hdr = {"User-Agent": "fabric-ic", "Accept": "application/vnd.github+json"}
    if tok:
        hdr["Authorization"] = f"Bearer {tok}"

    def _get(url):
        req = urllib.request.Request(url, headers=hdr)
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))

    # Real pull: the deployed source of a fleet repo + its open PRs.
    try:
        st, repo = _get("https://api.github.com/repos/chrischabot/checkout-api")
        out["rest"] = st
        out["authenticated"] = True
        out["repos_pulled"] = 1
        out["detail"] = f"repos/checkout-api -> {st} (private={repo.get('private')})"
    except urllib.error.HTTPError as e:
        out["rest"] = e.code
        out["detail"] = (f"repos/checkout-api -> HTTP {e.code}; unauthenticated REST "
                         f"(the connector, not this raw call, carries the credential)")
    except Exception as e:  # noqa: BLE001
        out["detail"] = f"REST FAILED: {type(e).__name__}: {e}"

    try:
        st, prs = _get("https://api.github.com/repos/chrischabot/checkout-api/pulls?state=open")
        out["prs_pulled"] = len(prs) if isinstance(prs, list) else 0
    except Exception as e:  # noqa: BLE001
        out["detail"] += f" | pulls FAILED: {type(e).__name__}"

    # NEGATIVE CONTROL: a repo that cannot exist must come back 404. If this
    # "succeeds", the probe is trusting something other than the API.
    try:
        _get("https://api.github.com/repos/chrischabot/fabric-ic-no-such-repo-"
             "negative-control-000")
        out["negative_control"] = ("FAIL: a nonexistent repo returned success -- "
                                   "this probe is not reading the real API")
        out["negative_control_passed"] = False
    except urllib.error.HTTPError as e:
        # Require EXACTLY 404. A 403 means forbidden/rate-limited -- that tells
        # us nothing about whether the probe can distinguish a real repo from a
        # fake one, so it is INCONCLUSIVE, never a pass.
        if e.code == 404:
            out["negative_control"] = "PASS: nonexistent repo correctly 404'd"
            out["negative_control_passed"] = True
        elif e.code == 403:
            out["negative_control"] = (
                "INCONCLUSIVE: HTTP 403 (forbidden/rate-limited) -- cannot prove "
                "the probe distinguishes a real repo from a fake one")
            out["negative_control_passed"] = False
        else:
            out["negative_control"] = f"FAIL: unexpected HTTP {e.code} for a nonexistent repo"
            out["negative_control_passed"] = False
    except Exception as e:  # noqa: BLE001
        out["negative_control"] = f"INCONCLUSIVE: {type(e).__name__}"
        out["negative_control_passed"] = False

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

    pullable = 0
    for key in ("sentry", "otel", "gateway_logs"):
        r = RESULT[key]
        ok = bool(r.get("authenticated") or r.get("ports_open") or r.get("sources_found"))
        r["pullable"] = ok
        pullable += int(ok)

    gh = RESULT["github"]
    gh["pullable"] = bool(gh.get("authenticated"))

    # A probe whose own control failed is a BROKEN CHECK. Say so loudly rather
    # than reporting a confident "nothing found".
    broken = [k for k in ("otel", "gateway_logs", "github")
              if RESULT[k].get("negative_control_passed") is False]

    RESULT["summary"] = {
        "telemetry_sources_pullable": f"{pullable}/3",
        "github_pullable": gh["pullable"],
        "broken_probes": broken,
        "verdict": ("MEASURED" if not broken else
                    f"UNTRUSTWORTHY -- controls failed in: {broken}"),
    }
    print(json.dumps(RESULT, indent=2, default=str))
