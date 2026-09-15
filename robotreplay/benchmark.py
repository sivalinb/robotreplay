"""Real streaming endpoint measurements; no fabricated token or GPU counters."""

import asyncio
import hashlib
import json
import math
import os
import platform
import statistics
import time
from pathlib import Path

import httpx

from robotreplay.config import Settings

PROMPT_PREFIX = (
    "You are a robotics teaching assistant. Ask one short experiment question. "
    "Students own their work. Use only the supplied evidence. "
    "Evidence: A green marker changes direction at 4.2 seconds in a generated practice drill. "
)


def percentile(values, quantile):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int((len(ordered) - 1) * quantile)))]


async def stream_request(client, base_url, model, api_key, prompt, max_tokens, timeout):
    start, first, last = time.perf_counter(), None, None
    gaps, usage = [], {}
    received = 0
    try:
        async with client.stream(
            "POST",
            base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + api_key},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0,
                "stream": True,
                "stream_options": {"include_usage": True},
            },
            timeout=timeout,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                received += len(line)
                if received > 1_000_000:
                    raise ValueError("response_limit")
                if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                    continue
                event = json.loads(line[5:])
                if not isinstance(event, dict):
                    raise ValueError("invalid_stream_object")
                if event.get("usage"):
                    usage = event["usage"]
                choices = event.get("choices", [])
                if choices and choices[0].get("delta", {}).get("content"):
                    now = time.perf_counter()
                    if first is None:
                        first = now
                    if last is not None:
                        gaps.append((now - last) * 1000)
                    last = now
        end = time.perf_counter()
        return {
            "ok": first is not None,
            "latency_ms": (end - start) * 1000,
            "ttft_ms": (first - start) * 1000 if first is not None else None,
            "inter_chunk_ms": gaps,
            "usage": usage,
            "error": None if first is not None else "no_content",
        }
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        return {
            "ok": False,
            "latency_ms": (time.perf_counter() - start) * 1000,
            "ttft_ms": None,
            "inter_chunk_ms": [],
            "usage": {},
            "error": "request_failed",
        }


async def benchmark(args):
    if not args.execute:
        return {
            "status": "plan_only",
            "requests": args.requests,
            "instructions": "Add --execute only after configuring the endpoint and budget. "
            "This command never provisions or stops a Brev VM.",
        }
    if not 1 <= args.requests <= 200 or not 1 <= args.concurrency <= 16:
        raise ValueError("Use 1–200 requests and 1–16 concurrent calls")
    if not 1 <= args.prefix_repeats <= 128 or not 1 <= args.max_tokens <= 8192:
        raise ValueError("Use 1–128 prefix repetitions and 1–8192 output tokens")
    if not 0.1 <= args.max_seconds <= 3600:
        raise ValueError("Use a deadline between 0.1 and 3600 seconds")
    costs = (args.budget_usd, args.input_price, args.output_price, args.hourly_rate)
    if any(not math.isfinite(value) or value < 0 for value in costs) or args.budget_usd == 0:
        raise ValueError("Use finite nonnegative prices and a positive experiment budget")
    if any(
        not math.isfinite(value) or value <= 0 for value in (args.ttft_slo_ms, args.latency_slo_ms)
    ):
        raise ValueError("Latency targets must be finite and positive")
    Settings(model_base_url=args.base_url).validate()
    prompt = PROMPT_PREFIX * args.prefix_repeats
    upper_tokens = len(prompt.encode()) + 1024
    token_ceiling = (
        args.requests
        * (upper_tokens * args.input_price + args.max_tokens * args.output_price)
        / 1e6
    )
    compute_ceiling = args.hourly_rate * args.max_seconds / 3600
    if args.hourly_rate <= 0 and (args.input_price <= 0 or args.output_price <= 0):
        raise ValueError("Provide both token prices or an explicit self-hosted hourly rate")
    if token_ceiling + compute_ceiling > args.budget_usd:
        raise ValueError("Conservative planned cost exceeds the experiment budget")
    semaphore = asyncio.Semaphore(args.concurrency)
    started = time.perf_counter()
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:

        async def one(index):
            async with semaphore:
                return await stream_request(
                    client,
                    args.base_url,
                    args.model,
                    os.getenv("RR_MODEL_API_KEY", ""),
                    prompt + f"\nQuestion {index}: what should we compare?",
                    args.max_tokens,
                    min(60, args.max_seconds),
                )

        tasks = [asyncio.create_task(one(i)) for i in range(args.requests)]
        _, pending = await asyncio.wait(tasks, timeout=args.max_seconds)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        # Preserve completed measurements even when the overall experiment deadline fires.
        results = [
            {
                "ok": False,
                "latency_ms": None,
                "ttft_ms": None,
                "inter_chunk_ms": [],
                "usage": {},
                "error": "experiment_deadline",
            }
            if task.cancelled()
            else task.result()
            for task in tasks
        ]
    elapsed = time.perf_counter() - started
    successful = [r for r in results if r["ok"]]
    good = [
        r
        for r in successful
        if r["ttft_ms"] <= args.ttft_slo_ms and r["latency_ms"] <= args.latency_slo_ms
    ]
    return {
        "status": "measured",
        "schema": "robotreplay-benchmark-v1",
        "model": args.model,
        "workload_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "requests": args.requests,
        "concurrency": args.concurrency,
        "max_tokens": args.max_tokens,
        "prefix_repeats": args.prefix_repeats,
        "elapsed_seconds": elapsed,
        "completed": len(successful),
        "failed": len(results) - len(successful),
        "ttft_p50_ms": percentile([r["ttft_ms"] for r in successful], 0.5),
        "ttft_p95_ms": percentile([r["ttft_ms"] for r in successful], 0.95),
        "latency_p95_ms": percentile([r["latency_ms"] for r in successful], 0.95),
        "latency_goodput_rps": len(good) / elapsed,
        "slo": {"ttft_ms": args.ttft_slo_ms, "completion_ms": args.latency_slo_ms},
        "inter_chunk_mean_ms": statistics.mean([g for r in results for g in r["inter_chunk_ms"]])
        if any(r["inter_chunk_ms"] for r in results)
        else None,
        "planned_cost_ceiling_usd": token_ceiling + compute_ceiling,
        "measured_cost_usd": None,
        "results": results,
        "limitations": "Chunk gaps are not token ITL. Provider usage is recorded only when reported. "
        "Latency goodput excludes quality checks. Client timeout cannot stop GPU billing. "
        "Record server revision, tokenizer, GPU topology and actual billing separately.",
    }


def save_report(report, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
