"""Build a chat model or embedder from settings and environment variables.

Provider selection when ``provider`` is ``auto``: ``ANTHROPIC_API_KEY`` ->
Anthropic, ``AZURE_OPENAI_ENDPOINT`` -> Azure OpenAI, ``OPENAI_API_KEY`` ->
OpenAI, ``CI_PINPOINT_BASE_URL`` -> any OpenAI-compatible server, otherwise a
local Ollama (``OLLAMA_HOST``, default ``http://localhost:11434``).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

import httpx

from ci_pinpoint.config import DEFAULT_MODELS, ConfigError, Settings
from ci_pinpoint.providers.anthropic import AnthropicChat
from ci_pinpoint.providers.base import ChatModel, Embedder
from ci_pinpoint.providers.fake import FakeChat, FakeEmbedder
from ci_pinpoint.providers.http import JsonHttpClient, RetryPolicy
from ci_pinpoint.providers.openai_compat import (
    AzureOpenAIChat,
    OpenAICompatibleChat,
    OpenAICompatibleEmbedder,
)

DEFAULT_AZURE_API_VERSION = "2024-10-21"
DEFAULT_OLLAMA_EMBED_MODEL = "nomic-embed-text"


def resolve_provider(settings: Settings, env: Mapping[str, str]) -> str:
    if settings.provider != "auto":
        return settings.provider
    if env.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if env.get("AZURE_OPENAI_ENDPOINT"):
        return "azure"
    if env.get("OPENAI_API_KEY"):
        return "openai"
    if settings.base_url:
        return "openai-compatible"
    return "ollama"


def ollama_base_url(env: Mapping[str, str]) -> str:
    host = env.get("OLLAMA_HOST", "http://localhost:11434").strip().rstrip("/")
    if not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    return f"{host}/v1"


def _require(env: Mapping[str, str], key: str, provider: str) -> str:
    value = env.get(key)
    if not value:
        raise ConfigError(f"provider {provider!r} needs {key} to be set")
    return value


def build_chat(
    settings: Settings,
    env: Mapping[str, str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ChatModel:
    env = os.environ if env is None else env
    provider = resolve_provider(settings, env)
    http = JsonHttpClient(
        timeout_s=settings.timeout_s,
        retry=RetryPolicy(max_retries=settings.max_retries),
        transport=transport,
    )
    if provider == "fake":
        return FakeChat()
    if provider == "anthropic":
        return AnthropicChat(
            api_key=_require(env, "ANTHROPIC_API_KEY", provider),
            model=settings.model or DEFAULT_MODELS["anthropic"],
            base_url=settings.base_url
            or env.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
            http=http,
        )
    if provider == "azure":
        endpoint = _require(env, "AZURE_OPENAI_ENDPOINT", provider)
        deployment = settings.model or _require(env, "AZURE_OPENAI_DEPLOYMENT", provider)
        return AzureOpenAIChat(
            endpoint=endpoint,
            deployment=deployment,
            api_key=_require(env, "AZURE_OPENAI_API_KEY", provider),
            api_version=env.get("AZURE_OPENAI_API_VERSION", DEFAULT_AZURE_API_VERSION),
            http=http,
        )
    if provider == "openai":
        return OpenAICompatibleChat(
            base_url=settings.base_url or env.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            model=settings.model or DEFAULT_MODELS["openai"],
            api_key=_require(env, "OPENAI_API_KEY", provider),
            provider="openai",
            http=http,
        )
    if provider == "openai-compatible":
        if not settings.base_url:
            raise ConfigError("provider 'openai-compatible' needs CI_PINPOINT_BASE_URL")
        if not settings.model:
            raise ConfigError("provider 'openai-compatible' needs CI_PINPOINT_MODEL")
        return OpenAICompatibleChat(
            base_url=settings.base_url,
            model=settings.model,
            api_key=env.get("CI_PINPOINT_API_KEY") or env.get("OPENAI_API_KEY"),
            provider="openai-compatible",
            http=http,
        )
    # ollama
    return OpenAICompatibleChat(
        base_url=settings.base_url or ollama_base_url(env),
        model=settings.model or DEFAULT_MODELS["ollama"],
        api_key=None,
        provider="ollama",
        http=http,
    )


def resolve_embedder(settings: Settings, env: Mapping[str, str]) -> str:
    """``auto`` follows the chat provider: Ollama and OpenAI have embedding
    endpoints we can use without extra configuration; the others don't."""
    if settings.embedder != "auto":
        return settings.embedder
    chat = resolve_provider(settings, env)
    return chat if chat in ("ollama", "openai") else "none"


def build_embedder(
    settings: Settings,
    env: Mapping[str, str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> Embedder | None:
    """The embedder for block ranking, or ``None`` when ranking is off.

    In ``auto`` mode a missing embedding model is not an error (ranking falls
    back to heuristics), so calls are not retried to keep that fallback fast.
    """
    env = os.environ if env is None else env
    kind = resolve_embedder(settings, env)
    retries = 0 if settings.embedder == "auto" else settings.max_retries
    http = JsonHttpClient(
        timeout_s=min(settings.timeout_s, 60.0),
        retry=RetryPolicy(max_retries=retries),
        transport=transport,
    )
    model = settings.embed_model
    if kind == "none":
        return None
    if kind == "fake":
        return FakeEmbedder()
    if kind == "openai":
        return OpenAICompatibleEmbedder(
            base_url=env.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            model=model or "text-embedding-3-small",
            api_key=_require(env, "OPENAI_API_KEY", "openai"),
            http=http,
        )
    if kind == "openai-compatible":
        if not model:
            raise ConfigError("embedder 'openai-compatible' needs CI_PINPOINT_EMBED_MODEL")
        return OpenAICompatibleEmbedder(
            base_url=_require(env, "CI_PINPOINT_EMBED_BASE_URL", "openai-compatible"),
            model=model,
            api_key=env.get("CI_PINPOINT_API_KEY"),
            http=http,
        )
    return OpenAICompatibleEmbedder(
        base_url=ollama_base_url(env), model=model or DEFAULT_OLLAMA_EMBED_MODEL, http=http
    )
