"""Central configuration for the free-tier LLM orchestrator.

All API keys are read from environment variables (never hardcoded).
Add them via your environment / .env file / Freebuff Keys tab.
The orchestrator only uses providers whose key is present.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict

try:  # optional dev convenience
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class ProviderConfig:
    name: str                      # registry id, e.g. "groq"
    label: str                     # human-readable
    env_key: str                   # env var holding the API key
    base_url: str                  # OpenAI-compatible base or Gemini root
    kind: str = "openai"           # "openai" | "gemini"
    default_model: str = ""
    model_env: str = ""            # env var to override default model
    rpm_limit: int = 25            # requests / minute (conservative free-tier)
    tpm_limit: int = 100_000       # tokens / minute
    context_window: int = 32_768
    strengths: tuple = ()          # task types this provider excels at
    priority: int = 50             # lower = preferred on ties


PROVIDERS: Dict[str, ProviderConfig] = {
    "gemini": ProviderConfig(
        name="gemini",
        label="Google Gemini (AI Studio)",
        env_key="GEMINI_API_KEY",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        kind="gemini",
        default_model="gemini-2.0-flash",
        model_env="GEMINI_MODEL",
        rpm_limit=15,
        tpm_limit=1_000_000,
        context_window=1_000_000,
        strengths=("context_load", "chat"),
        priority=10,
    ),
    "groq": ProviderConfig(
        name="groq",
        label="Groq (LPU, ultra-fast)",
        env_key="GROQ_API_KEY",
        base_url="https://api.groq.com/openai/v1",
        default_model="llama-3.3-70b-versatile",
        model_env="GROQ_MODEL",
        rpm_limit=28,
        tpm_limit=60_000,
        context_window=128_000,
        strengths=("code_patch", "quick_text", "chat"),
        priority=20,
    ),
    "nvidia": ProviderConfig(
        name="nvidia",
        label="NVIDIA NIM (DeepSeek-class reasoning)",
        env_key="NVIDIA_API_KEY",
        base_url="https://integrate.api.nvidia.com/v1",
        default_model="deepseek-ai/deepseek-r1",
        model_env="NVIDIA_MODEL",
        rpm_limit=20,
        tpm_limit=80_000,
        context_window=64_000,
        strengths=("reasoning", "test_fix"),
        priority=30,
    ),
    "openrouter": ProviderConfig(
        name="openrouter",
        label="OpenRouter (free model pool)",
        env_key="OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api/v1",
        default_model="qwen/qwen-2.5-coder-32b-instruct:free",
        model_env="OPENROUTER_MODEL",
        rpm_limit=18,
        tpm_limit=60_000,
        context_window=64_000,
        strengths=("code_patch", "reasoning"),
        priority=40,
    ),
    "cerebras": ProviderConfig(
        name="cerebras",
        label="Cerebras (fastest inference)",
        env_key="CEREBRAS_API_KEY",
        base_url="https://api.cerebras.ai/v1",
        default_model="llama-3.3-70b",
        model_env="CEREBRAS_MODEL",
        rpm_limit=25,
        tpm_limit=60_000,
        context_window=64_000,
        strengths=("quick_text", "code_patch"),
        priority=35,
    ),
    "mistral": ProviderConfig(
        name="mistral",
        label="Mistral (free experiment tier)",
        env_key="MISTRAL_API_KEY",
        base_url="https://api.mistral.ai/v1",
        default_model="open-mistral-nemo",
        model_env="MISTRAL_MODEL",
        rpm_limit=15,
        tpm_limit=50_000,
        context_window=32_000,
        strengths=("chat", "quick_text"),
        priority=45,
    ),
}


def provider_api_key(cfg: ProviderConfig) -> str:
    return _env(cfg.env_key)


def provider_model(cfg: ProviderConfig) -> str:
    override = _env(cfg.model_env) if cfg.model_env else ""
    return override or cfg.default_model


@dataclass
class Settings:
    """Runtime settings with env overrides."""

    max_test_rounds: int = 3          # pytest feedback-loop attempts per code step
    max_retries_per_call: int = 3     # provider HTTP retries
    repo_ingest_budget: int = 700_000 # ~tokens of repo context fed to big models
    memory_path: str = ".orchestrator/memory.json"
    staging_root: str = ".orchestrator/worktrees"
    request_timeout: int = 120
    enabled: Dict[str, bool] = field(default_factory=dict)

    def providers_available(self) -> list:
        out = []
        for name, cfg in PROVIDERS.items():
            if self.enabled.get(name, True) and provider_api_key(cfg):
                out.append(name)
        return out


def get_settings() -> Settings:
    s = Settings()
    for name in PROVIDERS:
        flag = _env(f"ENABLE_{name.upper()}")
        if flag:
            s.enabled[name] = flag.lower() in ("1", "true", "yes")
    return s
