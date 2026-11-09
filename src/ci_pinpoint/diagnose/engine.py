"""Stage 2: ask a model for a structured diagnosis, repair it if needed, verify it."""

from __future__ import annotations

import logging
import time

from ci_pinpoint.diagnose.citations import verify_citations
from ci_pinpoint.diagnose.parse import DiagnosisParseError, parse_diagnosis
from ci_pinpoint.diagnose.prompt import DIAGNOSIS_SCHEMA, build_messages, repair_message
from ci_pinpoint.models import DiagnosisResult, Excerpt, NormalizedLog, TokenUsage
from ci_pinpoint.providers.base import ChatModel, Message

log = logging.getLogger(__name__)


class DiagnosisFailedError(RuntimeError):
    """The model never produced a usable diagnosis."""


def diagnose(
    chat: ChatModel,
    log_: NormalizedLog,
    excerpt: Excerpt,
    *,
    source: str,
    job_name: str | None = None,
    failed_steps: list[str] | None = None,
    max_output_tokens: int = 1024,
    repair_attempts: int = 1,
) -> DiagnosisResult:
    messages: list[Message] = build_messages(
        excerpt, source=source, job_name=job_name, failed_steps=failed_steps or []
    )
    usage = TokenUsage()
    started = time.perf_counter()
    lines = log_.by_number()
    shown = excerpt.line_numbers()
    last_error = ""
    model_name = chat.model
    for attempt in range(1, repair_attempts + 2):
        completion = chat.complete(
            messages,
            max_tokens=max_output_tokens,
            temperature=0.0,
            json_mode=True,
            json_schema=DIAGNOSIS_SCHEMA,
        )
        usage = usage + completion.usage
        model_name = completion.model
        try:
            diagnosis = parse_diagnosis(completion.text)
        except DiagnosisParseError as exc:
            last_error = str(exc)
            log.warning(
                "model reply did not parse",
                extra={"attempt": attempt, "error": last_error, "reply": completion.text[:600]},
            )
        else:
            verified, rejected = verify_citations(diagnosis.citations, lines, shown)
            if verified or attempt > repair_attempts:
                if rejected:
                    log.info(
                        "citations rejected",
                        extra={"rejected": len(rejected), "verified": len(verified)},
                    )
                return DiagnosisResult(
                    diagnosis=diagnosis,
                    citations=verified,
                    rejected_citations=rejected,
                    model=model_name,
                    provider=chat.provider,
                    attempts=attempt,
                    usage=usage,
                    latency_s=round(time.perf_counter() - started, 3),
                )
            last_error = "none of the citations match the log: " + "; ".join(
                f"L{r.line} ({r.reason})" for r in rejected[:3]
            )
        log.warning(
            "diagnosis unusable, asking for a repair",
            extra={"attempt": attempt, "error": last_error},
        )
        messages = [
            *messages,
            Message("assistant", completion.text[:4000]),
            repair_message(last_error),
        ]
    raise DiagnosisFailedError(
        f"{chat.provider}/{model_name} gave no usable diagnosis after "
        f"{repair_attempts + 1} attempt(s): {last_error}"
    )
