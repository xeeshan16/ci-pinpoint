"""Check every citation against the log. Nothing unverifiable reaches the user.

A citation is:

* ``verified`` when its line number exists and its text matches that line,
* ``relocated`` when the number is wrong but the text is found verbatim on
  exactly one line the model was shown (we fix the number),
* rejected otherwise (invented text, a line the model never saw, or a line that
  reads like an instruction to the model: see :mod:`ci_pinpoint.injection`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Set
from difflib import SequenceMatcher

from ci_pinpoint.models import (
    Citation,
    CitationStatus,
    LogLine,
    RejectedCitation,
    VerifiedCitation,
)
from ci_pinpoint.tokens import CLIP_MARKER

_WS = re.compile(r"\s+")
_PREFIX = re.compile(r"^\s*L\d+:\s?")
MIN_SUBSTRING_CHARS = 12
MIN_RATIO = 0.85
UNTRUSTED_REASON = "line contains instruction-like text from the log and cannot be evidence"


def _norm(text: str) -> str:
    text = CLIP_MARKER.sub("", _PREFIX.sub("", text))
    return _WS.sub(" ", text).strip().lower()


def text_matches(cited: str, actual: str) -> bool:
    a, b = _norm(cited), _norm(actual)
    if not a or not b:
        return False
    if a == b:
        return True
    # Models often quote the meaningful part of a long line, or trim an ellipsis.
    a_core = a.removesuffix("...").removesuffix("…").strip()
    if len(a_core) >= MIN_SUBSTRING_CHARS and a_core in b:
        return True
    if len(b) >= MIN_SUBSTRING_CHARS and b in a and len(b) / len(a) > 0.6:
        return True
    return SequenceMatcher(None, a, b).ratio() >= MIN_RATIO


def verify_citations(
    citations: Iterable[Citation],
    lines: dict[int, LogLine],
    shown: set[int],
    untrusted: Set[int] = frozenset(),
) -> tuple[list[VerifiedCitation], list[RejectedCitation]]:
    """``untrusted`` lines (instruction-like text in the log) are never accepted."""
    verified: list[VerifiedCitation] = []
    rejected: list[RejectedCitation] = []
    seen: set[int] = set()
    citable = shown - untrusted
    for citation in citations:
        actual = lines.get(citation.line)
        if citation.line in untrusted and actual is not None and citation.line in shown:
            rejected.append(
                RejectedCitation(
                    line=citation.line,
                    text=citation.text[:300],
                    reason=UNTRUSTED_REASON,
                )
            )
            continue
        if (
            actual is not None
            and citation.line in shown
            and text_matches(citation.text, actual.text)
        ):
            if citation.line not in seen:
                verified.append(
                    VerifiedCitation(
                        line=actual.no, text=actual.text, status=CitationStatus.VERIFIED
                    )
                )
                seen.add(actual.no)
            continue
        matches = [
            no
            for no in sorted(citable)
            if no in lines and text_matches(citation.text, lines[no].text)
        ]
        if len(matches) == 1:
            no = matches[0]
            if no not in seen:
                verified.append(
                    VerifiedCitation(line=no, text=lines[no].text, status=CitationStatus.RELOCATED)
                )
                seen.add(no)
            continue
        if actual is None:
            reason = "line number is outside the log"
        elif citation.line not in shown:
            reason = "line was not in the excerpt the model saw"
        elif len(matches) > 1:
            reason = "text does not match the cited line and is ambiguous elsewhere"
        else:
            reason = "text does not match the log"
        rejected.append(
            RejectedCitation(line=citation.line, text=citation.text[:300], reason=reason)
        )
    return verified, rejected
