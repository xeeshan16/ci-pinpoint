"""Turn model text into a validated :class:`Diagnosis`, tolerating common wrappers."""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any

from pydantic import ValidationError

from ci_pinpoint.models import Diagnosis, FailureCategory

FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class DiagnosisParseError(ValueError):
    pass


def _first_json_object(text: str) -> str | None:
    """Find the first balanced {...} span, respecting strings."""
    start = text.find("{")
    while start != -1:
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
        start = text.find("{", start + 1)
    return None


def _normalise(data: dict[str, Any]) -> dict[str, Any]:
    """Fix harmless deviations small local models make, before strict validation."""
    out = dict(data)
    category = out.get("category")
    if isinstance(category, str):
        value = category.strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "format": "lint_format",
            "lint": "lint_format",
            "formatting": "lint_format",
            "tests": "test",
            "build": "compile",
            "config": "configuration",
            "infra": "infrastructure",
            "dependencies": "dependency",
        }
        value = aliases.get(value, value)
        valid = {c.value for c in FailureCategory}
        out["category"] = value if value in valid else FailureCategory.OTHER.value
    citations = out.get("citations")
    if isinstance(citations, list):
        fixed = []
        for item in citations:
            if isinstance(item, dict):
                line = item.get("line")
                if isinstance(line, str):
                    digits = re.sub(r"^\s*L", "", line).strip()
                    fixed.append({**item, "line": int(digits) if digits.isdigit() else line})
                else:
                    fixed.append(item)
        out["citations"] = fixed
    confidence = out.get("confidence")
    if isinstance(confidence, str):
        with contextlib.suppress(ValueError):
            out["confidence"] = float(confidence.strip().rstrip("%")) / (
                100 if confidence.strip().endswith("%") else 1
            )
    if isinstance(out.get("confidence"), int | float) and out["confidence"] > 1:
        out["confidence"] = min(1.0, out["confidence"] / 100)
    return out


def parse_diagnosis(text: str) -> Diagnosis:
    candidates: list[str] = [m.group(1) for m in FENCE.finditer(text)]
    candidates.append(text)
    last_error = "no JSON object found in the reply"
    for candidate in candidates:
        blob = _first_json_object(candidate)
        if blob is None:
            continue
        try:
            data = json.loads(blob)
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
