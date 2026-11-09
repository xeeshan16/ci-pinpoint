"""Wire the stages together for one raw log."""

from __future__ import annotations

import logging

from ci_pinpoint.config import Settings
from ci_pinpoint.diagnose import DiagnosisFailedError, diagnose
from ci_pinpoint.heuristics import build_excerpt
from ci_pinpoint.models import Brief, Excerpt, ExcerptBlockOut, LogStats, NormalizedLog, RawLog
from ci_pinpoint.normalize import normalize
from ci_pinpoint.providers.base import ChatModel, ProviderError
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
        )
        for b in excerpt.blocks
    ]


def explain(raw: RawLog, settings: Settings, chat: ChatModel | None) -> Brief:
    """Normalise, select an excerpt and (if a model is given) diagnose one log."""
    normalized = normalize(raw.text)
    excerpt = build_excerpt(normalized, settings.budget_tokens)
    stats = _stats(normalized, excerpt)
    brief = Brief(
        source=raw.source,
        job_name=raw.job_name,
        job_url=raw.job_url,
        failed_steps=list(raw.failed_steps) or stats.failed_sections,
        stats=stats,
        excerpt=_blocks_out(excerpt),
        diagnosis=None,
    )
    log.info(
        "excerpt selected",
        extra={
            "source": raw.source,
            "raw_tokens": brief.stats.raw_tokens,
            "excerpt_tokens": excerpt.tokens,
            "blocks": len(excerpt.blocks),
            "candidates": excerpt.candidates,
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
    return brief.model_copy(update={"diagnosis": result})
