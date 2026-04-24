"""
In-process metrics registry for Callisto — no external dependencies.

Designed to be Prometheus-wire-compatible (the exposition format in
``render_prometheus`` is the text format accepted by Prometheus scrapers,
``version=0.0.4``), but without pulling ``prometheus_client``. Callisto is
kept lean; one text-format renderer is cheaper than a C extension.

Usage
-----
    from tools.metrics import get_registry

    registry = get_registry()
    tasks_submitted = registry.counter(
        "callisto_tasks_submitted_total",
        "Total research tasks submitted to the AGP pipeline.",
        labelnames=("priority", "source"),
    )
    tasks_submitted.inc(labels={"priority": "1", "source": "api"})

    task_duration = registry.histogram(
        "callisto_task_duration_seconds",
        "Task wall-clock duration by terminal status.",
        labelnames=("status",),
        buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800),
    )
    task_duration.observe(42.1, labels={"status": "completed"})

    bankroll = registry.gauge(
        "callisto_bets_bankroll_gauge",
        "Current bettable bankroll in USD.",
    )
    bankroll.set(1234.56)

Thread safety
-------------
Each metric holds its own ``threading.Lock``; the registry also has one so
two threads can't race to create a metric of the same name. Expected call
rate is low (hundreds/sec at peak), so lock contention is negligible.

Singleton
---------
``get_registry()`` returns the same process-wide registry so every
module instruments into the same namespace. Tests should call
``reset_default_registry()`` between cases.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


_VALID_NAME_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:"
)


def _validate_name(name: str) -> None:
    if not name:
        raise ValueError("metric name must be non-empty")
    if name[0].isdigit():
        raise ValueError(f"metric name cannot start with a digit: {name}")
    bad = [c for c in name if c not in _VALID_NAME_CHARS]
    if bad:
        raise ValueError(f"invalid metric name {name!r}: bad chars {bad}")


def _escape_label_value(v: str) -> str:
    return v.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n")


def _render_labels(
    labelnames: Sequence[str], values: Tuple[str, ...]
) -> str:
    if not labelnames:
        return ""
    pairs = [
        f'{n}="{_escape_label_value(str(v))}"'
        for n, v in zip(labelnames, values)
    ]
    return "{" + ",".join(pairs) + "}"


def _labels_to_tuple(
    labelnames: Sequence[str], labels: Optional[Dict[str, str]]
) -> Tuple[str, ...]:
    if not labelnames:
        if labels:
            raise ValueError(
                f"metric has no label names but labels={labels!r} supplied"
            )
        return ()
    if not labels:
        raise ValueError(
            f"metric requires labels {list(labelnames)} but none supplied"
        )
    missing = [n for n in labelnames if n not in labels]
    if missing:
        raise ValueError(
            f"missing label values {missing} for labels {list(labelnames)}"
        )
    extra = [k for k in labels if k not in labelnames]
    if extra:
        raise ValueError(
            f"unknown labels {extra} — registered label names {list(labelnames)}"
        )
    return tuple(str(labels[n]) for n in labelnames)


class _MetricBase:
    """Shared state + rendering scaffolding for a single metric family."""

    kind: str = "untyped"

    def __init__(
        self,
        name: str,
        description: str,
        labelnames: Sequence[str] = (),
    ) -> None:
        _validate_name(name)
        for ln in labelnames:
            _validate_name(ln)
        self.name = name
        self.description = description.replace("\n", " ").strip()
        self.labelnames: Tuple[str, ...] = tuple(labelnames)
        self._lock = threading.Lock()

    def _header_lines(self) -> List[str]:
        return [
            f"# HELP {self.name} {self.description}",
            f"# TYPE {self.name} {self.kind}",
        ]


class Counter(_MetricBase):
    """Monotonically-increasing counter.

    ``inc(amount)`` bumps the counter by ``amount`` (default 1). Negative
    values raise ``ValueError`` — counters never go backwards.
    """

    kind = "counter"

    def __init__(
        self,
        name: str,
        description: str,
        labelnames: Sequence[str] = (),
    ) -> None:
        super().__init__(name, description, labelnames)
        self._values: Dict[Tuple[str, ...], float] = {}

    def inc(
        self,
        amount: float = 1.0,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        if amount < 0:
            raise ValueError(f"Counter.inc amount must be >= 0, got {amount}")
        key = _labels_to_tuple(self.labelnames, labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + float(amount)

    def value(self, labels: Optional[Dict[str, str]] = None) -> float:
        key = _labels_to_tuple(self.labelnames, labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def reset(self) -> None:
        with self._lock:
            self._values.clear()

    def render(self) -> List[str]:
        lines = self._header_lines()
        with self._lock:
            if not self._values:
                # No observations yet — emit a single zero without label
                # set. Prometheus treats this as the unlabeled series,
                # which is fine for "count has never incremented" sentinels
                # and matches the client_python behavior for labeled-but-
                # empty families.
                if not self.labelnames:
                    lines.append(f"{self.name} 0")
                # For labeled families with no samples, omit the line —
                # scrapers will simply show the family HELP/TYPE headers
                # and zero recorded observations. Avoids a bogus
                # `name{} 0` series.
            else:
                for key in sorted(self._values.keys()):
                    val = self._values[key]
                    lines.append(
                        f"{self.name}{_render_labels(self.labelnames, key)} "
                        f"{_format_float(val)}"
                    )
        return lines

    def snapshot_json(self) -> dict:
        with self._lock:
            data = [
                {"labels": dict(zip(self.labelnames, key)), "value": val}
                for key, val in sorted(self._values.items())
            ]
        return {
            "name": self.name,
            "type": self.kind,
            "description": self.description,
            "samples": data,
        }


class Gauge(_MetricBase):
    """Point-in-time value. Can go up or down."""

    kind = "gauge"

    def __init__(
        self,
        name: str,
        description: str,
        labelnames: Sequence[str] = (),
    ) -> None:
        super().__init__(name, description, labelnames)
        self._values: Dict[Tuple[str, ...], float] = {}

    def set(
        self,
        value: float,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        key = _labels_to_tuple(self.labelnames, labels)
        with self._lock:
            self._values[key] = float(value)

    def inc(
        self,
        amount: float = 1.0,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        key = _labels_to_tuple(self.labelnames, labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + float(amount)

    def dec(
        self,
        amount: float = 1.0,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        self.inc(-amount, labels=labels)

    def value(self, labels: Optional[Dict[str, str]] = None) -> float:
        key = _labels_to_tuple(self.labelnames, labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def reset(self) -> None:
        with self._lock:
            self._values.clear()

    def render(self) -> List[str]:
        lines = self._header_lines()
        with self._lock:
            if not self._values:
                if not self.labelnames:
                    lines.append(f"{self.name} 0")
                # Labeled gauge with no samples — emit headers only.
            else:
                for key in sorted(self._values.keys()):
                    val = self._values[key]
                    lines.append(
                        f"{self.name}{_render_labels(self.labelnames, key)} "
                        f"{_format_float(val)}"
                    )
        return lines

    def snapshot_json(self) -> dict:
        with self._lock:
            data = [
                {"labels": dict(zip(self.labelnames, key)), "value": val}
                for key, val in sorted(self._values.items())
            ]
        return {
            "name": self.name,
            "type": self.kind,
            "description": self.description,
            "samples": data,
        }


# Sensible default bucket set (seconds — good for task durations / HTTP latencies).
DEFAULT_HISTOGRAM_BUCKETS: Tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0,
    2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0,
)


@dataclass
class _HistogramSeries:
    bucket_counts: List[int]
    sum_val: float
    count: int


class Histogram(_MetricBase):
    """Cumulative histogram with fixed buckets.

    Renders Prometheus histogram wire format: one ``_bucket`` series per
    bucket (cumulative ``le=`` values) plus ``_sum`` and ``_count``.

    Buckets must be sorted; ``+Inf`` is appended automatically.
    """

    kind = "histogram"

    def __init__(
        self,
        name: str,
        description: str,
        labelnames: Sequence[str] = (),
        buckets: Sequence[float] = DEFAULT_HISTOGRAM_BUCKETS,
    ) -> None:
        super().__init__(name, description, labelnames)
        if "le" in labelnames:
            raise ValueError(
                "'le' is reserved for histogram buckets — drop it from labelnames"
            )
        cleaned: List[float] = []
        for b in buckets:
            bf = float(b)
            if math.isinf(bf) or math.isnan(bf):
                continue
            cleaned.append(bf)
        cleaned.sort()
        if not cleaned:
            raise ValueError("histogram needs at least one finite bucket")
        self._buckets: Tuple[float, ...] = tuple(cleaned)
        self._series: Dict[Tuple[str, ...], _HistogramSeries] = {}

    @property
    def buckets(self) -> Tuple[float, ...]:
        return self._buckets

    def _get_series(self, key: Tuple[str, ...]) -> _HistogramSeries:
        s = self._series.get(key)
        if s is None:
            s = _HistogramSeries(
                bucket_counts=[0] * len(self._buckets),
                sum_val=0.0,
                count=0,
            )
            self._series[key] = s
        return s

    def observe(
        self,
        value: float,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        v = float(value)
        key = _labels_to_tuple(self.labelnames, labels)
        with self._lock:
            s = self._get_series(key)
            s.sum_val += v
            s.count += 1
            # Store non-cumulative counts per bucket; ``render`` computes the
            # cumulative totals required by the Prometheus wire format. If the
            # observation is larger than every finite upper bound, it only
            # lands in the synthetic ``+Inf`` bucket (handled via ``count``).
            for i, upper in enumerate(self._buckets):
                if v <= upper:
                    s.bucket_counts[i] += 1
                    break

    def reset(self) -> None:
        with self._lock:
            self._series.clear()

    def count(self, labels: Optional[Dict[str, str]] = None) -> int:
        key = _labels_to_tuple(self.labelnames, labels)
        with self._lock:
            s = self._series.get(key)
            return s.count if s else 0

    def sum(self, labels: Optional[Dict[str, str]] = None) -> float:
        key = _labels_to_tuple(self.labelnames, labels)
        with self._lock:
            s = self._series.get(key)
            return s.sum_val if s else 0.0

    def render(self) -> List[str]:
        lines = self._header_lines()
        with self._lock:
            if not self._series:
                # Unlabeled histogram: emit a zero baseline so scrapers
                # always see the series even before the first observation.
                # Labeled histograms with no samples get headers only.
                if self.labelnames:
                    return lines
                for upper in self._buckets:
                    lines.append(
                        f'{self.name}_bucket{{le="{_format_bucket(upper)}"}} 0'
                    )
                lines.append(f'{self.name}_bucket{{le="+Inf"}} 0')
                lines.append(f"{self.name}_sum 0")
                lines.append(f"{self.name}_count 0")
                return lines
            for key in sorted(self._series.keys()):
                s = self._series[key]
                cumulative = 0
                for i, upper in enumerate(self._buckets):
                    cumulative += s.bucket_counts[i]
                    le_labels = _render_histogram_labels(
                        self.labelnames, key, le=_format_bucket(upper),
                    )
                    lines.append(
                        f"{self.name}_bucket{le_labels} {cumulative}"
                    )
                le_labels = _render_histogram_labels(
                    self.labelnames, key, le="+Inf"
                )
                lines.append(f"{self.name}_bucket{le_labels} {s.count}")
                lines.append(
                    f"{self.name}_sum{_render_labels(self.labelnames, key)} "
                    f"{_format_float(s.sum_val)}"
                )
                lines.append(
                    f"{self.name}_count{_render_labels(self.labelnames, key)} "
                    f"{s.count}"
                )
        return lines

    def snapshot_json(self) -> dict:
        with self._lock:
            samples = []
            for key, s in sorted(self._series.items()):
                bucket_view = []
                cumulative = 0
                for i, upper in enumerate(self._buckets):
                    cumulative += s.bucket_counts[i]
                    bucket_view.append(
                        {"le": _format_bucket(upper), "count": cumulative}
                    )
                bucket_view.append({"le": "+Inf", "count": s.count})
                samples.append(
                    {
                        "labels": dict(zip(self.labelnames, key)),
                        "buckets": bucket_view,
                        "sum": s.sum_val,
                        "count": s.count,
                    }
                )
        return {
            "name": self.name,
            "type": self.kind,
            "description": self.description,
            "bucket_bounds": [_format_bucket(b) for b in self._buckets] + ["+Inf"],
            "samples": samples,
        }


def _format_float(v: float) -> str:
    if math.isnan(v):
        return "NaN"
    if v == math.inf:
        return "+Inf"
    if v == -math.inf:
        return "-Inf"
    if v == int(v) and abs(v) < 1e16:
        return str(int(v))
    return repr(v)


def _format_bucket(v: float) -> str:
    if v == int(v) and abs(v) < 1e16:
        return str(int(v))
    return repr(v)


def _render_histogram_labels(
    labelnames: Sequence[str], values: Tuple[str, ...], le: str,
) -> str:
    pairs = [
        f'{n}="{_escape_label_value(str(v))}"'
        for n, v in zip(labelnames, values)
    ]
    pairs.append(f'le="{_escape_label_value(le)}"')
    return "{" + ",".join(pairs) + "}"


class MetricsRegistry:
    """Holds all metric families and renders them."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._metrics: Dict[str, _MetricBase] = {}
        self._process_start = time.time()

    def counter(
        self,
        name: str,
        description: str,
        labelnames: Sequence[str] = (),
    ) -> Counter:
        return self._register(Counter(name, description, labelnames))

    def gauge(
        self,
        name: str,
        description: str,
        labelnames: Sequence[str] = (),
    ) -> Gauge:
        return self._register(Gauge(name, description, labelnames))

    def histogram(
        self,
        name: str,
        description: str,
        labelnames: Sequence[str] = (),
        buckets: Sequence[float] = DEFAULT_HISTOGRAM_BUCKETS,
    ) -> Histogram:
        return self._register(Histogram(name, description, labelnames, buckets))

    def _register(self, metric: _MetricBase) -> _MetricBase:
        with self._lock:
            existing = self._metrics.get(metric.name)
            if existing is not None:
                if type(existing) is not type(metric):
                    raise ValueError(
                        f"metric {metric.name!r} already registered as "
                        f"{type(existing).__name__}, got {type(metric).__name__}"
                    )
                if existing.labelnames != metric.labelnames:
                    raise ValueError(
                        f"metric {metric.name!r} label names mismatch: "
                        f"registered={existing.labelnames}, new={metric.labelnames}"
                    )
                return existing
            self._metrics[metric.name] = metric
            return metric

    def get(self, name: str) -> Optional[_MetricBase]:
        with self._lock:
            return self._metrics.get(name)

    def names(self) -> List[str]:
        with self._lock:
            return sorted(self._metrics.keys())

    def count(self) -> int:
        with self._lock:
            return len(self._metrics)

    def render_prometheus(self) -> str:
        """Emit Prometheus text exposition format, version 0.0.4.

        Deterministic: metrics sorted by name, series within each sorted
        by label tuple. Output ends with a trailing newline as required.
        """
        with self._lock:
            ordered = sorted(self._metrics.values(), key=lambda m: m.name)
        out: List[str] = []
        for m in ordered:
            out.extend(m.render())
        out.append("")
        return "\n".join(out)

    def render_json(self) -> dict:
        with self._lock:
            ordered = sorted(self._metrics.values(), key=lambda m: m.name)
        return {
            "process_start_epoch": self._process_start,
            "uptime_seconds": round(time.time() - self._process_start, 3),
            "metric_count": len(ordered),
            "metrics": [m.snapshot_json() for m in ordered],
        }

    def reset_values(self) -> None:
        """Zero every metric's state (used by tests)."""
        with self._lock:
            for m in self._metrics.values():
                m.reset()

    def clear(self) -> None:
        """Drop every registered metric (used by tests)."""
        with self._lock:
            self._metrics.clear()


_default_registry_lock = threading.Lock()
_default_registry: Optional[MetricsRegistry] = None


def get_registry() -> MetricsRegistry:
    global _default_registry
    with _default_registry_lock:
        if _default_registry is None:
            _default_registry = MetricsRegistry()
            _register_core_metrics(_default_registry)
        return _default_registry


def reset_default_registry() -> None:
    """Tests: wipe the process-wide registry and re-declare the core set."""
    global _default_registry
    with _default_registry_lock:
        _default_registry = MetricsRegistry()
        _register_core_metrics(_default_registry)


# ── Core metric declarations — one place, so every module instruments the
# same families. Other modules call ``get_registry().counter(...)`` which
# returns the pre-declared instance. ───────────────────────────────────────

_TASK_DURATION_BUCKETS = (
    0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0, 3600.0,
)

_SCRAPER_LATENCY_BUCKETS = (
    0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 45.0, 90.0,
)


def _register_core_metrics(reg: MetricsRegistry) -> None:
    reg.counter(
        "callisto_tasks_submitted_total",
        "Research tasks submitted to the AGP pipeline.",
        labelnames=("priority", "source"),
    )
    reg.counter(
        "callisto_tasks_completed_total",
        "Tasks reaching a terminal state (completed/failed/timeout/short_circuit).",
        labelnames=("status",),
    )
    reg.histogram(
        "callisto_task_duration_seconds",
        "Task wall-clock duration from pickup to terminal state.",
        labelnames=("status",),
        buckets=_TASK_DURATION_BUCKETS,
    )

    reg.counter(
        "callisto_hypotheses_created_total",
        "Hypotheses persisted to the hypotheses table.",
        labelnames=("sport",),
    )
    reg.counter(
        "callisto_hypotheses_promoted_total",
        "Hypotheses advanced to a later lifecycle stage.",
        labelnames=("from_status", "to_status"),
    )
    reg.counter(
        "callisto_hypotheses_rejected_total",
        "Hypotheses rejected by auto-promote, admin action, or cascade demotion.",
        labelnames=("reason",),
    )

    reg.counter(
        "callisto_edges_detected_total",
        "Edges identified by any scanner (cross-book, vig, pace, alt-line).",
        labelnames=("sport", "market"),
    )

    reg.counter(
        "callisto_bets_placed_total",
        "Bets recorded in the bets table (successful placements).",
        labelnames=("sport", "market", "book"),
    )
    reg.gauge(
        "callisto_bets_bankroll_gauge",
        "Current bankroll tracked by the bet executor (USD).",
    )

    reg.counter(
        "callisto_ingestion_success_total",
        "Ingestion runs finishing in status ok/partial.",
        labelnames=("source",),
    )
    reg.counter(
        "callisto_ingestion_failure_total",
        "Ingestion runs finishing in status failed/rate_limited.",
        labelnames=("source", "reason"),
    )

    reg.counter(
        "callisto_claude_calls_total",
        "Claude CLI subprocess invocations.",
        labelnames=("status",),
    )
    reg.counter(
        "callisto_claude_rate_limit_total",
        "Claude Code calls that hit a rate limit (cooldown entered).",
    )

    reg.gauge(
        "callisto_db_connection_gauge",
        "Open aiosqlite connections currently tracked by the registry.",
    )
    reg.counter(
        "callisto_db_lock_hits_total",
        "Write operations that retried after 'database is locked'.",
        labelnames=("operation",),
    )

    reg.histogram(
        "callisto_scraper_latency_seconds",
        "Wall-clock duration for scraper/ingestion functions.",
        labelnames=("scraper",),
        buckets=_SCRAPER_LATENCY_BUCKETS,
    )


# ── Convenience helpers for callers — keep the happy path a one-liner.

def observe_task_duration(status: str, duration_seconds: float) -> None:
    reg = get_registry()
    reg.get("callisto_task_duration_seconds").observe(  # type: ignore[union-attr]
        duration_seconds, labels={"status": status}
    )
    reg.get("callisto_tasks_completed_total").inc(  # type: ignore[union-attr]
        labels={"status": status}
    )


def observe_scraper_latency(scraper: str, duration_seconds: float) -> None:
    reg = get_registry()
    reg.get("callisto_scraper_latency_seconds").observe(  # type: ignore[union-attr]
        duration_seconds, labels={"scraper": scraper}
    )


def record_ingestion_result(
    source: str, status: str, reason: Optional[str] = None
) -> None:
    """Route an ingestion terminal-status tag into the success/failure counters."""
    reg = get_registry()
    if status in ("ok", "partial"):
        reg.get("callisto_ingestion_success_total").inc(  # type: ignore[union-attr]
            labels={"source": source}
        )
    else:
        reg.get("callisto_ingestion_failure_total").inc(  # type: ignore[union-attr]
            labels={"source": source, "reason": reason or status}
        )


def record_task_submission(priority: int, source: str = "api") -> None:
    reg = get_registry()
    reg.get("callisto_tasks_submitted_total").inc(  # type: ignore[union-attr]
        labels={"priority": str(priority), "source": source}
    )


def record_hypothesis_created(sport: str) -> None:
    reg = get_registry()
    reg.get("callisto_hypotheses_created_total").inc(  # type: ignore[union-attr]
        labels={"sport": sport or "unknown"}
    )


def record_hypothesis_promoted(from_status: str, to_status: str) -> None:
    reg = get_registry()
    reg.get("callisto_hypotheses_promoted_total").inc(  # type: ignore[union-attr]
        labels={"from_status": from_status, "to_status": to_status}
    )


def record_hypothesis_rejected(reason: str) -> None:
    reg = get_registry()
    reg.get("callisto_hypotheses_rejected_total").inc(  # type: ignore[union-attr]
        labels={"reason": reason or "unknown"}
    )


def record_edge_detected(sport: str, market: str, count: int = 1) -> None:
    if count <= 0:
        return
    reg = get_registry()
    reg.get("callisto_edges_detected_total").inc(  # type: ignore[union-attr]
        amount=count,
        labels={"sport": sport or "unknown", "market": market or "unknown"},
    )


def record_bet_placed(sport: str, market: str, book: str) -> None:
    reg = get_registry()
    reg.get("callisto_bets_placed_total").inc(  # type: ignore[union-attr]
        labels={
            "sport": sport or "unknown",
            "market": market or "unknown",
            "book": book or "unknown",
        }
    )


def set_bankroll(amount: float) -> None:
    reg = get_registry()
    reg.get("callisto_bets_bankroll_gauge").set(float(amount))  # type: ignore[union-attr]


def record_claude_call(status: str) -> None:
    """``status`` in {ok, error, rate_limited, blocked, timeout, cli_missing}."""
    reg = get_registry()
    reg.get("callisto_claude_calls_total").inc(  # type: ignore[union-attr]
        labels={"status": status or "unknown"}
    )
    if status == "rate_limited":
        reg.get("callisto_claude_rate_limit_total").inc()  # type: ignore[union-attr]


def record_db_lock_hit(operation: str) -> None:
    reg = get_registry()
    reg.get("callisto_db_lock_hits_total").inc(  # type: ignore[union-attr]
        labels={"operation": operation or "unknown"}
    )


def set_db_connection_count(n: int) -> None:
    reg = get_registry()
    reg.get("callisto_db_connection_gauge").set(float(n))  # type: ignore[union-attr]


__all__ = [
    "Counter",
    "Gauge",
    "Histogram",
    "MetricsRegistry",
    "DEFAULT_HISTOGRAM_BUCKETS",
    "get_registry",
    "reset_default_registry",
    "observe_task_duration",
    "observe_scraper_latency",
    "record_ingestion_result",
    "record_task_submission",
    "record_hypothesis_created",
    "record_hypothesis_promoted",
    "record_hypothesis_rejected",
    "record_edge_detected",
    "record_bet_placed",
    "set_bankroll",
    "record_claude_call",
    "record_db_lock_hit",
    "set_db_connection_count",
]


def iter_metrics() -> Iterable[_MetricBase]:
    """Iterate all registered metrics (used by some tests)."""
    reg = get_registry()
    with reg._lock:  # noqa: SLF001
        return list(reg._metrics.values())  # noqa: SLF001
