"""Cheap, dependency-free token estimates.

Exact counts depend on the model's tokenizer, which we usually don't have
locally. For budgeting, a conservative estimate is enough: log text (paths,
hashes, stack frames) tokenises at roughly 3.5 to 4 characters per token for
BPE tokenizers, so we use 3.5 and round up.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

CHARS_PER_TOKEN = 3.5
LINE_OVERHEAD_TOKENS = 3  # the "L1234: " prefix and the newline


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def estimate_line_tokens(text: str) -> int:
    return estimate_tokens(text) + LINE_OVERHEAD_TOKENS


def estimate_lines_tokens(lines: Iterable[str]) -> int:
    return sum(estimate_line_tokens(line) for line in lines)
