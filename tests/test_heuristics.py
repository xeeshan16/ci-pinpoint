from __future__ import annotations

import pytest

from ci_pinpoint.heuristics import build_excerpt, score_lines, score_text, template
from ci_pinpoint.normalize import normalize
from ci_pinpoint.tokens import estimate_lines_tokens, estimate_tokens

from .conftest import RealCase


@pytest.mark.parametrize(
    "line",
    [
        "[INFO] Tests run: 4, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.1 s",
        "Found 0 errors in 12 files",
        'metric{error_type="none"} 1.0',
        "gcc -Werror -o main main.c",
        "[ERROR] Re-run Maven using the -X switch to enable full debug logging.",
        "> Run with --stacktrace option to get the stack trace.",
        "",
    ],
)
def test_success_lines_score_zero(line: str) -> None:
    assert score_text(line)[0] == 0.0


@pytest.mark.parametrize(
    ("line", "signal"),
    [
        ("Caused by: java.net.ConnectException: Connection refused", "caused_by"),
        ("src/main.c:12:5: error: 'x' undeclared", "compiler_error"),
        ("error[E0425]: cannot find value `x` in this scope", "compiler_error"),
        ("FAILED tests/test_api.py::test_login - AssertionError", "test_failure"),
        ("Traceback (most recent call last):", "traceback"),
        ("npm ERR! code ELIFECYCLE", "npm_error"),
        ("panic: runtime error: index out of range", "panic"),
        ("The runner has received a shutdown signal. Killed", "oom"),
        ("MISSING              gen_ai_request_model", "missing"),
    ],
)
def test_failure_lines_score(line: str, signal: str) -> None:
    score, names = score_text(line)
    assert score >= 2.0
    assert signal in names


def test_warning_level_is_dampened() -> None:
    fatal = score_text("java.lang.IllegalStateException: boom")[0]
    logged = score_text("12:00:00 [main] WARN Foo -- IllegalStateException: boom")[0]
    assert logged < fatal


def test_template_masks_numbers_hashes_and_quotes() -> None:
    assert template("Took 12.5 s for 'abc' at 3f2a9b1c") == template(
        "Took 3 s for 'x' at 9a8b7c6d5"
    )


def test_tokens() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcdefg") == 2
    assert estimate_lines_tokens(["abc", ""]) == 1 + 3 + 0 + 3


def test_failed_step_tail_is_selected_even_without_error_words() -> None:
    lines = [f"2026-01-01T00:00:00.0000000Z ok step {i}" for i in range(200)]
    lines += [
        "2026-01-01T00:00:00.0000000Z ##[group]Run ./check-dashboards.sh",
        "2026-01-01T00:00:00.0000000Z ##[endgroup]",
        "2026-01-01T00:00:00.0000000Z dashboards: 2 metric(s) not exported",
        "2026-01-01T00:00:00.0000000Z ##[error]Process completed with exit code 1.",
    ]
    excerpt = build_excerpt(normalize("\n".join(lines)), budget=300)
    texts = [line.text for block in excerpt.blocks for line in block.lines]
    assert "dashboards: 2 metric(s) not exported" in texts
    assert excerpt.tokens <= 300


def test_command_echo_is_not_evidence() -> None:
    log = normalize("##[group]Run make\necho 'error: will not happen'\n##[endgroup]\nall good\n")
    echo = next(s for s in score_lines(log) if s.line.no == 2)
    assert echo.score < 1.0
    assert "command_echo" in echo.signals


def test_stack_frames_are_compacted() -> None:
    frames = [f"\tat com.example.C{i}.m(C{i}.java:{i})" for i in range(30)]
    text = "\n".join(["java.lang.IllegalStateException: boom", *frames, "BUILD FAILURE"])
    excerpt = build_excerpt(normalize(text), budget=2000)
    kept = [line.text for block in excerpt.blocks for line in block.lines]
    assert sum(1 for t in kept if t.startswith("\tat ")) < 10  # of 30


def test_repeated_evidence_is_not_selected_twice() -> None:
    chunk = [
        "[ERROR] com.example.FooTest.case -- <<< FAILURE!",
        "Caused by: java.lang.IllegalArgumentException: unknown PII type 'x'",
    ]
    filler = [f"[INFO] building module {i}" for i in range(20)]
    text = "\n".join((chunk + filler) * 5)
    excerpt = build_excerpt(normalize(text), budget=2000)
    cause_lines = [
        line for block in excerpt.blocks for line in block.lines if "unknown PII type" in line.text
    ]
    assert len(cause_lines) == 1


def test_empty_log_gives_empty_excerpt() -> None:
    excerpt = build_excerpt(normalize(""), budget=500)
    assert excerpt.blocks == ()
    assert excerpt.tokens == 0


@pytest.mark.parametrize("budget", [800, 2000, 4000])
def test_real_logs_root_cause_is_in_the_excerpt(real_case: RealCase, budget: int) -> None:
    """On real failed GitHub Actions jobs, every labelled root-cause line survives selection."""
    log = normalize(real_case.text())
    excerpt = build_excerpt(log, budget=budget)
    selected = [line.text for block in excerpt.blocks for line in block.lines]
    for anchor in real_case.anchors:
        assert any(anchor in text for text in selected), anchor
    assert excerpt.tokens <= budget
    raw = estimate_lines_tokens(line.text for line in log.lines)
    assert excerpt.tokens < raw * 0.35
