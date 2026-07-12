#!/usr/bin/env python3
"""Fabric incident commander -- RUN 34 evidence probe.

Every number this run reports must come from HERE, not from a prior write-up.

Two jobs:
  1. TELEMETRY PULL, negative-controlled. "Nothing found" is only a measurement
     if the probe can prove it would have found something that WAS there. So
     each probe plants its own positive and must detect it.
  2. DEFECT LIVENESS, by EXECUTING the deployed source. Not by grepping, not by
     reading an issue body.

Stdlib only. Writes one JSON artifact. Mutates nothing.
"""
import importlib.util
import json
import os
import socket
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))


def find_source(*relative_candidates):
    """Locate a deployed source file STRUCTURALLY, by walking upward from __file__.

    This probe must run in two places: the commander's multi-repo workspace (fleet
    repos as siblings) AND inside a bare single-repo checkout (what CI clones). A
    probe whose verdict depends on the directory it was launched from is not a
    verdict -- so discovery never uses the cwd.

    Returns the first existing path, or None. A None MUST become a SKIP at the
    call site, never a silent pass: a probe that inspected nothing has not given
    anything a clean bill of health.
    """
    base = HERE
    for _ in range(6):
        for rel in relative_candidates:
            candidate = os.path.join(base, rel)
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)
        base = os.path.dirname(base)
    return None


CHECKOUT_PY = find_source(
    "checkout.py",
    os.path.join("fabric-ic-incident-target", "checkout.py"),
)
AGGREGATOR_PY = find_source(
    os.path.join("service", "usage_aggregator.py"),
    os.path.join("fabric-gateway-demo", "service", "usage_aggregator.py"),
)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 1. TELEMETRY
# ---------------------------------------------------------------------------
def fetch_sentry_issues(base_url, token, org="sentry", project="internal"):
    """THE ACTUAL ISSUE-PULL PATH. Returns (status, [issue dicts]).

    This is the ONE function used for both the real sentry.io pull and for the
    planted negative control below. That is the whole point: if this function is
    broken, the control fails and "0 issues" is rejected as a non-measurement.
    Parsing the real Sentry issues payload shape (id/title/culprit/count) means a
    credentialed run pulls real symptoms through code that has been EXERCISED.
    """
    url = "%s/api/0/projects/%s/%s/issues/?statsPeriod=24h" % (
        base_url.rstrip("/"), org, project)
    req = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + (token or "")})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            payload = json.loads(r.read().decode())
            issues = [{
                "id": i.get("id"),
                "title": i.get("title"),
                "culprit": i.get("culprit"),
                "events": int(i.get("count", 0) or 0),
                "users": i.get("userCount"),
                "first_seen": i.get("firstSeen"),
            } for i in payload]
            return r.status, issues
    except urllib.error.HTTPError as e:
        return e.code, []


class _SentryStub(BaseHTTPRequestHandler):
    """A minimal auth-ENFORCING Sentry. Serves one issue to a bearer token,
    401s without it -- exactly like the real API."""

    ISSUE = [{"id": "CONTROL-1", "title": "KeyError: 'tokens'",
              "culprit": "aggregate_usage", "count": "7", "userCount": 3,
              "firstSeen": "2026-07-12T00:00:00Z"}]

    def do_GET(self):  # noqa: N802
        if self.headers.get("Authorization") != "Bearer CONTROL_TOKEN":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"detail":"Authentication credentials were not provided."}')
            return
        body = json.dumps(self.ISSUE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence
        pass


def probe_sentry():
    """Pull Sentry issues, and distinguish MISSING CREDENTIAL (401) from NO
    EGRESS (network error) -- completely different owner actions. Collapsing them
    into "sentry unavailable" is how a fleet stays blind for 33 runs.

    NEGATIVE CONTROL (symmetric with the OTEL/log probes): the same
    fetch_sentry_issues() is run against a planted auth-enforcing stub. It must
    (a) 401 with no token and (b) return exactly 1 parsed issue WITH the token.
    Only then is "0 issues from production" a measurement rather than a
    never-exercised code path claiming a clean bill of health.
    """
    out = {"source": "sentry", "env_vars_present": sorted(
        k for k in os.environ if k.upper().startswith("SENTRY"))}

    # --- NEGATIVE CONTROL: prove the pull path actually pulls. ---
    srv = HTTPServer(("127.0.0.1", 0), _SentryStub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ctrl_base = "http://127.0.0.1:%d" % srv.server_port
    try:
        unauth_status, unauth_issues = fetch_sentry_issues(ctrl_base, None)
        auth_status, auth_issues = fetch_sentry_issues(ctrl_base, "CONTROL_TOKEN")
    finally:
        srv.shutdown()
        srv.server_close()

    control_ok = (unauth_status == 401 and unauth_issues == []
                  and auth_status == 200 and len(auth_issues) == 1
                  and auth_issues[0]["events"] == 7)
    out["negative_control"] = {
        "stub_401_without_token": unauth_status == 401,
        "stub_pulled_1_issue_with_token": len(auth_issues) == 1,
        "stub_parsed_event_count": auth_issues[0]["events"] if auth_issues else None,
        "passed": control_ok,
    }
    out["measurement_valid"] = control_ok

    # --- Unauthenticated reachability: egress, independent of credentials. ---
    try:
        with urllib.request.urlopen("https://sentry.io/api/0/", timeout=15) as r:
            out["egress_status"] = r.status
            out["egress_ok"] = True
    except urllib.error.HTTPError as e:
        out["egress_status"] = e.code
        out["egress_ok"] = True          # an HTTP answer IS egress
    except Exception as e:               # noqa: BLE001
        out["egress_status"] = None
        out["egress_ok"] = False
        out["egress_error"] = type(e).__name__

    # --- THE REAL PULL, through the exact code path the control just proved. ---
    token = os.environ.get("SENTRY_AUTH_TOKEN")
    org = os.environ.get("SENTRY_ORG", "sentry")
    project = os.environ.get("SENTRY_PROJECT", "internal")
    try:
        status, issues = fetch_sentry_issues("https://sentry.io", token, org, project)
        out["auth_status"] = status
        out["issues_pulled"] = len(issues)
        out["issues"] = issues
        out["pullable"] = status == 200
        if status in (401, 403):
            out["diagnosis"] = (
                "CREDENTIAL MISSING (egress works, server answered %d)" % status
                if out["egress_ok"] else "HTTP %d" % status)
        elif status != 200:
            out["diagnosis"] = "HTTP %d" % status
    except Exception as e:              # noqa: BLE001
        out["auth_status"] = None
        out["issues_pulled"] = 0
        out["issues"] = []
        out["pullable"] = False
        out["diagnosis"] = "NO EGRESS (%s)" % type(e).__name__
    return out


OTEL_PORTS = [4317, 4318, 9411, 16686, 14268, 55681, 8126, 13133, 9090, 3200, 3100]


def probe_otel():
    """Port scan + NEGATIVE CONTROL: the probe must see a port it opens itself."""
    def is_open(port, host="127.0.0.1"):
        with socket.socket() as s:
            s.settimeout(0.4)
            return s.connect_ex((host, port)) == 0

    open_ports = [p for p in OTEL_PORTS if is_open(p)]

    # Negative control: stand up a listener and confirm the SAME code path sees it.
    ctrl = socket.socket()
    ctrl.bind(("127.0.0.1", 0))
    ctrl.listen(1)
    ctrl_port = ctrl.getsockname()[1]
    threading.Thread(target=lambda: None, daemon=True).start()
    control_detected = is_open(ctrl_port)
    ctrl.close()

    return {
        "source": "otel",
        "env_vars_present": sorted(
            k for k in os.environ if "OTEL" in k.upper() or "OTLP" in k.upper()),
        "ports_probed": len(OTEL_PORTS),
        "ports_open": open_ports,
        "negative_control_detected_own_port": control_detected,
        "pullable": bool(open_ports),
        "traces_pulled": 0,
        "measurement_valid": control_detected,
    }


LOG_PATHS = [
    "/var/log/gateway", "/var/log/gateway.log", "/var/log/fabric",
    "/var/log/fabric-gateway.log", "/var/log/nginx/access.log",
    "/logs", "/data/logs", "/telemetry", "./logs", "./gateway.log",
    os.path.expanduser("~/logs"),
]


def probe_gateway_logs():
    """Scan for a log source + NEGATIVE CONTROL: plant a file, read it back
    THROUGH THE SAME scan+parse code path the real scan uses."""
    def scan(paths):
        found = []
        for p in paths:
            if os.path.isfile(p):
                try:
                    with open(p, "r", errors="replace") as fh:
                        found.append({"path": p, "lines": sum(1 for _ in fh)})
                except OSError:
                    pass
            elif os.path.isdir(p):
                for name in sorted(os.listdir(p))[:50]:
                    fp = os.path.join(p, name)
                    if os.path.isfile(fp) and name.endswith((".log", ".jsonl")):
                        with open(fp, "r", errors="replace") as fh:
                            found.append({"path": fp, "lines": sum(1 for _ in fh)})
        return found

    real = scan(LOG_PATHS)

    with tempfile.TemporaryDirectory() as td:
        planted = os.path.join(td, "control.log")
        with open(planted, "w") as fh:
            fh.write('{"status":500,"route":"/v1/usage"}\n')
        control = scan([planted])

    control_ok = len(control) == 1 and control[0]["lines"] == 1
    return {
        "source": "gateway_logs",
        "paths_checked": len(LOG_PATHS),
        "sources_found": len(real),
        "log_lines_pulled": sum(f["lines"] for f in real),
        "negative_control_read_planted_file": control_ok,
        "pullable": bool(real),
        "measurement_valid": control_ok,
    }


# ---------------------------------------------------------------------------
# 2. DEFECT LIVENESS -- by executing the DEPLOYED source
# ---------------------------------------------------------------------------
class Exploding(dict):
    """Raises if ANY field is read. Proves price-blindness structurally."""
    def __getitem__(self, k):
        raise AssertionError("item field read")

    def get(self, *a, **k):
        raise AssertionError("item field read")


def probe_inc6():
    if not CHECKOUT_PY:
        return {"incident": "INC-6", "skipped": True,
                "reason": "checkout.py not found from __file__; NOT a clean bill of health",
                "live": None}
    co = load(CHECKOUT_PY, "deployed_checkout")

    reads_price = True
    try:
        co.apply_discount(30_000, [Exploding()])
        reads_price = False
    except AssertionError:
        reads_price = True

    cheap = co.apply_discount(30_000, [{"price_cents": 1}])
    dear = co.apply_discount(30_000, [{"price_cents": 29_999}])
    one = co.apply_discount(30_000, [{"price_cents": 1_000}])

    # How the leak scales with eligible-item COUNT (the tell: it scales INVERSELY)
    scaling = {
        n: co.apply_discount(30_000, [{"price_cents": 1_000}] * n)
        for n in (1, 5, 20)
    }
    return {
        "incident": "INC-6",
        "repo": "fabric-ic-incident-target",
        "file": "checkout.py",
        "reads_any_item_field": reads_price,
        "charge_1c_item": cheap,
        "charge_29999c_item": dear,
        "price_blind": (not reads_price) and cheap == dear,
        "charge_300usd_order_one_10usd_item": one,
        "leak_cents": 30_000 - one,
        "leak_by_eligible_count": scaling,
        "all_eligible_5x100": co.apply_discount(50_000, [{"price_cents": 10_000}] * 5),
        "declared_policy": getattr(co, "DISCOUNT_POLICY", None),
        "live": (not reads_price) and cheap == dear and one != 30_000,
    }


def probe_inc5_inc8():
    # THE TRAP, proven by execution: the fix for INC-5 does NOT fix INC-8.
    # Computed unconditionally -- it is a fact about Python, not about the fleet.
    trap = {
        "expression": '{"model": None}.get("model", "unknown")',
        "result": repr({"model": None}.get("model", "unknown")),
        "implication": ("a repair guarding only ABSENT keys passes a NULL straight "
                        "through -- fixing INC-5 that way leaves INC-8 LIVE. "
                        "They are ONE decision."),
    }
    if not AGGREGATOR_PY:
        skip = {"skipped": True,
                "reason": ("usage_aggregator.py not found from __file__; "
                           "NOT a clean bill of health"),
                "live": None}
        return (dict(skip, incident="INC-5"), dict(skip, incident="INC-8"), trap)
    ag = load(AGGREGATOR_PY, "deployed_aggregator")

    # INC-5: one record missing 'tokens' amid VALID billable records.
    batch = [
        {"model": "gpt-4", "tokens": 100},
        {"model": "claude", "tokens": 40},
        {"model": "gpt-4"},                       # malformed
    ]
    inc5 = {"incident": "INC-5", "repo": "fabric-gateway-demo",
            "file": "service/usage_aggregator.py",
            "valid_billable_tokens_in_batch": 140}
    try:
        ag.aggregate_usage(list(batch))
        inc5["raised"] = None
        inc5["live"] = False
    except BaseException as e:                    # noqa: BLE001
        inc5["raised"] = "%s(%r)" % (type(e).__name__, str(e))
        inc5["whole_batch_destroyed"] = True
        inc5["valid_tokens_lost"] = 140
        inc5["live"] = True

    # INC-8: null model books billable tokens against a None key, SILENTLY.
    inc8 = {"incident": "INC-8", "repo": "fabric-gateway-demo",
            "file": "service/usage_aggregator.py"}
    try:
        out = ag.aggregate_usage([{"model": "gpt-4", "tokens": 100},
                                  {"model": None, "tokens": 40}])
        inc8["raised"] = None
        inc8["per_model"] = {repr(k): v for k, v in out["per_model"].items()}
        inc8["grand_total"] = out["grand_total"]
        inc8["none_bucket_present"] = None in out["per_model"]
        # The nastiest part: the books BALANCE, so no invoice check can catch it.
        inc8["grand_total_reconciles"] = (
            out["grand_total"] == sum(out["per_model"].values()))
        inc8["json_serializes_none_key_as"] = json.dumps(
            {k: v for k, v in out["per_model"].items()})
        inc8["live"] = inc8["none_bucket_present"]
    except BaseException as e:                    # noqa: BLE001
        inc8["raised"] = type(e).__name__
        inc8["live"] = False

    # THE TRAP is computed above, unconditionally.
    return inc5, inc8, trap


if __name__ == "__main__":
    sentry = probe_sentry()
    otel = probe_otel()
    logs = probe_gateway_logs()
    inc6 = probe_inc6()
    inc5, inc8, trap = probe_inc5_inc8()

    controls_ok = (otel["negative_control_detected_own_port"]
                   and logs["negative_control_read_planted_file"]
                   and sentry["negative_control"]["passed"])
    pullable = sum(1 for s in (sentry, otel, logs) if s["pullable"])
    symptoms = (sentry["issues_pulled"] + otel["traces_pulled"]
                + logs["log_lines_pulled"])

    report = {
        "run": 34,
        "run_utc": datetime.now(timezone.utc).isoformat(),
        "telemetry": {
            "sources_pullable": "%d of 3" % pullable,
            "total_production_symptoms_pulled": symptoms,
            "negative_controls_all_passed": controls_ok,
            "blast_radius_estimable": symptoms > 0,
            "sentry": sentry, "otel": otel, "gateway_logs": logs,
        },
        "defects_live_by_execution": [inc6, inc5, inc8],
        "inc5_inc8_are_one_decision": trap,
    }

    with open(os.path.join(HERE, "run34_evidence.json"), "w") as fh:
        json.dump(report, fh, indent=2, default=str)

    print("=" * 78)
    print("RUN 34 -- TELEMETRY PULL (negative-controlled)")
    print("=" * 78)
    print("  sentry        : egress=%s auth=%s -> %s" % (
        sentry["egress_status"], sentry["auth_status"],
        sentry.get("diagnosis", "OK")))
    print("                  issues pulled: %d (control: 401 w/o token=%s, "
          "pulled 1 issue w/ token=%s)" % (
              sentry["issues_pulled"],
              sentry["negative_control"]["stub_401_without_token"],
              sentry["negative_control"]["stub_pulled_1_issue_with_token"]))
    print("  otel          : %d ports probed, %d open (control saw own port: %s)" % (
        otel["ports_probed"], len(otel["ports_open"]),
        otel["negative_control_detected_own_port"]))
    print("  gateway logs  : %d paths, %d sources (control read planted file: %s)" % (
        logs["paths_checked"], logs["sources_found"],
        logs["negative_control_read_planted_file"]))
    print()
    print("  SOURCES PULLABLE ........ %d of 3" % pullable)
    print("  PRODUCTION SYMPTOMS ..... %d" % symptoms)
    print("  NEGATIVE CONTROLS ....... %s" % ("PASSED" if controls_ok else "FAILED"))
    print("  BLAST RADIUS ESTIMABLE .. %s" % (symptoms > 0))
    if not controls_ok:
        raise SystemExit("negative controls FAILED -- 'nothing found' is not a "
                         "measurement. Refusing to report a clean bill of health.")
    print()
    print("=" * 78)
    print("DEFECT LIVENESS -- by EXECUTING the deployed source")
    print("=" * 78)
    for d in (inc6, inc5, inc8):
        if d.get("skipped"):
            print("  %-6s SKIPPED -- %s" % (d["incident"], d["reason"]))
    if inc6.get("skipped"):
        pass
    else:
        print("  INC-6  live=%s  price_blind=%s" % (inc6["live"], inc6["price_blind"]))
        print("         $300 order / one $10 eligible item -> charged $%.2f (leak $%.2f)"
              % (inc6["charge_300usd_order_one_10usd_item"] / 100,
                 inc6["leak_cents"] / 100))
        print("         $0.01 item -> $%.2f ; $299.99 item -> $%.2f  (IDENTICAL)"
              % (inc6["charge_1c_item"] / 100, inc6["charge_29999c_item"] / 100))
        print("         leak by eligible count: " + ", ".join(
            "%d->$%.2f" % (n, c / 100)
            for n, c in inc6["leak_by_eligible_count"].items()))
        print("         all-eligible 5x$100 -> $%.2f (deployed == correct here)"
              % (inc6["all_eligible_5x100"] / 100))
        print("         owner DISCOUNT_POLICY declaration: %r"
              % inc6["declared_policy"])
    if not inc5.get("skipped"):
        print("  INC-5  live=%s  raised=%s  valid billable tokens LOST=%s"
              % (inc5["live"], inc5["raised"], inc5.get("valid_tokens_lost")))
        print("  INC-8  live=%s  per_model=%s  grand_total=%s  reconciles=%s"
              % (inc8["live"], inc8.get("per_model"), inc8.get("grand_total"),
                 inc8.get("grand_total_reconciles")))
        print("         serialized: %s  <- a model that cannot be invoiced"
              % inc8.get("json_serializes_none_key_as"))
    print()
    print("  THE TRAP: %s -> %s" % (trap["expression"], trap["result"]))
    print("  %s" % trap["implication"])
    print("=" * 78)
    print("wrote run34_evidence.json")
