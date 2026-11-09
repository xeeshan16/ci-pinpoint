# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `ci-pinpoint explain` for log files (plain or gzip), stdin and GitHub Actions
  run/attempt/job URLs, with text, Markdown and JSON output and `--no-llm`.
- Log normaliser for GitHub Actions and GitLab logs (timestamps, ANSI, groups,
  sections, annotations) that keeps raw line numbers.
- Heuristic candidate selection with failed-step tails, negative patterns,
  boilerplate filtering, stack-frame compaction and a token budget.
- Providers: OpenAI-compatible (Ollama, vLLM), OpenAI, Azure OpenAI, Anthropic and
  deterministic fakes for tests; retries with full-jitter backoff and Retry-After.
- Diagnosis with a fixed JSON schema, one repair round trip, and citation
  verification against the raw log.
- Six hand-labelled real failed GitHub Actions logs as test fixtures.
- CI for Python 3.11 to 3.13, a clean-venv package smoke test, and a workflow
  that runs the fixtures through a real Ollama model.
