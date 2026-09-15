import asyncio
import json
import math

import httpx

from robotreplay.policy import input_policy, redact
from robotreplay.provider import ModelUnavailable
from robotreplay.retrieval import reciprocal_rank_fusion


async def hybrid_retrieve(provider, team, clip_ids, question, lexical):
    settings, store = provider.settings, provider.store
    if settings.demo or not settings.embedding_model or not settings.embedding_base_url:
        raise ModelUnavailable("embeddings_unconfigured")
    if settings.embedding_usd_per_million <= 0:
        raise ModelUnavailable("embedding_pricing_unconfigured")
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
    with provider.telemetry.span("retrieval.embed_and_fuse", team):
        try:
            async with asyncio.timeout(settings.model_timeout):
                async with httpx.AsyncClient(
                    transport=provider.transport, trust_env=False, follow_redirects=False
                ) as client:
                    async with client.stream(
                        "POST",
                        settings.embedding_base_url.rstrip("/") + "/embeddings",
                        headers={"Authorization": "Bearer " + settings.embedding_api_key},
                        json={"model": settings.embedding_model, "input": inputs},
                    ) as response:
                        response.raise_for_status()
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > 5_000_000:
                                raise ValueError("embedding_response_limit")
                        body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("invalid_response_object")
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
            usage = body.get("usage", {})
            tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
            if type(tokens) is int and 0 <= tokens < 1_000_000:
                store.settle(
                    reservation, tokens * settings.embedding_usd_per_million / 1e6, tokens, 0
                )
            return [by_id[eid] for eid in fused]
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError, OverflowError):
            raise ModelUnavailable("embedding_unavailable") from None
