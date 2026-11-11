"""Stage 2: ask a model for a structured diagnosis, repair it if needed, verify it."""

from __future__ import annotations

import logging
import time

from ci_pinpoint.diagnose.citations import UNTRUSTED_REASON, verify_citations
from ci_pinpoint.diagnose.parse import DiagnosisParseError, parse_diagnosis
from ci_pinpoint.diagnose.prompt import DIAGNOSIS_SCHEMA, build_messages, repair_message
from ci_pinpoint.injection import INJECTED_CONFIDENCE_CAP, check_output, flag_lines
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
    flagged = flag_lines(line for block in excerpt.blocks for line in block.lines)
    if flagged:
        log.warning(
            "instruction-like text in the log",
            extra={"source": source, "lines": sorted(flagged)[:20]},
        )
    messages: list[Message] = build_messages(
        excerpt,
        source=source,
        job_name=job_name,
        failed_steps=failed_steps or [],
        flagged=flagged,
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
            if completion.finish_reason in ("length", "max_tokens"):
                last_error += f" (stopped at the {max_output_tokens}-token output limit)"
            log.warning(
                "model reply did not parse",
                extra={"attempt": attempt, "error": last_error, "reply": completion.text[:600]},
            )
        else:
            verified, rejected = verify_citations(diagnosis.citations, lines, shown, flagged)
            if verified or attempt > repair_attempts:
                if rejected:
                    log.info(
                        "citations rejected",
                        extra={"rejected": len(rejected), "verified": len(verified)},
                    )
                check = check_output(diagnosis, lines, flagged, shown)
                if check.injection_suspected and (diagnosis.confidence or 0) > (
                    INJECTED_CONFIDENCE_CAP
                ):
                    diagnosis = diagnosis.model_copy(update={"confidence": INJECTED_CONFIDENCE_CAP})
                warnings = list(check.warnings)
                if any(r.reason == UNTRUSTED_REASON for r in rejected):
                    warnings.append(
                        "the model cited instruction-like log lines; those citations were dropped"
                    )
                if warnings:
                    log.warning("diagnosis warnings", extra={"warnings": warnings})
                return DiagnosisResult(
                    diagnosis=diagnosis,
                    citations=verified,
                    rejected_citations=rejected,
                    model=model_name,
                    provider=chat.provider,
                    attempts=attempt,
                    usage=usage,
                    latency_s=round(time.perf_counter() - started, 3),
                    warnings=warnings,
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
