"""Turn model text into a validated :class:`Diagnosis`, tolerating common wrappers."""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any

from pydantic import ValidationError

from ci_pinpoint.models import Diagnosis, FailureCategory

MAX_CITATIONS = 8
FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class DiagnosisParseError(ValueError):
    pass


class _TruncatedError(Exception):
    pass


def _first_json_object(text: str) -> str | None:
    """The first top-level {...} span, respecting strings.

    Only the outermost object counts: if it never closes, the reply was cut off,
    and an inner object (a single citation, a ``{0}`` quoted from the log) must
    not be mistaken for the answer.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise _TruncatedError


_TEXT_KEYS = ("description", "explanation", "cause", "summary", "details", "text", "reason")
_CITATION_KEYS = ("citations", "evidence", "cited_lines", "lines", "log_lines")
_CITED_LINE = re.compile(r"^\s*L?(\d+)\s*[:|\-]\s?(.*)$", re.DOTALL)
_CATEGORY_ALIASES = {
    "format": "lint_format",
    "lint": "lint_format",
    "formatting": "lint_format",
    "tests": "test",
    "build": "compile",
    "config": "configuration",
    "infra": "infrastructure",
    "dependencies": "dependency",
}


def _as_text(value: Any) -> Any:
    """Small models sometimes wrap a string field in an object or a list."""
    if isinstance(value, dict):
        for key in _TEXT_KEYS:
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key]
        parts = [v for v in value.values() if isinstance(v, str) and v.strip()]
        return "\n".join(parts) if parts else value
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        return "\n".join(f"- {v}" if len(value) > 1 else v for v in value)
    return value


def _citation(item: Any) -> Any:
    if isinstance(item, str):
        match = _CITED_LINE.match(item)
        return {"line": int(match.group(1)), "text": match.group(2)} if match else item
    if isinstance(item, dict):
        line = item.get("line", item.get("line_number", item.get("lineNumber")))
        text = item.get("text", item.get("content", item.get("line_text")))
        if isinstance(line, str):
            digits = re.sub(r"^\s*L", "", line).strip()
            line = int(digits) if digits.isdigit() else line
        return {"line": line, "text": text}
    return item


def _lift_nested(out: dict[str, Any]) -> dict[str, Any]:
    """Move fields nested one level down (e.g. citations inside root_cause) up."""
    for key in ("root_cause", "diagnosis", "analysis"):
        nested = out.get(key)
        if isinstance(nested, dict):
            for inner in (*_CITATION_KEYS, "confidence", "suggested_fix", "category"):
                if inner in nested and inner not in out:
                    out[inner] = nested[inner]
    if "citations" not in out:
        for alias in _CITATION_KEYS[1:]:
            if isinstance(out.get(alias), list):
                out["citations"] = out[alias]
                break
    return out


def _as_confidence(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        with contextlib.suppress(ValueError):
            value = float(text.rstrip("%")) / (100 if text.endswith("%") else 1)
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 1:
        return min(1.0, value / 100)
    return value


def _normalise(data: dict[str, Any]) -> dict[str, Any]:
    """Fix harmless deviations small local models make, before strict validation.

    Nothing here invents content: values are only moved, unwrapped or re-typed.
    Citations still have to verify against the log afterwards.
    """
    out = _lift_nested(dict(data))
    for key in ("summary", "root_cause", "suggested_fix"):
        out[key] = _as_text(out.get(key))
    if out.get("summary") is None and isinstance(out.get("root_cause"), str):
        out["summary"] = out["root_cause"].split("\n")[0][:300]

    category = out.get("category")
    if isinstance(category, str):
        value = category.strip().lower().replace("-", "_").replace(" ", "_")
        value = _CATEGORY_ALIASES.get(value, value)
        valid = {c.value for c in FailureCategory}
        out["category"] = value if value in valid else FailureCategory.OTHER.value
    citations = out.get("citations")
    if isinstance(citations, list):
        # The schema allows 8; extra citations are dropped rather than failing the
        # whole answer (they are listed most important first).
        out["citations"] = [_citation(item) for item in citations[:MAX_CITATIONS]]

    out["confidence"] = _as_confidence(out.get("confidence"))
    if isinstance(out.get("summary"), str) and len(out["summary"]) > 300:
        out["summary"] = out["summary"][:297] + "..."
    return out


def parse_diagnosis(text: str) -> Diagnosis:
    candidates: list[str] = [m.group(1) for m in FENCE.finditer(text)]
    candidates.append(text)
    last_error = "no JSON object found in the reply"
    for candidate in candidates:
        try:
            blob = _first_json_object(candidate)
        except _TruncatedError:
            last_error = (
                "the JSON object is incomplete (the reply was probably cut off); "
                "keep root_cause short and do not paste log lines into it"
            )
            continue
        if blob is None:
            continue
        try:
            data = json.loads(blob, strict=False)
        except json.JSONDecodeError as exc:
            last_error = f"invalid JSON: {exc.msg} at position {exc.pos}"
            continue
        if not isinstance(data, dict):
            last_error = "the JSON value is not an object"
            continue
        try:
            return Diagnosis.model_validate(_normalise(data))
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]
            )
            last_error = f"schema validation failed: {problems}"
    raise DiagnosisParseError(last_error)
