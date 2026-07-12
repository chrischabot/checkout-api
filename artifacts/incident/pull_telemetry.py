#!/usr/bin/env python3
"""Fabric incident commander -- TELEMETRY PULL.

This is the RETRIEVAL layer, not merely a reachability probe. For each source it
actually ATTEMPTS THE FETCH and records the outcome:

  * Sentry  -> GET /api/0/organizations/{org}/issues/  (unresolved, sorted by freq)
               and, per issue, the latest event with its stack frames.
  * OTEL    -> queries whichever collector answers: Jaeger (/api/traces),
               Tempo (/api/search), Zipkin (/api/v2/traces), or an OTLP HTTP
               endpoint. Pulls recent traces and extracts error spans.
  * Gateway -> reads and PARSES log lines from any discovered source (file, dir,
               or glob), extracting level/status/route/exception.

WHY THE FETCH CODE EXISTS EVEN THOUGH THE CREDENTIAL DOES NOT

A commander that only *checks whether it could* pull telemetry is not a commander
that pulls telemetry. The moment a SENTRY_AUTH_TOKEN is wired in, or a collector
starts listening, or a log path is mounted, this module retrieves and clusters the
real signal -- with no further code change. Shipping only the probe would mean the
next operator discovers, at the worst possible moment, that the retrieval path was
never written.

EVERY OUTCOME IS RECORDED AS PROVENANCE. A fetch that is impossible (no
credential) is reported as `unavailable` with the exact HTTP status that proves it;
a fetch that is attempted and returns nothing is reported as `empty`; a fetch that
returns data is clustered. These are three different facts and the brief must not
conflate them.

Output: telemetry_pull.json -- the machine-readable provenance record that the
incident brief cites. Exit 0 if the pull layer RAN (whatever it found); non-zero
only if the puller itself is broken.
"""
from __future__ import annotations

import glob
import json
import os
import pathlib
import re
import socket
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT / "telemetry_pull.json"

# ---------------------------------------------------------------- Sentry ----
SENTRY_BASE = os.environ.get("SENTRY_URL", "https://sentry.io").rstrip("/")
SENTRY_ORG = os.environ.get("SENTRY_ORG", "")
SENTRY_PROJECT = os.environ.get("SENTRY_PROJECT", "")


def _token() -> str | None:
    for k in ("SENTRY_AUTH_TOKEN", "SENTRY_TOKEN", "SENTRY_API_TOKEN", "SENTRY_DSN_TOKEN"):
        v = os.environ.get(k)
        if v:
            return v
    return None


def _http(url: str, token: str | None = None, timeout: float = 20.0):
    """Return (status, body_text, error). Never raises."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(200_000).decode("utf-8", "replace"), None
    except urllib.error.HTTPError as e:
        return e.code, e.read(4_000).decode("utf-8", "replace"), None
    except Exception as e:  # noqa: BLE001
        return None, "", f"{type(e).__name__}: {e}"


def pull_sentry() -> dict:
    """ACTUALLY FETCH unresolved issues + their latest events."""
    tok = _token()
    rec: dict = {
        "source": "sentry",
        "base_url": SENTRY_BASE,
        "org": SENTRY_ORG or None,
        "credential_present": bool(tok),
        "attempted": [],
        "issues": [],
        "status": "unavailable",
        "reason": None,
    }

    # Unauthenticated liveness: distinguishes "no network" from "no credential".
    code, body, err = _http(f"{SENTRY_BASE}/api/0/")
    rec["attempted"].append({"url": f"{SENTRY_BASE}/api/0/", "status": code, "error": err})
    if code is None:
        rec["reason"] = f"network failure reaching Sentry ({err}) -- cannot distinguish block from missing credential"
        return rec

    # Discover the org if not configured.
    org = SENTRY_ORG
    if tok and not org:
        c, b, e = _http(f"{SENTRY_BASE}/api/0/organizations/", tok)
        rec["attempted"].append({"url": f"{SENTRY_BASE}/api/0/organizations/", "status": c, "error": e})
        if c == 200:
            try:
                orgs = json.loads(b)
                if orgs:
                    org = orgs[0].get("slug", "")
                    rec["org"] = org
            except Exception:  # noqa: BLE001
                pass

    if not tok:
        # Prove the credential is what is missing, by showing the authed endpoint's status.
        c, b, e = _http(f"{SENTRY_BASE}/api/0/organizations/")
        rec["attempted"].append({"url": f"{SENTRY_BASE}/api/0/organizations/", "status": c,
                                 "error": e, "body": b[:200]})
        rec["reason"] = (
            f"NO CREDENTIAL. Unauthenticated /api/0/ answered {code} (egress works); "
            f"/api/0/organizations/ answered {c}. Zero SENTRY* env vars. "
            f"This is a MISSING SECRET, not a network block. "
            f"Set SENTRY_AUTH_TOKEN (and optionally SENTRY_ORG) and this puller "
            f"retrieves live issues with NO code change."
        )
        return rec

    if not org:
        rec["reason"] = "credential present but no organization resolved (set SENTRY_ORG)"
        return rec

    # THE REAL PULL: unresolved issues, most frequent first.
    url = (f"{SENTRY_BASE}/api/0/organizations/{org}/issues/"
           f"?query=is:unresolved&statsPeriod=24h&sort=freq&limit=25")
    c, b, e = _http(url, tok)
    rec["attempted"].append({"url": url, "status": c, "error": e})
    if c != 200:
        rec["reason"] = f"issue fetch returned {c}: {b[:200]}"
        return rec

    try:
        raw = json.loads(b)
    except Exception as exc:  # noqa: BLE001
        rec["reason"] = f"issue payload not JSON: {exc}"
        return rec

    for it in raw:
        issue = {
            "id": it.get("id"),
            "title": it.get("title"),
            "culprit": it.get("culprit"),
            "level": it.get("level"),
            "count": it.get("count"),
            "users_affected": it.get("userCount"),
            "first_seen": it.get("firstSeen"),
            "last_seen": it.get("lastSeen"),
            "permalink": it.get("permalink"),
            "frames": [],
        }
        # Latest event -> stack frames. This is what ties a symptom to a FILE:LINE,
        # which is what makes fixability analysis possible at all.
        ec, eb, _ = _http(f"{SENTRY_BASE}/api/0/issues/{issue['id']}/events/latest/", tok)
        if ec == 200:
            try:
                ev = json.loads(eb)
                for entry in ev.get("entries", []):
                    if entry.get("type") != "exception":
                        continue
                    for val in entry.get("data", {}).get("values", []):
                        for f in (val.get("stacktrace") or {}).get("frames", [])[-8:]:
                            issue["frames"].append({
                                "filename": f.get("filename"),
                                "function": f.get("function"),
                                "lineno": f.get("lineNo"),
                                "in_app": f.get("inApp"),
                            })
            except Exception:  # noqa: BLE001
                pass
        rec["issues"].append(issue)

    rec["status"] = "pulled" if rec["issues"] else "empty"
    rec["reason"] = f"{len(rec['issues'])} unresolved issue(s) retrieved"
    return rec


# ------------------------------------------------------------------ OTEL ----
# (host, port, name, trace-query path). The puller tries each in turn and
# QUERIES the first that answers -- it does not merely note that a port is open.
OTEL_BACKENDS = [
    ("127.0.0.1", 16686, "jaeger", "/api/traces?service={svc}&limit=20&lookback=1h"),
    ("127.0.0.1", 3200, "tempo", "/api/search?tags=&limit=20"),
    ("127.0.0.1", 9411, "zipkin", "/api/v2/traces?limit=20"),
    ("127.0.0.1", 4318, "otlp-http", "/v1/traces"),
    ("127.0.0.1", 4317, "otlp-grpc", None),          # gRPC: reachability only
    ("127.0.0.1", 14268, "jaeger-collector", None),
    ("127.0.0.1", 55681, "otlp-legacy", None),
    ("127.0.0.1", 8126, "datadog-apm", None),
    ("127.0.0.1", 13133, "otel-health", "/"),
    ("127.0.0.1", 9090, "prometheus", "/api/v1/query?query=up"),
    ("127.0.0.1", 3100, "loki", "/loki/api/v1/labels"),
]

SERVICES = ["checkout-api", "fabric-gateway", "checkout", "gateway"]


def _port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _negative_control() -> tuple[bool, int]:
    """The probe must detect a port IT OPENS ITSELF, or its 'closed' verdicts are junk."""
    import threading

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def loop():
        srv.settimeout(0.3)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                c.close()
            except OSError:
                pass

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    seen = _port_open("127.0.0.1", port)
    stop.set()
    t.join(timeout=1.0)
    srv.close()
    return seen, port


def pull_otel() -> dict:
    """Find a collector and ACTUALLY QUERY it for recent traces + error spans."""
    ctl_ok, ctl_port = _negative_control()
    rec: dict = {
        "source": "otel",
        "negative_control": {"port": ctl_port, "detected": ctl_ok},
        "env_vars": sorted(k for k in os.environ if k.startswith(("OTEL", "OTLP"))),
        "endpoint_env": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"),
        "probed": [],
        "reachable": [],
        "traces": [],
        "error_spans": [],
        "status": "unavailable",
        "reason": None,
    }

    if not ctl_ok:
        rec["status"] = "instrument_broken"
        rec["reason"] = ("the probe could not see a port it opened itself, so NO "
                         "'closed' verdict from it can be trusted")
        return rec

    for host, port, name, path in OTEL_BACKENDS:
        is_open = _port_open(host, port)
        rec["probed"].append({"backend": name, "port": port, "open": is_open})
        if is_open:
            rec["reachable"].append({"backend": name, "port": port})

    if not rec["reachable"] and not rec["endpoint_env"]:
        rec["reason"] = (
            f"no collector: {len(OTEL_BACKENDS)} backends probed, 0 reachable; "
            f"0 OTEL*/OTLP* env vars. Negative control PASSED (port {ctl_port} "
            f"was detected), so 'closed' is a real measurement. Start a collector "
            f"or set OTEL_EXPORTER_OTLP_ENDPOINT and this puller queries it with "
            f"NO code change."
        )
        return rec

    rec["attempted"] = []

    # THE REAL PULL, part 1: every reachable LOCALHOST backend.
    for host, port, name, path in OTEL_BACKENDS:
        if path is None or not any(r["port"] == port for r in rec["reachable"]):
            continue
        for svc in (SERVICES if "{svc}" in path else [""]):
            url = f"http://{host}:{port}{path.replace('{svc}', svc)}"
            c, b, e = _http(url, timeout=10.0)
            rec["attempted"].append({"url": url, "status": c, "error": e})
            if c != 200:
                continue
            try:
                payload = json.loads(b)
            except Exception:  # noqa: BLE001
                continue
            spans = _extract_spans(payload, name)
            rec["traces"].extend(spans["traces"])
            rec["error_spans"].extend(spans["errors"])

    # THE REAL PULL, part 2: the CONFIGURED endpoint.
    #
    # This block is the whole point of honouring OTEL_EXPORTER_OTLP_ENDPOINT. An
    # earlier draft accepted the env var as grounds to proceed and then looped ONLY
    # over reachable localhost ports -- so a configured REMOTE collector was never
    # actually queried, and the puller reported a confident "empty" having attempted
    # nothing at all. That is a check asserting coverage it does not have: exactly
    # the disease this fleet exists to cure, committed inside the puller written to
    # cure it. The endpoint is now genuinely fetched.
    ep = rec["endpoint_env"]
    if ep:
        base = ep.rstrip("/")
        # The env var names an OTLP INGEST endpoint, which has no read API. Read-side
        # query APIs live alongside it, so try the known shapes and record each
        # attempt. Whatever answers 200 with parseable JSON is used.
        candidates: list[tuple[str, str]] = []
        for svc in SERVICES:
            candidates.append(("jaeger", f"{base}/api/traces?service={svc}&limit=20&lookback=1h"))
        candidates.append(("tempo", f"{base}/api/search?tags=&limit=20"))
        candidates.append(("zipkin", f"{base}/api/v2/traces?limit=20"))

        for backend, url in candidates:
            c, b, e = _http(url, timeout=10.0)
            rec["attempted"].append({"url": url, "status": c, "error": e,
                                     "via": "OTEL_EXPORTER_OTLP_ENDPOINT"})
            if c != 200:
                continue
            try:
                payload = json.loads(b)
            except Exception:  # noqa: BLE001
                continue
            spans = _extract_spans(payload, backend)
            rec["traces"].extend(spans["traces"])
            rec["error_spans"].extend(spans["errors"])

    n_ok = sum(1 for a in rec["attempted"] if a["status"] == 200)
    rec["status"] = "pulled" if rec["traces"] else "empty"
    rec["reason"] = (
        f"{len(rec['attempted'])} query attempt(s) ({n_ok} answered 200) across "
        f"{len(rec['reachable'])} reachable localhost backend(s)"
        + (f" + configured endpoint {ep}" if ep else "")
        + f"; {len(rec['traces'])} trace(s), {len(rec['error_spans'])} error span(s) retrieved"
    )
    if rec["status"] == "empty" and ep and n_ok == 0:
        rec["reason"] += (
            ". NOTE: the configured endpoint was QUERIED and did not answer a read API "
            "(OTLP ingest endpoints have no query interface) -- set the endpoint to a "
            "Jaeger/Tempo/Zipkin query API to retrieve traces."
        )
    return rec


def _extract_spans(payload, backend: str) -> dict:
    """Normalise Jaeger/Tempo/Zipkin shapes into traces + error spans."""
    traces: list = []
    errors: list = []
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        return {"traces": traces, "errors": errors}

    for tr in data:
        spans = tr.get("spans", []) if isinstance(tr, dict) else (tr if isinstance(tr, list) else [])
        tid = tr.get("traceID") if isinstance(tr, dict) else None
        summary = {"trace_id": tid, "backend": backend, "span_count": len(spans), "operations": []}
        for sp in spans:
            if not isinstance(sp, dict):
                continue
            op = sp.get("operationName") or sp.get("name")
            summary["operations"].append(op)
            tags = {}
            for t in sp.get("tags", []) or []:
                if isinstance(t, dict) and "key" in t:
                    tags[t["key"]] = t.get("value")
            is_err = (
                str(tags.get("error", "")).lower() == "true"
                or str(tags.get("otel.status_code", "")).upper() == "ERROR"
                or (isinstance(tags.get("http.status_code"), int)
                    and tags["http.status_code"] >= 500)
            )
            if is_err:
                errors.append({
                    "trace_id": tid or sp.get("traceId"),
                    "operation": op,
                    "http_status": tags.get("http.status_code"),
                    "http_route": tags.get("http.route") or tags.get("http.target"),
                    "exception": tags.get("exception.type") or tags.get("error.type"),
                    "message": tags.get("exception.message"),
                    "duration_us": sp.get("duration"),
                })
        traces.append(summary)
    return {"traces": traces, "errors": errors}


# --------------------------------------------------------- gateway logs ----
LOG_CANDIDATES = [
    "/var/log/gateway", "/var/log/gateway/*.log", "/var/log/fabric",
    "/var/log/fabric-gateway", "/var/log/fabric/*.log",
    "./logs", "./logs/*.log", "./gateway-logs", "./gateway-logs/*.log",
    "/tmp/gateway", "/tmp/gateway/*.log", "/mnt/logs", "/mnt/logs/*.log",
    "/data/logs", "/data/logs/*.log",
]

# Each facet is matched INDEPENDENTLY. A single alternating regex
# (`level|status|exc`) short-circuits on the FIRST alternative: on the line
#
#     2026-07-12T10:40:03Z ERROR POST /v1/usage 500 9ms KeyError: 'tokens'
#
# it matches `ERROR` and STOPS -- so `http_status` and, far worse, the EXCEPTION
# TYPE are silently dropped. The exception type is the single field that ties a
# symptom to a code defect (it is what makes fixability analysis possible), so
# losing it while still reporting the entry as "extracted" is a check that claims
# coverage it does not have. My own T2 gate caught this.
LOG_LEVEL_RE = re.compile(r"\b(ERROR|WARNING|WARN|CRITICAL|FATAL|INFO|DEBUG)\b")
LOG_STATUS_RE = re.compile(r"\b([45]\d{2})\b")
LOG_EXC_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception))\b")
LOG_ROUTE_RE = re.compile(r"\b(?:GET|POST|PUT|PATCH|DELETE)\s+(/\S*)")


def parse_log_line(line: str) -> dict | None:
    """Extract every facet present, independently. Returns None for a clean line.

    A line is ANOMALOUS if it carries a non-INFO level, a 4xx/5xx status, or an
    exception name. A clean INFO/200 line must return None -- a detector that flags
    everything is as useless as one that flags nothing (T2 asserts both directions).
    """
    lvl_m = LOG_LEVEL_RE.search(line)
    st_m = LOG_STATUS_RE.search(line)
    exc_m = LOG_EXC_RE.search(line)
    route_m = LOG_ROUTE_RE.search(line)

    level = lvl_m.group(1).upper() if lvl_m else None
    status = st_m.group(1) if st_m else None
    exc = exc_m.group(1) if exc_m else None

    anomalous = (
        (level is not None and level not in {"INFO", "DEBUG"})
        or status is not None
        or exc is not None
    )
    if not anomalous:
        return None

    return {
        "level": level,
        "http_status": status,
        "exception": exc,
        "http_route": route_m.group(1) if route_m else None,
        "line": line[:300],
    }


def pull_gateway_logs() -> dict:
    """Discover a log source and ACTUALLY READ + PARSE it."""
    rec: dict = {
        "source": "gateway_logs",
        "env_path": os.environ.get("FABRIC_GATEWAY_LOG_PATH"),
        "candidates_checked": len(LOG_CANDIDATES),
        "found": [],
        "lines_read": 0,
        "entries": [],
        "status": "unavailable",
        "reason": None,
    }

    paths: list[pathlib.Path] = []
    env_path = rec["env_path"]
    search = ([env_path] if env_path else []) + LOG_CANDIDATES
    for cand in search:
        for hit in glob.glob(cand):
            p = pathlib.Path(hit)
            if p.is_file():
                paths.append(p)
            elif p.is_dir():
                paths.extend(q for q in p.rglob("*.log") if q.is_file())

    rec["found"] = [str(p) for p in paths]
    if not paths:
        rec["reason"] = (
            f"no gateway-log source: {len(LOG_CANDIDATES)} candidate paths checked "
            f"(plus FABRIC_GATEWAY_LOG_PATH, unset), 0 found. Mount a log path or "
            f"set FABRIC_GATEWAY_LOG_PATH and this puller parses it with NO code change."
        )
        return rec

    # THE REAL PULL: read the tail of each log and extract anomalies.
    for p in paths[:20]:
        try:
            lines = p.read_text(errors="replace").splitlines()[-2000:]
        except OSError as exc:
            rec["entries"].append({"file": str(p), "unreadable": str(exc)})
            continue
        rec["lines_read"] += len(lines)
        for ln in lines:
            parsed = parse_log_line(ln)
            if parsed is None:
                continue
            parsed["file"] = str(p)
            rec["entries"].append(parsed)

    rec["status"] = "pulled" if rec["entries"] else "empty"
    rec["reason"] = (f"{rec['lines_read']} line(s) read from {len(paths)} file(s); "
                     f"{len(rec['entries'])} anomalous entr(ies) extracted")
    return rec


# ---------------------------------------------------------------- report ----
def main() -> int:
    print("=" * 78)
    print("TELEMETRY PULL (retrieval attempted against every source)")
    print("=" * 78)

    sentry = pull_sentry()
    otel = pull_otel()
    logs = pull_gateway_logs()

    for rec in (sentry, otel, logs):
        print(f"\n--- {rec['source']} -> {rec['status'].upper()}")
        print(f"    {rec['reason']}")
        for a in rec.get("attempted", []):
            print(f"    fetch: {a['url']} -> "
                  f"{a['status'] if a['status'] is not None else 'NETWORK FAILURE'}")
        if rec["source"] == "otel":
            closed = [p["port"] for p in rec["probed"] if not p["open"]]
            print(f"    negative control: port {rec['negative_control']['port']} "
                  f"detected={rec['negative_control']['detected']}")
            print(f"    backends probed={len(rec['probed'])} reachable={len(rec['reachable'])} "
                  f"closed={len(closed)}")
        if rec["source"] == "gateway_logs":
            print(f"    candidates={rec['candidates_checked']} found={len(rec['found'])} "
                  f"lines_read={rec['lines_read']}")

    pulled = [r["source"] for r in (sentry, otel, logs) if r["status"] == "pulled"]
    empty = [r["source"] for r in (sentry, otel, logs) if r["status"] == "empty"]
    dark = [r["source"] for r in (sentry, otel, logs)
            if r["status"] in {"unavailable", "instrument_broken"}]

    n_issues = len(sentry["issues"])
    n_traces = len(otel["traces"])
    n_errspans = len(otel["error_spans"])
    n_logs = len(logs["entries"])

    print("\n" + "=" * 78)
    print("PULL RESULT")
    print("=" * 78)
    print(f"  sentry issues retrieved : {n_issues}")
    print(f"  otel traces retrieved   : {n_traces} ({n_errspans} error spans)")
    print(f"  gateway log entries     : {n_logs}")
    print(f"\n  pulled : {pulled or 'none'}")
    print(f"  empty  : {empty or 'none'}")
    print(f"  dark   : {dark or 'none'}")

    signal = n_issues + n_traces + n_logs
    if signal == 0:
        print("\n  => ZERO production symptoms retrieved from ANY source.")
        print("     Findings must be established by EXECUTING THE DEPLOYED SOURCE.")
        print("     Blast radius is UNKNOWN and MUST NOT be estimated.")
    else:
        print(f"\n  => {signal} production symptom(s) retrieved. Cluster these against")
        print("     the deploy context and bound blast radius from the observed signal.")

    payload = {
        "run_utc": datetime.now(timezone.utc).isoformat(),
        "sentry": sentry,
        "otel": otel,
        "gateway_logs": logs,
        "summary": {
            "sentry_issues": n_issues,
            "otel_traces": n_traces,
            "otel_error_spans": n_errspans,
            "gateway_log_entries": n_logs,
            "sources_pulled": pulled,
            "sources_empty": empty,
            "sources_dark": dark,
            "total_symptoms": signal,
            "blast_radius_estimable": signal > 0,
        },
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"\n  provenance artifact written: {OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
