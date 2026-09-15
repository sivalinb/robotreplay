# Validation record

Version 0.2 verified on September 14, 2026 using Python 3.12.14 on macOS. Provider results use synthetic records only.

| Check | Observed result |
|---|---|
| Python integration / regression suite | **45 passed**, including video processing, authorization, deletion, hybrid retrieval, worker recovery, key-file privacy, provider URL binding, invalid/truncated responses, shared budget reservations, cache isolation, honest fallback scoring, team-scoped inference telemetry and benchmark percentiles. |
| NeMo input rail | Executed with HF_HUB_OFFLINE=1. Allowed a teaching question and blocked a notebook-authorship request without a generation-model download or paid API call. |
| Versioned policy / output evaluation | **12 / 12 passed**. Synthetic fixture coverage only. |
| Ruff | Passed after formatting. |
| Dependency audit | 120 pinned packages audited; no known vulnerabilities reported at the time of this check. This is not a guarantee against unknown vulnerabilities. |
| Configuration parsing | The Grafana dashboard contains 22 panels with unique IDs. Compose and the full observability stack are exercised by Linux CI. Parsing alone is not a container runtime test. |
| Browser workflow | Signed in; generated clear and obscured recordings; confirmed 12-second playable video and a timestamp jump to 4.2 seconds; compared 100% versus 82% marker visibility; verified unconfigured hybrid fallback; inspected the real trace; ran evaluations, policy check and KV calculator. Saved trials remained after a process restart. |
| Responsive UI | Architecture and Provider lab inspected at a 375-pixel viewport with no document-level horizontal overflow. Provider model selection and usage tables verified in the browser; viewport restored after testing. |
| Live provider evaluation | Nemotron Lightning and GPT-OSS on each provider: 4 runs × 8 checks, with 12 successful model requests and zero fallbacks. Five checks per run test local policy/abstention, not model safety. [Recorded evidence](../robotreplay/data/provider-results.json). |
| Cache experiment | Six real Fireworks requests in three isolation/repeat pairs. Initial requests reported zero cached input; exact repeats reported 658 tokens each. One repeat was slower despite reuse. |
| Live hybrid retrieval | One Nebius Qwen3 embedding request followed by GPT-OSS selection completed the hybrid_rrf workflow. This is an integration check, not a retrieval-quality benchmark. |
| Accounted API cost | 20 requests total: $0.00294250 at configured token prices, with no unknown-use reservations. Actual invoices and credit adjustments may differ. |
| GPU / training | No GPU, dedicated endpoint, fine-tuning job or Brev resource provisioned. GPU performance remains unmeasured. |
| Docker observability stack | **Passed in GitHub Actions on Linux:** image build, generated-video analysis/playback/investigation inside the non-root container, authenticated Prometheus scraping, Collector scraping, Grafana dashboard provisioning, OTLP trace retrieval from Tempo, and lifecycle-log retrieval from Loki. Initial infrastructure evidence: [run 34927259953](https://github.com/sivalinb/robotreplay/actions/runs/34927259953). Docker was unavailable on the local macOS host. |

The test suite reports two upstream deprecation warnings (Starlette/AnyIO and NeMo's legacy configuration field). They did not fail the tests.

Provider measurements are non-streaming wall times and reported usage only. Cached/reasoning counts stay unknown when omitted. Full benchmark conditions and reproducible commands are documented in the [provider lab guide](provider-lab.md). Nearest-rank percentiles are computed from request durations; three requests cannot establish a stable tail-latency estimate.

## Reproduce

~~~bash
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
python -m ruff check .
HF_HUB_OFFLINE=1 python -m pytest
robotreplay evaluate
python -m pip_audit --disable-pip --no-deps -r requirements.lock
~~~

GitHub Actions stores test XML, policy results and infrastructure diagnostics as workflow artifacts. Those are test results, not production performance measurements.

The vision baseline is intentionally narrow: one distinctive green marker, a fixed camera and a controlled drill. It cannot establish real VEX game accuracy. A real team pilot needs permitted recordings, mentor labels, held-out sessions and review of false detections and useful abstention.
