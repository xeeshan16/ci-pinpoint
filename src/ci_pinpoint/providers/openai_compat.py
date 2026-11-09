"""OpenAI-style chat and embeddings: OpenAI, Azure OpenAI, Ollama, vLLM, LM Studio, ...

Ollama and vLLM both expose ``/v1/chat/completions`` and ``/v1/embeddings``,
so one implementation covers hosted and local models.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from typing import Any

from ci_pinpoint.models import TokenUsage
from ci_pinpoint.providers.base import Completion, Message, ProviderError
from ci_pinpoint.providers.http import JsonHttpClient

log = logging.getLogger(__name__)


def _usage(data: dict[str, Any]) -> TokenUsage:
    usage = data.get("usage") or {}
    return TokenUsage(
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
    )


class OpenAICompatibleChat:
    """``POST {base_url}/chat/completions``."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        provider: str = "openai-compatible",
        http: JsonHttpClient | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._provider = provider
        self._http = http or JsonHttpClient()
        self._extra_headers = extra_headers or {}
        # Flipped off the first time a server rejects json_schema response formats.
        self._schema_supported = True

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model(self) -> str:
        return self._model

    def _url(self) -> str:
        return f"{self._base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = dict(self._extra_headers)
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _params(self) -> dict[str, str] | None:
        return None

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._http.post_json(
            self._url(),
            payload,
            headers=self._headers(),
            params=self._params(),
            what=f"{self._provider} chat ({self._model})",
        )

    def complete(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        json_mode: bool = False,
        json_schema: Mapping[str, Any] | None = None,
    ) -> Completion:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        use_schema = json_schema is not None and self._schema_supported
        if use_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "strict": True,
                    "schema": dict(json_schema or {}),
                },
            }
        elif json_mode or json_schema is not None:
            payload["response_format"] = {"type": "json_object"}
        started = time.perf_counter()
        try:
            data = self._post(payload)
        except ProviderError as exc:
            if not (use_schema and exc.status in (400, 422)):
                raise
            # Older servers and some gateways reject json_schema: fall back to JSON mode.
            log.info(
                "server rejected json_schema, falling back to json_object",
                extra={"provider": self._provider, "error": str(exc)},
            )
            self._schema_supported = False
            payload["response_format"] = {"type": "json_object"}
            data = self._post(payload)
        latency = time.perf_counter() - started
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"{self._provider} returned no choices: {str(data)[:200]}") from exc
        return Completion(
            text=text,
            model=str(data.get("model") or self._model),
            usage=_usage(data),
            latency_s=latency,
            finish_reason=choice.get("finish_reason"),
        )


class AzureOpenAIChat(OpenAICompatibleChat):
    """Azure OpenAI: deployment in the path, ``api-key`` header, ``api-version`` query."""

    def __init__(
        self,
        *,
        endpoint: str,
        deployment: str,
        api_key: str,
        api_version: str,
        http: JsonHttpClient | None = None,
    ) -> None:
        super().__init__(
            base_url=f"{endpoint.rstrip('/')}/openai/deployments/{deployment}",
            model=deployment,
            api_key=None,
            provider="azure",
            http=http,
            extra_headers={"api-key": api_key},
        )
        self._api_version = api_version

    def _params(self) -> dict[str, str] | None:
        return {"api-version": self._api_version}


class OpenAICompatibleEmbedder:
    """``POST {base_url}/embeddings``; works with OpenAI, Ollama and vLLM."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        http: JsonHttpClient | None = None,
        batch_size: int = 64,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._http = http or JsonHttpClient()
        self._batch_size = batch_size

    @property
    def model(self) -> str:
        return self._model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = list(texts[start : start + self._batch_size])
            data = self._http.post_json(
                f"{self._base_url}/embeddings",
                {"model": self._model, "input": batch},
                headers=headers,
                what=f"embeddings ({self._model})",
            )
            items = sorted(data.get("data") or [], key=lambda item: item.get("index", 0))
            if len(items) != len(batch):
                raise ProviderError(
                    f"embeddings returned {len(items)} vectors for {len(batch)} inputs"
                )
            vectors.extend([float(x) for x in item["embedding"]] for item in items)
        return vectors
