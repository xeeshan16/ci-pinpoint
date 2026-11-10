# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `ci-pinpoint eval`: scores line selection against `tail` and `grep` baselines at equal
  token budgets on LogChunks (797 labelled Travis CI logs, downloaded and checksum-verified
  with `--fetch`) or any `manifest.json` of labelled logs; failure shown, line recall,
  precision, tokens, a paired bootstrap interval, a dev/test split by repository,
  `--diagnose N` for model citations, JSON and Markdown reports and a `--fail-under` gate.
- Travis CI logs: fold and time markers, top-level `$ command` steps and the failed
  command line.
- Leftover excerpt budget is spent on the failed step's output, nearest the failure first
  (LogChunks test split line recall 60.5% to 78.3% at 2,000 tokens).
- Drain-style template mining; selection dampens weak repeated lines, judges block
  novelty by template and folds runs of same-template lines into one line plus a count.
- Optional embedding re-ranking of candidate blocks (`--embedder`, off by default) with
  Ollama, OpenAI or any OpenAI-compatible embeddings endpoint.
- On-disk cache for verified briefs and embedding vectors (`--no-cache`,
  `CI_PINPOINT_CACHE_DIR`).
- Long lines are clipped in excerpts; windows stay inside their step.
- `ci-pinpoint explain` for log files (plain or gzip), stdin and GitHub Actions
  run/attempt/job URLs, with text, Markdown and JSON output and `--no-llm`.
- Log normaliser for GitHub Actions and GitLab logs (timestamps, ANSI, groups,
  sections, annotations) that keeps raw line numbers.
- Heuristic candidate selection with failed-step tails, negative patterns,
  boilerplate filtering, stack-frame compaction and a token budget.
- Providers: OpenAI-compatible (Ollama, vLLM), OpenAI, Azure OpenAI, Anthropic and
  deterministic fakes for tests; retries with full-jitter backoff and Retry-After.
- Diagnosis with a fixed JSON schema (sent as a structured-output schema where the
  provider supports it), one repair round trip, and citation verification against
  the raw log.
- Six hand-labelled real failed GitHub Actions logs as test fixtures.
- CI for Python 3.11 to 3.13, a clean-venv package smoke test, and a workflow
  that runs the fixtures through a real Ollama model.

### Fixed
- The `\x1b(B` that `tput sgr0` emits is stripped; a line ending in `\r\x1b[0K` keeps its
  text.
- Small-model replies with fields nested under `root_cause`, string citations or
  more than eight citations no longer fail validation.
- A reply cut off at the output limit is reported as such instead of an inner
  object being parsed as the answer.
- Raw-log token estimates no longer count clipped line lengths.
