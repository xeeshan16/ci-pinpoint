"""Provider interfaces for chat models and embedders.

Everything above this layer depends only on these protocols, so adding a
provider means adding one class and one branch in :mod:`.factory`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from ci_pinpoint.models import TokenUsage

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    model: str
    usage: TokenUsage
    latency_s: float
    finish_reason: str | None = None


class ProviderError(RuntimeError):
    """A provider call failed after retries, or the provider is misconfigured."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@runtime_checkable
class ChatModel(Protocol):
    """A chat-completion model. Implementations must be safe to call from one thread at a time."""

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    def complete(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        json_mode: bool = False,
    ) -> Completion: ...


@runtime_checkable
class Embedder(Protocol):
    @property
    def model(self) -> str: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...
