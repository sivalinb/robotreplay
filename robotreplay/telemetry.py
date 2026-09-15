import time
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Status, StatusCode
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    GCCollector,
    Histogram,
    PlatformCollector,
    ProcessCollector,
)

from robotreplay import __version__
from robotreplay.store import encode


class SQLiteExporter(SpanExporter):
    def __init__(self, store):
        self.store = store

    def export(self, spans):
        try:
            with self.store.connect() as db:
                for span in spans:
                    attrs = dict(span.attributes or {})
                    team = attrs.pop("rr.team", "system")
                    db.execute(
                        """INSERT INTO spans(team,trace_id,span_id,parent_id,name,
                      started,duration_ms,attrs) VALUES(?,?,?,?,?,?,?,?)""",
                        (
                            team,
                            format(span.context.trace_id, "032x"),
                            format(span.context.span_id, "016x"),
                            format(span.parent.span_id, "016x") if span.parent else None,
                            span.name,
                            span.start_time / 1e9,
                            (span.end_time - span.start_time) / 1e6,
                            encode(attrs),
                        ),
                    )
                # Bounded local retention; no prompts, responses, filenames or video in spans.
                db.execute("DELETE FROM spans WHERE started<?", (time.time() - 7 * 86400,))
                db.execute(
                    "DELETE FROM spans WHERE id NOT IN (SELECT id FROM spans ORDER BY id DESC LIMIT 10000)"
                )
            return SpanExportResult.SUCCESS
        except Exception:
            return SpanExportResult.FAILURE


class Telemetry:
    def __init__(self, store, otlp_endpoint=""):
        self.store = store
        self.provider = TracerProvider()
        self.provider.add_span_processor(SimpleSpanProcessor(SQLiteExporter(store)))
        if otlp_endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            self.provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint, timeout=3))
            )
        self.tracer = self.provider.get_tracer("robotreplay", __version__)
        self.registry = CollectorRegistry()
        ProcessCollector(registry=self.registry)
        PlatformCollector(registry=self.registry)
        GCCollector(registry=self.registry)
        Gauge(
            "rr_queue_depth", "Queued or running media jobs", registry=self.registry
        ).set_function(
            lambda: store.one(
                "SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued','running')"
            )["n"]
        )
        Gauge(
            "rr_budget_accounted_usd",
            "Settled estimates plus unknown-use reservations",
            registry=self.registry,
        ).set_function(
            lambda: store.one(
                "SELECT COALESCE(SUM(CASE WHEN status='settled' THEN charged ELSE reserved END),0) AS n FROM ledger"
            )["n"]
        )
        self.requests = Counter(
            "rr_requests_total",
            "API responses",
            ["route", "method", "status"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "rr_request_duration_seconds", "API wall time", ["route"], registry=self.registry
        )
        self.jobs = Counter(
            "rr_jobs_total", "Completed analysis jobs", ["outcome"], registry=self.registry
        )
        self.policy = Counter(
            "rr_policy_total", "Policy decisions", ["reason"], registry=self.registry
        )
        self.model = Counter(
            "rr_model_calls_total",
            "Model attempts",
            ["provider", "outcome"],
            registry=self.registry,
        )
        self.model_duration = Histogram(
            "rr_model_duration_seconds",
            "Provider request wall time",
            ["provider", "operation", "outcome"],
            registry=self.registry,
            buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60),
        )
        self.model_tokens = Counter(
            "rr_model_tokens_total",
            "Provider-reported token counts; details are subsets",
            ["provider", "operation", "kind"],
            registry=self.registry,
        )
        self.model_usage = Counter(
            "rr_model_usage_reports_total",
            "Requests by token-usage availability",
            ["provider", "operation", "availability"],
            registry=self.registry,
        )
        self.cache_eligible = Counter(
            "rr_model_cache_eligible_input_tokens_total",
            "Input tokens only from requests with reported cache counts",
            ["provider"],
            registry=self.registry,
        )
        self.model_cost = Counter(
            "rr_model_accounted_cost_usd_total",
            "Configured-price estimates, not invoices",
            ["provider", "operation"],
            registry=self.registry,
        )

    def record_model(
        self,
        team,
        provider,
        operation,
        outcome,
        duration_ms,
        usage,
        cost_usd,
        cost_basis,
        http_status=None,
        finish_reason=None,
    ):
        self.model.labels(provider, outcome).inc()
        self.model_duration.labels(provider, operation, outcome).observe(duration_ms / 1000)
        known = usage.get("input_tokens") is not None and usage.get("output_tokens") is not None
        self.model_usage.labels(provider, operation, "reported" if known else "unknown").inc()
        for kind in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens"):
            if usage.get(kind) is not None:
                self.model_tokens.labels(provider, operation, kind).inc(usage[kind])
        if usage.get("cached_input_tokens") is not None:
            self.cache_eligible.labels(provider).inc(usage["input_tokens"])
        if cost_usd is not None:
            self.model_cost.labels(provider, operation).inc(cost_usd)
        attrs = {
            "rr.provider": provider,
            "rr.outcome": outcome,
            "rr.duration_ms": duration_ms,
            "rr.cost_basis": cost_basis,
        }
        attrs.update({"rr." + k: v for k, v in usage.items() if v is not None})
        if cost_usd is not None:
            attrs["rr.accounted_cost_usd"] = cost_usd
        span = trace.get_current_span()
        for key, value in attrs.items():
            span.set_attribute(key, value)
        with self.store.connect() as db:
            db.execute(
                """INSERT INTO inference_events(team,created,provider,operation,outcome,duration_ms,
                input_tokens,output_tokens,cached_input_tokens,reasoning_tokens,cost_usd,
                cost_basis,http_status,finish_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    team,
                    time.time(),
                    provider,
                    operation,
                    outcome,
                    duration_ms,
                    usage.get("input_tokens"),
                    usage.get("output_tokens"),
                    usage.get("cached_input_tokens"),
                    usage.get("reasoning_tokens"),
                    cost_usd,
                    cost_basis,
                    http_status,
                    finish_reason,
                ),
            )
            db.execute("DELETE FROM inference_events WHERE created<?", (time.time() - 7 * 86400,))
            db.execute(
                "DELETE FROM inference_events WHERE id NOT IN "
                "(SELECT id FROM inference_events ORDER BY id DESC LIMIT 10000)"
            )

    @contextmanager
    def span(self, name, team="system", context=None, **attrs):
        with self.tracer.start_as_current_span(
            name,
            context=context,
            attributes={"rr.team": team, **attrs},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                yield span
            except BaseException:
                # Exception messages can contain user content or provider credentials.
                span.set_status(Status(StatusCode.ERROR))
                span.set_attribute("rr.failed", True)
                raise

    def close(self):
        self.provider.shutdown()


def trace_id():
    return format(trace.get_current_span().get_span_context().trace_id, "032x")
