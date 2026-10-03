"""Thin, provider-agnostic LLM clients: Google Gemini (free tier, via its OpenAI-compatible
endpoint), the Anthropic Messages API, and any other OpenAI-compatible chat endpoint
(OpenAI, Groq, OpenRouter).

No SDKs: plain HTTPS keeps the dependency surface small and the retry policy explicit.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from ..config import Settings, settings as default_settings
from ..logging_setup import agent_log


class LLMError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    attempts: int = 1


class BaseClient:
    provider = "base"

    def __init__(self, model: str, timeout: float = 90, max_retries: int = 2):
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries

    def _call(self, system: str, messages: list[dict]) -> LLMResponse:  # pragma: no cover - abstract
        raise NotImplementedError

    def complete(self, system: str, messages: list[dict]) -> LLMResponse:
        attempt = 0
        while True:
            attempt += 1
            started = time.monotonic()
            try:
                resp = self._call(system, messages)
                resp.latency_ms = int((time.monotonic() - started) * 1000)
                resp.attempts = attempt
                agent_log.info("llm_call", extra={"provider": self.provider, "model": resp.model,
                                                  "latency_ms": resp.latency_ms, "attempt": attempt,
                                                  "input_tokens": resp.input_tokens,
                                                  "output_tokens": resp.output_tokens})
                return resp
            except httpx.TimeoutException as exc:
                err = LLMError(f"{self.provider} request timed out after {self.timeout:.0f}s", retryable=True)
                err.__cause__ = exc
            except httpx.HTTPError as exc:
                err = LLMError(f"{self.provider} network error: {exc}", retryable=True)
            except LLMError as exc:
                err = exc
            agent_log.warning("llm_call_failed", extra={"provider": self.provider, "attempt": attempt,
                                                        "error": str(err), "retryable": err.retryable})
            if not err.retryable or attempt > self.max_retries:
                raise err
            time.sleep(min(8.0, 1.5 * attempt))

    @staticmethod
    def _raise_for(resp: httpx.Response, provider: str) -> None:
        if resp.status_code < 400:
            return
        body = resp.text[:300]
        retryable = resp.status_code in (408, 409, 425, 429) or resp.status_code >= 500
        if resp.status_code in (401, 403):
            raise LLMError(f"{provider} rejected the API key (HTTP {resp.status_code})")
        raise LLMError(f"{provider} returned HTTP {resp.status_code}: {body}", retryable=retryable)


class AnthropicClient(BaseClient):
    provider = "anthropic"

    def __init__(self, api_key: str, model: str, base_url: str = "", **kw):
        super().__init__(model, **kw)
        self.api_key = api_key
        self.url = (base_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages"

    def _call(self, system: str, messages: list[dict]) -> LLMResponse:
        r = httpx.post(self.url, timeout=self.timeout, headers={
            "x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json",
        }, json={"model": self.model, "max_tokens": 6000, "temperature": 0, "system": system, "messages": messages})
        self._raise_for(r, self.provider)
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        usage = data.get("usage", {})
        if data.get("stop_reason") == "max_tokens":
            raise LLMError("Model output was cut off at the token limit", retryable=False)
        return LLMResponse(text=text, model=data.get("model", self.model),
                           input_tokens=usage.get("input_tokens", 0), output_tokens=usage.get("output_tokens", 0))


GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"


class OpenAICompatibleClient(BaseClient):
    provider = "openai"

    def __init__(self, api_key: str, model: str, base_url: str = "", provider: str = "openai", **kw):
        super().__init__(model, **kw)
        self.provider = provider
        self.api_key = api_key
        self.url = (base_url or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"

    def _call(self, system: str, messages: list[dict]) -> LLMResponse:
        r = httpx.post(self.url, timeout=self.timeout, headers={
            "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
        }, json={"model": self.model, "temperature": 0, "response_format": {"type": "json_object"},
                 "messages": [{"role": "system", "content": system}, *messages]})
        self._raise_for(r, self.provider)
        data = r.json()
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Unexpected response shape: {json.dumps(data)[:200]}") from exc
        if choice.get("finish_reason") == "length":
            raise LLMError("Model output was cut off at the token limit")
        usage = data.get("usage", {})
        return LLMResponse(text=text, model=data.get("model", self.model),
                           input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0))


class MockClient(BaseClient):
    """Offline client for tests and local development. ``handler`` maps
    (system, messages) to response text; by default it answers like a careful analyst
    using the deterministic fallback, so the full pipeline runs without a key."""
    provider = "mock"

    def __init__(self, handler: Callable[[str, list[dict]], str] | None = None, model: str = "mock-analyst", **kw):
        super().__init__(model, **kw)
        self.handler = handler
        self.calls: list[list[dict]] = []

    def _call(self, system: str, messages: list[dict]) -> LLMResponse:
        self.calls.append(messages)
        if self.handler:
            return LLMResponse(text=self.handler(system, messages), model=self.model)
        from .fallback import build_fallback
        from .prompts import extract_context
        ctx = extract_context(messages[0]["content"])
        out = build_fallback(ctx.get("calculation"), ctx)
        out["case_summary"] = "[mock model] " + out["case_summary"]
        return LLMResponse(text=json.dumps(out), model=self.model)


def get_client(cfg: Settings | None = None) -> BaseClient:
    cfg = cfg or default_settings
    kw = {"timeout": cfg.llm_timeout_s, "max_retries": cfg.llm_max_retries}
    if cfg.llm_provider == "mock":
        return MockClient(model=cfg.resolved_model, **kw)
    if not cfg.llm_api_key:
        raise LLMError(f"LLM_API_KEY is not set for provider '{cfg.llm_provider}'")
    if cfg.llm_provider == "gemini":
        return OpenAICompatibleClient(cfg.llm_api_key, cfg.resolved_model,
                                      cfg.llm_base_url or GEMINI_OPENAI_BASE_URL, provider="gemini", **kw)
    if cfg.llm_provider == "anthropic":
        return AnthropicClient(cfg.llm_api_key, cfg.resolved_model, cfg.llm_base_url, **kw)
    if cfg.llm_provider == "openai":
        return OpenAICompatibleClient(cfg.llm_api_key, cfg.resolved_model, cfg.llm_base_url, **kw)
    raise LLMError(f"Unknown LLM_PROVIDER '{cfg.llm_provider}' (use gemini, anthropic, openai or mock)")
