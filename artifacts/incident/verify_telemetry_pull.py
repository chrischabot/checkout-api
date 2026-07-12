#!/usr/bin/env python3
"""PROOF THAT THE TELEMETRY PULL LAYER ACTUALLY PULLS.

The fleet's telemetry is dark this run (no Sentry credential, no collector, no log
source). That creates a credibility trap: a puller that retrieves NOTHING looks
exactly like a puller that CANNOT retrieve. "It would work if a source existed" is
an unproven claim -- and this fleet's whole incident history is about checks that
assert coverage they do not have.

So this test STANDS UP REAL SOURCES and proves the retrieval path retrieves:

  T1  a real HTTP server speaking the Jaeger trace API on a live port
      -> pull_otel() must FIND it, QUERY it, and extract the ERROR SPAN
  T2  a real gateway log file on disk
      -> pull_gateway_logs() must READ it and extract the anomalous lines
  T3  a real HTTP server speaking the Sentry issues API, with a credential
      -> pull_sentry() must AUTHENTICATE, fetch issues, and extract STACK FRAMES
  T4  NEGATIVE CONTROL: with the sources gone, every pull reports unavailable/empty
      and INVENTS NOTHING

T4 is what makes T1-T3 meaningful. A puller that hallucinated data would sail
through T1-T3 and die here. Together they prove: it pulls when there is something
to pull, and it reports honest emptiness when there is not.

Exit 0 = all gates green.
"""
from __future__ import annotations

import http.server
import importlib.util
import json
import os
import pathlib
import tempfile
import threading

ROOT = pathlib.Path(__file__).resolve().parent
RESULTS: list[tuple[str, bool, str]] = []


def gate(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n         {detail}" if detail else ""))


def load_puller():
    """Import the SHIPPED puller -- never a reimplementation.

    Testing a copy of the code instead of the code is the 'proves the wrong thing'
    failure this fleet keeps producing. These gates drive the real functions.
    """
    spec = importlib.util.spec_from_file_location("pt", ROOT / "pull_telemetry.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


JAEGER_PAYLOAD = {
    "data": [
        {
            "traceID": "abc123deadbeef",
            "spans": [
                {
                    "operationName": "POST /v1/usage",
                    "duration": 91234,
                    "tags": [
                        {"key": "error", "value": "true"},
                        {"key": "http.status_code", "value": 500},
                        {"key": "http.route", "value": "/v1/usage"},
                        {"key": "exception.type", "value": "KeyError"},
                        {"key": "exception.message", "value": "'tokens'"},
                    ],
                },
                {
                    "operationName": "aggregate_usage",
                    "duration": 120,
                    "tags": [{"key": "http.status_code", "value": 200}],
                },
            ],
        }
    ]
}

SENTRY_ISSUES = [
    {
        "id": "9001",
        "title": "KeyError: 'tokens'",
        "culprit": "service/usage_aggregator in aggregate_usage",
        "level": "error",
        "count": "412",
        "userCount": 37,
        "firstSeen": "2026-07-11T02:40:00Z",
        "lastSeen": "2026-07-12T10:55:00Z",
        "permalink": "https://sentry.io/org/gw/issues/9001/",
    }
]

SENTRY_EVENT = {
    "entries": [
        {
            "type": "exception",
            "data": {
                "values": [
                    {
                        "stacktrace": {
                            "frames": [
                                {
                                    "filename": "service/usage_aggregator.py",
                                    "function": "aggregate_usage",
                                    "lineNo": 6,
                                    "inApp": True,
                                }
                            ]
                        }
                    }
                ]
            },
        }
    ]
}


class FakeBackend(http.server.BaseHTTPRequestHandler):
    """One server speaking BOTH the Jaeger trace API and the Sentry issues API.

    Auth is enforced for real: the Sentry routes return 401 without a Bearer token,
    so T3 proves the puller actually authenticates rather than getting lucky.
    """

    def log_message(self, *a):  # silence the default access log
        pass

    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path
        authed = "Bearer" in self.headers.get("Authorization", "")
        unauth = {"detail": "Authentication credentials were not provided."}

        if path.startswith("/api/traces"):
            return self._json(200, JAEGER_PAYLOAD)
        if path == "/api/0/":
            return self._json(200, {"version": "0", "auth": None, "user": None})
        if path == "/api/0/organizations/":
            return self._json(200, [{"slug": "gw"}]) if authed else self._json(401, unauth)
        if "/issues/" in path and path.startswith("/api/0/organizations/"):
            return self._json(200, SENTRY_ISSUES) if authed else self._json(401, unauth)
        if path.startswith("/api/0/issues/") and path.endswith("/events/latest/"):
            return self._json(200, SENTRY_EVENT) if authed else self._json(401, unauth)
        return self._json(404, {"detail": "not found"})


def serve() -> tuple[http.server.HTTPServer, int]:
    srv = http.server.HTTPServer(("127.0.0.1", 0), FakeBackend)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


GATEWAY_LOG = (
    "2026-07-12T10:40:01Z INFO  POST /v1/usage 200 12ms model=gpt-4o tokens=120\n"
    "2026-07-12T10:40:02Z INFO  POST /v1/usage 200 11ms model=claude tokens=40\n"
    "2026-07-12T10:40:03Z ERROR POST /v1/usage 500 9ms KeyError: 'tokens' batch=b-8831\n"
    "2026-07-12T10:40:04Z WARN  POST /v1/usage 200 14ms model=None tokens=40 null_bucket\n"
    "2026-07-12T10:40:05Z INFO  GET  /healthz 200 1ms\n"
)


def main() -> int:
    print("=" * 78)
    print("TELEMETRY PULL LAYER -- PROOF OF RETRIEVAL")
    print("=" * 78)
    print("The fleet is dark this run, so a puller that returns nothing looks exactly")
    print("like one that CANNOT pull. These gates stand up REAL sources and prove the")
    print("retrieval path works -- then prove it invents nothing when they go away.\n")

    pt = load_puller()

    # ---------------------------------------------------------------- T1 ----
    srv, port = serve()
    try:
        pt.OTEL_BACKENDS = [
            ("127.0.0.1", port, "jaeger",
             "/api/traces?service={svc}&limit=20&lookback=1h")
        ]
        otel = pt.pull_otel()
        errs = otel["error_spans"]
        found = any(
            e.get("exception") == "KeyError"
            and e.get("http_status") == 500
            and e.get("http_route") == "/v1/usage"
            for e in errs
        )
        gate(
            "T1 OTEL PULL -- a live collector is FOUND, QUERIED, and its error span extracted",
            otel["status"] == "pulled" and found,
            f"status={otel['status']} traces={len(otel['traces'])} "
            f"error_spans={len(errs)}; extracted={errs[:1]}",
        )
    finally:
        srv.shutdown()

    # ---------------------------------------------------------------- T2 ----
    with tempfile.TemporaryDirectory() as td:
        logdir = pathlib.Path(td) / "logs"
        logdir.mkdir()
        (logdir / "gateway.log").write_text(GATEWAY_LOG)
        os.environ["FABRIC_GATEWAY_LOG_PATH"] = str(logdir / "*.log")
        try:
            logs = pt.pull_gateway_logs()
            entries = logs["entries"]
            got_error = any(e.get("level") == "ERROR" for e in entries)
            got_exc = any(e.get("exception") == "KeyError" for e in entries)
            got_warn = any(e.get("level") in {"WARN", "WARNING"} for e in entries)
            # The clean INFO/healthz line must NOT be reported as an anomaly: a
            # detector that flags everything is as useless as one that flags nothing.
            no_noise = not any("healthz" in (e.get("line") or "") for e in entries)
            gate(
                "T2 GATEWAY LOG PULL -- a real log file is READ and its anomalies extracted",
                logs["status"] == "pulled" and got_error and got_exc
                and got_warn and no_noise,
                f"status={logs['status']} lines_read={logs['lines_read']} "
                f"entries={len(entries)} (ERROR={got_error}, KeyError={got_exc}, "
                f"WARN={got_warn}, healthy-line-noise-suppressed={no_noise})",
            )
        finally:
            os.environ.pop("FABRIC_GATEWAY_LOG_PATH", None)

    # ---------------------------------------------------------------- T3 ----
    srv, port = serve()
    try:
        pt.SENTRY_BASE = f"http://127.0.0.1:{port}"
        os.environ["SENTRY_AUTH_TOKEN"] = "test-token-not-a-real-secret"
        os.environ["SENTRY_ORG"] = "gw"
        try:
            sentry = pt.pull_sentry()
            issues = sentry["issues"]
            frames = issues[0]["frames"] if issues else []
            got_frame = any(
                f.get("filename") == "service/usage_aggregator.py"
                and f.get("function") == "aggregate_usage"
                for f in frames
            )
            gate(
                "T3 SENTRY PULL -- authenticates, fetches issues, extracts STACK FRAMES",
                sentry["status"] == "pulled" and len(issues) == 1 and got_frame,
                f"status={sentry['status']} issues={len(issues)} "
                f"title={issues[0]['title'] if issues else None!r} "
                f"users_affected={issues[0]['users_affected'] if issues else None} "
                f"frames={frames}",
            )
        finally:
            os.environ.pop("SENTRY_AUTH_TOKEN", None)
            os.environ.pop("SENTRY_ORG", None)
    finally:
        srv.shutdown()

    # ---------------------------------------------------------------- T4 ----
    # NEGATIVE CONTROL. Fresh module (pristine backend list, real sentry.io base),
    # no credential, no collector, no log path. The puller must report honest
    # emptiness and fabricate NOTHING. This is what makes T1-T3 trustworthy.
    pt2 = load_puller()
    otel_dark = pt2.pull_otel()
    logs_dark = pt2.pull_gateway_logs()
    sentry_dark = pt2.pull_sentry()

    invented = (
        len(otel_dark["traces"]) + len(otel_dark["error_spans"])
        + len(logs_dark["entries"]) + len(sentry_dark["issues"])
    )
    honest = (
        otel_dark["status"] in {"unavailable", "empty"}
        and logs_dark["status"] in {"unavailable", "empty"}
        and sentry_dark["status"] in {"unavailable", "empty"}
        and sentry_dark["credential_present"] is False
    )
    gate(
        "T4 NEGATIVE CONTROL -- with no sources, the puller INVENTS NOTHING",
        invented == 0 and honest,
        f"symptoms fabricated={invented} (must be 0); "
        f"sentry={sentry_dark['status']} otel={otel_dark['status']} "
        f"logs={logs_dark['status']}; otel negative control detected="
        f"{otel_dark['negative_control']['detected']}",
    )

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 78)
    print(f"PULL-LAYER PROOF: {passed}/{total} passed")
    print("=" * 78)
    if passed != total:
        for n, ok, _ in RESULTS:
            if not ok:
                print(f"  FAILED: {n}")
        return 1
    print("The retrieval path is REAL: it pulls Sentry issues (with stack frames), OTEL")
    print("error spans, and gateway-log anomalies when those sources exist -- and reports")
    print("honest emptiness, inventing nothing, when they do not. The fleet's telemetry is")
    print("dark because THE SOURCES ARE ABSENT, not because the puller cannot pull.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
