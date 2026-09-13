"""Unified provider client.

Every provider is reached over plain HTTP (requests):
- OpenAI-compatible chat/completions (Groq, NVIDIA NIM, OpenRouter, Cerebras, Mistral)
- Google Gemini generateContent REST API

This keeps one code path for all free tiers and avoids heavyweight SDKs.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import requests

from . import discovery
from .config import (
    PROVIDERS,
    Settings,
    get_settings,
    provider_api_key,
    provider_model,
)

Message = Dict[str, str]  # {"role": "...", "content": "..."}


class ProviderError(RuntimeError):
    """Raised when a provider call fails; ``status_code`` is set for HTTP failures."""

    def __init__(self, message: str, status_code: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _post_with_retry(
    url: str,
    headers: Dict[str, str],
    payload: dict,
    timeout: int,
    max_retries: int,
) -> dict:
    """POST with bounded retries on network errors and transient statuses.

    Retired-model and other permanent errors raise immediately with the
    status attached so the caller can decide whether to rediscover a model.
    """
    last_err: Optional[str] = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except requests.RequestException as exc:  # network hiccup
            last_err = f"network error: {exc}"
            discovery.sleep(discovery.retry_delay(attempt))
            continue
        if resp.status_code == 200:
            return resp.json()
        body = resp.text[:400]
        if discovery.is_transient(resp.status_code) and attempt < max_retries:
            last_err = f"HTTP {resp.status_code}: {body[:200]}"
            discovery.sleep(discovery.retry_delay(attempt, resp.headers.get("Retry-After")))
            continue
        raise ProviderError(f"HTTP {resp.status_code}: {body}", status_code=resp.status_code, body=body)
    raise ProviderError(f"provider failed after {max_retries} attempts: {last_err}")


def _with_model_recovery(cfg, call):
    """Run ``call(model_id)``; on a retired-model error rediscover once and retry.

    On a persistent transient error, try up to two sibling models from the
    vendor list before giving up, so one overloaded model does not block a key.
    """
    model_id = provider_model(cfg)
    try:
        return call(model_id)
    except ProviderError as exc:
        status = exc.status_code
        if status is not None and discovery.looks_like_retired_model(status, exc.body):
            produced: dict = {}

            def usable(candidate: str) -> bool:
                # The validation call is the real call: keep its result so a
                # usable candidate costs one request, not two.
                try:
                    produced[candidate] = call(candidate)
                    return True
                except ProviderError as probe_error:
                    return discovery.is_transient(probe_error.status_code or 0)

            replacement = discovery.discover(
                cfg.name, cfg.kind, cfg.base_url, provider_api_key(cfg), exclude=(model_id,), validate=usable
            )
            if replacement and replacement in produced:
                return produced[replacement]
            if replacement and replacement != model_id:
                return call(replacement)
            raise
        if status is not None and discovery.is_transient(status):
            for sibling in discovery.alternates(cfg.name, cfg.kind, cfg.base_url, provider_api_key(cfg), model_id):
                try:
                    return call(sibling)
                except ProviderError:
                    continue
        raise


def _call_openai_compatible(
    provider: str,
    messages: List[Message],
    settings: Settings,
    max_tokens: int,
    temperature: float,
) -> Tuple[str, int]:
    cfg = PROVIDERS[provider]
    headers = {
        "Authorization": f"Bearer {provider_api_key(cfg)}",
        "Content-Type": "application/json",
    }
    if provider == "openrouter":
        headers["HTTP-Referer"] = "https://freebuff.dev"
        headers["X-Title"] = "Free-tier Orchestrator"

    def call(model_id: str) -> dict:
        payload = {
            "model": model_id,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        return _post_with_retry(
            f"{cfg.base_url}/chat/completions",
            headers,
            payload,
            settings.request_timeout,
            settings.max_retries_per_call,
        )

    data = _with_model_recovery(cfg, call)
    try:
        text = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage", {})
        tokens = int(usage.get("total_tokens", _estimate(messages, text)))
    except (KeyError, IndexError) as exc:
        raise ProviderError(f"unexpected response from {provider}: {data}") from exc
    return text, tokens


def _call_gemini(
    provider: str,
    messages: List[Message],
    settings: Settings,
    max_tokens: int,
    temperature: float,
) -> Tuple[str, int]:
    cfg = PROVIDERS[provider]
    system_parts = [m["content"] for m in messages if m["role"] == "system"]
    contents = [
        {
            "role": "model" if m["role"] == "assistant" else "user",
            "parts": [{"text": m["content"]}],
        }
        for m in messages
        if m["role"] in ("user", "assistant")
    ]
    payload: dict = {"contents": contents}
    if system_parts:
        payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
    payload["generationConfig"] = {
        "maxOutputTokens": max_tokens,
        "temperature": temperature,
    }
    def call(model_id: str) -> dict:
        return _post_with_retry(
            f"{cfg.base_url}/models/{model_id}:generateContent",
            {"x-goog-api-key": provider_api_key(cfg), "Content-Type": "application/json"},
            payload,
            settings.request_timeout,
            settings.max_retries_per_call,
        )

    data = _with_model_recovery(cfg, call)
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError) as exc:
        raise ProviderError(f"unexpected response from gemini: {data}") from exc
    usage = data.get("usageMetadata", {})
    tokens = int(usage.get("totalTokenCount", _estimate(messages, text)))
    return text, tokens


def _estimate(messages: List[Message], output: str) -> int:
    total_chars = sum(len(m.get("content", "")) for m in messages) + len(output)
    return max(1, total_chars // 4)  # ~4 chars per token


def chat(
    provider: str,
    messages: List[Message],
    max_tokens: int = 4096,
    temperature: float = 0.2,
    settings: Optional[Settings] = None,
) -> Tuple[str, int]:
    """Send a chat completion to a provider. Returns (text, total_tokens)."""
    s = settings or get_settings()
    cfg = PROVIDERS[provider]
    key = provider_api_key(cfg)
    if not key:
        raise ProviderError(f"no API key configured for provider '{provider}'")
    try:
        if cfg.kind == "gemini":
            return _call_gemini(provider, messages, s, max_tokens, temperature)
        return _call_openai_compatible(provider, messages, s, max_tokens, temperature)
    except ProviderError as exc:
        # Never let a vendor echo of the credential escape into logs or the vault.
        message = str(exc).replace(key, "[REDACTED_SECRET]")
        raise ProviderError(message, status_code=exc.status_code, body=exc.body.replace(key, "[REDACTED_SECRET]")) from None
