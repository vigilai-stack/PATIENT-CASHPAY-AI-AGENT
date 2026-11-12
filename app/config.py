"""Settings from environment variables. Secrets never live in files."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SANDBOX_BASE = "https://api-m.sandbox.paypal.com"
# Model id from the OpenAI model page: Chat Completions and structured outputs,
# and no deprecated or shutdown status. See docs/openai.md for the retrieved pages.
DEFAULT_OPENAI_MODEL = "gpt-6-luna"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
_MODEL_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")


class LiveEndpointRefused(RuntimeError):
    """Raised when someone points the app at live PayPal. Sandbox only."""


@dataclass(frozen=True)
class Settings:
    database_url: str
    payment_mode: str  # "fake" or "sandbox"
    paypal_client_id: str
    paypal_client_secret: str = field(repr=False)
    paypal_webhook_id: str
    paypal_base_url: str
    session_secret: str = field(repr=False)
    fake_webhook_key: str = field(repr=False)
    fhir_base_url: str
    llm_mode: str
    openai_api_key: str = field(repr=False)
    openai_model: str
    openai_base_url: str
    openai_timeout_seconds: float
    openai_max_output_tokens: int
    openai_max_requests_per_minute: int
    openai_max_concurrency: int

    @property
    def sandbox_ready(self) -> bool:
        return bool(self.paypal_client_id and self.paypal_client_secret)

    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_api_key)


def load_settings() -> Settings:
    base = os.getenv("PAYPAL_BASE_URL", SANDBOX_BASE).rstrip("/")
    if "sandbox" not in base:
        raise LiveEndpointRefused(f"Refusing to start: PAYPAL_BASE_URL must be the sandbox ({SANDBOX_BASE}).")
    client_id = os.getenv("PAYPAL_CLIENT_ID", "")
    secret = os.getenv("PAYPAL_CLIENT_SECRET", "")
    mode = os.getenv("PAYMENT_MODE", "").strip().lower()
    if mode not in ("fake", "sandbox"):
        mode = "sandbox" if (client_id and secret) else "fake"
    if mode == "sandbox" and not (client_id and secret):
        mode = "fake"
    llm_mode = os.getenv("LLM_MODE", "mock").strip().lower()
    if llm_mode not in ("mock", "openai"):
        llm_mode = "mock"
    model = os.getenv("OPENAI_MODEL", "").strip() or DEFAULT_OPENAI_MODEL
    if not _MODEL_NAME.fullmatch(model):
        model = DEFAULT_OPENAI_MODEL
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if any(ch.isspace() for ch in api_key):
        api_key = ""
    return Settings(
        database_url=os.getenv("DATABASE_URL", f"sqlite:///{ROOT / 'cashpay.db'}"),
        payment_mode=mode,
        paypal_client_id=client_id,
        paypal_client_secret=secret,
        paypal_webhook_id=os.getenv("PAYPAL_WEBHOOK_ID", ""),
        paypal_base_url=base,
        session_secret=os.getenv("SESSION_SECRET", "local-demo-session-secret"),
        fake_webhook_key=os.getenv("FAKE_WEBHOOK_KEY", "local-demo-webhook-key"),
        fhir_base_url=os.getenv("FHIR_BASE_URL", "").rstrip("/"),
        llm_mode=llm_mode,
        openai_api_key=api_key,
        openai_model=model,
        openai_base_url=os.getenv("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL).strip() or DEFAULT_OPENAI_BASE_URL,
        openai_timeout_seconds=_env_float("OPENAI_TIMEOUT_SECONDS", 8.0, 1.0, 20.0),
        openai_max_output_tokens=_env_int("OPENAI_MAX_OUTPUT_TOKENS", 120, 32, 256),
        openai_max_requests_per_minute=_env_int("OPENAI_MAX_REQUESTS_PER_MINUTE", 12, 1, 60),
        openai_max_concurrency=_env_int("OPENAI_MAX_CONCURRENCY", 2, 1, 4),
    )


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return min(hi, max(lo, value))


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return min(hi, max(lo, value))


@lru_cache
def load_policy(path: str | None = None) -> dict:
    p = Path(path) if path else ROOT / "config" / "clinic_policy.yaml"
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)
