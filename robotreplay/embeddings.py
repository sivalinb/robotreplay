import asyncio
import json
import math
import time
from urllib.parse import urlparse

import httpx

from robotreplay.policy import input_policy, redact
from robotreplay.provider import ModelUnavailable, token_count
from robotreplay.retrieval import reciprocal_rank_fusion


async def hybrid_retrieve(provider, team, clip_ids, question, lexical):
    settings, store = provider.settings, provider.store
    if settings.demo or not settings.embedding_model or not settings.embedding_base_url:
        raise ModelUnavailable("embeddings_unconfigured")
    if settings.embedding_usd_per_million <= 0:
        raise ModelUnavailable("embedding_pricing_unconfigured")
    try:
        api_key = settings.embedding_key()
    except ValueError:
        raise ModelUnavailable("embedding_credentials_unavailable") from None
    # No persistent cross-team embedding cache; deleting evidence removes future candidates.
    candidates = [
        e
        for cid in clip_ids
        for e in store.evidence(cid, team)
        if e["origin"] == "green-marker-v1"
        and e["kind"] != "visibility_gap"
        and input_policy(e["text"]).allowed
    ][:48]
    if not candidates:
        raise ModelUnavailable("no_exportable_evidence")
    inputs = [redact(question), *[e["text"] for e in candidates]]
    estimate = (
        (sum(len(text.encode()) for text in inputs) + 1024)
        * settings.embedding_usd_per_million
        / 1e6
    )
    reservation = store.reserve(team, estimate, settings.model_budget_usd)
    if not reservation:
        raise ModelUnavailable("budget_ceiling")
    started, outcome, charge, http_status = time.perf_counter(), "failed", None, None
    usage = {"input_tokens": None, "output_tokens": None}
    host = urlparse(settings.embedding_base_url).hostname
    vendor = {"api.tokenfactory.nebius.com": "nebius", "api.fireworks.ai": "fireworks"}.get(
        host, "custom"
    )
    with provider.telemetry.span("retrieval.embed_and_fuse", team):
        try:
            async with asyncio.timeout(settings.model_timeout):
                async with httpx.AsyncClient(
                    transport=provider.transport, trust_env=False, follow_redirects=False
                ) as client:
                    async with client.stream(
                        "POST",
                        settings.embedding_base_url.rstrip("/") + "/embeddings",
                        headers={"Authorization": "Bearer " + api_key} if api_key else {},
                        json={"model": settings.embedding_model, "input": inputs},
                    ) as response:
                        http_status = response.status_code
                        response.raise_for_status()
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > 5_000_000:
                                raise ValueError("embedding_response_limit")
                        body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("invalid_response_object")
            raw_usage = body.get("usage", {})
            tokens = (
                token_count(raw_usage.get("prompt_tokens")) if isinstance(raw_usage, dict) else None
            )
            if tokens is not None:
                charge = tokens * settings.embedding_usd_per_million / 1e6
                usage = {"input_tokens": tokens, "output_tokens": 0}
                store.settle(reservation, charge, tokens, 0)
            records = body["data"]
            if not isinstance(records, list) or len(records) != len(inputs):
                raise ValueError("embedding_count")
            vectors = {r["index"]: r["embedding"] for r in records}
            if set(vectors) != set(range(len(inputs))):
                raise ValueError("embedding_count")
            dimension = len(vectors[0])
            if not 1 <= dimension <= 16384:
                raise ValueError("embedding_dimensions")
            normalized = {}
            for index, vector in vectors.items():
                if len(vector) != dimension or not all(
                    isinstance(v, (int, float)) and math.isfinite(v) for v in vector
                ):
                    raise ValueError("invalid_embedding")
                norm = math.hypot(*vector)
                if norm == 0 or not math.isfinite(norm):
                    raise ValueError("empty_embedding")
                normalized[index] = [v / norm for v in vector]
            semantic = sorted(
                range(1, len(inputs)),
                key=lambda i: -sum(a * b for a, b in zip(normalized[0], normalized[i])),
            )
            fused = reciprocal_rank_fusion(
                [e["id"] for e in lexical], [candidates[i - 1]["id"] for i in semantic]
            )
            by_id = {e["id"]: e for e in [*lexical, *candidates]}
            outcome = "valid"
            return [by_id[eid] for eid in fused]
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError, OverflowError):
            outcome = "embedding_unavailable"
            raise ModelUnavailable("embedding_unavailable") from None
        finally:
            provider.telemetry.record_model(
                team,
                vendor,
                "embedding",
                outcome,
                (time.perf_counter() - started) * 1000,
                usage,
                charge,
                "configured_token_prices",
                http_status,
            )
