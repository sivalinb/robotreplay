# GPU and inference learning lab

The first Brev experiment should answer one question: **does exact-prefix reuse reduce prefill latency for repeated evidence prompts without harming output quality?**

## A $20 experiment envelope

Use $12 for setup/baseline, $5 for one comparison, and $3 as contingency. This is an allocation, not an hourly-price quote. Record the actual rate and GPU/model fit from the account before starting.

Prepare containers, prompts, evaluation cases and scripts locally. Start one compatible GPU only for the measured batch. Include model download, setup and idle time in the instance ledger. Export artifacts and release billable resources afterward. Stopping a container does not stop the Brev VM; stopped instances can still incur storage charges. See [Brev GPU instance behavior](https://docs.nvidia.com/brev/concepts/gpu-instances).

No script provisions cloud resources or redeems credits.

## Single-GPU server

On a supported Linux NVIDIA host with Docker and NVIDIA Container Toolkit:

~~~bash
export RR_GPU_MODEL='YOUR_REVIEWED_SUPPORTED_MODEL'
export RR_GPU_CONTEXT=8192
docker compose --profile gpu up -d vllm
~~~

Select the model after checking its license, native context, dtype support and VRAM. Normal startup does not download an arbitrary model. Validate the versioned vLLM image against the host driver/CUDA combination.

The default profile enables prefix caching. For the cold condition, restart the server or use a clean workload and record how cache state was controlled. Exclude warm-up from measurement. A higher hit rate is not proof of better tail latency.

## Python benchmark runner

The default command produces a plan without making requests:

~~~bash
robotreplay benchmark --model YOUR_MODEL --requests 20
~~~

For a run, supply the actual self-hosted hourly rate or API token prices, a positive budget, and the execution flag:

~~~bash
robotreplay benchmark --model YOUR_MODEL --requests 20 \
  --concurrency 2 --max-seconds 120 --hourly-rate YOUR_ACTUAL_RATE \
  --budget-usd 1 --execute --output artifacts/warm-prefix.json
~~~

The runner measures client TTFT, completion latency, streaming **chunk** gaps, and latency goodput. It records usage only if the endpoint reports it. Chunk gaps are not token ITL; use [NVIDIA AIPerf](https://docs.nvidia.com/aiperf/welcome-to-ai-perf-documentation) with a pinned tokenizer for token-aware benchmarks. The client deadline cannot end GPU billing.

## Controlled comparisons

| Experiment | Change | Evidence |
|---|---|---|
| Prefix cache | Cold vs matching repeated prefix | TTFT, reused tokens, queue; decode separately |
| Context length | 2K, 8K, then a supported larger context | Grounding, input/output counts, prefill and VRAM |
| Continuous batching | Concurrency/scheduling at fixed workload | Throughput, tail latency, queue, memory |
| Chunked prefill | Explicit chunk/token scheduler settings | TTFT vs ITL on mixed long/short requests |
| Weight quantization | Weight dtype only | Weight memory, kernels, quality and latency |
| KV quantization | Cache dtype only | KV memory, long-context quality and kernels |
| LMCache offload | GPU vs CPU/disk reuse | Saved compute vs transfer latency and privacy |
| Disaggregated serving | Separate prefill/decode workers | Equal-GPU control, KV transfer, latency, GPU-hours |
| Cache-aware routing | Round-robin vs cache-aware | Locality, queue skew, worker load, tail latency |

The last three are follow-on studies. Distributed-serving diagrams are illustrations, not active deployments. Two processes on one GPU do not prove two-GPU benefits.

The memory planner computes ordinary full-attention KV state as **2 × layers × KV heads × head dimension × sequence tokens × active sequences × bytes per element**. It excludes weights, buffers, block rounding, quantization scales, sliding windows and architecture-specific compression. Its output is an estimate, never a hardware reading.

## References

- [vLLM prefix caching](https://docs.vllm.ai/en/latest/features/automatic_prefix_caching/) — prefill reuse, not automatic decode acceleration.
- [Scheduling and memory optimization](https://docs.vllm.ai/en/latest/configuration/optimization/).
- [Disaggregated prefill](https://docs.vllm.ai/en/latest/features/disagg_prefill/).
- [LMCache architecture](https://docs.lmcache.ai/developer_guide/architecture.html).
- [Dynamo cache-aware routing](https://docs.nvidia.com/dynamo/dev/knowledge-base/concepts/system-architecture/kv-aware-routing).
- [AIPerf goodput](https://docs.nvidia.com/aiperf/tutorials/metrics-analysis/benchmark-goodput-with-ai-perf) — latency constraints are separate from quality gates.
