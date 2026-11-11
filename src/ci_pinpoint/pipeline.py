"""Wire the stages together for one raw log."""

from __future__ import annotations

import logging

from ci_pinpoint.cache import BriefCache, brief_key
from ci_pinpoint.config import Settings
from ci_pinpoint.diagnose import DiagnosisFailedError, diagnose
from ci_pinpoint.diagnose.prompt import PROMPT_VERSION
from ci_pinpoint.heuristics import build_excerpt
from ci_pinpoint.models import (
    Brief,
    ElisionOut,
    Excerpt,
    ExcerptBlockOut,
    LogStats,
    NormalizedLog,
    RawLog,
)
from ci_pinpoint.normalize import normalize
from ci_pinpoint.providers.base import ChatModel, ProviderError
from ci_pinpoint.ranking import EmbeddingRanker
from ci_pinpoint.redact import redact_log
from ci_pinpoint.tokens import estimate_lines_tokens

log = logging.getLogger(__name__)


def _stats(normalized: NormalizedLog, excerpt: Excerpt) -> LogStats:
    return LogStats(
        raw_lines=len(normalized.lines),
        raw_tokens=estimate_lines_tokens(line.text for line in normalized.lines),
        excerpt_tokens=excerpt.tokens,
        sections=len(normalized.sections),
        failed_sections=[s.title for s in normalized.sections if s.has_error],
    )


def _blocks_out(excerpt: Excerpt) -> list[ExcerptBlockOut]:
    return [
        ExcerptBlockOut(
            start=b.start,
            end=b.end,
            score=b.score,
            signals=list(b.signals),
            lines=[(line.no, line.text) for line in b.lines],
            elided=[
                ElisionOut(after=e.after, count=e.count, template=e.template) for e in b.elided
            ],
        )
        for b in excerpt.blocks
    ]


def _cache_key(
    raw: RawLog, settings: Settings, chat: ChatModel, ranker: EmbeddingRanker | None
) -> str:
    return brief_key(
        raw.text,
        {
            "budget": settings.budget_tokens,
            "provider": chat.provider,
            "model": chat.model,
            "prompt": PROMPT_VERSION,
            "embedder": ranker.model if ranker else None,
            "max_output_tokens": settings.max_output_tokens,
            "repair_attempts": settings.repair_attempts,
            "failed_steps": list(raw.failed_steps),
            "redact": settings.redact,
        },
    )


def explain(
    raw: RawLog,
    settings: Settings,
    chat: ChatModel | None,
    *,
    ranker: EmbeddingRanker | None = None,
    cache: BriefCache | None = None,
) -> Brief:
    """Normalise, select an excerpt and (if a model is given) diagnose one log.

    With a cache, a verified diagnosis of the same log under the same settings
    is returned without calling any model.
    """
    key = _cache_key(raw, settings, chat, ranker) if cache and chat else None
    if cache and key:
        hit = cache.get(key)
        if hit is not None:
            log.info("brief served from cache", extra={"source": raw.source})
            return hit.model_copy(
                update={"cached": True, "source": raw.source, "job_url": raw.job_url}
            )

    normalized = normalize(raw.text)
    redactions: dict[str, int] = {}
    if settings.redact:
        normalized, redactions = redact_log(normalized)
        if redactions:
            log.warning(
                "secrets redacted from the log",
                extra={"source": raw.source, "redactions": redactions},
            )
    excerpt = build_excerpt(normalized, settings.budget_tokens, ranker)
    stats = _stats(normalized, excerpt)
    brief = Brief(
        source=raw.source,
        job_name=raw.job_name,
        job_url=raw.job_url,
        failed_steps=list(raw.failed_steps) or stats.failed_sections,
        stats=stats,
        excerpt=_blocks_out(excerpt),
        diagnosis=None,
        ranked_with=ranker.model if ranker and ranker.error is None else None,
        redactions=redactions,
    )
    log.info(
        "excerpt selected",
        extra={
            "source": raw.source,
            "raw_tokens": brief.stats.raw_tokens,
            "excerpt_tokens": excerpt.tokens,
            "blocks": len(excerpt.blocks),
            "candidates": excerpt.candidates,
            "ranked_with": brief.ranked_with,
        },
    )
    if chat is None or not excerpt.blocks:
        return brief
    try:
        result = diagnose(
            chat,
            normalized,
            excerpt,
            source=raw.source,
            job_name=raw.job_name,
            failed_steps=brief.failed_steps,
            max_output_tokens=settings.max_output_tokens,
            repair_attempts=settings.repair_attempts,
        )
    except (ProviderError, DiagnosisFailedError) as exc:
        return brief.model_copy(update={"error": str(exc)})
    brief = brief.model_copy(update={"diagnosis": result})
    if cache and key and result.verified:
        cache.put(key, brief)
    return brief
