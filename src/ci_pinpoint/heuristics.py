"""Stage 1: cheap, explainable candidate selection.

Every line gets a score from weighted signals (exceptions, ``Caused by:``,
compiler errors, test failures, OOM kills, refused connections, "cannot" /
"invalid" / "MISSING" phrasing and so on), adjusted by:

* negative patterns that look like errors but aren't (``Failures: 0, Errors: 0``,
  ``-Werror``, ``error_type="none"``),
* the section the line sits in (the step that emitted ``##[error]`` counts more,
  runner setup and cleanup count less),
* stack-frame dampening, so frames are context rather than evidence.

High-scoring lines seed windows that are merged into blocks, and the tail of
every failed step becomes a block too. Blocks are then chosen greedily under a
token budget, skipping blocks whose evidence repeats what is already chosen
(a Spring context failure can repeat the same ``Caused by`` 20 times).

This stage needs no model. It is also the "heuristics only" baseline in the eval.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from ci_pinpoint.models import (
    Block,
    Excerpt,
    LineKind,
    LogLine,
    NormalizedLog,
    ScoredLine,
    Section,
)
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
    r"Run with --scan to get full insights|Get more help at https://help\.gradle\.org",
)
WARNING = re.compile(r"\bwarn(?:ing)?\b|\bdeprecat", re.IGNORECASE)
# A log level of WARN/INFO/DEBUG means the program itself did not treat the line as fatal.
NON_FATAL_LEVEL = re.compile(r"(?:^|[\s\[])(?:WARN(?:ING)?|INFO|DEBUG|TRACE)(?:[\s\]:]|$)")
STACK_FRAME = re.compile(
    r"^\s+at [\w.$<>/\-]+[.(]|^\s+File \"[^\"]+\", line \d+|^\s*\.\.\. \d+ more$|"
    r"^\s+[\w./\-]+\.go:\d+|^\s+at .+:\d+:\d+\)?$"
)
GENERIC_EXIT = re.compile(r"^Process completed with exit code \d+\.?$")
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

_TEMPLATE_SUBS = (
    (re.compile(r"0x[0-9a-f]+|\b[0-9a-f]{7,}\b", re.IGNORECASE), "#"),
    (re.compile(r"\d+(?:\.\d+)*"), "0"),
    (re.compile(r"'[^']*'|\"[^\"]*\""), "Q"),
    (re.compile(r"\s+"), " "),
)


def template(text: str) -> str:
    """A coarse template used to spot repeated lines (numbers, hashes and quotes masked)."""
    out = text.strip().lower()
    for pattern, repl in _TEMPLATE_SUBS:
        out = pattern.sub(repl, out)
    return out


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
    """Dampen lines whose template repeats: the 40th identical warning is noise."""
    return 1.0 / (1.0 + 0.3 * math.log(count)) if count > 1 else 1.0


def score_lines(log: NormalizedLog) -> list[ScoredLine]:
    sections = {s.index: s for s in log.sections}
    templates = Counter(template(line.text) for line in log.lines if line.text.strip())
    scored: list[ScoredLine] = []
    for line in log.lines:
        if line.kind in (LineKind.GROUP, LineKind.ENDGROUP):
            scored.append(ScoredLine(line, 0.0, ()))
            continue
        score, names = score_text(line.text)
        if score > 0:
            score *= _rarity(templates[template(line.text)])
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


def _make_block(
    lines: Sequence[LogLine], scored: dict[int, ScoredLine], prior: float = 0.0, tag: str = ""
) -> Block:
    kept = tuple(line for line in _compact_frames(lines) if line.kind is not LineKind.ENDGROUP)
    seeds = [scored[line.no] for line in kept if scored[line.no].score >= SEED_THRESHOLD]
    best = max((s.score for s in seeds), default=0.0)
    score = max(best + min(2.0, 0.25 * max(0, len(seeds) - 1)), prior)
    signals = sorted({name for s in seeds for name in s.signals} | ({tag} if tag else set()))
    return Block(
        start=kept[0].no if kept else lines[0].no,
        end=kept[-1].no if kept else lines[-1].no,
        score=round(score, 3),
        signals=tuple(signals),
        lines=kept,
    )


def candidate_blocks(log: NormalizedLog, scored_lines: Sequence[ScoredLine]) -> list[Block]:
    """Merge windows around high-scoring lines into blocks, plus failed-step tails."""
    scored = {s.line.no: s for s in scored_lines}
    index_of = {line.no: i for i, line in enumerate(log.lines)}
    n = len(log.lines)

    windows: list[tuple[int, int]] = []
    for i, s in enumerate(scored_lines):
        if s.score >= SEED_THRESHOLD:
            windows.append((max(0, i - CONTEXT_BEFORE), min(n - 1, i + CONTEXT_AFTER)))
    merged: list[tuple[int, int]] = []
    for lo, hi in windows:
        if merged and lo <= merged[-1][1] + MERGE_GAP:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))

    blocks: list[Block] = []
    for lo, hi in merged:
        for start in range(lo, hi + 1, MAX_BLOCK_LINES):
            chunk = log.lines[start : min(hi, start + MAX_BLOCK_LINES - 1) + 1]
            blocks.append(_make_block(chunk, scored))

    # The last lines of every step that emitted ##[error] are the strongest prior
    # there is, even when they don't contain a single error word.
    for section in log.sections:
        if not section.has_error:
            continue
        members = [
            line
            for line in log.lines[index_of[section.first_line] : index_of[section.last_line] + 1]
            if line.kind is LineKind.TEXT
            and line.text.strip()
            and not BOILERPLATE.search(line.text)
        ]
        if not members:
            continue
        annotations = [
            line
            for line in log.lines[index_of[section.first_line] : index_of[section.last_line] + 1]
            if line.kind is LineKind.ERROR_ANNOTATION
        ]
        tail = members[-TAIL_LINES:]
        if annotations:
            first_annotation = annotations[0].no
            before = [line for line in members if line.no < first_annotation]
            tail = [*before[-TAIL_LINES:], annotations[0]]
        blocks.append(_make_block(tail, scored, prior=TAIL_PRIOR, tag="failed_step_tail"))
    return blocks


def _block_tokens(lines: Sequence[LogLine]) -> int:
    return sum(estimate_line_tokens(line.text) for line in lines)


def _trim_to_budget(block: Block, scored: dict[int, ScoredLine], budget: int) -> Block | None:
    """Shrink a block around its best line until it fits ``budget`` tokens."""
    if not block.lines or budget <= 0:
        return None
    best = max(range(len(block.lines)), key=lambda i: scored[block.lines[i].no].score)
    lo = hi = best
    used = estimate_line_tokens(block.lines[best].text)
    if used > budget:
        return None
    while True:
        grew = False
        for candidate in (hi + 1, lo - 1):
            if 0 <= candidate < len(block.lines) and (candidate > hi or candidate < lo):
                cost = estimate_line_tokens(block.lines[candidate].text)
                if used + cost <= budget:
                    used += cost
                    lo, hi = min(lo, candidate), max(hi, candidate)
                    grew = True
        if not grew:
            break
    kept = block.lines[lo : hi + 1]
    return Block(kept[0].no, kept[-1].no, block.score, block.signals, kept)


def select_blocks(
    blocks: Sequence[Block], scored_lines: Sequence[ScoredLine], budget: int
) -> Excerpt:
    """Greedy, diversity-aware selection of blocks under a token budget."""
    scored = {s.line.no: s for s in scored_lines}
    # Failed-step tails go first: they are cheap and orient the reader (and the
    # model) even when the cause is elsewhere. Then the rest by score.
    ordered = sorted(blocks, key=lambda b: ("failed_step_tail" not in b.signals, -b.score, -b.end))
    chosen: list[Block] = []
    covered_lines: set[int] = set()
    covered_templates: set[str] = set()
    used = 0
    for block in ordered:
        remaining = budget - used
        if remaining <= 0:
            break
        fresh = tuple(line for line in block.lines if line.no not in covered_lines)
        if not fresh:
            continue
        evidence = {
            template(line.text) for line in fresh if scored[line.no].score >= SEED_THRESHOLD
        }
        if evidence and "failed_step_tail" not in block.signals:
            novelty = len(evidence - covered_templates) / len(evidence)
            if novelty < MIN_NOVELTY:
                continue  # mostly the same evidence as a block we already have
        candidate = Block(fresh[0].no, fresh[-1].no, block.score, block.signals, fresh)
        cost = _block_tokens(candidate.lines)
        share = TAIL_SHARE if "failed_step_tail" in block.signals else MAX_BLOCK_SHARE
        limit = min(remaining, max(1, int(budget * share)))
        if cost > limit:
            trimmed = _trim_to_budget(candidate, scored, limit)
            if trimmed is None:
                continue
            candidate, cost = trimmed, _block_tokens(trimmed.lines)
        chosen.append(candidate)
        used += cost
        covered_lines.update(line.no for line in candidate.lines)
        covered_templates.update(
            template(line.text)
            for line in candidate.lines
            if scored[line.no].score >= SEED_THRESHOLD
        )
    chosen.sort(key=lambda b: b.start)
    return Excerpt(blocks=tuple(chosen), tokens=used, budget=budget, candidates=len(blocks))


def build_excerpt(log: NormalizedLog, budget: int) -> Excerpt:
    scored = score_lines(log)
    return select_blocks(candidate_blocks(log, scored), scored, budget)
