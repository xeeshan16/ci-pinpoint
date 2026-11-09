# Contributing

Thanks for helping. Bug reports with a real failing log are the most useful thing
you can send: attach the log (or a link to the public run) and say which line you
expected ci-pinpoint to point at.

## Development setup

```bash
git clone https://github.com/xeeshan16/ci-pinpoint && cd ci-pinpoint
uv sync                      # Python 3.11+; installs dev tools too
uv run pytest                # unit and integration tests, coverage gate 85%
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run ci-pinpoint explain tests/fixtures/logs/smoke-test-missing-metrics.log.gz --provider fake
```

Tests never need a model or network access. The fake provider (`--provider fake`)
is deterministic and only for tests; its output is labelled as fake everywhere.
To try a real model locally: install [Ollama](https://ollama.com), run
`ollama pull qwen2.5:3b`, then drop `--provider fake`.

Optional live test against the GitHub API:
`CI_PINPOINT_LIVE_GITHUB=1 GITHUB_TOKEN=... uv run pytest -m live`.

## Adding a labelled log

1. Add the unmodified log as `tests/fixtures/logs/<short-name>.log.gz`. Only use
   logs from public runs whose license allows it; check them for secrets first.
2. Add an entry to `tests/fixtures/logs/manifest.json` with the run URL, the job,
   the category and one or more `anchors`: substrings of the line(s) a person needs
   to see to understand the failure.
3. `uv run pytest tests/test_heuristics.py` checks that the anchors survive selection.

## Pull requests

- Keep each PR to one change, with tests. CI must be green.
- Describe the user-visible effect; if it changes selection quality, include the
  before and after of `scripts/fixture_report.py` (and the eval once it exists).
- New providers implement `ChatModel` / `Embedder` in `src/ci_pinpoint/providers/`
  and get a branch in `factory.py` plus mocked-HTTP tests.
