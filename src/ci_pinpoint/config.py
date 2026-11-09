"""Settings: defaults < config file < environment < command-line flags.

Config file: ``.ci-pinpoint.toml`` in the working directory, or the path in
``CI_PINPOINT_CONFIG``. Keys mirror the environment variables without the
``CI_PINPOINT_`` prefix, lower-cased (``provider``, ``model``, ``base_url``,
``budget_tokens``, ``timeout_s``, ``max_retries``, ``max_output_tokens``,
``repair_attempts``).
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

PROVIDERS = ("auto", "ollama", "openai", "anthropic", "azure", "openai-compatible", "fake")

DEFAULT_MODELS = {
    "ollama": "qwen2.5:3b",
    "openai": "gpt-4.1-mini",
    "anthropic": "claude-haiku-4-5",
}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Settings:
    provider: str = "auto"
    model: str | None = None
    base_url: str | None = None
    budget_tokens: int = 2000
    timeout_s: float = 120.0
    max_retries: int = 3
    max_output_tokens: int = 1024
    repair_attempts: int = 1
    github_api_url: str = "https://api.github.com"

    def validate(self) -> Settings:
        if self.provider not in PROVIDERS:
            raise ConfigError(
                f"unknown provider {self.provider!r}; use one of {', '.join(PROVIDERS)}"
            )
        if self.budget_tokens < 200:
            raise ConfigError("budget_tokens must be at least 200")
        if self.timeout_s <= 0:
            raise ConfigError("timeout_s must be positive")
        if not 0 <= self.max_retries <= 10:
            raise ConfigError("max_retries must be between 0 and 10")
        if not 0 <= self.repair_attempts <= 3:
            raise ConfigError("repair_attempts must be between 0 and 3")
        return self


_ENV_KEYS = {
    "provider": "CI_PINPOINT_PROVIDER",
    "model": "CI_PINPOINT_MODEL",
    "base_url": "CI_PINPOINT_BASE_URL",
    "budget_tokens": "CI_PINPOINT_BUDGET_TOKENS",
    "timeout_s": "CI_PINPOINT_TIMEOUT_S",
    "max_retries": "CI_PINPOINT_MAX_RETRIES",
    "max_output_tokens": "CI_PINPOINT_MAX_OUTPUT_TOKENS",
    "repair_attempts": "CI_PINPOINT_REPAIR_ATTEMPTS",
    "github_api_url": "GITHUB_API_URL",
}


def _coerce(name: str, value: Any) -> Any:
    types = {f.name: f.type for f in fields(Settings)}
    kind = str(types[name])
    try:
        if kind == "int":
            return int(value)
        if kind == "float":
            return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc
    return str(value) if value is not None else None


def _from_mapping(base: Settings, values: Mapping[str, Any], origin: str) -> Settings:
    known = {f.name for f in fields(Settings)}
    unknown = set(values) - known
    if unknown:
        raise ConfigError(f"{origin}: unknown setting(s) {', '.join(sorted(unknown))}")
    return replace(base, **{k: _coerce(k, v) for k, v in values.items()})


def load_settings(
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Settings:
    env = os.environ if env is None else env
    settings = Settings()

    config_path = env.get("CI_PINPOINT_CONFIG")
    path = Path(config_path) if config_path else (cwd or Path.cwd()) / ".ci-pinpoint.toml"
    if config_path and not path.is_file():
        raise ConfigError(f"CI_PINPOINT_CONFIG points to {path}, which does not exist")
    if path.is_file():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: {exc}") from exc
        settings = _from_mapping(settings, data, str(path))

    from_env = {name: env[key] for name, key in _ENV_KEYS.items() if env.get(key)}
    settings = _from_mapping(settings, from_env, "environment")

    if overrides:
        settings = _from_mapping(
            settings, {k: v for k, v in overrides.items() if v is not None}, "flags"
        )
    return settings.validate()
