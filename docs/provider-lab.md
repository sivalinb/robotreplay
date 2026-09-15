# Provider lab: real measurements before a bigger stack

RobotReplay v0.2 connects Nebius Token Factory and Fireworks AI to the same bounded teaching workflow. It records provider-reported usage and keeps application safety checks independent of the model. The **Provider lab** page displays reviewed synthetic experiment artifacts; opening it never spends credits.

## Recorded experiment — September 14, 2026

| Provider / model | Suite checks | Successful model calls | Local fallbacks | Median response | Estimated token cost |
|---|---:|---:|---:|---:|---:|
| Nebius / Nemotron 3.5 Lightning | 8/8 | 3/3 | 0 | 3,253 ms | $0.00062814 |
| Fireworks / Nemotron Lightning 3.5 | 8/8 | 3/3 | 0 | 1,715 ms | $0.00043990 |
| Nebius / GPT-OSS-120B | 8/8 | 3/3 | 0 | 970 ms | $0.00036465 |
| Fireworks / GPT-OSS-120B | 8/8 | 3/3 | 0 | 1,116 ms | $0.00036645 |

Each run has three live selection requests and five local policy/abstention checks. The latter do **not** establish a provider's jailbreak resistance. Cases test teaching-question selection, citations, occlusion abstention, student authorship, credential requests, team scope, and injected source text.

This is a small application integration experiment, not a general model ranking. Runs were sequential from one client. Nebius used JSON-object mode; Fireworks used schema-constrained output and team cache isolation. Provider model aliases, serving revisions, tokenizers, cache conditions, reasoning defaults and network time can differ. The report preserves model IDs, prices, output limits, deadlines and the dataset hash. No model weights or serving revisions were independently pinned.

The [published JSON artifact](../robotreplay/data/provider-results.json) also records:

- **Cache reuse:** three fresh Fireworks GPT-OSS isolation scopes reported zero cached tokens on the initial request and 658 on the exact repeat. Response times were 1,864→1,623 ms, 1,226→1,054 ms, and 1,499→1,625 ms. A cache hit did not consistently lower wall time. Cost estimate: $0.00102861.
- **Hybrid retrieval:** one Nebius Qwen3-Embedding-8B call flowed through vector validation, rank fusion and GPT-OSS selection. It returned hybrid_rrf with cited evidence. This verifies integration, not retrieval quality. Cost estimate including selection: $0.00011475.
- **Combined:** 20 paid API requests, $0.00294250 at configured token prices, and no unknown-use reservations. These are application estimates; provider invoices, discounts and credit adjustments remain authoritative. No GPU or training job was created.

## Reproduce deliberately

The CLI reads environment variables. It does not automatically load .env. A key file contains just one token; its value and path are excluded from Settings representations. Do not set both a key value and its key-file alternative. In a container, mount the file read-only and use its container path.

~~~bash
export RR_PROVIDER=fireworks
export RR_MODEL=accounts/fireworks/models/gpt-oss-120b
export RR_MODEL_API_KEY_FILE=/absolute/path/to/private/fireworks-key
export RR_DATA_DIR=.data/provider-lab
export RR_MODEL_BUDGET_USD=0.20
export RR_INPUT_USD_PER_MILLION=0.15
export RR_OUTPUT_USD_PER_MILLION=0.60
export RR_CACHED_INPUT_USD_PER_MILLION=0.015
export RR_CONTEXT_LIMIT=16384
export RR_OUTPUT_TOKENS=1024
export RR_MODEL_TIMEOUT=20

robotreplay provider-evaluate
robotreplay cache-evaluate
# Inspect the plans and verify current prices before explicit execution:
robotreplay provider-evaluate --execute
robotreplay cache-evaluate --execute
~~~

Prices above were checked for the recorded run, not guaranteed for future use. The [Fireworks GPT-OSS model page](https://fireworks.ai/models/fireworks/gpt-oss-120b) lists model-specific input, cached-input and output rates. For [Nemotron Lightning](https://fireworks.ai/models/fireworks/nemotron-lightning-3p5-30b-a3b), the recorded rates were $0.05 / $0.01 / $0.20 per million tokens respectively. Nebius model metadata supplies current token prices through its [model-list API](https://docs.tokenfactory.nebius.com/api-reference/models/list-models); multiply per-token rates by one million before setting these variables.

For Nebius, select RR_PROVIDER=nebius, its own model ID and key file, and its verified prices. Clear an explicit RR_MODEL_BASE_URL when changing providers: the adapter chooses the matching official endpoint and rejects a different vendor's URL. Clear a cached-input price unless verified for the selected model. Cached-token reporting does not by itself establish the discount rate.

All evaluation and application calls using the same RR_DATA_DIR share an atomic reservation ledger. Changing data directories creates a separate ledger; the limit is not an account-wide billing cap. Admission reserves undiscounted input/output estimates. Unknown usage, including timeouts, retains the reservation; no automatic retry or paid failover occurs. Reasoning tokens are a reported subset of output, never billed twice by the app. Request limits are conservative estimates, not a provider-enforced invoice limit.

For hybrid retrieval, also configure RR_EMBEDDING_BASE_URL, RR_EMBEDDING_MODEL, RR_EMBEDDING_API_KEY_FILE and RR_EMBEDDING_USD_PER_MILLION. The recorded Nebius model was Qwen/Qwen3-Embedding-8B at $0.01 per million input tokens. Its key is loaded separately; no credentials are shared between vendors.

## What each provider adds

| Capability | Practical use in RobotReplay | Status / boundary |
|---|---|---|
| Nebius hosted Nemotron and GPT-OSS | Compare a compact NVIDIA model with another model family | Live synthetic calls verified; model catalog access is not proof of every model's quota |
| Nebius embeddings | Compare lexical retrieval with semantic retrieval and reciprocal-rank fusion | Live integration verified; a labeled retrieval dataset is next |
| Fireworks structured output | Select approved teaching questions with constrained JSON | Implemented; application validation still checks actual evidence IDs |
| Fireworks prefix caching | Repeat a static prompt safely within a team; inspect reported reuse | Implemented and measured; opaque team keys isolate cache scope |
| Long context and reasoning controls | Vary evidence length and output reserve; measure correctness, truncation, latency and cost | Token accounting implemented; a long-context quality sweep remains future work |
| Vision / video APIs | Compare frame captions with measured events on permitted or generated recordings | Future adapter. Model and serving-path support must be checked individually |
| Batch APIs | Run larger offline evaluation datasets more economically | Documented provider capability; not used by this app |
| Provider metrics and billing exports | Reconcile app estimates with provider usage; separate service latency from local request time | Additional account integration; current app collects its own traces and responses |
| Managed evaluation / tuning | Score held-out tasks, then consider SFT, preference tuning or reinforcement learning | Future work; dataset, job cost and credit eligibility must be established first |

Fireworks documents [schema-constrained output](https://docs.fireworks.ai/structured-responses/structured-response-formatting), [cache routing and isolation](https://docs.fireworks.ai/guides/prompt-caching), [video/audio model restrictions](https://docs.fireworks.ai/guides/video-audio-inputs), and [evaluation jobs](https://docs.fireworks.ai/api-reference/create-evaluation-job). Its video guide currently lists GLM 5.3 Flash as serverless, while Qwen3 Omni and Molmo2 require dedicated deployments. Native video support is different from sending selected images to a vision model.

Nebius offers [post-training workflows](https://docs.tokenfactory.nebius.com/post-training/overview), including a [custom EAGLE3 speculator](https://docs.tokenfactory.nebius.com/post-training/custom-speculator). A useful later study would compare draft acceptance rate, accepted length and quality at equal cost. These jobs are not covered by an inference-only experiment plan. Nebius's portable eagle3_original artifact and Token Factory-optimized eagle3 artifact have different deployment targets.

## Observability and guardrails

The app records per-call provider, operation, outcome, wall time, input/output counts, cached-input counts, reasoning-token counts, and configured-price estimates. Optional fields remain null when absent or inconsistent. No prompt, response, key, private note or video is stored in inference telemetry. Team-scoped records have a seven-day/10,000-record bound. The public Provider lab is a separate synthetic release artifact.

The Grafana dashboard now has 22 panels. Provider latency, token rates, usage availability, reported cache ratio, reasoning counts and cost estimates complement API, process, queue, policy, trace, log and optional GPU signals. Cache-ratio denominators include only requests with cache reporting. CLI experiments write durable local observations and report files; their in-process Prometheus counters end with the CLI process. Application serving exposes the live scrape endpoint.

There is an important provider visibility boundary: Nebius's [Observability Metrics documentation](https://docs.tokenfactory.nebius.com/ai-models-inference/observability) explicitly restricts those metrics to Dedicated Endpoints. Its [Prometheus integration](https://docs.tokenfactory.nebius.com/ai-models-inference/observability-api-integrations) also needs the project ID and permissions. A serverless API key alone does not grant GPU utilization, replica or KV-occupancy visibility. Fireworks has [deployment metrics export](https://docs.fireworks.ai/deployments/exporting-metrics) and [usage/cost export](https://docs.fireworks.ai/accounts/exporting-usage-and-costs); available series depend on the service used. Rated costs are not remaining credit balance.

Authorization, input policy, optional NeMo input rails, source filtering, bounded read-only tools, strict output validation, deadlines and the shared budget ledger apply before or after provider calls as appropriate. Structured JSON is not a semantic safety guarantee. Model catalogs containing a guard model do not imply serverless access: several catalogued Llama Guard and safeguard models were not serverless-enabled in the inspected Fireworks page. Keep application checks in place regardless of a future moderation model.

## NVIDIA and infrastructure learning path

| Track | Tools and concrete experiment | What requires more than API credits |
|---|---|---|
| Agent engineering | Wrap the existing LangGraph workflow with NeMo Agent Toolkit profiling; retain trace and evaluation continuity | Toolkit integration work; model calls still use the chosen provider |
| Agent evaluation | Adapt the synthetic suite and later mentor-labeled cases to NeMo Evaluator; score tool trajectories and abstention separately | Judge-model calls may cost tokens; a full managed platform is optional |
| Single-GPU serving | Brev + vLLM + AIPerf; test one small fitting model, stable prompt prefixes and controlled concurrency | A GPU instance and its full lifetime cost |
| Hardware observability | DCGM / nvidia-smi + Prometheus / Grafana; correlate VRAM, utilization and queueing | Access to the GPU host; serverless token endpoints do not provide this |
| Kernel and engine study | Nsight Systems, TensorRT-LLM / TensorRT, ModelOpt; compare one supported configuration with vLLM | Hardware/model compatibility, engine build time and an additional measured run |
| KV-cache engineering | vLLM prefix caching, chunked prefill; later LMCache CPU offload with transfer costs | An owned serving backend. LMCache is an independent open-source project |
| Distributed inference | Dynamo scheduling/cache routing and NIXL transfer; compare separate prefill/decode workers | Enough compatible GPUs, memory and network to make the comparison meaningful |
| Video understanding | Explore NVIDIA Cosmos Reason or selected VSS components against hand-labeled clips | Model-specific GPU capacity and a separate vision accuracy evaluation |
| Linux observability | bpftrace or a narrowly scoped eBPF probe for process I/O and latency; Kubernetes/Tetragon only when there is a cluster question to answer | Linux/kernel support; macOS is not the target execution environment |

See the official [NeMo Agent Toolkit overview](https://docs.nvidia.com/nemo/agent-toolkit/latest/index.html), [NeMo agent evaluation](https://docs.nvidia.com/nemo-platform/documentation/evaluate-models/agent-eval), [AIPerf documentation](https://docs.nvidia.com/aiperf/), [Dynamo documentation](https://docs.nvidia.com/dynamo/latest/), and [DCGM guide](https://docs.nvidia.com/datacenter/dcgm/latest/user-guide/) for the respective extension points. Engine and profiling work is covered by [TensorRT-LLM](https://nvidia.github.io/TensorRT-LLM/), [ModelOpt](https://github.com/NVIDIA/Model-Optimizer), and [Nsight Systems](https://docs.nvidia.com/nsight-systems/UserGuide/index.html). [VSS Agents](https://docs.nvidia.com/vss/latest/VSS-Agents.html) illustrate a larger video-understanding architecture; [bpftrace](https://bpftrace.org/docs/release_024/language) supports a narrower Linux instrumentation study. These are a learning roadmap, not installed or benchmarked components in v0.2.

Use the small Brev allocation for one prepared single-GPU baseline and one comparison. Include download, startup, idle time and storage. API credits do not transfer automatically to GPU compute: Nebius Token Factory and Nebius AI Cloud have separate product/account considerations, and Fireworks credits remain subject to that account's eligible services. NIM access or licensing is also separate from owning Brev credits. Do not assume that a model with 3B active MoE parameters needs memory for only 3B weights. The KV planner estimates full-attention state; hybrid Mamba, compressed attention and other architectures need their own memory model.

The next useful product milestone is a consented mentor pilot: label a small set of controlled drills, hold out whole sessions, and measure event error, retrieval relevance, useful abstention and whether the suggested next test helps students. That supplies meaningful evidence for the infrastructure experiments.
