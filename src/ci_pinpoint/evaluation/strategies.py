"""Ways to pick the lines of a failed log that a reader (or a model) gets to see.

Every strategy gets the same normalised log and the same token budget, counted
with the same estimator ci-pinpoint uses for its own excerpts (long lines are
clipped the same way), so the comparison is about *which* lines are chosen.

* ``tail``: the last lines of the log, what ``tail -n`` or most CI UIs show.
* ``grep``: the last lines matching ``error|fail`` (case-insensitive), like
  ``grep -iE 'error|fail' | tail``.
* ``pinpoint``: ci-pinpoint's selection stage (heuristics, template mining,
  failed-step tails), the excerpt the model sees. No model involved.
* ``pinpoint-nofill``: the same without spending leftover budget on the failed
  step's output (the selection before day 3), kept as an ablation.
* ``pinpoint+emb``: the same, with blocks re-ranked by an embedding model.
* ``pinpoint+llm``: the same, with blocks re-ranked by a chat model
  (:mod:`ci_pinpoint.rerank`).

The baselines run on the cleaned log (no ANSI codes or timestamps), which makes
them cheaper and therefore stronger than running them on the raw bytes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from ci_pinpoint.heuristics import BlockRanker, analyse, candidate_blocks, select_blocks
from ci_pinpoint.models import LineKind, LogLine, NormalizedLog
from ci_pinpoint.tokens import estimate_line_tokens

GREP_RE = re.compile(r"error|fail", re.IGNORECASE)
_HIDDEN = (LineKind.ENDGROUP,)


@dataclass(frozen=True, slots=True)
class Selection:
    """The raw line numbers a strategy shows, and what they cost."""

    lines: frozenset[int]
    tokens: int


class Strategy(Protocol):
    name: str

    def select(self, log: NormalizedLog, budgets: Sequence[int]) -> dict[int, Selection]: ...


def _visible(log: NormalizedLog) -> list[LogLine]:
    return [line for line in log.lines if line.text.strip() and line.kind not in _HIDDEN]


def _take_from_end(lines: Iterable[LogLine], budget: int) -> Selection:
    chosen: set[int] = set()
    used = 0
    for line in reversed(list(lines)):
        cost = estimate_line_tokens(line.text)
        if used + cost > budget:
            break
        chosen.add(line.no)
        used += cost
    return Selection(frozenset(chosen), used)


class TailStrategy:
    name = "tail"

    def select(self, log: NormalizedLog, budgets: Sequence[int]) -> dict[int, Selection]:
        lines = _visible(log)
        return {b: _take_from_end(lines, b) for b in budgets}


class GrepStrategy:
    name = "grep"

    def __init__(self, pattern: re.Pattern[str] = GREP_RE) -> None:
        self.pattern = pattern

    def select(self, log: NormalizedLog, budgets: Sequence[int]) -> dict[int, Selection]:
        lines = [line for line in _visible(log) if self.pattern.search(line.text)]
        return {b: _take_from_end(lines, b) for b in budgets}


class PinpointStrategy:
    """ci-pinpoint's own selection stage, optionally with an embedding re-ranker."""

    def __init__(
        self,
        ranker: BlockRanker | None = None,
        name: str | None = None,
        *,
        fill: bool = True,
        kind: str = "emb",
    ) -> None:
        self.ranker = ranker
        self.fill = fill
        default = f"pinpoint+{kind}" if ranker is not None else "pinpoint"
        self.name = name or (default if fill else f"{default}-nofill")

    def select(self, log: NormalizedLog, budgets: Sequence[int]) -> dict[int, Selection]:
        ctx = analyse(log)
        blocks = candidate_blocks(ctx)
        if self.ranker is not None:
            blocks = self.ranker.rank(blocks, ctx)
        out: dict[int, Selection] = {}
        for budget in budgets:
            excerpt = select_blocks(blocks, ctx, budget, fill=self.fill)
            out[budget] = Selection(frozenset(excerpt.line_numbers()), excerpt.tokens)
        return out


BASELINES = ("tail", "grep")


def build_strategies(
    names: Sequence[str],
    ranker: BlockRanker | None = None,
    llm_ranker: BlockRanker | None = None,
) -> list[Strategy]:
    """Strategies by name. ``pinpoint+emb`` needs a ranker, ``pinpoint+llm`` an LLM ranker."""
    out: list[Strategy] = []
    for name in names:
        if name == "tail":
            out.append(TailStrategy())
        elif name == "grep":
            out.append(GrepStrategy())
        elif name == "pinpoint":
            out.append(PinpointStrategy())
        elif name == "pinpoint-nofill":
            out.append(PinpointStrategy(fill=False))
        elif name == "pinpoint+emb":
            if ranker is None:
                raise ValueError("pinpoint+emb needs an embedder (--embedder)")
            out.append(PinpointStrategy(ranker))
        elif name == "pinpoint+llm":
            if llm_ranker is None:
                raise ValueError("pinpoint+llm needs a chat model (--rerank with --provider)")
            out.append(PinpointStrategy(llm_ranker, kind="llm"))
        else:
            raise ValueError(f"unknown strategy {name!r}")
    return out


STRATEGY_NAMES = ("tail", "grep", "pinpoint", "pinpoint-nofill", "pinpoint+emb", "pinpoint+llm")
