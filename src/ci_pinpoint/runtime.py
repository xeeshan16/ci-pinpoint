"""Build what a diagnosis needs (chat model, rankers, caches) from settings, once.

Shared by the CLI and the MCP server so both resolve providers, caches and
ranking the same way.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ci_pinpoint.cache import BriefCache, CachedEmbedder, EmbeddingStore, default_cache_dir
from ci_pinpoint.config import Settings
from ci_pinpoint.providers import ChatModel, build_chat, build_embedder
from ci_pinpoint.ranking import EmbeddingRanker
from ci_pinpoint.rerank import ChainRanker, LLMRanker, NamedRanker

log = logging.getLogger(__name__)


def _open_store(root: Path) -> EmbeddingStore | None:
    """The embedding cache, or None if the cache directory is not writable."""
    try:
        return EmbeddingStore(root / "embeddings.sqlite3")
    except (OSError, sqlite3.Error) as exc:
        log.warning("embedding cache disabled", extra={"path": str(root), "error": str(exc)})
        return None


@dataclass
class Runtime:
    settings: Settings
    chat: ChatModel | None
    ranker: NamedRanker | None
    cache: BriefCache | None
    store: EmbeddingStore | None = None

    def close(self) -> None:
        if self.store is not None:
            self.store.close()
            self.store = None


def build_runtime(
    settings: Settings,
    env: Mapping[str, str],
    *,
    use_llm: bool = True,
    use_cache: bool = True,
) -> Runtime:
    """Raises ``ConfigError`` for an unusable provider configuration."""
    use_cache = use_cache and not env.get("CI_PINPOINT_NO_CACHE")
    chat = build_chat(settings, env) if use_llm else None
    cache_root = Path(settings.cache_dir) if settings.cache_dir else default_cache_dir(env)
    embedder = build_embedder(settings, env)
    store = _open_store(cache_root) if embedder is not None and use_cache else None
    if embedder is not None and store is not None:
        embedder = CachedEmbedder(embedder, store)
    rankers: list[NamedRanker] = []
    if embedder is not None:
        rankers.append(EmbeddingRanker(embedder))
    if settings.rerank and chat is not None:
        rankers.append(LLMRanker(chat))
    ranker: NamedRanker | None = (
        None if not rankers else rankers[0] if len(rankers) == 1 else ChainRanker(rankers)
    )
    cache = BriefCache(cache_root) if use_cache else None
    return Runtime(settings=settings, chat=chat, ranker=ranker, cache=cache, store=store)
