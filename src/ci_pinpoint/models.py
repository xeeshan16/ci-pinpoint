"""Core data types shared by every stage of the pipeline.

The pipeline is: raw log -> normalised lines and sections -> scored candidate
blocks -> a budgeted excerpt -> an LLM diagnosis whose citations are checked
against the original log -> a brief.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, computed_field


@dataclass(frozen=True, slots=True)
class RawLog:
    """A log exactly as it was fetched, plus where it came from."""

    text: str
    source: str
    job_name: str | None = None
    job_url: str | None = None
    failed_steps: tuple[str, ...] = ()


class LineKind(StrEnum):
    TEXT = "text"
    GROUP = "group"
    ENDGROUP = "endgroup"
    COMMAND = "command"  # the script echo GitHub prints inside a "Run ..." group header
    ERROR_ANNOTATION = "error_annotation"
    WARNING_ANNOTATION = "warning_annotation"


@dataclass(frozen=True, slots=True)
class LogLine:
    """One normalised line. ``no`` is the 1-based line number in the raw log."""

    no: int
    text: str
    section: int
    kind: LineKind = LineKind.TEXT


@dataclass(frozen=True, slots=True)
class Section:
    """A run of lines under one ``##[group]`` / ``::group::`` / GitLab section header."""

    index: int
    title: str
    first_line: int
    last_line: int
    has_error: bool


@dataclass(frozen=True, slots=True)
class NormalizedLog:
    lines: tuple[LogLine, ...]
    sections: tuple[Section, ...]

    def by_number(self) -> dict[int, LogLine]:
        return {line.no: line for line in self.lines}


@dataclass(frozen=True, slots=True)
class ScoredLine:
    line: LogLine
    score: float
    signals: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Block:
    """A contiguous range of lines that is a candidate explanation for the failure."""

    start: int  # raw line number, inclusive
    end: int  # raw line number, inclusive
    score: float
    signals: tuple[str, ...]
    lines: tuple[LogLine, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class Excerpt:
    """The blocks chosen to fit a token budget, in log order."""

    blocks: tuple[Block, ...]
    tokens: int
    budget: int
    candidates: int

    def line_numbers(self) -> set[int]:
        return {line.no for block in self.blocks for line in block.lines}


class FailureCategory(StrEnum):
    COMPILE = "compile"
    TEST = "test"
    LINT_FORMAT = "lint_format"
    DEPENDENCY = "dependency"
    CONFIGURATION = "configuration"
    INFRASTRUCTURE = "infrastructure"
    NETWORK = "network"
    TIMEOUT = "timeout"
    RESOURCE = "resource"
    PERMISSION = "permission"
    OTHER = "other"


class Citation(BaseModel):
    """A log line the model cites as evidence."""

    model_config = ConfigDict(extra="ignore")

    line: int = Field(
        ge=1, description="Line number exactly as shown in the excerpt (the L prefix)."
    )
    text: str = Field(min_length=1, description="The cited line copied verbatim from the excerpt.")


class Diagnosis(BaseModel):
    """What the model must return. Validated before anything is shown to a user."""

    model_config = ConfigDict(extra="ignore")

    category: FailureCategory
    summary: str = Field(min_length=1, max_length=300)
    root_cause: str = Field(min_length=1, max_length=2000)
    citations: list[Citation] = Field(min_length=1, max_length=8)
    suggested_fix: str = Field(min_length=1, max_length=2000)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class CitationStatus(StrEnum):
    VERIFIED = "verified"
    RELOCATED = "relocated"


class VerifiedCitation(BaseModel):
    line: int
    text: str
    status: CitationStatus


class RejectedCitation(BaseModel):
    line: int
    text: str
    reason: str


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class DiagnosisResult(BaseModel):
    diagnosis: Diagnosis
    citations: list[VerifiedCitation]
    rejected_citations: list[RejectedCitation]
    model: str
    provider: str
    attempts: int
    usage: TokenUsage
    latency_s: float

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verified(self) -> bool:
        return bool(self.citations)


class ExcerptBlockOut(BaseModel):
    start: int
    end: int
    score: float
    signals: list[str]
    lines: list[tuple[int, str]]


class LogStats(BaseModel):
    raw_lines: int
    raw_tokens: int
    excerpt_tokens: int
    sections: int
    failed_sections: list[str]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def reduction(self) -> float:
        if self.raw_tokens == 0:
            return 0.0
        return 1.0 - self.excerpt_tokens / self.raw_tokens


class Brief(BaseModel):
    """The output of ``ci-pinpoint explain`` for one job."""

    source: str
    job_name: str | None
    job_url: str | None
    failed_steps: list[str] = Field(
        default_factory=list,
        description="Step names from the CI API when known, else titles of sections with errors.",
    )
    stats: LogStats
    excerpt: list[ExcerptBlockOut]
    diagnosis: DiagnosisResult | None
    error: str | None = None
