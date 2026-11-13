from __future__ import annotations

import threading
import time
from collections.abc import Mapping, Sequence

from ci_pinpoint.config import Settings
from ci_pinpoint.models import RawLog
from ci_pinpoint.pipeline import explain_all
from ci_pinpoint.providers.base import Completion, Message
from ci_pinpoint.providers.fake import FakeChat

LOG = "\n".join(
    ["Run tests", "=== FAILURES ===", "E   assert 1 == 2", "AssertionError", "exit code 1"]
)


def _raws(n: int) -> list[RawLog]:
    return [
        RawLog(text=LOG, source=f"job{i}", job_name=f"job{i}", failed_steps=("Run tests",))
        for i in range(n)
    ]


class _SlowChat(FakeChat):
    """A fake that sleeps and records how many calls overlap."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def complete(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        json_mode: bool = False,
        json_schema: Mapping[str, object] | None = None,
    ) -> Completion:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.1)
            return super().complete(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                json_mode=json_mode,
                json_schema=json_schema,
            )
        finally:
            with self._lock:
                self.active -= 1


def test_results_keep_input_order() -> None:
    briefs = explain_all(_raws(5), Settings(concurrency=4), FakeChat(), env={})
    assert [b.job_name for b in briefs] == [f"job{i}" for i in range(5)]


def test_jobs_run_concurrently() -> None:
    chat = _SlowChat()
    briefs = explain_all(_raws(4), Settings(concurrency=4), chat, env={})
    assert len(briefs) == 4
    assert chat.max_active > 1  # at least two diagnoses overlapped


def test_concurrency_one_is_sequential() -> None:
    chat = _SlowChat()
    explain_all(_raws(3), Settings(concurrency=1), chat, env={})
    assert chat.max_active == 1


def test_matches_sequential_results() -> None:
    raws = _raws(3)
    parallel = explain_all(raws, Settings(concurrency=3), FakeChat(), env={})
    serial = explain_all(raws, Settings(concurrency=1), FakeChat(), env={})
    # Exclude latency_s (wall-clock timing varies run to run).
    dump = {"diagnosis": {"latency_s"}}
    assert [b.model_dump(exclude=dump) for b in parallel] == [
        b.model_dump(exclude=dump) for b in serial
    ]


def test_usage_total_adds_across_jobs() -> None:
    briefs = explain_all(_raws(3), Settings(concurrency=3), FakeChat(), env={})
    for brief in briefs:
        assert brief.accounting is not None
        assert brief.accounting.input_tokens > 0
