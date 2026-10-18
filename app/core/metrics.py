"""Prometheus metrics.

Nothing is labelled by page_id: it comes from the caller and is unbounded, so
it would let anyone create an unlimited number of series.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

VISITS_RECORDED = Counter(
    "visit_counter_visits_recorded_total",
    "Visits accepted into the in-memory write buffer.",
)

VISITS_FLUSHED = Counter(
    "visit_counter_visits_flushed_total",
    "Visits durably written to Redis.",
    ["shard"],
)

VISITS_LOST = Counter(
    "visit_counter_visits_lost_total",
    "Buffered visits discarded because they could not be flushed.",
)

READS_TOTAL = Counter(
    "visit_counter_reads_total",
    "Count reads served, by where the value came from.",
    ["source"],  # memory | redis
)

BUFFER_FLUSHES = Counter(
    "visit_counter_buffer_flushes_total",
    "Write-buffer flush operations.",
    ["trigger", "outcome"],  # trigger: interval|read|shutdown, outcome: success|error
)

BUFFER_DEPTH = Gauge(
    "visit_counter_buffer_depth",
    "Visits currently held in memory and not yet written to Redis. "
    "This is the amount of data at risk if the process dies right now.",
)

BUFFER_PAGES = Gauge(
    "visit_counter_buffer_pages",
    "Distinct pages with unflushed visits.",
)

CACHE_ENTRIES = Gauge(
    "visit_counter_cache_entries",
    "Entries currently held in the in-process read cache.",
)

REDIS_OP_LATENCY = Histogram(
    "visit_counter_redis_operation_seconds",
    "Latency of individual Redis operations.",
    ["operation", "shard"],
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

REDIS_OP_ERRORS = Counter(
    "visit_counter_redis_operation_errors_total",
    "Redis operations that raised.",
    ["operation", "shard"],
)

REDIS_SHARD_UP = Gauge(
    "visit_counter_redis_shard_up",
    "1 if the shard answered the most recent health check, 0 otherwise.",
    ["shard"],
)

BUILD_INFO = Gauge(
    "visit_counter_build_info",
    "Build identity of the running process. Always 1; read the labels.",
    ["version", "git_sha"],
)

HTTP_REQUESTS = Counter(
    "visit_counter_http_requests_total",
    "HTTP requests served.",
    ["method", "route", "status"],
)

HTTP_DURATION = Histogram(
    "visit_counter_http_request_duration_seconds",
    "End-to-end HTTP request latency.",
    ["method", "route"],
    # 0.2 is an explicit edge: the SLO query reads that bucket directly
    # rather than interpolating between two.
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.5, 1.0, 2.5, 5.0),
)
