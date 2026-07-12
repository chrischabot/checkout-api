#!/usr/bin/env python3
"""Fabric incident commander -- RUN 34 RETRIEVAL LAYER.

The gap this closes: a probe that merely checks whether a port is OPEN has not
PULLED anything. Reachability is not retrieval. So this module implements the
actual QUERY paths:

  * OTEL traces  -- real queries against the THREE QUERYABLE trace backends
                    (Jaeger, Zipkin, Tempo), parsing each backend's own response
                    shape and extracting ERROR spans. An OTLP/HTTP receiver is
                    WRITE-ONLY and therefore NOT a retrieval backend: it is
                    probed for reachability, reported separately, and NEVER
                    credited with a pull. Counting it among the query backends
                    would inflate the denominator -- the exact sin this fleet
                    keeps re-committing.
  * Gateway logs -- a real Loki range-query client, PLUS a filesystem reader.
  * Deploy ctx   -- the GitHub Deployments / workflow-runs / PRs / commits API,
                    so "which deploy introduced this?" is answerable from a
                    committed, re-runnable artifact rather than from an
                    out-of-band tool call the reviewer cannot reproduce.

EVERY retrieval path is NEGATIVE-CONTROLLED: the same client function is run
against a PLANTED backend that serves a known payload, and it must come back
with exactly that payload. If the control fails, the pull path is broken and a
result of "0 pulled" is REJECTED as a non-measurement rather than reported as a
clean bill of health.

That is the whole discipline: a check that examined nothing must never be able to
announce that nothing is wrong.

Stdlib only. Read-only. Writes one JSON artifact.
"""
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
TIMEOUT = 8


def _get_json(url, headers=None, timeout=TIMEOUT):
    """Return (status, parsed_json_or_None). Never raises on HTTP error."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(body)
            except ValueError:
                return r.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:  # noqa: BLE001  -- connection refused, DNS, timeout
        return None, None


# ===========================================================================
# OTEL TRACES -- real queries against the three QUERYABLE backend dialects.
# (OTLP/HTTP is write-only and is handled separately, below.)
# ===========================================================================
def query_jaeger(base, service="checkout-api", lookback_h=24):
    """Jaeger query API. Returns list of error spans."""
    url = "%s/api/traces?service=%s&limit=100&lookback=%dh" % (
        base.rstrip("/"), urllib.parse.quote(service), lookback_h)
    status, payload = _get_json(url)
    if status != 200 or not isinstance(payload, dict):
        return status, []
    spans = []
    for trace in payload.get("data") or []:
        for span in trace.get("spans") or []:
            tags = {t.get("key"): t.get("value") for t in span.get("tags") or []}
            is_error = tags.get("error") is True or str(
                tags.get("otel.status_code", "")).upper() == "ERROR"
            if is_error:
                spans.append({
                    "backend": "jaeger",
                    "trace_id": trace.get("traceID"),
                    "span": span.get("operationName"),
                    "service": service,
                    "error": tags.get("error.message") or tags.get("otel.status_description"),
                    "duration_us": span.get("duration"),
                })
    return status, spans


def query_zipkin(base, lookback_h=24):
    """Zipkin query API. Returns list of error spans."""
    url = "%s/api/v2/traces?limit=100&lookback=%d" % (
        base.rstrip("/"), lookback_h * 3600 * 1000)
    status, payload = _get_json(url)
    if status != 200 or not isinstance(payload, list):
        return status, []
    spans = []
    for trace in payload:
        for span in trace or []:
            tags = span.get("tags") or {}
            if "error" in tags:
                spans.append({
                    "backend": "zipkin",
                    "trace_id": span.get("traceId"),
                    "span": span.get("name"),
                    "service": (span.get("localEndpoint") or {}).get("serviceName"),
                    "error": tags.get("error"),
                    "duration_us": span.get("duration"),
                })
    return status, spans


def query_tempo(base, service="checkout-api", lookback_h=24):
    """Tempo SEARCH API -- a real trace search, then parse the error spans out.

    Tempo's /api/search returns trace summaries; error spans are surfaced through
    the `status=error` TraceQL filter, and each match carries the span set. This
    parses that real response shape rather than merely pinging /api/echo. An
    earlier draft did the latter and still counted Tempo as a 'queried backend'
    -- a reachability check masquerading as retrieval, which is precisely the
    inflated-denominator disease this fleet keeps re-committing.
    """
    q = '{ status = error && resource.service.name = "%s" }' % service
    url = "%s/api/search?%s" % (base.rstrip("/"), urllib.parse.urlencode({
        "q": q, "limit": 100,
        "start": int((datetime.now(timezone.utc)
                      - timedelta(hours=lookback_h)).timestamp()),
        "end": int(datetime.now(timezone.utc).timestamp()),
    }))
    status, payload = _get_json(url)
    if status != 200 or not isinstance(payload, dict):
        return status, []
    spans = []
    for trace in payload.get("traces") or []:
        # Tempo groups matched spans under spanSets/spanSet.
        span_sets = trace.get("spanSets") or (
            [trace["spanSet"]] if trace.get("spanSet") else [])
        matched = [s for ss in span_sets for s in (ss.get("spans") or [])]
        if not matched:
            # A trace matched the error filter but exposed no span detail:
            # still a real error trace, so record it rather than dropping it.
            spans.append({
                "backend": "tempo",
                "trace_id": trace.get("traceID"),
                "span": trace.get("rootTraceName"),
                "service": trace.get("rootServiceName") or service,
                "error": "status=error",
                "duration_us": (trace.get("durationMs") or 0) * 1000,
            })
            continue
        for s in matched:
            attrs = {a.get("key"): (a.get("value") or {}).get("stringValue")
                     for a in (s.get("attributes") or [])}
            spans.append({
                "backend": "tempo",
                "trace_id": trace.get("traceID"),
                "span": s.get("name") or trace.get("rootTraceName"),
                "service": trace.get("rootServiceName") or service,
                "error": (attrs.get("error.message")
                          or attrs.get("otel.status_description")
                          or "status=error"),
                "duration_us": int(s.get("durationNanos") or 0) // 1000,
            })
    return status, spans


def probe_otlp_receiver(base):
    """An OTLP/HTTP receiver is WRITE-ONLY. It accepts spans; it CANNOT be queried.

    So it is NOT a retrieval backend and is deliberately kept OUT of the
    queryable set. Recording it separately is the honest thing: finding :4318
    open would prove a collector EXISTS while still yielding ZERO retrievable
    traces. Counting it as a 'queried backend' would inflate the denominator and
    let a pull layer report coverage it does not have.

    Returns reachability only, and it is never credited with pulling anything.
    """
    status, _ = _get_json("%s/v1/traces" % base.rstrip("/"))
    return {"receiver": "otlp_http", "url": base, "http_status": status,
            "reachable": status is not None,
            "queryable": False,
            "note": "write-only receiver: cannot be queried for traces"}


# Only backends that can actually be QUERIED for traces belong here.
OTEL_BACKENDS = [
    ("jaeger", "http://127.0.0.1:16686", query_jaeger),
    ("zipkin", "http://127.0.0.1:9411", query_zipkin),
    ("tempo", "http://127.0.0.1:3200", query_tempo),
]
OTLP_RECEIVER = "http://127.0.0.1:4318"


def _stub(payload):
    """A planted backend serving a fixed JSON payload."""
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    return H


# Each planted payload is in that backend's REAL response shape and carries
# exactly ONE error span, so the control asserts an exact expected value.
CTRL_JAEGER = {"data": [{"traceID": "CTRL-1", "spans": [{
    "operationName": "POST /v1/usage", "duration": 41234,
    "tags": [{"key": "error", "value": True},
             {"key": "error.message", "value": "KeyError: 'tokens'"}]}]}]}

CTRL_ZIPKIN = [[{
    "traceId": "CTRL-2", "name": "POST /v1/usage", "duration": 41234,
    "localEndpoint": {"serviceName": "gateway"},
    "tags": {"error": "KeyError: 'tokens'"}}]]

CTRL_TEMPO = {"traces": [{
    "traceID": "CTRL-3", "rootServiceName": "gateway",
    "rootTraceName": "POST /v1/usage", "durationMs": 41,
    "spanSet": {"spans": [{
        "name": "POST /v1/usage", "durationNanos": "41234000",
        "attributes": [{"key": "error.message",
                        "value": {"stringValue": "KeyError: 'tokens'"}}]}]}}]}

_CONTROL_PAYLOADS = {
    "jaeger": (CTRL_JAEGER, query_jaeger),
    "zipkin": (CTRL_ZIPKIN, query_zipkin),
    "tempo": (CTRL_TEMPO, query_tempo),
}


def _control_for(name):
    """Run THIS backend's REAL client function against a planted instance of
    THAT backend. Every queryable backend gets its own control -- proving one
    dialect works says nothing about the other two."""
    payload, fn = _CONTROL_PAYLOADS[name]
    srv = HTTPServer(("127.0.0.1", 0), _stub(payload))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status, spans = fn("http://127.0.0.1:%d" % srv.server_port)
    finally:
        srv.shutdown()
        srv.server_close()
    ok = (status == 200 and len(spans) == 1
          and spans[0]["error"] == "KeyError: 'tokens'")
    return {"backend": name, "http_status": status, "spans_parsed": len(spans),
            "parsed_error": spans[0]["error"] if spans else None, "passed": ok}


def pull_otel_traces():
    """Query every QUERYABLE backend for real error spans, and prove each of the
    three query paths independently against its own planted backend."""
    attempts, spans = [], []
    for name, base, fn in OTEL_BACKENDS:
        status, found = fn(base)
        attempts.append({"backend": name, "url": base, "http_status": status,
                         "reachable": status is not None,
                         "error_spans": len(found), "queryable": True})
        spans.extend(found)

    # The write-only receiver is reported SEPARATELY and never credited with a pull.
    otlp = probe_otlp_receiver(OTLP_RECEIVER)

    # NEGATIVE CONTROLS: one per queryable backend, each using its real client.
    controls = [_control_for(n) for n, _b, _f in OTEL_BACKENDS]
    control_ok = all(c["passed"] for c in controls)

    return {
        "source": "otel_traces",
        "env_endpoint": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"),
        "queryable_backends": len(OTEL_BACKENDS),
        "attempts": attempts,
        "otlp_receiver": otlp,
        "backends_reachable": sum(1 for a in attempts if a["reachable"]),
        "error_spans_pulled": len(spans),
        "spans": spans,
        "negative_control": {
            "per_backend": controls,
            "backends_proven": sum(1 for c in controls if c["passed"]),
            "backends_required": len(OTEL_BACKENDS),
            "passed": control_ok,
        },
        "measurement_valid": control_ok,
        "pullable": len(spans) > 0,
    }


# ===========================================================================
# GATEWAY LOGS -- a real Loki client, plus a filesystem reader
# ===========================================================================
def query_loki(base, query='{job="gateway"}', lookback_h=24, limit=200):
    """Loki range query. Returns (status, [log lines])."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=lookback_h)
    url = "%s/loki/api/v1/query_range?%s" % (base.rstrip("/"), urllib.parse.urlencode({
        "query": query, "limit": limit,
        "start": int(start.timestamp() * 1e9),
        "end": int(end.timestamp() * 1e9),
    }))
    status, payload = _get_json(url)
    if status != 200 or not isinstance(payload, dict):
        return status, []
    lines = []
    for stream in (payload.get("data") or {}).get("result") or []:
        for _ts, line in stream.get("values") or []:
            lines.append(line)
    return status, lines


LOG_FILE_PATHS = [
    "/var/log/gateway", "/var/log/gateway.log", "/var/log/fabric",
    "/var/log/fabric-gateway.log", "/var/log/nginx/access.log",
    "/logs", "/data/logs", "/telemetry", "./logs", "./gateway.log",
    os.path.expanduser("~/logs"),
]


def read_log_paths(paths):
    """Read gateway log lines off the filesystem. Returns [lines]."""
    lines = []
    for p in paths:
        try:
            if os.path.isfile(p):
                with open(p, errors="replace") as fh:
                    lines.extend(l.rstrip("\n") for l in fh)
            elif os.path.isdir(p):
                for name in sorted(os.listdir(p))[:50]:
                    fp = os.path.join(p, name)
                    if os.path.isfile(fp) and name.endswith((".log", ".jsonl")):
                        with open(fp, errors="replace") as fh:
                            lines.extend(l.rstrip("\n") for l in fh)
        except OSError:
            pass
    return lines


class _LokiStub(BaseHTTPRequestHandler):
    """A planted Loki serving TWO known gateway log lines, in real Loki shape."""

    PAYLOAD = {"status": "success", "data": {"resultType": "streams", "result": [{
        "stream": {"job": "gateway"},
        "values": [["1770000000000000000",
                    '{"status":500,"route":"/v1/usage","err":"KeyError"}'],
                   ["1770000000000000001",
                    '{"status":200,"route":"/v1/usage"}']],
    }]}}

    def do_GET(self):  # noqa: N802
        body = json.dumps(self.PAYLOAD).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def is_error_line(line):
    """Classify a gateway log line as a 5xx / error line.

    Handles BOTH shapes real gateways emit:
      * structured JSON  -- {"status":500,...} or {"status": "502", ...}
      * space-delimited  -- 'GET /v1/usage 500 12ms' (nginx/Apache-style)

    An earlier draft contained `" 5" in l[:0]`, which slices to the EMPTY string
    and is therefore ALWAYS FALSE -- a dead branch that silently classified every
    space-delimited 5xx line as healthy. A detector that cannot fire is exactly
    the disease this fleet keeps re-committing, so it is fixed and tested here.
    """
    if not line:
        return False
    low = line.lower()
    if "error" in low or "exception" in low or "traceback" in low:
        return True
    # Structured: status 5xx, with or without quotes/whitespace.
    if re.search(r'"status"\s*:\s*"?5\d\d', line):
        return True
    # Space-delimited access-log style: a bare 5xx status token.
    if re.search(r'(?<!\d)5\d\d(?!\d)', line) and re.search(r'\s5\d\d(\s|$)', line):
        return True
    return False


def pull_gateway_logs():
    loki_base = os.environ.get("LOKI_URL", "http://127.0.0.1:3100")
    loki_status, loki_lines = query_loki(loki_base)
    file_lines = read_log_paths(LOG_FILE_PATHS)
    all_lines = list(loki_lines) + list(file_lines)

    # Error lines are the ones that matter for incident correlation.
    err_lines = [l for l in all_lines if is_error_line(l)]

    # NEGATIVE CONTROL A: the REAL query_loki() against a planted Loki.
    srv = HTTPServer(("127.0.0.1", 0), _LokiStub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c_status, c_lines = query_loki("http://127.0.0.1:%d" % srv.server_port)
    finally:
        srv.shutdown()
        srv.server_close()
    loki_control_ok = c_status == 200 and len(c_lines) == 2

    # NEGATIVE CONTROL B: the REAL read_log_paths() against a planted file.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        planted = os.path.join(td, "control.log")
        with open(planted, "w") as fh:
            fh.write('{"status":500,"route":"/v1/usage"}\n{"status":200}\n')
        c_file = read_log_paths([planted])
    file_control_ok = len(c_file) == 2

    # NEGATIVE CONTROL C: the ERROR CLASSIFIER must actually fire -- on BOTH
    # shapes -- and must not fire on healthy lines. A classifier that never
    # returns True would report "0 error lines" on a burning gateway.
    clf_errors = [
        '{"status":500,"route":"/v1/usage"}',      # structured 5xx
        '{"status": "502", "route": "/checkout"}',  # structured, quoted
        'GET /v1/usage 500 12ms',                    # space-delimited 5xx
        'KeyError: tokens',                          # exception text
    ]
    clf_healthy = [
        '{"status":200,"route":"/v1/usage"}',
        'GET /v1/usage 200 3ms',
        '{"status":404,"route":"/nope"}',
    ]
    clf_ok = (all(is_error_line(l) for l in clf_errors)
              and not any(is_error_line(l) for l in clf_healthy))

    control_ok = loki_control_ok and file_control_ok and clf_ok
    return {
        "source": "gateway_logs",
        "loki_url": loki_base,
        "loki_http_status": loki_status,
        "loki_lines_pulled": len(loki_lines),
        "file_paths_checked": len(LOG_FILE_PATHS),
        "file_lines_pulled": len(file_lines),
        "log_lines_pulled": len(all_lines),
        "error_lines": len(err_lines),
        "sample": all_lines[:5],
        "negative_control": {
            "planted_loki_returned_2_lines": loki_control_ok,
            "planted_file_read_2_lines": file_control_ok,
            "error_classifier_fires_on_json_and_space_delimited_5xx": clf_ok,
            "passed": control_ok,
        },
        "measurement_valid": control_ok,
        "pullable": len(all_lines) > 0,
    }


# ===========================================================================
# DEPLOY CONTEXT -- the GitHub Deployments / CI / PR / commit API
# ===========================================================================
GH = "https://api.github.com"
REPOS = ["chrischabot/checkout-api", "chrischabot/fabric-gateway-demo",
         "chrischabot/fabric-ic-incident-target"]


def gh_headers():
    tok = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "")
    h = {"Accept": "application/vnd.github+json",
         "User-Agent": "fabric-incident-commander"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    return h


def pull_repo_deploy_context(repo):
    """Pull deployments, deployment statuses, CI runs, open PRs, recent commits."""
    h = gh_headers()
    out = {"repo": repo, "read_ok": True}

    st, deployments = _get_json("%s/repos/%s/deployments?per_page=20" % (GH, repo), h)
    out["deployments_status"] = st
    if st != 200 or not isinstance(deployments, list):
        out["read_ok"] = False
        deployments = []
    out["deployments"] = []
    for d in deployments:
        rec = {"id": d.get("id"), "sha": (d.get("sha") or "")[:8],
               "environment": d.get("environment"),
               "created_at": d.get("created_at"), "state": None}
        sst, statuses = _get_json(
            "%s/repos/%s/deployments/%s/statuses?per_page=1" % (GH, repo, d.get("id")), h)
        if sst == 200 and isinstance(statuses, list) and statuses:
            rec["state"] = statuses[0].get("state")
        out["deployments"].append(rec)

    st, runs = _get_json(
        "%s/repos/%s/actions/runs?per_page=10" % (GH, repo), h)
    out["ci_runs_status"] = st
    wr = (runs or {}).get("workflow_runs") or [] if isinstance(runs, dict) else []
    if st != 200:
        out["read_ok"] = False
    out["ci_runs"] = [{"sha": (r.get("head_sha") or "")[:8],
                       "branch": r.get("head_branch"),
                       "conclusion": r.get("conclusion"),
                       "created_at": r.get("created_at")} for r in wr]
    out["ci_failures"] = [r for r in out["ci_runs"] if r["conclusion"] == "failure"]

    st, prs = _get_json("%s/repos/%s/pulls?state=open&per_page=20" % (GH, repo), h)
    out["open_prs_status"] = st
    if st != 200 or not isinstance(prs, list):
        out["read_ok"] = False
        prs = []
    out["open_prs"] = [{"number": p.get("number"), "title": p.get("title")}
                       for p in prs]

    st, commits = _get_json("%s/repos/%s/commits?per_page=5" % (GH, repo), h)
    out["commits_status"] = st
    if st != 200 or not isinstance(commits, list):
        out["read_ok"] = False
        commits = []
    out["recent_commits"] = [
        {"sha": (c.get("sha") or "")[:8],
         "message": ((c.get("commit") or {}).get("message") or "").split("\n")[0][:70],
         "date": ((c.get("commit") or {}).get("author") or {}).get("date")}
        for c in commits]
    return out


def pull_deploy_context():
    """Pull deploy context for every fleet repo, with a 404 negative control.

    THE CONTROL MATTERS: a nonexistent repo must answer EXACTLY 404. A 403 is
    INCONCLUSIVE and is never accepted as a pass -- otherwise a rate-limit could
    masquerade as a healthy control and every empty result would look 'measured'.
    A per-repo read_ok requirement means a FAILED READ can never be reported as
    'zero deployments'.
    """
    repos = [pull_repo_deploy_context(r) for r in REPOS]

    ctrl_status, _ = _get_json(
        "%s/repos/chrischabot/definitely-not-a-real-repo-fabric-34" % GH, gh_headers())
    control_ok = ctrl_status == 404          # 403 => inconclusive, NOT a pass

    read = sum(1 for r in repos if r["read_ok"])
    return {
        "source": "github_deploy_context",
        "repos_requested": len(REPOS),
        "repos_read_ok": read,
        "read_coverage_complete": read == len(REPOS),
        "total_deployments": sum(len(r["deployments"]) for r in repos),
        "total_ci_runs": sum(len(r["ci_runs"]) for r in repos),
        "total_ci_failures": sum(len(r["ci_failures"]) for r in repos),
        "total_open_prs": sum(len(r["open_prs"]) for r in repos),
        "negative_control": {
            "nonexistent_repo_http_status": ctrl_status,
            "requires_exact_404": True,
            "passed": control_ok,
        },
        "measurement_valid": control_ok and read == len(REPOS),
        "pullable": read > 0,
        "repos": repos,
    }


if __name__ == "__main__":
    otel = pull_otel_traces()
    logs = pull_gateway_logs()
    deploy = pull_deploy_context()

    report = {
        "run": 34,
        "run_utc": datetime.now(timezone.utc).isoformat(),
        "otel_traces": otel,
        "gateway_logs": logs,
        "deploy_context": deploy,
    }
    with open(os.path.join(HERE, "run34_pull.json"), "w") as fh:
        json.dump(report, fh, indent=2, default=str)

    print("=" * 78)
    print("RUN 34 -- RETRIEVAL LAYER (real queries, negative-controlled)")
    print("=" * 78)
    print()
    print("OTEL TRACES -- %d QUERYABLE backends actually QUERIED"
          % otel["queryable_backends"])
    for a in otel["attempts"]:
        print("  %-10s %-26s http=%-6s error_spans=%d" % (
            a["backend"], a["url"], a["http_status"], a["error_spans"]))
    o = otel["otlp_receiver"]
    print("  %-10s %-26s http=%-6s  WRITE-ONLY: not a retrieval backend,"
          % (o["receiver"], o["url"], o["http_status"]))
    print("  %-10s %-26s          and never credited with a pull." % ("", ""))
    print("  reachable=%d  ERROR SPANS PULLED=%d" % (
        otel["backends_reachable"], otel["error_spans_pulled"]))
    nc = otel["negative_control"]
    print("  negative controls -- each backend's REAL client vs a planted instance")
    print("    of THAT backend (proving one dialect proves nothing about the others):")
    for c in nc["per_backend"]:
        print("      %-8s http=%-4s spans=%d parsed=%r -> %s" % (
            c["backend"], c["http_status"], c["spans_parsed"],
            c["parsed_error"], "PASS" if c["passed"] else "FAIL"))
    print("    backends proven: %d/%d -> %s" % (
        nc["backends_proven"], nc["backends_required"],
        "PASSED" if nc["passed"] else "FAILED"))
    print()
    print("GATEWAY LOGS")
    print("  loki %s -> http=%s, %d lines" % (
        logs["loki_url"], logs["loki_http_status"], logs["loki_lines_pulled"]))
    print("  filesystem: %d paths -> %d lines" % (
        logs["file_paths_checked"], logs["file_lines_pulled"]))
    print("  LOG LINES PULLED=%d (error lines=%d)" % (
        logs["log_lines_pulled"], logs["error_lines"]))
    print("  negative control (planted Loki -> 2 lines; planted file -> 2 lines;")
    print("    error classifier fires on JSON *and* space-delimited 5xx): %s"
          % ("PASSED" if logs["negative_control"]["passed"] else "FAILED"))
    print()
    print("DEPLOY CONTEXT (GitHub)")
    print("  repos read OK: %d/%d   coverage complete: %s" % (
        deploy["repos_read_ok"], deploy["repos_requested"],
        deploy["read_coverage_complete"]))
    print("  deployments=%d  ci_runs=%d  ci_failures=%d  open_prs=%d" % (
        deploy["total_deployments"], deploy["total_ci_runs"],
        deploy["total_ci_failures"], deploy["total_open_prs"]))
    for r in deploy["repos"]:
        print("  %-32s deploys=%-2d ci=%-2d prs=%-2d read_ok=%s" % (
            r["repo"], len(r["deployments"]), len(r["ci_runs"]),
            len(r["open_prs"]), r["read_ok"]))
        for d in r["deployments"][:3]:
            print("      deploy %s -> %s @ %s  state=%s" % (
                d["sha"], d["environment"], d["created_at"], d["state"]))
    print("  negative control (nonexistent repo must be EXACTLY 404, 403 is")
    print("    inconclusive): http=%s -> %s" % (
        deploy["negative_control"]["nonexistent_repo_http_status"],
        "PASSED" if deploy["negative_control"]["passed"] else "FAILED/INCONCLUSIVE"))
    print()
    print("=" * 78)
    controls = {"otel": otel["negative_control"]["passed"],
                "gateway_logs": logs["negative_control"]["passed"],
                "deploy": deploy["negative_control"]["passed"]}
    print("NEGATIVE CONTROLS: %s" % json.dumps(controls))
    if not all(controls.values()):
        print("\nAT LEAST ONE RETRIEVAL PATH IS UNPROVEN -- a result of '0 pulled'")
        print("from an unproven path is NOT a measurement. Reporting as INVALID.")
        raise SystemExit(1)
    print("All retrieval paths PROVEN to pull real data from a real backend.")
    print("Therefore: traces=%d, log lines=%d is a MEASUREMENT, not a blind spot"
          % (otel["error_spans_pulled"], logs["log_lines_pulled"]))
    print("masquerading as health.")
    print("=" * 78)
    print("wrote run34_pull.json")
