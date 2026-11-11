from __future__ import annotations

import io
import json

import pytest

from ci_pinpoint.cli import run
from ci_pinpoint.config import ConfigError, load_settings
from ci_pinpoint.diagnose.prompt import build_messages
from ci_pinpoint.heuristics import build_excerpt
from ci_pinpoint.normalize import normalize
from ci_pinpoint.redact import redact_log, redact_text

from .conftest import RealCase

# Shapes of real credentials; none of these values are live.
GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
SECRETS = [
    (f"export GITHUB_TOKEN={GH}", "github_token", GH),
    ("remote: https://x-access-token:ghs_" + "Z" * 36 + "@github.com/o/r", "github_token", "ghs_"),
    ("glpat-abcdefghij0123456789xyz", "gitlab_token", "glpat-"),
    ("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE", "aws_access_key_id", "AKIAIOSFODNN7EXAMPLE"),
    ("token xoxb-1234567890-abcdefghij", "slack_token", "xoxb-1234567890"),
    ("OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwx", "llm_api_key", "sk-proj-"),
    ("ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnopqrstuv", "llm_api_key", "sk-ant-"),
    ("jdbc:postgresql://app:s3cretPass@db:5432/orders", "url_credentials", "s3cretPass"),
    ("amqp://guest:guestpw@rabbit:5672/", "url_credentials", "guestpw"),
    (
        "curl -H 'Authorization: Bearer abcdef123456789xyz' https://api.example.com",
        "authorization_header",
        "abcdef123456789xyz",
    ),
    ("spring.datasource.password=hunter2abc", "credential_assignment", "hunter2abc"),
    ('{"client_secret": "Zx9-very-long-client-secret"}', "credential_assignment", "Zx9-very"),
    ("mvn deploy --password=Sup3rSecret!", "credential_assignment", "Sup3rSecret"),
    (
        "id_token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
        "jwt",
        "eyJhbGciOiJIUzI1NiJ9",
    ),
]
NOT_SECRETS = [
    "TOKEN_TTL=3600",
    "max_tokens=1024",
    "tokenizer=gpt2",
    "password: ${{ secrets.DB_PASSWORD }}",
    "password=*****",
    "Using token: [secure]",
    "Error: invalid token: expired",
    "commit 4b825dc642cb6eb9a060e54bf8d69288fbee4904",
    "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "Caused by: java.lang.IllegalArgumentException: unknown PII type: card",
    "##[error]Process completed with exit code 1.",
]


@pytest.mark.parametrize(("line", "kind", "secret"), SECRETS)
def test_secrets_are_redacted(line: str, kind: str, secret: str) -> None:
    redacted, kinds = redact_text(line)
    assert secret not in redacted
    assert f"[REDACTED:{kind}]" in redacted
    assert kinds[0] == kind


@pytest.mark.parametrize("line", NOT_SECRETS)
def test_evidence_is_left_alone(line: str) -> None:
    assert redact_text(line) == (line, [])


def test_redaction_is_idempotent() -> None:
    once, _ = redact_text(SECRETS[0][0])
    assert redact_text(once) == (once, [])


def test_private_key_blocks_are_removed_whole_and_line_numbers_kept() -> None:
    text = "\n".join(
        [
            "step 1",
            "-----BEGIN OPENSSH PRIVATE KEY-----",
            "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW",
            "QyNTUxOQAAACDx",
            "-----END OPENSSH PRIVATE KEY-----",
            "Permission denied (publickey).",
        ]
    )
    log, counts = redact_log(normalize(text))
    assert counts == {"private_key": 1}
    assert [line.no for line in log.lines] == [1, 2, 3, 4, 5, 6]
    assert all("b3BlbnNz" not in line.text and "QyNTUx" not in line.text for line in log.lines)
    assert log.lines[-1].text == "Permission denied (publickey)."


def test_real_logs_have_nothing_to_redact(real_case: RealCase) -> None:
    log = normalize(real_case.text())
    redacted, counts = redact_log(log)
    assert counts == {}
    assert redacted is log


def test_secrets_never_reach_the_prompt() -> None:
    lines = [f"line {i}" for i in range(30)]
    lines += [
        f"+ curl -H 'Authorization: token {GH}' https://api.github.com/repos/o/r",
        "Error: connection to jdbc:postgresql://app:s3cretPass@db:5432/orders refused",
        "##[error]Process completed with exit code 1.",
    ]
    log, counts = redact_log(normalize("\n".join(lines)))
    assert counts == {"github_token": 1, "url_credentials": 1}
    excerpt = build_excerpt(log, 2000)
    prompt = "\n".join(
        m.content for m in build_messages(excerpt, source="t", job_name=None, failed_steps=[])
    )
    assert GH not in prompt
    assert "s3cretPass" not in prompt
    assert "[REDACTED:url_credentials]" in prompt


def test_explain_reports_redactions_and_can_be_turned_off() -> None:
    log = (
        b"Run ./deploy.sh\n"
        b"Error: connection to jdbc:postgresql://app:s3cretPass@db:5432/orders refused\n"
        b"##[error]Process completed with exit code 1.\n"
    )

    def explain(*extra: str) -> tuple[str, str]:
        out, err = io.StringIO(), io.StringIO()
        run(
            ["explain", "-", "--no-llm", "-f", "json", "--no-cache", *extra],
            env={},
            stdin=io.BytesIO(log),
            stdout=out,
            stderr=err,
        )
        return out.getvalue(), err.getvalue()

    out, _ = explain()
    brief = json.loads(out)
    assert brief["redactions"] == {"url_credentials": 1}
    assert "s3cretPass" not in out

    out, _ = explain("--no-redact")
    assert json.loads(out)["redactions"] == {}
    assert "s3cretPass" in out


def test_redact_setting_parses_booleans() -> None:
    assert load_settings(env={"CI_PINPOINT_REDACT": "off"}, overrides={}).redact is False
    assert load_settings(env={"CI_PINPOINT_RERANK": "1"}, overrides={}).rerank is True
    with pytest.raises(ConfigError, match="true or false"):
        load_settings(env={"CI_PINPOINT_REDACT": "maybe"}, overrides={})
