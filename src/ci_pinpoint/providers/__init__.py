"""Model providers: OpenAI-compatible (OpenAI, Ollama, vLLM), Azure OpenAI, Anthropic, fake."""

from ci_pinpoint.providers.base import ChatModel, Completion, Embedder, Message, ProviderError
from ci_pinpoint.providers.factory import build_chat, build_embedder, resolve_provider

__all__ = [
    "ChatModel",
    "Completion",
    "Embedder",
    "Message",
    "ProviderError",
    "build_chat",
    "build_embedder",
    "resolve_provider",
]
