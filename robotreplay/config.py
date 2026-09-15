import math
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(".data")
    demo: bool = False
    provider: str = "local"
    model: str = ""
    model_base_url: str = "https://api.tokenfactory.nebius.com/v1"
    model_api_key: str = ""
    model_timeout: float = 8.0
    input_usd_per_million: float = 0.0
    output_usd_per_million: float = 0.0
    gpu_hourly_rate: float = 0.0
    model_budget_usd: float = 0.0
    context_limit: int = 8192
    output_tokens: int = 256
    embedding_model: str = ""
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_usd_per_million: float = 0.0
    nemo_enabled: bool = False
    secure_cookies: bool = False
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "app", "testserver")
    otlp_endpoint: str = ""
    max_upload_bytes: int = 64 * 1024 * 1024
    max_duration: float = 180.0
    worker_timeout: float = 90.0
    worker_poll: float = 0.25
    retention_days: int = 30

    @classmethod
    def from_env(cls):
        values = {}
        for name, field in cls.__dataclass_fields__.items():
            value = os.getenv("RR_" + name.upper())
            if value is None:
                continue
            default = field.default
            if isinstance(default, bool):
                values[name] = value.lower() == "true"
            elif isinstance(default, Path):
                values[name] = Path(value)
            elif isinstance(default, tuple):
                values[name] = tuple(x.strip() for x in value.split(",") if x.strip())
            elif isinstance(default, (int, float)):
                values[name] = type(default)(value)
            else:
                values[name] = value
        settings = cls(**values)
        settings.validate()
        return settings

    def validate(self):
        if self.provider not in {"local", "nebius", "vllm"}:
            raise ValueError("RR_PROVIDER must be local, nebius, or vllm")
        if self.context_limit <= self.output_tokens or self.output_tokens < 32:
            raise ValueError("Context must leave room for output")
        prices = (
            self.model_budget_usd,
            self.gpu_hourly_rate,
            self.input_usd_per_million,
            self.output_usd_per_million,
            self.embedding_usd_per_million,
        )
        if any(not math.isfinite(value) or value < 0 for value in prices):
            raise ValueError("Budgets and prices must be finite and nonnegative")
        if not 0.1 <= self.model_timeout <= 60:
            raise ValueError("Model deadline must be between 0.1 and 60 seconds")
        if self.demo and self.provider != "local":
            raise ValueError("Demo mode cannot use an external model")
        for address in (self.model_base_url, self.embedding_base_url):
            if not address:
                continue
            parsed = urlparse(address)
            local = parsed.hostname in {"localhost", "127.0.0.1", "vllm"}
            if not parsed.hostname:
                raise ValueError("Provider URLs require a hostname")
            if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
                raise ValueError("Provider URLs require HTTPS, except explicit local model hosts")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("Provider URLs must not embed credentials or query parameters")
