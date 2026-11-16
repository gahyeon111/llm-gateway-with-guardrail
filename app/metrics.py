"""Prometheus metrics. Metadata only — never bodies."""

from prometheus_client import Counter, Histogram

REQUESTS = Counter("gateway_requests_total", "Gateway requests", ["path", "method", "status"])
UPSTREAM_REQUESTS = Counter("gateway_upstream_requests_total", "Upstream requests", ["mode", "model", "status"])
AIM_CHECKS = Counter("aim_checks_total", "AIM checks (per chunk)", ["direction", "verdict"])
AIM_LATENCY = Histogram(
    "aim_check_seconds",
    "AIM check latency",
    ["direction"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
AIM_ERRORS = Counter("aim_errors_total", "AIM call failures (after retry)", ["direction"])
AIM_FAIL_OPEN = Counter("aim_fail_open_total", "AIM failures treated as PASS (fail-open)", ["direction"])
GUARD_VERDICTS = Counter(
    "guard_verdicts_total", "Final guard verdicts per checked field", ["endpoint", "stage", "verdict"]
)
CACHE_LOOKUPS = Counter("guard_cache_lookups_total", "Verdict cache lookups", ["result"])
