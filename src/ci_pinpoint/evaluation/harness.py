"""Run strategies over a labelled dataset and score them.

Per case, strategy and budget:

* **hit**: at least one labelled line is shown, i.e. the reader is pointed at
  the failure at all.
* **recall**: the share of label requirements met (for LogChunks, the share of
  the failure chunk's lines that are shown).
* **precision**: the share of shown, non-blank lines that are labelled. A fixed
  budget is usually larger than the chunk, so this is low for every strategy;
  it shows how much of the budget is spent on the failure itself.
* **tokens**: the size of what is shown, and the reduction from the raw log.

Optionally, the model diagnosis is scored on a deterministic sample: was it
verified, and does it cite a labelled line?
"""

from __future__ import annotations

import hashlib
import logging
import random
import statistics
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field

from ci_pinpoint.diagnose import DiagnosisFailedError, diagnose
from ci_pinpoint.evaluation.dataset import Case
from ci_pinpoint.evaluation.strategies import PinpointStrategy, Selection, Strategy
from ci_pinpoint.heuristics import BlockRanker, build_excerpt
from ci_pinpoint.models import NormalizedLog
from ci_pinpoint.normalize import normalize
from ci_pinpoint.providers.base import ChatModel, ProviderError
from ci_pinpoint.tokens import estimate_lines_tokens

log = logging.getLogger(__name__)

BOOTSTRAP_SAMPLES = 2000
BOOTSTRAP_SEED = 7


@dataclass(frozen=True, slots=True)
class Score:
    hit: bool
    recall: float
    precision: float
    tokens: int
    shown: int


@dataclass(frozen=True, slots=True)
class DiagnosisScore:
    verified: bool
    cited_label: bool
    citation_precision: float
    citations: int
    input_tokens: int
    output_tokens: int
    latency_s: float
    error: str | None = None


@dataclass(slots=True)
class CaseResult:
    id: str
    repo: str
    split: str
    language: str | None
    raw_lines: int
    raw_tokens: int
    label_lines: int
    requirements: int
    scores: dict[str, dict[int, Score]] = field(default_factory=dict)
    diagnosis: DiagnosisScore | None = None


@dataclass(frozen=True, slots=True)
class DiagnoseOptions:
    chat: ChatModel
    limit: int = 20
    max_output_tokens: int = 1024
    repair_attempts: int = 1
    ranker: BlockRanker | None = None


def score(case: Case, log_: NormalizedLog, selection: Selection) -> Score:
    nonblank = {line.no for line in log_.lines if line.text.strip()}
    shown = selection.lines & nonblank
    met = sum(1 for requirement in case.requirements if requirement & selection.lines)
    recall = met / len(case.requirements) if case.requirements else 0.0
    precision = len(shown & case.label_lines) / len(shown) if shown else 0.0
    return Score(
        hit=met > 0,
        recall=round(recall, 4),
        precision=round(precision, 4),
        tokens=selection.tokens,
        shown=len(shown),
    )


def evaluate_case(case: Case, strategies: Sequence[Strategy], budgets: Sequence[int]) -> CaseResult:
    log_ = normalize(case.text())
    result = CaseResult(
        id=case.id,
        repo=case.repo,
        split=case.split,
        language=case.language,
        raw_lines=len(log_.lines),
        raw_tokens=estimate_lines_tokens(line.text for line in log_.lines),
        label_lines=len(case.label_lines),
        requirements=len(case.requirements),
    )
    for strategy in strategies:
        selections = strategy.select(log_, budgets)
        result.scores[strategy.name] = {b: score(case, log_, selections[b]) for b in budgets}
    return result


def _evaluate_one(args: tuple[Case, Sequence[Strategy], Sequence[int]]) -> CaseResult:
    return evaluate_case(*args)


def diagnosis_sample(cases: Sequence[Case], limit: int) -> list[Case]:
    """A deterministic, repository-spread sample: cases ordered by a hash of their id."""
    ordered = sorted(cases, key=lambda c: hashlib.sha256(c.id.encode()).hexdigest())
    return ordered[: max(0, limit)]


def score_diagnosis(case: Case, budget: int, options: DiagnoseOptions) -> DiagnosisScore:
    log_ = normalize(case.text())
    excerpt = build_excerpt(log_, budget, options.ranker)
    try:
        result = diagnose(
            options.chat,
            log_,
            excerpt,
            source=case.id,
            max_output_tokens=options.max_output_tokens,
            repair_attempts=options.repair_attempts,
        )
    except (ProviderError, DiagnosisFailedError) as exc:
        return DiagnosisScore(False, False, 0.0, 0, 0, 0, 0.0, error=str(exc)[:300])
    cited = [c.line for c in result.citations]
    in_label = [no for no in cited if no in case.label_lines]
    return DiagnosisScore(
        verified=result.verified,
        cited_label=bool(in_label),
        citation_precision=round(len(in_label) / len(cited), 4) if cited else 0.0,
        citations=len(cited),
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        latency_s=round(result.latency_s, 3),
    )


def run(
    cases: Sequence[Case],
    strategies: Sequence[Strategy],
    budgets: Sequence[int],
    *,
    jobs: int = 1,
    diagnose_options: DiagnoseOptions | None = None,
    diagnose_budget: int | None = None,
) -> list[CaseResult]:
    """Score every strategy on every case; model-free strategies may run in parallel."""
    parallel = jobs > 1 and all(
        not isinstance(s, PinpointStrategy) or s.ranker is None for s in strategies
    )
    work = [(case, strategies, budgets) for case in cases]
    if parallel:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            results = list(pool.map(_evaluate_one, work, chunksize=4))
    else:
        results = [_evaluate_one(item) for item in work]
    if diagnose_options is not None:
        by_id = {r.id: r for r in results}
        budget = diagnose_budget or max(budgets)
        sample = diagnosis_sample(cases, diagnose_options.limit)
        for index, case in enumerate(sample, start=1):
            by_id[case.id].diagnosis = score_diagnosis(case, budget, diagnose_options)
            log.info(
                "diagnosed",
                extra={"case": case.id, "n": index, "of": len(sample)},
            )
    return results


@dataclass(frozen=True, slots=True)
class Summary:
    cases: int
    hit_rate: float
    mean_recall: float
    full_recall_rate: float
    mean_precision: float
    mean_tokens: float
    median_tokens: float
    mean_reduction: float


def summarise(results: Sequence[CaseResult], strategy: str, budget: int) -> Summary:
    rows = [(r, r.scores[strategy][budget]) for r in results if strategy in r.scores]
    if not rows:
        return Summary(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    n = len(rows)
    return Summary(
        cases=n,
        hit_rate=round(sum(s.hit for _, s in rows) / n, 4),
        mean_recall=round(sum(s.recall for _, s in rows) / n, 4),
        full_recall_rate=round(sum(s.recall >= 1.0 for _, s in rows) / n, 4),
        mean_precision=round(sum(s.precision for _, s in rows) / n, 4),
        mean_tokens=round(sum(s.tokens for _, s in rows) / n, 1),
        median_tokens=float(statistics.median(s.tokens for _, s in rows)),
        mean_reduction=round(
            sum(1 - s.tokens / r.raw_tokens if r.raw_tokens else 0.0 for r, s in rows) / n, 4
        ),
    )


@dataclass(frozen=True, slots=True)
class Delta:
    """Paired difference in mean recall (a - b) with a bootstrap 95% interval."""

    a: str
    b: str
    budget: int
    mean: float
    low: float
    high: float


def paired_delta(
    results: Sequence[CaseResult],
    a: str,
    b: str,
    budget: int,
    *,
    samples: int = BOOTSTRAP_SAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> Delta:
    diffs = [
        r.scores[a][budget].recall - r.scores[b][budget].recall
        for r in results
        if a in r.scores and b in r.scores
    ]
    if not diffs:
        return Delta(a, b, budget, 0.0, 0.0, 0.0)
    rng = random.Random(seed)  # noqa: S311 - resampling for a confidence interval
    n = len(diffs)
    means = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(samples))
    return Delta(
        a,
        b,
        budget,
        mean=round(sum(diffs) / n, 4),
        low=round(means[int(0.025 * samples)], 4),
        high=round(means[int(0.975 * samples) - 1], 4),
    )


@dataclass(frozen=True, slots=True)
class DiagnosisSummary:
    cases: int
    verified_rate: float
    cited_label_rate: float
    mean_citation_precision: float
    errors: int
    mean_latency_s: float
    mean_input_tokens: float
    mean_output_tokens: float


def summarise_diagnosis(results: Sequence[CaseResult]) -> DiagnosisSummary | None:
    scored = [r.diagnosis for r in results if r.diagnosis is not None]
    if not scored:
        return None
    n = len(scored)
    return DiagnosisSummary(
        cases=n,
        verified_rate=round(sum(d.verified for d in scored) / n, 4),
        cited_label_rate=round(sum(d.cited_label for d in scored) / n, 4),
        mean_citation_precision=round(sum(d.citation_precision for d in scored) / n, 4),
        errors=sum(1 for d in scored if d.error),
        mean_latency_s=round(sum(d.latency_s for d in scored) / n, 2),
        mean_input_tokens=round(sum(d.input_tokens for d in scored) / n, 1),
        mean_output_tokens=round(sum(d.output_tokens for d in scored) / n, 1),
    )
