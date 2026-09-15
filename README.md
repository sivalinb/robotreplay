# RobotReplay

**Turn robotics practice into evidence students can inspect.**

RobotReplay is a Python application for reviewing short practice recordings, comparing visible events, and asking better experiment questions. It connects a useful mentor workflow to hands-on work in observability, bounded agents, RAG, evaluation, and GPU inference.

Students choose the next experiment and write their own conclusions. The application does not score VEX games, control a robot, diagnose mechanical causes from video, or write an engineering notebook.

## Run it

Python 3.12 is required. No GPU, model key, Docker, or external recording is needed for the local demonstration.

~~~bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
robotreplay demo
~~~

Open **http://127.0.0.1:8000** and sign in with **demo / robotreplay-demo**.

1. Select **Analyze a clear sample**. The app generates an original 12-second video, transcodes it, and analyzes its actual pixels.
2. Open the trial and select a timestamped observation.
3. Generate the obscured sample and inspect its visibility gap.
4. Ask an evidence question, then inspect its trace under **Observability**.
5. Run **Evaluations** and try the notebook/injection cases in **Safety lab**.
6. Open **Inference lab** for context and KV-memory planning. Hardware readings appear only when an NVIDIA GPU is actually available.

Demo mode accepts generated media only and disables paid inference. Data persists under .data/demo/.

For your own permitted recordings:

~~~bash
robotreplay init --username coach
robotreplay serve
~~~

The initializer asks for a new password without echoing it. Sign in as that mentor and import a permitted recording. Original media, derivatives, the database, credentials, and experiment artifacts are excluded from Git.

## What is implemented

| Capability | Implementation and verification boundary |
|---|---|
| Video review | Real MP4/MOV/AVI/WebM ingestion, bounded validation/transcoding, browser playback, timestamp navigation, annotations, comparison, retention and deletion. |
| Computer vision | OpenCV green-marker baseline with measured coordinates, direction-change candidates, low-motion candidates, and visibility gaps. Verified on original procedural drills; general VEX match accuracy is **not established**. |
| Persistence | SQLite WAL, durable single-worker jobs, restart recovery, source hashes, permission records, evidence and FTS5 indexes. |
| Bounded agents | A fixed LangGraph investigation with scoped evidence retrieval, trial comparison, and a versioned teaching-concept lookup. No shell, actuator, write, or export tool is exposed to a model. |
| RAG | SQLite lexical search and chronological fallback. Optional embedding endpoint plus reciprocal-rank fusion; remote protocol tested with a local transport double. No paid embedding request has been run. |
| Model integration | Nebius Token Factory or a vLLM-compatible endpoint selects approved teaching prompts and real evidence IDs. Strict JSON validation, total deadline, no automatic retry, local fallback, and cost reservations. Protocol/failure tests use local transport doubles. |
| NeMo | An actual NeMo Guardrails input rail, tested without a generation model or model download. Enable with RR_NEMO_ENABLED=true. Authorization and output validity remain application checks. |
| Observability | Real OpenTelemetry spans persisted locally, an authenticated dashboard, sanitized lifecycle logs, bounded Prometheus metrics, and optional OTLP export. |
| Infrastructure | Dockerfile, optional Grafana/Prometheus/Tempo/Loki/Collector profile, provisioned dashboard and alerts. Consult the validation record before assuming a profile was exercised on your hardware. |
| GPU experiments | A single-GPU vLLM Compose profile, real SSE benchmark runner, local GPU query, and an analytical KV-memory planner. **No GPU performance measurements are claimed.** |
| Distributed serving | Illustrated Dynamo/NIXL/offload/routing designs and an experiment plan. Multi-GPU disaggregation, production KV offload, Kubernetes/eBPF, and vendor-specific optimization studies are follow-on research, not deployed features. |

The browser uses ordinary HTML/CSS/JavaScript for playback and interaction. Application logic, APIs, persistence, policy, retrieval, media processing, evaluations, and benchmark orchestration are Python.

## Architecture

~~~mermaid
flowchart TD
  U["Mentor + students<br/>Browser / HTML video"] --> A["FastAPI<br/>Session · CSRF · permission gate"]
  A --> Q["SQLite durable queue<br/>One bounded worker"]
  Q --> V["FFmpeg + OpenCV<br/>Real media → visible observations"]
  V --> E["SQLite FTS5 + local media<br/>Team scope · timestamps · provenance"]
  U --> G["LangGraph investigation<br/>Read-only tools"]
  G --> E
  G --> R["Input policy<br/>Optional NeMo rail"]
  R --> M{"Reasoning route"}
  M --> L["Local teaching templates"]
  M --> N["Optional Nebius API<br/>Text evidence only"]
  M --> B["Optional Brev GPU / vLLM<br/>Prefill → KV cache → decode"]
  L --> O["Pydantic output contract<br/>Allowed question + evidence IDs"]
  N --> O
  B --> O
  O --> U
  A -.-> T["OpenTelemetry<br/>Local trace store + OTLP Collector"]
  V -.-> T
  G -.-> T
  T --> D["Grafana · Tempo · Prometheus · Loki"]
  B -.-> H["vLLM metrics · DCGM / nvidia-smi"]
~~~

See [architecture and boundaries](docs/architecture.md), [GPU experiment plan](docs/gpu-lab.md), [observability](docs/observability.md), and [validation record](docs/validation.md).

## Optional model configuration

.env.example documents the settings. The Python CLI reads **environment variables**; it does not silently load a file. Export only the variables you intend to use, or use your process manager's environment-file support. Compose reads .env through its explicit env_file.

For Nebius, configure RR_PROVIDER=nebius, RR_MODEL, RR_MODEL_API_KEY, verified input/output prices per million tokens, and a positive RR_MODEL_BUDGET_USD. The base URL defaults to the [Token Factory endpoint](https://docs.tokenfactory.nebius.com/api-reference/introduction).

For self-hosted vLLM, configure RR_PROVIDER=vllm, the endpoint/model, RR_GPU_HOURLY_RATE, and a positive request budget. Its ledger apportions **request wall-time at the configured rate**; it does not represent the entire GPU bill. Idle time, startup, storage, and the rest of a Brev instance's lifetime need their own ledger.

Model and embedding calls are disabled when required settings or budget are missing. Timeouts and unknown usage retain their reservation. Reported token counts and configured prices determine accounted API cost; billing statements remain authoritative.

Hybrid retrieval also requires RR_EMBEDDING_BASE_URL, RR_EMBEDDING_MODEL, RR_EMBEDDING_API_KEY, and RR_EMBEDDING_USD_PER_MILLION. Select hybrid retrieval in the question form. It embeds a bounded set of computed observations; human annotations and raw video are not sent. An unavailable or unconfigured provider produces an explicitly labeled lexical fallback.

## Checks

~~~bash
python -m ruff check .
HF_HUB_OFFLINE=1 python -m pytest
robotreplay evaluate
python -m pip_audit --disable-pip --no-deps -r requirements.lock
~~~

The lock file records the tested Python 3.12 environment, including the optional NeMo input rail and development tools. A small synthetic policy suite does not establish real-world safety, retrieval quality, or student learning.

## Your learning progression

1. **Product and media engineering:** reproduce the video-to-evidence workflow and let another mentor use it.
2. **Observability:** explain one complete trace and one failure without logging private content.
3. **RAG and agents:** compare lexical and hybrid retrieval on a versioned held-out query set.
4. **Evaluation and security:** add a regression that catches an unsupported answer or cross-team read.
5. **GPU inference:** publish a controlled cold/warm prefix comparison with quality, latency, memory and cost.
6. **Distributed systems:** after more budget, compare disaggregation and routing at equal resource counts.

Publish measured reports and limitations as the project progresses. Do not convert design diagrams or analytical memory estimates into performance claims.
