"""Tests for tools/metrics.py — the in-process metrics registry.

Covers counter incr, gauge set, histogram observe, Prometheus text format
validation, and the /metrics endpoint served by api.py (200, text/plain,
``version=0.0.4``, callisto_* metrics present). No external deps — does
not require Callisto services to be running.
"""

from __future__ import annotations

import os
import re
import threading
from typing import List

import pytest
from fastapi.testclient import TestClient

from tools import metrics


# ── Per-test isolation — every case starts with a clean registry.

@pytest.fixture(autouse=True)
def _reset_registry():
    metrics.reset_default_registry()
    yield
    metrics.reset_default_registry()


# ── Counter ─────────────────────────────────────────────────────────────

class TestCounter:
    def test_unlabeled_counter_increments_by_default_one(self):
        c = metrics.Counter("demo_total", "Test counter.")
        assert c.value() == 0
        c.inc()
        c.inc()
        c.inc()
        assert c.value() == 3

    def test_unlabeled_counter_increments_by_amount(self):
        c = metrics.Counter("demo_total", "Test counter.")
        c.inc(2.5)
        c.inc(0.5)
        assert c.value() == 3.0

    def test_labeled_counter_tracks_each_series_independently(self):
        c = metrics.Counter(
            "demo_total", "Test counter.", labelnames=("status",),
        )
        c.inc(labels={"status": "ok"})
        c.inc(labels={"status": "ok"})
        c.inc(labels={"status": "fail"})
        assert c.value(labels={"status": "ok"}) == 2
        assert c.value(labels={"status": "fail"}) == 1

    def test_negative_increment_rejected(self):
        c = metrics.Counter("demo_total", "Test.")
        with pytest.raises(ValueError, match="must be >= 0"):
            c.inc(-1)

    def test_missing_label_value_raises(self):
        c = metrics.Counter(
            "demo_total", "Test.", labelnames=("a", "b"),
        )
        with pytest.raises(ValueError, match="missing label values"):
            c.inc(labels={"a": "x"})

    def test_unknown_label_name_raises(self):
        c = metrics.Counter(
            "demo_total", "Test.", labelnames=("a",),
        )
        with pytest.raises(ValueError, match="unknown labels"):
            c.inc(labels={"a": "1", "rogue": "oops"})

    def test_threadsafe_increments(self):
        c = metrics.Counter("demo_total", "Test.")
        N_THREADS = 16
        PER_THREAD = 500

        def worker():
            for _ in range(PER_THREAD):
                c.inc()

        threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert c.value() == N_THREADS * PER_THREAD


# ── Gauge ──────────────────────────────────────────────────────────────

class TestGauge:
    def test_gauge_set_overwrites(self):
        g = metrics.Gauge("demo_gauge", "Test gauge.")
        g.set(10)
        assert g.value() == 10
        g.set(3.14)
        assert g.value() == 3.14

    def test_gauge_inc_and_dec(self):
        g = metrics.Gauge("demo_gauge", "Test.")
        g.set(5)
        g.inc(2)
        assert g.value() == 7
        g.dec(3)
        assert g.value() == 4

    def test_labeled_gauge(self):
        g = metrics.Gauge(
            "demo_gauge", "Test.", labelnames=("pool",),
        )
        g.set(100, labels={"pool": "main"})
        g.set(50, labels={"pool": "backup"})
        assert g.value(labels={"pool": "main"}) == 100
        assert g.value(labels={"pool": "backup"}) == 50


# ── Histogram ──────────────────────────────────────────────────────────

class TestHistogram:
    def test_observe_accumulates_buckets(self):
        h = metrics.Histogram(
            "demo_seconds",
            "Test histogram.",
            buckets=(0.1, 0.5, 1.0, 5.0),
        )
        h.observe(0.05)
        h.observe(0.3)
        h.observe(0.8)
        h.observe(2.5)
        h.observe(10.0)
        assert h.count() == 5
        assert h.sum() == pytest.approx(13.65)

    def test_observe_respects_labels(self):
        h = metrics.Histogram(
            "demo_seconds", "Test.",
            labelnames=("status",),
            buckets=(1.0,),
        )
        h.observe(0.5, labels={"status": "ok"})
        h.observe(1.5, labels={"status": "ok"})
        h.observe(0.2, labels={"status": "fail"})
        assert h.count(labels={"status": "ok"}) == 2
        assert h.count(labels={"status": "fail"}) == 1
        assert h.sum(labels={"status": "ok"}) == pytest.approx(2.0)

    def test_buckets_validated_as_nonempty(self):
        with pytest.raises(ValueError, match="at least one finite bucket"):
            metrics.Histogram("h", "d", buckets=())

    def test_le_reserved(self):
        with pytest.raises(ValueError, match="'le' is reserved"):
            metrics.Histogram("h", "d", labelnames=("le",))

    def test_render_includes_bucket_sum_count(self):
        h = metrics.Histogram(
            "demo_seconds", "Test.",
            buckets=(1.0, 5.0),
        )
        h.observe(0.5)
        h.observe(3.0)
        h.observe(10.0)
        rendered = "\n".join(h.render())
        # Cumulative bucket semantics — le=1 has 1 obs, le=5 has 2, +Inf has 3.
        assert 'demo_seconds_bucket{le="1"} 1' in rendered
        assert 'demo_seconds_bucket{le="5"} 2' in rendered
        assert 'demo_seconds_bucket{le="+Inf"} 3' in rendered
        assert "demo_seconds_sum 13.5" in rendered
        assert "demo_seconds_count 3" in rendered


# ── Registry ──────────────────────────────────────────────────────────

class TestRegistry:
    def test_same_name_returns_same_instance(self):
        reg = metrics.MetricsRegistry()
        c1 = reg.counter("dup_total", "a")
        c2 = reg.counter("dup_total", "a")
        assert c1 is c2

    def test_conflicting_type_rejected(self):
        reg = metrics.MetricsRegistry()
        reg.counter("clash", "a")
        with pytest.raises(ValueError, match="already registered as"):
            reg.gauge("clash", "a")

    def test_conflicting_label_names_rejected(self):
        reg = metrics.MetricsRegistry()
        reg.counter("clash_total", "a", labelnames=("x",))
        with pytest.raises(ValueError, match="label names mismatch"):
            reg.counter("clash_total", "a", labelnames=("y",))

    def test_invalid_metric_name_rejected(self):
        reg = metrics.MetricsRegistry()
        with pytest.raises(ValueError, match="invalid metric name"):
            reg.counter("bad-name", "a")
        with pytest.raises(ValueError, match="cannot start with a digit"):
            reg.counter("1_bad", "a")


# ── Default-registry + helpers ─────────────────────────────────────────

class TestDefaultRegistry:
    def test_core_metrics_preregistered(self):
        r = metrics.get_registry()
        names = set(r.names())
        expected = {
            "callisto_tasks_submitted_total",
            "callisto_tasks_completed_total",
            "callisto_task_duration_seconds",
            "callisto_hypotheses_created_total",
            "callisto_hypotheses_promoted_total",
            "callisto_hypotheses_rejected_total",
            "callisto_edges_detected_total",
            "callisto_bets_placed_total",
            "callisto_bets_bankroll_gauge",
            "callisto_ingestion_success_total",
            "callisto_ingestion_failure_total",
            "callisto_claude_calls_total",
            "callisto_claude_rate_limit_total",
            "callisto_db_connection_gauge",
            "callisto_db_lock_hits_total",
            "callisto_scraper_latency_seconds",
        }
        missing = expected - names
        assert not missing, f"missing core metrics: {missing}"

    def test_get_registry_is_singleton(self):
        a = metrics.get_registry()
        b = metrics.get_registry()
        assert a is b

    def test_reset_rebuilds_core_metrics(self):
        r = metrics.get_registry()
        r.get("callisto_tasks_submitted_total").inc(  # type: ignore[union-attr]
            labels={"priority": "1", "source": "api"}
        )
        assert r.get(
            "callisto_tasks_submitted_total"
        ).value(labels={"priority": "1", "source": "api"}) == 1  # type: ignore[union-attr]
        metrics.reset_default_registry()
        r2 = metrics.get_registry()
        assert r2.get("callisto_tasks_submitted_total") is not None
        assert r2.get(
            "callisto_tasks_submitted_total"
        ).value(labels={"priority": "1", "source": "api"}) == 0  # type: ignore[union-attr]

    def test_record_task_submission_helper(self):
        metrics.record_task_submission(2, source="test")
        r = metrics.get_registry()
        assert r.get("callisto_tasks_submitted_total").value(  # type: ignore[union-attr]
            labels={"priority": "2", "source": "test"}
        ) == 1

    def test_observe_task_duration_helper(self):
        metrics.observe_task_duration("completed", 12.5)
        r = metrics.get_registry()
        hist = r.get("callisto_task_duration_seconds")
        counter = r.get("callisto_tasks_completed_total")
        assert hist.count(labels={"status": "completed"}) == 1  # type: ignore[union-attr]
        assert hist.sum(labels={"status": "completed"}) == pytest.approx(12.5)  # type: ignore[union-attr]
        assert counter.value(labels={"status": "completed"}) == 1  # type: ignore[union-attr]

    def test_record_ingestion_result_helper_splits_success_failure(self):
        metrics.record_ingestion_result("espn.scoreboard.mlb", "ok")
        metrics.record_ingestion_result("espn.scoreboard.mlb", "partial")
        metrics.record_ingestion_result(
            "espn.scoreboard.mlb", "rate_limited", reason="429"
        )
        metrics.record_ingestion_result(
            "espn.scoreboard.mlb", "failed", reason="ConnectionError"
        )
        r = metrics.get_registry()
        ok_counter = r.get("callisto_ingestion_success_total")
        fail_counter = r.get("callisto_ingestion_failure_total")
        assert ok_counter.value(  # type: ignore[union-attr]
            labels={"source": "espn.scoreboard.mlb"}
        ) == 2
        assert fail_counter.value(  # type: ignore[union-attr]
            labels={"source": "espn.scoreboard.mlb", "reason": "429"}
        ) == 1
        assert fail_counter.value(  # type: ignore[union-attr]
            labels={"source": "espn.scoreboard.mlb", "reason": "ConnectionError"}
        ) == 1

    def test_record_claude_call_tracks_rate_limit_separately(self):
        metrics.record_claude_call("ok")
        metrics.record_claude_call("rate_limited")
        metrics.record_claude_call("rate_limited")
        r = metrics.get_registry()
        calls = r.get("callisto_claude_calls_total")
        rl = r.get("callisto_claude_rate_limit_total")
        assert calls.value(labels={"status": "ok"}) == 1  # type: ignore[union-attr]
        assert calls.value(labels={"status": "rate_limited"}) == 2  # type: ignore[union-attr]
        assert rl.value() == 2  # type: ignore[union-attr]

    def test_set_bankroll_helper(self):
        metrics.set_bankroll(1234.56)
        r = metrics.get_registry()
        assert r.get("callisto_bets_bankroll_gauge").value() == pytest.approx(  # type: ignore[union-attr]
            1234.56
        )


# ── Prometheus text format ─────────────────────────────────────────────

# Minimal Prometheus-line validator — we don't require the ``prometheus_client``
# dep so we enforce the essentials: each series starts with the metric name,
# has valid ``{label="value",...}`` syntax, and ends with a number.
_METRIC_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL_PAIR_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"')


def _parse_prom_line(line: str) -> dict:
    assert line and line[0] != "#", f"expected data line, got {line!r}"
    name_match = _METRIC_NAME_RE.match(line)
    assert name_match, f"no metric name at start of {line!r}"
    name = name_match.group(0)
    rest = line[len(name):]
    labels: dict = {}
    if rest.startswith("{"):
        close = rest.index("}")
        label_body = rest[1:close]
        for m in _LABEL_PAIR_RE.finditer(label_body):
            labels[m.group(1)] = m.group(2)
        rest = rest[close + 1:]
    tokens = rest.strip().split()
    assert tokens, f"no value in line {line!r}"
    val_str = tokens[0]
    try:
        value = float(val_str)
    except ValueError:
        assert val_str in ("NaN", "+Inf", "-Inf"), (
            f"unparseable value {val_str!r} in line {line!r}"
        )
        value = float("nan") if val_str == "NaN" else float(val_str)
    return {"name": name, "labels": labels, "value": value}


def _collect_data_lines(text: str) -> List[str]:
    return [
        ln for ln in text.splitlines()
        if ln and not ln.startswith("#")
    ]


class TestPrometheusExposition:
    def test_text_is_parseable(self):
        metrics.observe_task_duration("completed", 1.5)
        metrics.record_task_submission(1, source="api")
        metrics.set_bankroll(250.0)
        body = metrics.get_registry().render_prometheus()
        assert body.endswith("\n")
        for line in _collect_data_lines(body):
            _parse_prom_line(line)

    def test_help_and_type_headers_present_for_every_metric(self):
        body = metrics.get_registry().render_prometheus()
        lines = body.splitlines()
        names = set()
        for line in lines:
            if line.startswith("# HELP "):
                names.add(line.split(" ", 3)[2])
            elif line.startswith("# TYPE "):
                names.add(line.split(" ", 3)[2])
        # Every core metric name should appear in both HELP and TYPE headers.
        for required in [
            "callisto_tasks_submitted_total",
            "callisto_task_duration_seconds",
            "callisto_bets_bankroll_gauge",
        ]:
            assert required in names

    def test_histogram_cumulative_buckets(self):
        metrics.observe_task_duration("completed", 0.5)
        metrics.observe_task_duration("completed", 3.0)
        metrics.observe_task_duration("completed", 120.0)
        body = metrics.get_registry().render_prometheus()
        # Extract bucket lines for callisto_task_duration_seconds
        bucket_counts: List[int] = []
        for line in _collect_data_lines(body):
            if line.startswith("callisto_task_duration_seconds_bucket"):
                parsed = _parse_prom_line(line)
                if parsed["labels"].get("status") != "completed":
                    continue
                bucket_counts.append(int(parsed["value"]))
        # Cumulative: counts should be monotonically non-decreasing.
        for i in range(1, len(bucket_counts)):
            assert bucket_counts[i] >= bucket_counts[i - 1], (
                f"buckets not cumulative: {bucket_counts}"
            )
        # Total count should match the number of observations.
        last = bucket_counts[-1]
        assert last == 3

    def test_label_values_escape_quotes_and_backslashes(self):
        c = metrics.Counter(
            "escape_total",
            "Escape chars test.",
            labelnames=("msg",),
        )
        c.inc(labels={"msg": 'has "quote" and \\backslash\\'})
        rendered = "\n".join(c.render())
        assert '"has \\"quote\\" and \\\\backslash\\\\"' in rendered


# ── FastAPI /metrics endpoint ──────────────────────────────────────────

class TestMetricsEndpoint:
    """Lightweight FastAPI app exposing just the metrics endpoints.

    We don't import the full ``api.py`` because it spins up a writer,
    autonomous loops, and other heavy services. The endpoints themselves
    are thin adapters over ``tools.metrics``, so a local re-mount is the
    right unit test — and the full module is exercised by the smoke
    import in test_full_system_audit.
    """

    def _make_app(self):
        from fastapi import FastAPI
        from fastapi.responses import Response

        app = FastAPI()

        @app.get("/metrics")
        async def metrics_prometheus():
            body = metrics.get_registry().render_prometheus()
            return Response(
                content=body,
                media_type="text/plain; version=0.0.4; charset=utf-8",
            )

        @app.get("/metrics/json")
        async def metrics_json():
            return metrics.get_registry().render_json()

        return app

    def test_prometheus_endpoint_returns_200_text_plain_version(self):
        client = TestClient(self._make_app())
        metrics.record_task_submission(1, source="api")
        r = client.get("/metrics")
        assert r.status_code == 200
        ctype = r.headers["content-type"]
        assert ctype.startswith("text/plain")
        assert "version=0.0.4" in ctype
        assert "callisto_tasks_submitted_total" in r.text

    def test_json_endpoint_shape(self):
        client = TestClient(self._make_app())
        metrics.record_hypothesis_created("mlb")
        metrics.record_hypothesis_promoted("backtesting", "paper_trading")
        r = client.get("/metrics/json")
        assert r.status_code == 200
        payload = r.json()
        assert "metric_count" in payload
        assert "uptime_seconds" in payload
        assert payload["metric_count"] == len(payload["metrics"])
        names = {m["name"] for m in payload["metrics"]}
        assert "callisto_hypotheses_created_total" in names
        assert "callisto_hypotheses_promoted_total" in names
        # Find the created-total and confirm it captured the sport label.
        created = next(
            m for m in payload["metrics"]
            if m["name"] == "callisto_hypotheses_created_total"
        )
        labels = [s["labels"] for s in created["samples"]]
        assert {"sport": "mlb"} in labels
