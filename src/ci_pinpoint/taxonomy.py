"""The failure-category taxonomy: what each category means and how to break ties.

Day 1 to 3 real-model runs got the category right on only 1 or 2 of the 6
labelled logs: a 3B model called a broker misconfiguration ``test`` (because a
test reported it) and a refused database connection ``compile`` (because Maven
printed ``BUILD FAILURE``). The category is only useful if it names the *cause*,
so the prompt now carries short definitions and explicit tie-break rules, and
the same rules back the deterministic fake provider and the ``--no-llm`` hint.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from ci_pinpoint.models import FailureCategory

DEFINITIONS: dict[FailureCategory, str] = {
    FailureCategory.COMPILE: "source does not compile or type-check (compiler, tsc, mypy, "
    "javac errors)",
    FailureCategory.TEST: "the code under test behaves wrongly: an assertion, an expected value, "
    "a check script that found missing output",
    FailureCategory.LINT_FORMAT: "a linter or formatter reported violations (ruff, eslint, "
    "spotless, prettier, gofmt)",
    FailureCategory.DEPENDENCY: "a package, module or image could not be resolved, downloaded or "
    "installed, or versions conflict",
    FailureCategory.CONFIGURATION: "an invalid, unknown or missing setting, property, env var or "
    "flag made a tool or the application refuse to start",
    FailureCategory.INFRASTRUCTURE: "a service the job needs (database, broker, container, "
    "runner) was unreachable or not ready",
    FailureCategory.NETWORK: "the network itself failed: DNS, TLS, proxy, registry or download "
    "errors not caused by a missing package",
    FailureCategory.TIMEOUT: "a step, test or wait hit a time limit",
    FailureCategory.RESOURCE: "out of memory, disk or file handles; process killed (exit 137)",
    FailureCategory.PERMISSION: "permission denied, unauthorized, forbidden, a missing or expired "
    "token",
    FailureCategory.OTHER: "none of the above fits",
}

TIE_BREAKS: tuple[str, ...] = (
    "Name the cause, not the reporter: a test that fails because the app rejects a setting is "
    "configuration; a test that fails because Postgres refused the connection is infrastructure.",
    "BUILD FAILURE, exit code 1 and 'Tests run: ..., Errors: N' are symptoms; never choose a "
    "category from them alone.",
    "compile only when a compiler or type checker reports an error in the project's own code.",
)


def prompt_section() -> str:
    """The taxonomy as it appears in the system prompt."""
    lines = ["Categories (choose the one that names the cause):"]
    lines += [f"- {category.value}: {text}" for category, text in DEFINITIONS.items()]
    lines += ["Tie-breaks:"]
    lines += [f"- {rule}" for rule in TIE_BREAKS]
    return "\n".join(lines)


# Ordered: the first rule that matches wins, so causes come before symptoms.
_RULES: tuple[tuple[re.Pattern[str], FailureCategory], ...] = (
    (
        re.compile(
            r"not formatted|format violations|spotless|prettier|\bblack\b|ruff format|"
            r"\blint(?:er|ing)?\b|eslint|gofmt",
            re.I,
        ),
        FailureCategory.LINT_FORMAT,
    ),
    (
        re.compile(r"OutOfMemory|OOMKilled|out of memory|exit code 137|no space left", re.I),
        FailureCategory.RESOURCE,
    ),
    (
        re.compile(
            r"connection refused|ECONNREFUSED|5432|9092|6379|27017|"
            r"is not ready|service unavailable|unable to connect",
            re.I,
        ),
        FailureCategory.INFRASTRUCTURE,
    ),
    (
        re.compile(r"could not resolve host|name or service not known|tls handshake|ssl", re.I),
        FailureCategory.NETWORK,
    ),
    (re.compile(r"timed? ?out|deadline exceeded", re.I), FailureCategory.TIMEOUT),
    (
        re.compile(r"permission denied|forbidden|unauthori[sz]ed|bad credentials|401|403", re.I),
        FailureCategory.PERMISSION,
    ),
    (
        re.compile(
            r"could not resolve dependencies|ModuleNotFoundError|No matching version|"
            r"ERESOLVE|could not find artifact|No module named|package .* not found",
            re.I,
        ),
        FailureCategory.DEPENDENCY,
    ),
    (
        re.compile(
            r"invalid (?:value|config|property|setting)|unknown (?:property|setting|option|"
            r"[a-z]+ type)|requirement failed|advertised\.listeners|"
            r"missing (?:required )?(?:property|setting|env)|"
            r"Failed to bind properties|APPLICATION FAILED TO START",
            re.I,
        ),
        FailureCategory.CONFIGURATION,
    ),
    (
        re.compile(
            r"error(?:\[\w+\])?:|\berror TS\d+|\berror CS\d+|cannot find symbol|"
            r"compilation (?:error|failure)",
            re.I,
        ),
        FailureCategory.COMPILE,
    ),
    (
        re.compile(r"FAILED|<<< FAILURE|AssertionError|Tests run:.*Failures: [1-9]|MISSING", re.I),
        FailureCategory.TEST,
    ),
)


def heuristic_category(lines: Iterable[str]) -> FailureCategory:
    """A category from keyword rules, causes before symptoms. Used by the fake
    provider and as a model-free hint; a real model reads the definitions."""
    joined = "\n".join(lines)
    for pattern, category in _RULES:
        if pattern.search(joined):
            return category
    return FailureCategory.OTHER
