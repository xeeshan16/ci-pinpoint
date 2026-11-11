from __future__ import annotations

import pytest

from ci_pinpoint.diagnose.prompt import SYSTEM_PROMPT
from ci_pinpoint.models import FailureCategory
from ci_pinpoint.taxonomy import DEFINITIONS, heuristic_category


def test_every_category_is_defined_in_the_prompt() -> None:
    assert set(DEFINITIONS) == set(FailureCategory)
    for category in FailureCategory:
        assert f"- {category.value}: " in SYSTEM_PROMPT
    assert "Name the cause, not the reporter" in SYSTEM_PROMPT


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (
            ["Tests run: 12, Failures: 0, Errors: 12", "Connection to localhost:5432 refused"],
            FailureCategory.INFRASTRUCTURE,
        ),
        (
            ["[ERROR] BUILD FAILURE", "Failed to bind properties under 'app.pii'"],
            FailureCategory.CONFIGURATION,
        ),
        (
            ["src/app.ts(3,7): error TS2322: Type 'string' is not assignable"],
            FailureCategory.COMPILE,
        ),
        (["ModuleNotFoundError: No module named 'httpx'"], FailureCategory.DEPENDENCY),
        (["The following files had format violations:"], FailureCategory.LINT_FORMAT),
        (["Process completed with exit code 137"], FailureCategory.RESOURCE),
        (["FAILED tests/test_api.py::test_get - AssertionError"], FailureCategory.TEST),
        (["all good"], FailureCategory.OTHER),
    ],
)
def test_heuristic_category_names_the_cause_first(
    lines: list[str], expected: FailureCategory
) -> None:
    assert heuristic_category(lines) is expected
