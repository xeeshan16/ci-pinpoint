"""Anthropic Messages API."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from ci_pinpoint.models import TokenUsage
from ci_pinpoint.providers.base import Completion, Message, ProviderError
from ci_pinpoint.providers.http import JsonHttpClient

ANTHROPIC_VERSION = "2023-06-01"


class AnthropicChat:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.anthropic.com",
        http: JsonHttpClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._http = http or JsonHttpClient()

    @property
    def provider(self) -> str:
        return "anthropic"

    @property
    def model(self) -> str:
        return self._model

    def complete(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        json_mode: bool = False,
    ) -> Completion:
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        if json_mode:
            system = f"{system}\n\nRespond with a single JSON object and nothing else.".strip()
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": m.role, "content": m.content} for m in messages if m.role != "system"
            ],
        }
        if system:
            payload["system"] = system
        started = time.perf_counter()
        data = self._http.post_json(
            f"{self._base_url}/v1/messages",
            payload,
            headers={"x-api-key": self._api_key, "anthropic-version": ANTHROPIC_VERSION},
            what=f"anthropic messages ({self._model})",
        )
        latency = time.perf_counter() - started
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise ProviderError(f"anthropic returned no content: {str(data)[:200]}")
        text = "".join(
            b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
        )
        usage = data.get("usage") or {}
        return Completion(
            text=text,
            model=str(data.get("model") or self._model),
            usage=TokenUsage(
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
            ),
            latency_s=latency,
            finish_reason=data.get("stop_reason"),
        )
