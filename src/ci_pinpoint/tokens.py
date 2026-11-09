"""Cheap, dependency-free token estimates.

Exact counts depend on the model's tokenizer, which we usually don't have
locally. For budgeting, a conservative estimate is enough: log text (paths,
hashes, stack frames) tokenises at roughly 3.5 to 4 characters per token for
BPE tokenizers, so we use 3.5 and round up.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable

CHARS_PER_TOKEN = 3.5
LINE_OVERHEAD_TOKENS = 3  # the "L1234: " prefix and the newline
MAX_LINE_CHARS = 400  # longer lines are clipped in excerpts (minified JS, base64, JSON blobs)
CLIP_MARKER = re.compile(r"\s*… \[\+\d+ chars\]$")


def clip_line(text: str, limit: int = MAX_LINE_CHARS) -> str:
    """Clip a long line for an excerpt, saying how much was cut."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]} … [+{len(text) - limit} chars]"


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def estimate_line_tokens(text: str) -> int:
    """Tokens for one excerpt line, as it is shown (clipped) to the model."""
    return estimate_tokens(clip_line(text)) + LINE_OVERHEAD_TOKENS


def estimate_lines_tokens(lines: Iterable[str]) -> int:
    """Tokens to send these lines unclipped, e.g. a whole raw log."""
    return sum(estimate_tokens(line) + LINE_OVERHEAD_TOKENS for line in lines)
