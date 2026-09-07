"""Unified provider client.

Every provider is reached over plain HTTP (requests):
- OpenAI-compatible chat/completions (Groq, NVIDIA NIM, OpenRouter, Cerebras, Mistral)
- Google Gemini generateContent REST API

This keeps one code path for all free tiers and avoids heavyweight SDKs.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

import requests

from .config import (
    PROVIDERS,
    Settings,
    get_settings,
    provider_api_key,
    provider_model,
)

Message = Dict[str, str]  # {"role": "...", "content": "..."}


class ProviderError(RuntimeError):
    pass


def _post_with_retry(
    url: str,
    headers: Dict[str, str],
    payload: dict,
    timeout: int,
    max_retries: int,
) -> dict:
    last_err: Optional[str] = None
    for attempt in range(max_retries):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except requests.RequestException as exc:  # network hiccup
            last_err = f"network error: {exc}"
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:  # rate limited: back off and retry
            last_err = f"429 rate limited: {resp.text[:200]}"
            time.sleep(min(2 ** attempt * 2, 30))
            continue
        raise ProviderError(f"HTTP {resp.status_code}: {resp.text[:400]}")
    raise ProviderError(f"provider failed after {max_retries} attempts: {last_err}")


def _call_openai_compatible(
    provider: str,
    messages: List[Message],
    settings: Settings,
    max_tokens: int,
    temperature: float,
) -> Tuple[str, int]:
    cfg = PROVIDERS[provider]
    payload = {
        "model": provider_model(cfg),
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    headers = {
        "Authorization": f"Bearer {provider_api_key(cfg)}",
        "Content-Type": "application/json",
    }
    if provider == "openrouter":
        headers["HTTP-Referer"] = "https://freebuff.dev"
        headers["X-Title"] = "Free-tier Orchestrator"
    data = _post_with_retry(
        f"{cfg.base_url}/chat/completions",
        headers,
        payload,
        settings.request_timeout,
        settings.max_retries_per_call,
    )
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
    data = _post_with_retry(
        f"{cfg.base_url}/models/{provider_model(cfg)}:generateContent",
        {"x-goog-api-key": provider_api_key(cfg), "Content-Type": "application/json"},
        payload,
        settings.request_timeout,
        settings.max_retries_per_call,
    )
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
    if not provider_api_key(cfg):
        raise ProviderError(f"no API key configured for provider '{provider}'")
    if cfg.kind == "gemini":
        return _call_gemini(provider, messages, s, max_tokens, temperature)
    return _call_openai_compatible(provider, messages, s, max_tokens, temperature)
