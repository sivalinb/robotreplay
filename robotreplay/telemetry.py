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
        self.provider = TracerProvider()
        self.provider.add_span_processor(SimpleSpanProcessor(SQLiteExporter(store)))
        if otlp_endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            self.provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint, timeout=3))
            )
        self.tracer = self.provider.get_tracer("robotreplay", "0.1.0")
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
