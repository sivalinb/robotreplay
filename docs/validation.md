# Validation record

Initial implementation verified on September 14, 2026 using Python 3.12.14 on macOS.

| Check | Observed result |
|---|---|
| Python integration / regression suite | **35 passed**, including real generated-video processing, occlusion, playback ranges, authorization, team isolation, deletion, scoped hybrid retrieval, common-word comparison regression, worker restart recovery, provider failures and benchmark deadlines. |
| NeMo input rail | Executed with HF_HUB_OFFLINE=1. Allowed a teaching question and blocked a notebook-authorship request without a generation-model download or paid API call. |
| Versioned policy / output evaluation | **12 / 12 passed**. Synthetic fixture coverage only. |
| Ruff | Passed after formatting. |
| Dependency audit | 120 pinned packages audited; no known vulnerabilities reported at the time of this check. This is not a guarantee against unknown vulnerabilities. |
| Configuration parsing | Nine YAML files and the 16-panel Grafana dashboard parsed successfully. Parsing is not a container runtime test. |
| Browser workflow | Signed in; generated clear and obscured recordings; confirmed 12-second playable video and a timestamp jump to 4.2 seconds; compared 100% versus 82% marker visibility; verified unconfigured hybrid fallback; inspected the real trace; ran evaluations, policy check and KV calculator. Saved trials remained after a process restart. |
| Responsive UI | Architecture view inspected at a 375-pixel viewport with no document-level horizontal overflow; desktop view inspected at the browser's normal size. |
| GPU / paid providers | No GPU, Nebius inference or paid embedding request executed. Remote protocol tests use local transport doubles. No Brev resources provisioned. |
| Docker observability stack | **Passed in GitHub Actions on Linux:** image build, generated-video analysis/playback/investigation inside the non-root container, authenticated Prometheus scraping, Collector scraping, Grafana dashboard provisioning, OTLP trace retrieval from Tempo, and lifecycle-log retrieval from Loki. Initial infrastructure evidence: [run 34927259953](https://github.com/sivalinb/robotreplay/actions/runs/34927259953). Docker was unavailable on the local macOS host. |

The test suite reports two upstream deprecation warnings (Starlette/AnyIO and NeMo's legacy configuration field). They did not fail the tests.

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
