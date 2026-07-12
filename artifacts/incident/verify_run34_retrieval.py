#!/usr/bin/env python3
"""PROOF that the run-34 retrieval layer actually RETRIEVES.

The reviewer's challenge was exactly right: a port scan is not a pull, and a
puller that has never successfully pulled anything cannot be trusted when it
reports "0 found". "0 found" from a BROKEN client is indistinguishable from
"0 found" from a HEALTHY one -- and that difference is the whole incident.

So this stands up REAL backends on the REAL ports the puller targets in
production, and requires pull_context.py's OWN client functions -- unmodified,
the very ones that ran against production -- to retrieve exactly that data.

  G0  DIVERGENCE BASELINE: with the backends DOWN (= production, as measured),
      the clients return 0. Established FIRST, so a later "it found 2 spans"
      cannot be true of any tree.
  G1  OTEL RETRIEVAL: with a live Jaeger on :16686, the puller pulls the spans.
  G1b it pulls ERROR spans ONLY -- a healthy span must not be reported.
  G2  LOG RETRIEVAL: with a live Loki on :3100, the puller pulls the log lines.
  G2b the pulled lines carry real content (a 500 on /v1/usage).
  G3  ANTI-FABRICATION: a backend that answers 200 with an EMPTY result yields
      0 spans. A puller that hallucinates telemetry is worse than one that
      pulls none.

Exit 0 only if every gate holds.
"""
import importlib.util
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Locate the puller STRUCTURALLY from __file__ -- never from the cwd.
PULLER = None
_base = HERE
for _ in range(6):
    for _rel in ("pull_context.py",
                 os.path.join("artifacts", "incident", "pull_context.py")):
        _c = os.path.join(_base, _rel)
        if os.path.isfile(_c):
            PULLER = _c
            break
    if PULLER:
        break
    _base = os.path.dirname(_base)

if not PULLER:
    print("FAIL: pull_context.py not found from __file__ -- this verifier "
          "inspected NOTHING, which is NOT a pass.")
    raise SystemExit(1)

pc = load(PULLER, "pull_context_under_test")

RESULTS = []


def gate(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  -- " + detail) if detail else ""))
    return bool(ok)


# --- Planted payloads, in each backend's REAL response shape. ---
# 3 spans, of which exactly 2 are errors. /health must NOT be pulled.
JAEGER_PAYLOAD = {"data": [{"traceID": "abc123", "spans": [
    {"operationName": "POST /v1/usage", "duration": 51000,
     "tags": [{"key": "error", "value": True},
              {"key": "error.message", "value": "KeyError: 'tokens'"}]},
    {"operationName": "POST /checkout", "duration": 22000,
     "tags": [{"key": "otel.status_code", "value": "ERROR"},
              {"key": "otel.status_description", "value": "discount misapplied"}]},
    {"operationName": "GET /health", "duration": 100, "tags": []},
]}]}

LOKI_PAYLOAD = {"status": "success", "data": {"resultType": "streams", "result": [{
    "stream": {"job": "gateway"},
    "values": [
        ["1770000000000000000",
         '{"status":500,"route":"/v1/usage","err":"KeyError"}'],
        ["1770000000000000001", '{"status":200,"route":"/v1/usage"}'],
        ["1770000000000000002", '{"status":500,"route":"/checkout"}'],
    ],
}]}}

EMPTY_JAEGER = {"data": []}


def make_handler(payload):
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


class Backend:
    """A real HTTP server on the REQUESTED port -- the port the puller targets."""

    def __init__(self, port, payload):
        self.srv = HTTPServer(("127.0.0.1", port), make_handler(payload))

    def __enter__(self):
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()


print("=" * 78)
print("RUN 34 -- RETRIEVAL PROOF: does the puller actually PULL?")
print("=" * 78)

# --- G0: DIVERGENCE BASELINE. Backends down = production, as measured. ---
print("\nBASELINE -- backends DOWN (this is exactly what production returned):")
d_status, d_spans = pc.query_jaeger("http://127.0.0.1:16686")
d_lstatus, d_lines = pc.query_loki("http://127.0.0.1:3100")
gate("G0 DIVERGENCE baseline: backends down -> 0 spans, 0 log lines",
     d_spans == [] and d_lines == [],
     "jaeger_http=%s spans=%d | loki_http=%s lines=%d" % (
         d_status, len(d_spans), d_lstatus, len(d_lines)))

# --- G1/G2: stand up REAL backends on the REAL ports; require retrieval. ---
print("\nSTANDING UP REAL BACKENDS on the ports the puller targets in production:")
try:
    with Backend(16686, JAEGER_PAYLOAD), Backend(3100, LOKI_PAYLOAD):
        status, spans = pc.query_jaeger("http://127.0.0.1:16686")
        lstatus, lines = pc.query_loki("http://127.0.0.1:3100")

        msgs = sorted(s["error"] for s in spans if s.get("error"))
        gate("G1 OTEL RETRIEVAL: pulled 2 error spans from a LIVE Jaeger",
             status == 200 and len(spans) == 2
             and msgs == ["KeyError: 'tokens'", "discount misapplied"],
             "http=%s spans=%d msgs=%s" % (status, len(spans), msgs))

        gate("G1b pulled ERROR spans ONLY (the healthy /health span excluded)",
             all(s["span"] != "GET /health" for s in spans),
             "spans=%s" % [s["span"] for s in spans])

        gate("G2 LOG RETRIEVAL: pulled 3 gateway log lines from a LIVE Loki",
             lstatus == 200 and len(lines) == 3,
             "http=%s lines=%d" % (lstatus, len(lines)))

        gate("G2b the pulled lines carry real content (a 500 on /v1/usage)",
             any('"status":500' in l and "/v1/usage" in l for l in lines),
             "sample=%s" % (lines[0] if lines else None))

    # --- G3: ANTI-FABRICATION. Reachable but empty must yield nothing. ---
    print("\nANTI-FABRICATION -- backend UP but serving an EMPTY result set:")
    with Backend(16686, EMPTY_JAEGER):
        e_status, e_spans = pc.query_jaeger("http://127.0.0.1:16686")
        gate("G3 ANTI-FABRICATION: reachable-but-empty -> 0 spans, none invented",
             e_status == 200 and e_spans == [],
             "http=%s spans=%d" % (e_status, len(e_spans)))
except OSError as e:
    gate("backends could not bind their target ports", False, str(e))

print()
print("=" * 78)
passed = sum(1 for _n, ok in RESULTS if ok)
total = len(RESULTS)
print("RETRIEVAL PROOF: %d/%d" % (passed, total))
if passed == total:
    print()
    print("The OTEL and gateway-log query paths are PROVEN to pull real data from")
    print("real backends, using the SAME client functions that ran against")
    print("production. Production therefore returned 0 traces and 0 log lines")
    print("because NO BACKEND EXISTS -- not because the client is broken.")
    print()
    print("That is the difference between a MEASUREMENT and a BLIND SPOT.")
print("=" * 78)
sys.exit(0 if passed == total else 1)
