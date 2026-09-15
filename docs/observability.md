# Observability from the first recording

Local traces are real OpenTelemetry SDK spans exported into a bounded SQLite store. An investigation links input policy, scoped tools, optional model selection and output policy. Queued analysis preserves its W3C traceparent. Parent/child durations overlap: the dashboard uses a common time axis rather than summing them.

Metrics contain bounded route templates and outcome categories. Session/team IDs, arbitrary exception text, media, source names, prompts and responses do not become metric labels. Infrastructure logs contain timestamp, service, action and a bounded reason. Team-scoped audit records remain in SQLite.

## Optional local stack

~~~bash
cp .env.example .env
# Set RR_OTLP_ENDPOINT=http://otelcol:4318/v1/traces in .env
# Set GRAFANA_ADMIN_PASSWORD to a local password of your choice.
docker compose build
docker compose run --rm app robotreplay init --username coach
docker compose --profile observability up -d
~~~

Open the app at **http://127.0.0.1:8000**, Grafana at **http://127.0.0.1:3000**, and Prometheus at **http://127.0.0.1:9090**. Grafana's local username is **admin**. Its fallback password is a local-lab convenience and must be changed before broader access.

Only loopback ports are published. Prometheus reads the generated metrics token through a read-only volume. The Collector reads sanitized lifecycle logs and exports them to Loki; traces go to Tempo. Its privacy transform removes the team attribute before external trace storage.

The replay-data volume persists application records. Grafana persists separately. Tempo/Loki/Prometheus lab storage is disposable; this is not production retention.

Optional vLLM/DCGM scrape targets are down until connected. This is not zero GPU usage. On Linux, replace the DCGM target with the reachable exporter address; host.docker.internal is a Docker Desktop convenience, not a universal host name.

## Dashboard panels

- API traffic and p95 request latency.
- Analysis completion/failure counts.
- Policy decisions and model outcomes.
- Provider response latency, input/output token rates and usage-report availability.
- Reported cache ratio, reasoning-token counts and configured-price cost estimates.
- Scrape health and Collector export failures.
- Supported vLLM queue/cache and DCGM utilization/memory metrics when connected.
- Tempo traces and sanitized Loki logs through provisioned sources.

External metric names can change; verify names and semantics on the selected server before interpreting a panel. Empty GPU panels are unmeasured.

The 22-panel dashboard distinguishes provider response metrics from GPU internals. Cached and reasoning tokens are subsets, not extra tokens. Requests without cache metadata do not enter the cache-ratio denominator. Durable team-scoped inference records are available in the application; published synthetic experiments are shown separately in Provider lab. See [provider visibility and accounting boundaries](provider-lab.md).

## Failure drills

1. Upload an invalid file: expect a failed job, a bounded reason, and no playable result.
2. Exercise a provider timeout: one attempt, retained reservation, local fallback.
3. Stop the Collector: inspect scrape health and missing exporter data; local traces continue.
4. Request unknown paths: metrics should label them unmatched rather than using raw URLs.
5. Delete a clip: verify media, samples and search entries are unavailable.

No production SLO is claimed. Establish clip size, resolution, hardware, concurrency and a representative workload first.
