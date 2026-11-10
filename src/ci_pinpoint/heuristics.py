"""Stage 1: cheap, explainable candidate selection (no model needed).

Every line gets a score from weighted signals (exceptions, ``Caused by:``,
compiler errors, test failures, OOM kills, refused connections, "cannot" /
"invalid" / "MISSING" phrasing and so on), adjusted by:

* negative patterns that look like errors but aren't (``Failures: 0, Errors: 0``,
  ``-Werror``, ``error_type="none"``),
* the section the line sits in (the step that emitted ``##[error]`` counts more,
  runner setup and cleanup count less),
* stack-frame dampening, so frames are context rather than evidence,
* template rarity (see :mod:`ci_pinpoint.templates`): weak lines whose template
  repeats hundreds of times are dampened; strong lines are not, because the same
  ``Caused by`` under 19 failing tests is a common cause, not noise.

High-scoring lines seed windows inside their section, windows merge into
blocks, and the tail of every failed step becomes a block too. Runs of
same-template lines inside a block are folded into one line plus a count.
Blocks are then chosen greedily under a token budget, skipping blocks whose
evidence templates repeat what is already chosen. An optional ranker (see
:mod:`ci_pinpoint.ranking`) can re-score blocks before selection.

This stage needs no model. It is also the "heuristics only" baseline in the eval.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from ci_pinpoint.models import (
    Block,
    Elision,
    Excerpt,
    LineKind,
    LogLine,
    NormalizedLog,
    ScoredLine,
    Section,
)
from ci_pinpoint.templates import TemplateIndex, mine
from ci_pinpoint.tokens import estimate_line_tokens


@dataclass(frozen=True, slots=True)
class Signal:
    name: str
    pattern: re.Pattern[str]
    weight: float


def _sig(name: str, pattern: str, weight: float, flags: int = re.IGNORECASE) -> Signal:
    return Signal(name, re.compile(pattern, flags), weight)


SIGNALS: tuple[Signal, ...] = (
    _sig("traceback", r"Traceback \(most recent call last\)", 3.0),
    _sig("caused_by", r"^\s*Caused by:", 3.0),
    _sig("exception", r"\b[A-Z]\w*(?:Exception|Error)\b(?::|\s*$)", 2.5, 0),
    _sig(
        "test_failure",
        r"<<< (?:FAILURE|ERROR)!|\bFAILED\b|^\s*FAIL\b|--- FAIL:|^\s*[\u2717\u2715\u00d7]\s",
        2.5,
        0,
    ),
    _sig("build_failure", r"\bBUILD (?:FAILURE|FAILED)\b|Failed to execute goal", 2.5),
    _sig(
        "compiler_error",
        r"(?:^|:\d+(?::\d+)?:?\s+)(?:fatal\s+)?error(?:\[\w+\])?:|^error TS\d+|\berror CS\d+",
        3.0,
    ),
    _sig("npm_error", r"^npm ERR!|^ERR_PNPM_|^error Command failed|^\s*ERR!", 2.5),
    _sig("panic", r"^panic:|^fatal error:|\bSIGSEGV\b|segmentation fault|core dumped", 3.0),
    _sig("oom", r"\bOOMKilled\b|out of memory|OutOfMemoryError|\bKilled\b|exit code 137", 3.0),
    _sig("timeout", r"timed?[ -]?out\b|deadline exceeded|maximum execution time", 2.0),
    _sig(
        "network",
        r"connection refused|ECONNREFUSED|ECONNRESET|could not resolve host|"
        r"name or service not known|temporary failure in name resolution|TLS handshake",
        2.5,
    ),
    _sig(
        "missing",
        r"\bMISSING\b|No such file or directory|command not found|cannot find|could not find|"
        r"ModuleNotFoundError|ImportError|\bunresolved\b|does not exist|\bnot found\b",
        2.0,
    ),
    _sig("permission", r"permission denied|EACCES|\bforbidden\b|unauthori[sz]ed", 2.0),
    _sig(
        "problem_phrase",
        r"\bcannot\b|\bcan't\b|\bcould not\b|\bcouldn't\b|\bunable to\b|\bnot allowed\b|"
        r"\binvalid\b|\bunknown\b|\bunexpected\b|\bmismatch\b|\bviolations?\b|"
        r"\bnot formatted\b|\bnot exported\b|\brequirement failed\b|\brefused\b",
        1.5,
    ),
    _sig(
        "assertion", r"\bassert(?:ion)?\b|\bexpected\b.*\b(?:but|got|actual)\b|^\s*Expecting\b", 1.5
    ),
    _sig("error_word", r"\berrors?\b|\bERR\b", 1.0),
    _sig("fail_word", r"\bfail(?:s|ed|ing|ure|ures)?\b", 1.0),
    _sig("exit_code", r"exit(?:ed)? (?:with )?(?:code|status) [1-9]\d*", 0.8),
)

# Lines that contain error words but report success or configure error handling.
NEGATIVE = re.compile(
    r"Failures: 0, Errors: 0|\b0 (?:errors?|failures?|failed)\b|\bno errors?\b|"
    r"\berror_type=\"none\"|-Werror\b|fail[-_]?fast|failOnError|ignore[-_]errors|"
    r"continue-on-error|\berrexit\b|\bset -e\b|::add-matcher::|\bcorrect(?:ed)? errors?\b",
    re.IGNORECASE,
)
# Advice that build tools print after every failure. Never the cause.
BOILERPLATE = re.compile(
    r"Re-run Maven using the -X switch|\[Help \d+\] http|"
    r"For more information about the errors and possible solutions|"
    r"After correcting the problems, you can resume the build|mvn <args> -rf :|"
    r"To see the full stack trace of the errors, re-run Maven|^\[ERROR\]\s*$|"
    r"^npm ERR! A complete log of this run can be found|^npm ERR!\s*$|"
    r"Run with --stacktrace option to get the stack trace|Run with --(?:info|debug) option|"
    r"Run with --scan to get full insights|Get more help at https://help\.gradle\.org|"
    # Travis CI prints these after every failed job.
    r"^Done\. Your build exited with \d+|job_stages: line \d+: shell_session_update: command not",
)
WARNING = re.compile(r"\bwarn(?:ing)?\b|\bdeprecat", re.IGNORECASE)
# A log level of WARN/INFO/DEBUG means the program itself did not treat the line as fatal.
NON_FATAL_LEVEL = re.compile(r"(?:^|[\s\[])(?:WARN(?:ING)?|INFO|DEBUG|TRACE)(?:[\s\]:]|$)")
STACK_FRAME = re.compile(
    r"^\s+at [\w.$<>/\-]+[.(]|^\s+File \"[^\"]+\", line \d+|^\s*\.\.\. \d+ more$|"
    r"^\s+[\w./\-]+\.go:\d+|^\s+at .+:\d+:\d+\)?$"
)
GENERIC_EXIT = re.compile(
    r"^Process completed with exit code \d+\.?$|^The command \".*\" (?:failed and )?exited with \d+"
)
LOW_VALUE_SECTION = re.compile(
    r"^(?:Set up job|Runner Image|Operating System|Runner Image Provisioner|"
    r"GITHUB_TOKEN Permissions|"
    r"Secret source|Prepare workflow|Post |Cleaning up orphan|Complete job|"
    r"Getting Git version|Temporarily overriding|Adding repository directory|Disabling automatic|"
    r"Setting up auth|Fetching the repository|Determining the checkout|Checking out the ref|"
    r"Initializing the repository|Download action repository|Getting action download info)",
    re.IGNORECASE,
)

SEED_THRESHOLD = 2.0
CONTEXT_BEFORE = 3
CONTEXT_AFTER = 4
MERGE_GAP = 2
TAIL_LINES = 10
TAIL_PRIOR = 4.0
TAIL_SHARE = 0.25  # failed-step tails together take at most this share of the budget
MAX_FRAMES_KEPT = 3
MAX_BLOCK_LINES = 60
MAX_BLOCK_SHARE = 0.4  # no single block may take more than this share of the budget
MIN_NOVELTY = 0.5  # a block must bring at least this share of new evidence templates
MIN_RUN = 3  # fold runs of at least this many same-template lines


def score_text(text: str) -> tuple[float, tuple[str, ...]]:
    """Score one line of text on its own, with no section or log context."""
    if not text.strip():
        return 0.0, ()
    if BOILERPLATE.search(text):
        return 0.0, ("boilerplate",)
    if NEGATIVE.search(text):
        return 0.0, ("negative",)
    names: list[str] = []
    total = 0.0
    best = 0.0
    for signal in SIGNALS:
        if signal.pattern.search(text):
            names.append(signal.name)
            total += signal.weight
            best = max(best, signal.weight)
    if not names:
        return 0.0, ()
    # The strongest signal counts fully; the rest add a little, so one line with
    # five weak words does not beat one line with a real exception.
    score = best + 0.25 * (total - best)
    if NON_FATAL_LEVEL.search(text[:80]):
        score *= 0.5
        names.append("non_fatal_level")
    elif WARNING.search(text) and best < 2.5:
        score *= 0.5
        names.append("warning")
    return score, tuple(names)


def _section_factor(section: Section) -> float:
    if section.has_error:
        return 1.6
    if LOW_VALUE_SECTION.match(section.title):
        return 0.3
    return 1.0


def _rarity(count: int) -> float:
    """Dampen lines whose template repeats: the 400th progress line is noise."""
    return 1.0 / (1.0 + 0.3 * math.log(count)) if count > 1 else 1.0


@dataclass(frozen=True, slots=True)
class LogContext:
    """Everything selection needs to know about one log, computed once."""

    log: NormalizedLog
    index: TemplateIndex
    scored: dict[int, ScoredLine]
    ordered: tuple[ScoredLine, ...]

    def cluster(self, line_no: int) -> int:
        return self.index.cluster_of.get(line_no, -1)

    def is_seed(self, line_no: int) -> bool:
        return self.scored[line_no].score >= SEED_THRESHOLD


def score_lines(log: NormalizedLog, index: TemplateIndex | None = None) -> list[ScoredLine]:
    index = index or mine((line.no, line.text) for line in log.lines)
    sections = {s.index: s for s in log.sections}
    scored: list[ScoredLine] = []
    for line in log.lines:
        if line.kind in (LineKind.GROUP, LineKind.ENDGROUP):
            scored.append(ScoredLine(line, 0.0, ()))
            continue
        score, names = score_text(line.text)
        if 0 < score < SEED_THRESHOLD:
            score *= _rarity(index.size_of(line.no))
        if line.kind is LineKind.COMMAND:
            # The script GitHub echoes before running a step: it may say "error",
            # but it is the command, not what happened.
            score *= 0.2
            names = (*names, "command_echo")
        if line.kind is LineKind.ERROR_ANNOTATION:
            if GENERIC_EXIT.match(line.text):
                score, names = max(score, 1.0), (*names, "exit_annotation")
            else:
                score, names = score + 3.0, (*names, "error_annotation")
        if STACK_FRAME.match(line.text):
            score *= 0.15
            names = (*names, "stack_frame")
        score *= _section_factor(sections[line.section])
        scored.append(ScoredLine(line, round(score, 3), names))
    return scored


def analyse(log: NormalizedLog) -> LogContext:
    index = mine((line.no, line.text) for line in log.lines)
    ordered = tuple(score_lines(log, index))
    return LogContext(log, index, {s.line.no: s for s in ordered}, ordered)


def _compact_frames(lines: Sequence[LogLine]) -> tuple[LogLine, ...]:
    """Keep the first few and the last frame of each stack-frame run."""
    out: list[LogLine] = []
    run: list[LogLine] = []

    def flush() -> None:
        if len(run) <= MAX_FRAMES_KEPT + 1:
            out.extend(run)
        else:
            out.extend(run[:MAX_FRAMES_KEPT])
            out.append(run[-1])
        run.clear()

    for line in lines:
        if STACK_FRAME.match(line.text):
            run.append(line)
        else:
            flush()
            out.append(line)
    flush()
    return tuple(out)


def _collapse_runs(
    lines: Sequence[LogLine], ctx: LogContext
) -> tuple[tuple[LogLine, ...], tuple[Elision, ...]]:
    """Fold runs of adjacent same-template, non-evidence lines into their first line."""
    kept: list[LogLine] = []
    elided: list[Elision] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        cid = ctx.cluster(line.no)
        j = i + 1
        if cid >= 0 and not ctx.is_seed(line.no):
            while (
                j < len(lines)
                and lines[j].no == lines[j - 1].no + 1
                and ctx.cluster(lines[j].no) == cid
                and not ctx.is_seed(lines[j].no)
            ):
                j += 1
        kept.append(line)
        if j - i >= MIN_RUN:
            elided.append(Elision(line.no, j - i - 1, ctx.index.templates[cid]))
        else:
            kept.extend(lines[i + 1 : j])
        i = j
    return tuple(kept), tuple(elided)


def _make_block(
    lines: Sequence[LogLine], ctx: LogContext, prior: float = 0.0, tag: str = ""
) -> Block:
    compact = [line for line in _compact_frames(lines) if line.kind is not LineKind.ENDGROUP]
    kept, elided = _collapse_runs(compact, ctx)
    seeds = [ctx.scored[line.no] for line in kept if ctx.is_seed(line.no)]
    best = max((s.score for s in seeds), default=0.0)
    score = max(best + min(2.0, 0.25 * max(0, len(seeds) - 1)), prior)
    signals = sorted({name for s in seeds for name in s.signals} | ({tag} if tag else set()))
    return Block(
        start=kept[0].no if kept else lines[0].no,
        end=kept[-1].no if kept else lines[-1].no,
        score=round(score, 3),
        signals=tuple(signals),
        lines=kept,
        elided=elided,
    )


def candidate_blocks(ctx: LogContext) -> list[Block]:
    """Windows around high-scoring lines (never crossing a section boundary),
    merged into blocks, plus the tail of every failed step."""
    log = ctx.log
    index_of = {line.no: i for i, line in enumerate(log.lines)}
    bounds = {
        s.index: (index_of.get(s.first_line, 0), index_of.get(s.last_line, len(log.lines) - 1))
        for s in log.sections
    }

    windows: list[tuple[int, int, int]] = []
    for i, s in enumerate(ctx.ordered):
        if s.score >= SEED_THRESHOLD:
            first, last = bounds[s.line.section]
            windows.append(
                (max(first, i - CONTEXT_BEFORE), min(last, i + CONTEXT_AFTER), s.line.section)
            )
    merged: list[tuple[int, int, int]] = []
    for lo, hi, sec in windows:
        if merged and merged[-1][2] == sec and lo <= merged[-1][1] + MERGE_GAP:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi), sec)
        else:
            merged.append((lo, hi, sec))

    blocks: list[Block] = []
    for lo, hi, _ in merged:
        for start in range(lo, hi + 1, MAX_BLOCK_LINES):
            chunk = log.lines[start : min(hi, start + MAX_BLOCK_LINES - 1) + 1]
            blocks.append(_make_block(chunk, ctx))

    # The last lines of every step that emitted ##[error] are the strongest prior
    # there is, even when they don't contain a single error word.
    for section in log.sections:
        if not section.has_error:
            continue
        first, last = bounds[section.index]
        span = log.lines[first : last + 1]
        members = [
            line
            for line in span
            if line.kind is LineKind.TEXT
            and line.text.strip()
            and not BOILERPLATE.search(line.text)
        ]
        if not members:
            continue
        annotations = [line for line in span if line.kind is LineKind.ERROR_ANNOTATION]
        tail = members[-TAIL_LINES:]
        if annotations:
            first_annotation = annotations[0].no
            before = [line for line in members if line.no < first_annotation]
            tail = [*before[-TAIL_LINES:], annotations[0]]
        blocks.append(_make_block(tail, ctx, prior=TAIL_PRIOR, tag="failed_step_tail"))
    return blocks


def elision_text(elision: Elision) -> str:
    return f"... {elision.count} more line(s) like: {elision.template}"


def block_tokens(block: Block) -> int:
    return sum(estimate_line_tokens(line.text) for line in block.lines) + sum(
        estimate_line_tokens(elision_text(e)) for e in block.elided
    )


def _restrict(block: Block, lines: Sequence[LogLine]) -> Block:
    keep = {line.no for line in lines}
    return replace(
        block,
        start=lines[0].no,
        end=lines[-1].no,
        lines=tuple(lines),
        elided=tuple(e for e in block.elided if e.after in keep),
    )


def _trim_to_budget(block: Block, ctx: LogContext, budget: int) -> Block | None:
    """Shrink a block around its best line until it fits ``budget`` tokens."""
    if not block.lines or budget <= 0:
        return None
    best = max(range(len(block.lines)), key=lambda i: ctx.scored[block.lines[i].no].score)
    lo = hi = best
    if block_tokens(_restrict(block, block.lines[best : best + 1])) > budget:
        return None
    grew = True
    while grew:
        grew = False
        for new_lo, new_hi in ((lo, hi + 1), (lo - 1, hi)):
            if new_lo >= 0 and new_hi < len(block.lines):
                candidate = _restrict(block, block.lines[new_lo : new_hi + 1])
                if block_tokens(candidate) <= budget:
                    lo, hi, grew = new_lo, new_hi, True
    return _restrict(block, block.lines[lo : hi + 1])


class BlockRanker(Protocol):
    """Re-scores candidate blocks before selection (see :mod:`ci_pinpoint.ranking`)."""

    def rank(self, blocks: Sequence[Block], ctx: LogContext) -> list[Block]: ...


def select_blocks(blocks: Sequence[Block], ctx: LogContext, budget: int) -> Excerpt:
    """Greedy, diversity-aware selection of blocks under a token budget."""
    # Failed-step tails go first: they are cheap and orient the reader (and the
    # model) even when the cause is elsewhere. Then the rest by score.
    ordered = sorted(blocks, key=lambda b: ("failed_step_tail" not in b.signals, -b.score, -b.end))
    chosen: list[Block] = []
    covered_lines: set[int] = set()
    covered_templates: set[int] = set()
    used = 0
    for block in ordered:
        remaining = budget - used
        if remaining <= 0:
            break
        fresh = tuple(line for line in block.lines if line.no not in covered_lines)
        if not fresh:
            continue
        evidence = {ctx.cluster(line.no) for line in fresh if ctx.is_seed(line.no)}
        if evidence and "failed_step_tail" not in block.signals:
            novelty = len(evidence - covered_templates) / len(evidence)
            if novelty < MIN_NOVELTY:
                continue  # mostly the same evidence as a block we already have
        candidate = _restrict(block, fresh)
        cost = block_tokens(candidate)
        share = TAIL_SHARE if "failed_step_tail" in block.signals else MAX_BLOCK_SHARE
        # A block may always take its single best line, even past its share.
        best_line = max(candidate.lines, key=lambda ln: ctx.scored[ln.no].score)
        floor = block_tokens(_restrict(candidate, [best_line]))
        limit = min(remaining, max(floor, int(budget * share)))
        if cost > limit:
            trimmed = _trim_to_budget(candidate, ctx, limit)
            if trimmed is None:
                continue
            candidate, cost = trimmed, block_tokens(trimmed)
        chosen.append(candidate)
        used += cost
        covered_lines.update(line.no for line in candidate.lines)
        covered_templates.update(
            ctx.cluster(line.no) for line in candidate.lines if ctx.is_seed(line.no)
        )
    chosen.sort(key=lambda b: b.start)
    return Excerpt(blocks=tuple(chosen), tokens=used, budget=budget, candidates=len(blocks))


def build_excerpt(log: NormalizedLog, budget: int, ranker: BlockRanker | None = None) -> Excerpt:
    ctx = analyse(log)
    blocks = candidate_blocks(ctx)
    if ranker is not None:
        blocks = ranker.rank(blocks, ctx)
    return select_blocks(blocks, ctx, budget)
