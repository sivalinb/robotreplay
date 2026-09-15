import csv
import io
import shutil
import subprocess

from pydantic import BaseModel, ConfigDict, Field


class ContextPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_tokens: int = Field(ge=1, le=1_000_000)
    output_tokens: int = Field(ge=1, le=100_000)
    context_limit: int = Field(ge=1, le=2_000_000)
    sequences: int = Field(default=1, ge=1, le=1000)
    layers: int = Field(default=32, ge=1, le=256)
    kv_heads: int = Field(default=8, ge=1, le=256)
    head_dim: int = Field(default=128, ge=1, le=1024)
    dtype: str = "fp16"


def context_plan(plan):
    if plan.dtype not in {"fp16", "fp8"}:
        raise ValueError("unsupported_dtype")
    total = plan.input_tokens + plan.output_tokens
    memory = (
        2
        * plan.layers
        * plan.kv_heads
        * plan.head_dim
        * total
        * plan.sequences
        * (2 if plan.dtype == "fp16" else 1)
    )
    return {
        "admission": "fits_context" if total <= plan.context_limit else "hold",
        "message": f"{total:,} tokens requested against a {plan.context_limit:,}-token native limit. "
        + (
            "Verify VRAM fit and answer quality."
            if total <= plan.context_limit
            else "Retrieve fewer passages or lower the output reserve."
        ),
        "kv_gib": memory / 2**30,
        "assumptions": "Analytical estimate for full-attention KV state only. Excludes model weights, "
        "runtime buffers, block rounding, quantization scales, sliding windows and "
        "architecture-specific compression. Actual model and kernels must support the dtype.",
    }


def gpu_snapshot():
    binary = shutil.which("nvidia-smi")
    if not binary:
        return {
            "status": "unavailable",
            "reason": "No local NVIDIA driver tool. GPU values are unknown.",
        }
    try:
        result = subprocess.run(
            [
                binary,
                "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            timeout=3,
            check=True,
        )
        devices = []
        for row in csv.reader(io.StringIO(result.stdout.decode())):
            devices.append(
                {
                    "name": row[0].strip(),
                    "utilization_pct": float(row[1]),
                    "memory_used_mib": float(row[2]),
                    "memory_total_mib": float(row[3]),
                }
            )
        return {"status": "available", "source": "nvidia-smi", "devices": devices}
    except (subprocess.SubprocessError, ValueError, IndexError):
        return {
            "status": "unavailable",
            "reason": "GPU query failed; no healthy value is inferred.",
        }
