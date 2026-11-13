"""Wire the stages together for one raw log."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor

from ci_pinpoint.cache import BriefCache, brief_key
from ci_pinpoint.config import Settings
from ci_pinpoint.diagnose import DiagnosisFailedError, diagnose
from ci_pinpoint.diagnose.prompt import PROMPT_VERSION
from ci_pinpoint.heuristics import build_excerpt
from ci_pinpoint.injection import flag_lines
from ci_pinpoint.models import (
    Accounting,
    Brief,
    ElisionOut,
    Excerpt,
    ExcerptBlockOut,
    LogStats,
    NormalizedLog,
    RawLog,
    TokenUsage,
)
from ci_pinpoint.normalize import normalize
from ci_pinpoint.pricing import price_for
from ci_pinpoint.providers.base import ChatModel, ProviderError
from ci_pinpoint.redact import redact_log
from ci_pinpoint.rerank import PROMPT_VERSION as RERANK_PROMPT_VERSION
from ci_pinpoint.rerank import NamedRanker, ranker_usage
from ci_pinpoint.telemetry import get_telemetry
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


def _cache_key(raw: RawLog, settings: Settings, chat: ChatModel, ranker: NamedRanker | None) -> str:
    return brief_key(
        raw.text,
        {
            "budget": settings.budget_tokens,
            "provider": chat.provider,
            "model": chat.model,
            "prompt": PROMPT_VERSION,
            "rerank_prompt": RERANK_PROMPT_VERSION,
            "embedder": ranker.model if ranker else None,
            "max_output_tokens": settings.max_output_tokens,
            "repair_attempts": settings.repair_attempts,
            "failed_steps": list(raw.failed_steps),
            "redact": settings.redact,
        },
    )


def _accounting(
    chat: ChatModel | None, usage: TokenUsage, env: Mapping[str, str]
) -> Accounting | None:
    """Tokens and estimated cost for the model work, or None without a model."""
    if chat is None:
        return None
    price = price_for(chat.provider, chat.model, env)
    return Accounting(
        provider=chat.provider,
        model=chat.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=price.cost(usage) if price is not None else None,
    )


def explain(
    raw: RawLog,
    settings: Settings,
    chat: ChatModel | None,
    *,
    ranker: NamedRanker | None = None,
    cache: BriefCache | None = None,
    env: Mapping[str, str] | None = None,
) -> Brief:
    """Normalise, select an excerpt and (if a model is given) diagnose one log.

    With a cache, a verified diagnosis of the same log under the same settings
    is returned without calling any model.
    """
    env = os.environ if env is None else env
    telemetry = get_telemetry()
    key = _cache_key(raw, settings, chat, ranker) if cache and chat else None
    if cache and key:
        hit = cache.get(key)
        if hit is not None:
            log.info("brief served from cache", extra={"source": raw.source})
            return hit.model_copy(
                update={"cached": True, "source": raw.source, "job_url": raw.job_url}
            )

    with telemetry.stage("normalize", source=raw.source):
        normalized = normalize(raw.text)
        redactions: dict[str, int] = {}
        if settings.redact:
            normalized, redactions = redact_log(normalized)
            if redactions:
                log.warning(
                    "secrets redacted from the log",
                    extra={"source": raw.source, "redactions": redactions},
                )
    with telemetry.stage("select", source=raw.source, budget_tokens=settings.budget_tokens):
        rerank_before = ranker_usage(ranker)
        excerpt = build_excerpt(normalized, settings.budget_tokens, ranker)
        rerank_usage = ranker_usage(ranker) - rerank_before
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
        untrusted_lines=sorted(
            flag_lines(line for block in excerpt.blocks for line in block.lines)
        ),
        accounting=_accounting(chat, rerank_usage, env),
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
        telemetry.record_brief(brief)
        return brief
    with telemetry.stage("diagnose", source=raw.source, provider=chat.provider, model=chat.model):
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
            brief = brief.model_copy(update={"error": str(exc)})
            telemetry.record_brief(brief)
            return brief
    accounting = _accounting(chat, result.usage + rerank_usage, env)
    brief = brief.model_copy(update={"diagnosis": result, "accounting": accounting})
    telemetry.record_brief(brief)
    if cache and key and result.verified:
        cache.put(key, brief)
    return brief


def explain_all(
    raws: Sequence[RawLog],
    settings: Settings,
    chat: ChatModel | None,
    *,
    ranker: NamedRanker | None = None,
    cache: BriefCache | None = None,
    env: Mapping[str, str] | None = None,
) -> list[Brief]:
    """Diagnose several jobs, concurrently when ``settings.concurrency`` allows.

    The jobs are independent, so each runs on its own thread; the returned
    briefs keep the order of ``raws``. One job, or a concurrency of 1, runs
    inline. A failing job still returns a brief (with ``error`` set), so one bad
    job never sinks the batch.
    """
    workers = min(settings.concurrency, len(raws))
    if workers <= 1:
        return [explain(raw, settings, chat, ranker=ranker, cache=cache, env=env) for raw in raws]
    results: list[Brief | None] = [None] * len(raws)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ci-pinpoint") as pool:
        futures = {
            pool.submit(explain, raw, settings, chat, ranker=ranker, cache=cache, env=env): i
            for i, raw in enumerate(raws)
        }
        for future, index in futures.items():
            results[index] = future.result()
    return [brief for brief in results if brief is not None]
