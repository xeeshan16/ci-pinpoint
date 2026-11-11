"""Redact secrets from log lines before any of them reach a model or an output.

CI logs leak credentials more often than they should: a debug ``env`` dump, a
``curl -H "Authorization: Bearer ..."`` echoed by ``set -x``, a JDBC URL with a
password, a private key printed by a misconfigured step. GitHub masks the
secrets it knows about as ``***``; everything else would be sent to whichever
model the user configured, and copied into briefs that end up in job summaries
and agent context. So redaction runs on every line before selection.

Rules are deliberately specific (known token formats, credentials in URLs,
authorization headers, ``*password*=`` / ``*token*=`` style assignments, PEM
private key blocks). A generic "high entropy string" rule would also redact
commit SHAs, digests and UUIDs, which are often the evidence. That trade-off is
written down in docs/adr/0004-logs-are-untrusted-input.md.

Line numbers never change: a redacted line keeps its number and only the secret
part is replaced with ``[REDACTED:<kind>]``.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace

from ci_pinpoint.models import NormalizedLog

MARKER = "[REDACTED:{kind}]"


@dataclass(frozen=True, slots=True)
class Rule:
    kind: str
    pattern: re.Pattern[str]
    group: str = "secret"  # the named group that is replaced


def _rule(kind: str, pattern: str, flags: int = 0) -> Rule:
    return Rule(kind, re.compile(pattern, flags))


# Order matters: specific token formats first, so an assignment like
# GITHUB_TOKEN=ghp_... is reported as a github_token rather than a generic one.
RULES: tuple[Rule, ...] = (
    _rule(
        "github_token",
        r"(?P<secret>\bgh[pousr]_[A-Za-z0-9]{36,255}\b|\bgithub_pat_[A-Za-z0-9_]{22,255}\b)",
    ),
    _rule("gitlab_token", r"(?P<secret>\bglpat-[A-Za-z0-9_\-]{20,}\b)"),
    _rule("aws_access_key_id", r"(?P<secret>\b(?:AKIA|ASIA)[0-9A-Z]{16}\b)"),
    _rule("slack_token", r"(?P<secret>\bxox[abposr]-[A-Za-z0-9-]{10,}\b)"),
    _rule("slack_webhook", r"(?P<secret>https://hooks\.slack\.com/services/[A-Za-z0-9/_\-]{20,})"),
    _rule("google_api_key", r"(?P<secret>\bAIza[0-9A-Za-z_\-]{35}\b)"),
    _rule("stripe_key", r"(?P<secret>\b(?:sk|rk)_live_[0-9A-Za-z]{16,}\b)"),
    _rule("llm_api_key", r"(?P<secret>\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,})"),
    _rule("npm_token", r"(?P<secret>\bnpm_[A-Za-z0-9]{36}\b)"),
    _rule(
        "jwt",
        r"(?P<secret>\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,})",
    ),
    # scheme://user:password@host  (JDBC, AMQP, Redis, git remotes, proxies)
    _rule(
        "url_credentials",
        r"\b[a-z][a-z0-9+.\-]*://[^/\s:@'\"]+:(?P<secret>[^@\s/'\"]{3,})@",
        re.IGNORECASE,
    ),
    _rule(
        "authorization_header",
        r"\b(?:authorization|proxy-authorization)\s*[:=]\s*[\"']?"
        r"(?:bearer|basic|token|bot)\s+(?P<secret>[A-Za-z0-9._~+/=\-]{8,})",
        re.IGNORECASE,
    ),
    _rule("bearer_token", r"\bbearer\s+(?P<secret>[A-Za-z0-9._~+/\-]{20,}=*)", re.IGNORECASE),
    # KEY=value, key: value, "key": "value", --password=value, where the key
    # ends in a secret-ish word. The value must look like a secret (see below).
    _rule(
        "credential_assignment",
        r"(?:^|[\s\"'{,(;]|--)"
        r"(?P<key>(?:[A-Za-z0-9]+[_.\-])*"
        r"(?:password|passwd|pwd|passphrase|secret|token|api[_\-]?key|apikey|access[_\-]?key"
        r"|private[_\-]?key|client[_\-]?secret|auth[_\-]?token|credentials?))"
        r"[\"']?\s*[:=]\s*[\"']?(?P<secret>[^\s\"',;)}]+)",
        re.IGNORECASE,
    ),
)

_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----")
_ALREADY_MASKED = re.compile(r"^(?:\*+|\[[A-Za-z_:]+\]|<[^>]*>|\$\{?\{?.*|%[A-Za-z_]+%?|x+|X+)$")
_PLAIN_VALUES = frozenset(
    {"true", "false", "null", "none", "nil", "undefined", "empty", "required"}
)


def _looks_secret(value: str) -> bool:
    """For ``key=value`` assignments: skip masked values, placeholders, variable
    references, booleans and short or single-class values (``TOKEN_TTL=3600``)."""
    if _ALREADY_MASKED.match(value) or value.lower() in _PLAIN_VALUES:
        return False
    if len(value) >= 16:
        return True
    has_alpha = any(c.isalpha() for c in value)
    has_digit = any(c.isdigit() for c in value)
    return len(value) >= 8 and has_alpha and has_digit


def redact_text(text: str) -> tuple[str, list[str]]:
    """Return ``text`` with secrets replaced, and the kind of each redaction."""
    kinds: list[str] = []
    for rule in RULES:

        def substitute(match: re.Match[str], rule: Rule = rule) -> str:
            secret = match.group(rule.group)
            if rule.kind == "credential_assignment" and not _looks_secret(secret):
                return match.group(0)
            if secret.startswith("[REDACTED:"):
                return match.group(0)
            kinds.append(rule.kind)
            start, end = match.span(rule.group)
            offset = match.start(0)
            whole = match.group(0)
            return whole[: start - offset] + MARKER.format(kind=rule.kind) + whole[end - offset :]

        text = rule.pattern.sub(substitute, text)
    return text, kinds


def redact_log(log: NormalizedLog) -> tuple[NormalizedLog, dict[str, int]]:
    """Redact every line of a normalised log; line numbers are unchanged.

    Lines inside a PEM private key block (``-----BEGIN ... PRIVATE KEY-----`` to
    ``-----END ...-----``) are replaced whole.
    """
    counts: Counter[str] = Counter()
    out = []
    in_key = False
    for line in log.lines:
        text = line.text
        if in_key or _PEM_BEGIN.search(text):
            ends = _PEM_END.search(text)
            if not in_key:
                counts["private_key"] += 1
            in_key = ends is None
            new = MARKER.format(kind="private_key")
        else:
            new, kinds = redact_text(text)
            counts.update(kinds)
        out.append(line if new == text else replace(line, text=new))
    if not counts:
        return log, {}
    return NormalizedLog(lines=tuple(out), sections=log.sections), dict(sorted(counts.items()))
