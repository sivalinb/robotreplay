import math
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

PROVIDER_URLS = {
    "nebius": "https://api.tokenfactory.nebius.com/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "vllm": "http://127.0.0.1:8001/v1",
}


def read_key(value, filename):
    """Read an operator-configured key without copying it into artifacts or exception text."""
    if value and filename:
        raise ValueError("Configure a key value or a key file, not both")
    if filename:
        try:
            fd = os.open(Path(filename).expanduser(), os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError("invalid_key_file")
                value = source.read(8193).decode("ascii").strip()
        except (OSError, UnicodeError, ValueError):
            raise ValueError(
                "Key file must be a readable regular file containing one token"
            ) from None
    if value and (len(value) > 8192 or not re.fullmatch(r"[A-Za-z0-9._~+/=-]+", value)):
        raise ValueError("API key must contain one token")
    return value


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(".data")
    demo: bool = False
    provider: str = "local"
    model: str = ""
    model_base_url: str = ""
    model_api_key: str = field(default="", repr=False)
    model_api_key_file: str = field(default="", repr=False)
    model_timeout: float = 8.0
    input_usd_per_million: float = 0.0
    output_usd_per_million: float = 0.0
    cached_input_usd_per_million: float | None = None
    gpu_hourly_rate: float = 0.0
    model_budget_usd: float = 0.0
    context_limit: int = 8192
    output_tokens: int = 256
    embedding_model: str = ""
    embedding_base_url: str = ""
    embedding_api_key: str = field(default="", repr=False)
    embedding_api_key_file: str = field(default="", repr=False)
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

    @property
    def resolved_model_url(self):
        return self.model_base_url or PROVIDER_URLS.get(self.provider, "")

    def model_key(self):
        return read_key(self.model_api_key, self.model_api_key_file)

    def embedding_key(self):
        return read_key(self.embedding_api_key, self.embedding_api_key_file)

    @classmethod
    def from_env(cls):
        values = {}
        for name, config_field in cls.__dataclass_fields__.items():
            value = os.getenv("RR_" + name.upper())
            if value is None:
                continue
            default = config_field.default
            if name == "cached_input_usd_per_million":
                values[name] = float(value) if value else None
            elif isinstance(default, bool):
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
        if self.provider not in {"local", "nebius", "fireworks", "vllm"}:
            raise ValueError("RR_PROVIDER must be local, nebius, fireworks, or vllm")
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
        if self.cached_input_usd_per_million is not None and (
            not math.isfinite(self.cached_input_usd_per_million)
            or not 0 <= self.cached_input_usd_per_million <= self.input_usd_per_million
        ):
            raise ValueError("Cached input price must be between zero and the input price")
        if not 0.1 <= self.model_timeout <= 60:
            raise ValueError("Model deadline must be between 0.1 and 60 seconds")
        if self.demo and self.provider != "local":
            raise ValueError("Demo mode cannot use an external model")
        if self.provider in {"nebius", "fireworks"} and (
            self.resolved_model_url.rstrip("/") != PROVIDER_URLS[self.provider]
        ):
            raise ValueError("Hosted provider URL must match the selected provider")
        if self.provider == "fireworks" and self.model and not self.model.startswith("accounts/"):
            raise ValueError("Fireworks requires a full accounts/.../models/... model name")
        for value, filename in (
            (self.model_api_key, self.model_api_key_file),
            (self.embedding_api_key, self.embedding_api_key_file),
        ):
            if value and filename:
                raise ValueError("Configure a key value or a key file, not both")
        for address in (self.resolved_model_url, self.embedding_base_url):
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
