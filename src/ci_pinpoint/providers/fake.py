"""Deterministic providers for tests and CI. Never a substitute for a real model.

``FakeChat`` reads the excerpt out of the prompt, picks the lines the heuristic
scorer likes best and returns a schema-valid diagnosis. Its output is labelled
``fake`` everywhere it is shown.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import deque
from collections.abc import Iterable, Sequence

from ci_pinpoint.heuristics import score_text
from ci_pinpoint.models import FailureCategory, TokenUsage
from ci_pinpoint.providers.base import Completion, Message
from ci_pinpoint.tokens import estimate_tokens

EXCERPT_LINE = re.compile(r"^L(\d+): (.*)$", re.MULTILINE)

_CATEGORY_RULES: tuple[tuple[re.Pattern[str], FailureCategory], ...] = (
    (
        re.compile(
            r"not formatted|format violations|spotless|prettier|black|ruff format|lint", re.I
        ),
        FailureCategory.LINT_FORMAT,
    ),
    (
        re.compile(r"connection refused|ECONNREFUSED|could not resolve|5432|network", re.I),
        FailureCategory.INFRASTRUCTURE,
    ),
    (
        re.compile(r"OutOfMemory|OOMKilled|out of memory|exit code 137", re.I),
        FailureCategory.RESOURCE,
    ),
    (re.compile(r"timed? ?out|deadline exceeded", re.I), FailureCategory.TIMEOUT),
    (re.compile(r"permission denied|forbidden|unauthori[sz]ed", re.I), FailureCategory.PERMISSION),
    (
        re.compile(r"could not resolve dependencies|ModuleNotFoundError|No matching version", re.I),
        FailureCategory.DEPENDENCY,
    ),
    (
        re.compile(r"error(?:\[\w+\])?:|cannot find symbol|compilation", re.I),
        FailureCategory.COMPILE,
    ),
    (
        re.compile(r"FAILED|<<< FAILURE|AssertionError|Tests run:.*Failures: [1-9]|MISSING", re.I),
        FailureCategory.TEST,
    ),
    (
        re.compile(r"invalid|unknown|requirement failed|advertised\.listeners", re.I),
        FailureCategory.CONFIGURATION,
    ),
)


def _category(lines: Iterable[str]) -> FailureCategory:
    joined = "\n".join(lines)
    for pattern, category in _CATEGORY_RULES:
        if pattern.search(joined):
            return category
    return FailureCategory.OTHER


class FakeChat:
    """Heuristic stand-in for a model. Deterministic for a given prompt."""

    def __init__(self, model: str = "fake-diagnoser") -> None:
        self._model = model

    @property
    def provider(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return self._model

    def complete(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        json_mode: bool = False,
    ) -> Completion:
        prompt = messages[-1].content if messages else ""
        excerpt = [(int(n), text) for n, text in EXCERPT_LINE.findall(prompt)]
        ranked = sorted(excerpt, key=lambda item: (-score_text(item[1])[0], item[0]))
        top = [item for item in ranked if score_text(item[1])[0] > 0][:3] or ranked[:1]
        top.sort()
        cited = [{"line": n, "text": text} for n, text in top] or [
            {"line": 1, "text": "(empty log)"}
        ]
        lead = top[0][1].strip() if top else "the log is empty"
        body = {
            "category": _category(text for _, text in top).value,
            "summary": f"[fake provider] {lead[:200]}",
            "root_cause": "[fake provider] Heuristic pick of the strongest error lines; "
            "configure a real model for an actual diagnosis.",
            "citations": cited,
            "suggested_fix": "[fake provider] Read the cited lines.",
            "confidence": 0.1,
        }
        text = json.dumps(body)
        return Completion(
            text=text,
            model=self._model,
            usage=TokenUsage(
                input_tokens=sum(estimate_tokens(m.content) for m in messages),
                output_tokens=estimate_tokens(text),
            ),
            latency_s=0.0,
            finish_reason="stop",
        )


class ScriptedChat:
    """Returns pre-written responses in order; records every request. For tests."""

    def __init__(self, responses: Iterable[str], model: str = "scripted") -> None:
        self._responses = deque(responses)
        self._model = model
        self.requests: list[list[Message]] = []

    @property
    def provider(self) -> str:
        return "scripted"

    @property
    def model(self) -> str:
        return self._model

    def complete(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        json_mode: bool = False,
    ) -> Completion:
        self.requests.append(list(messages))
        if not self._responses:
            raise AssertionError("ScriptedChat ran out of responses")
        text = self._responses.popleft()
        return Completion(text, self._model, TokenUsage(input_tokens=10, output_tokens=5), 0.0)


class FakeEmbedder:
    """Hashed bag-of-words vectors: similar text gets similar vectors, deterministically."""

    def __init__(self, dimensions: int = 256) -> None:
        self._dimensions = dimensions

    @property
    def model(self) -> str:
        return f"fake-hash-{self._dimensions}"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self._dimensions
            for token in re.findall(r"[a-z0-9_]+", text.lower()):
                digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "little") % self._dimensions
                vec[index] += 1.0 if digest[4] & 1 else -1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out
