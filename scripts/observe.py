#!/usr/bin/env python3
"""Read the SLO and alert state out of Prometheus.

Starts its own port-forward and cleans it up, so both commands work in a
single terminal. Reuses an existing forward if one is already serving.

    python3 scripts/observe.py slo
    python3 scripts/observe.py alerts
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

NAMESPACE = os.environ.get("MONITORING_NAMESPACE", "monitoring")
SERVICE = os.environ.get("PROM_SVC", "monitoring-kube-prometheus-prometheus")
PORT = os.environ.get("PROM_PORT", "9090")
BASE = f"http://127.0.0.1:{PORT}"

NO_DATA = "no data yet (run `make traffic`, then wait one scrape interval)"


def _get(path: str, params: dict[str, str] | None = None, timeout: float = 5.0):
    url = f"{BASE}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def _ready() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE}/-/ready", timeout=2):
            return True
    except (urllib.error.URLError, OSError):
        return False


class PortForward:
    """Reuses an existing forward, otherwise starts and tears down its own."""

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None

    def __enter__(self) -> PortForward:
        if _ready():
            return self

        self._process = subprocess.Popen(
            ["kubectl", "-n", NAMESPACE, "port-forward", f"svc/{SERVICE}", f"{PORT}:9090"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(40):
            if _ready():
                return self
            time.sleep(0.5)

        self.__exit__(None, None, None)
        sys.exit(
            f"Could not reach Prometheus in namespace '{NAMESPACE}'.\n"
            f"Has `make monitoring` finished?  kubectl -n {NAMESPACE} get pods"
        )

    def __exit__(self, *_exc) -> None:
        if self._process is not None:
            self._process.terminate()
            self._process.wait(timeout=5)
            self._process = None


def _scalar(query: str) -> float | None:
    """First value of an instant query, or None when nothing matched."""
    try:
        result = _get("/api/v1/query", {"query": query})["data"]["result"]
    except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError):
        return None
    if not result:
        return None
    return float(result[0]["value"][1])


def show_slo() -> int:
    rows = [
        ("reads under 200ms", "visit_counter:read_fast_ratio:rate5m", lambda v: f"{v:.4%}"),
        (
            "error budget burn",
            "visit_counter:read_budget_burn:rate5m",
            lambda v: f"{v:.2f}x  (alerts above 14.4x)",
        ),
        (
            "p95 latency",
            "1000 * histogram_quantile(0.95, sum by (le) "
            "(rate(visit_counter_http_request_duration_seconds_bucket[5m])))",
            lambda v: f"{v:.1f} ms",
        ),
        (
            "visits buffered",
            "sum(visit_counter_buffer_depth)",
            lambda v: f"{v:.0f}  (lost if every pod died now)",
        ),
    ]

    print("\nSLO: 99% of count reads answered within 200ms\n")
    for label, query, fmt in rows:
        value = _scalar(query)
        print(f"  {label:<20} {fmt(value) if value is not None else NO_DATA}")
    print()
    return 0


def show_alerts() -> int:
    try:
        groups = _get("/api/v1/rules", {"type": "alert"})["data"]["groups"]
    except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"Could not read rules from Prometheus: {exc}")
        return 1

    # Filter on type as well as group: the slo group holds recording rules,
    # and ?type=alert is a server-side filter this should not depend on.
    rules = [
        rule
        for group in groups
        if group["name"].startswith("visit-counter")
        for rule in group["rules"]
        if rule.get("type") == "alerting"
    ]

    if not rules:
        print("No visit-counter alert rules are loaded.")
        print("Has `make deploy` run since `make monitoring`? The PrometheusRule")
        print("needs the Prometheus operator to already be present.")
        return 1

    width = max(len(rule["name"]) for rule in rules)
    print(f"\n{len(rules)} alert rules loaded and evaluating\n")

    firing_total = 0
    for rule in sorted(rules, key=lambda r: r["name"]):
        name = rule["name"]
        severity = rule.get("labels", {}).get("severity", "-")
        firing = len(rule.get("alerts", []))
        firing_total += firing
        state = f"FIRING ({firing})" if firing else rule.get("state", "unknown")
        print(f"  {name:<{width}}  {severity:<9} {state}")

    print()
    if firing_total:
        print(f"{firing_total} alert(s) currently firing.")
    else:
        print("Nothing firing. 'inactive' means the rule is evaluating and healthy.")
    print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["slo", "alerts"])
    args = parser.parse_args()

    with PortForward():
        return show_slo() if args.command == "slo" else show_alerts()


if __name__ == "__main__":
    sys.exit(main())
