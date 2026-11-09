from __future__ import annotations

import gzip
import io
import json
from pathlib import Path

import pytest

from ci_pinpoint.cli import EXIT_OK, EXIT_UNDIAGNOSED, EXIT_USAGE, run
from ci_pinpoint.config import ConfigError, load_settings

from .conftest import FIXTURES

SMOKE = str(FIXTURES / "smoke-test-missing-metrics.log.gz")


def _run(
    argv: list[str], env: dict[str, str] | None = None, stdin: bytes = b""
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = run(argv, env=env or {}, stdin=io.BytesIO(stdin), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_explain_with_fake_provider_text() -> None:
    code, out, _ = _run(["explain", SMOKE, "--provider", "fake"])
    assert code == EXIT_OK
    assert "FAKE PROVIDER" in out
    assert "MISSING              gen_ai_request_model" in out
    assert "% smaller" in out


def test_explain_json_is_machine_readable() -> None:
    code, out, _ = _run(["explain", SMOKE, "-f", "json"], env={"CI_PINPOINT_PROVIDER": "fake"})
    assert code == EXIT_OK
    brief = json.loads(out)
    assert brief["diagnosis"]["verified"] is True
    assert brief["diagnosis"]["provider"] == "fake"
    assert brief["stats"]["reduction"] > 0.9
    assert brief["excerpt"][0]["lines"]


def test_explain_markdown() -> None:
    code, out, _ = _run(["explain", SMOKE, "--provider", "fake", "--format", "markdown"])
    assert code == EXIT_OK
    assert out.startswith("### ✖")
    assert "fake provider, not a real diagnosis" in out
    assert "<details>" in out


def test_no_llm_prints_excerpt_only() -> None:
    code, out, _ = _run(["explain", SMOKE, "--no-llm", "--budget", "400"])
    assert code == EXIT_OK
    assert "2 metric(s) queried by the dashboard are not exported" in out
    assert "Root cause" not in out


def test_stdin_plain_and_gzip() -> None:
    log = b"ok\nerror: something broke here\n"
    code, out, _ = _run(["explain", "-", "--no-llm"], stdin=log)
    assert code == EXIT_OK
    assert "error: something broke here" in out
    code, out, _ = _run(["explain", "-", "--no-llm"], stdin=gzip.compress(log))
    assert "error: something broke here" in out


def test_unreachable_model_exits_1_but_still_prints_lines() -> None:
    env = {
        "CI_PINPOINT_PROVIDER": "ollama",
        "OLLAMA_HOST": "http://127.0.0.1:9",
        "CI_PINPOINT_MAX_RETRIES": "0",
        "CI_PINPOINT_TIMEOUT_S": "2",
    }
    code, out, err = _run(["explain", SMOKE], env=env)
    assert code == EXIT_UNDIAGNOSED
    assert "diagnosis failed" in out
    assert "MISSING              gen_ai_request_model" in out
    assert "failed to connect" in err


@pytest.mark.parametrize(
    ("argv", "env", "message"),
    [
        (["explain", "missing.log"], {}, "no such file"),
        (["explain", SMOKE, "--budget", "10"], {}, "budget_tokens must be at least 200"),
        (["explain", SMOKE], {"CI_PINPOINT_PROVIDER": "openai"}, "OPENAI_API_KEY"),
        (["explain", SMOKE], {"CI_PINPOINT_TIMEOUT_S": "soon"}, "timeout_s must be a number"),
    ],
)
def test_usage_errors_exit_2(argv: list[str], env: dict[str, str], message: str) -> None:
    code, _, err = _run(argv, env=env)
    assert code == EXIT_USAGE
    assert message in err


def test_config_file_env_and_flag_precedence(tmp_path: Path) -> None:
    (tmp_path / ".ci-pinpoint.toml").write_text(
        'provider = "fake"\nbudget_tokens = 900\nmodel = "a"\n'
    )
    settings = load_settings(
        env={"CI_PINPOINT_MODEL": "b"}, cwd=tmp_path, overrides={"budget_tokens": 1200}
    )
    assert (settings.provider, settings.model, settings.budget_tokens) == ("fake", "b", 1200)


def test_config_errors(tmp_path: Path) -> None:
    (tmp_path / ".ci-pinpoint.toml").write_text("colour = 'red'\n")
    with pytest.raises(ConfigError, match="unknown setting"):
        load_settings(env={}, cwd=tmp_path)
    with pytest.raises(ConfigError, match="does not exist"):
        load_settings(env={"CI_PINPOINT_CONFIG": str(tmp_path / "x.toml")})
    (tmp_path / "bad.toml").write_text("provider = \n")
    with pytest.raises(ConfigError, match=r"bad\.toml"):
        load_settings(env={"CI_PINPOINT_CONFIG": str(tmp_path / "bad.toml")})
    with pytest.raises(ConfigError, match="unknown provider"):
        load_settings(env={"CI_PINPOINT_PROVIDER": "gemini"}, cwd=tmp_path / "none")


def test_json_logging_goes_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = _run(
        ["explain", SMOKE, "--no-llm", "-v"], env={"CI_PINPOINT_LOG_FORMAT": "json"}
    )
    assert code == EXIT_OK
    captured = capsys.readouterr()
    record = json.loads(captured.err.strip().splitlines()[-1])
    assert record["msg"] == "excerpt selected"
    assert record["raw_tokens"] > record["excerpt_tokens"]
    assert "excerpt selected" not in out


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        run(["--version"])
    assert info.value.code == 0
    assert "ci-pinpoint" in capsys.readouterr().out
