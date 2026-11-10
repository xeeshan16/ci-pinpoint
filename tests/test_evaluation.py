from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest

from ci_pinpoint.cli import run as cli_run
from ci_pinpoint.evaluation.dataset import (
    Case,
    DatasetError,
    Split,
    chunk_targets,
    label_text,
    load_logchunks,
    load_manifest,
    locate_chunk,
    split_of,
)
from ci_pinpoint.evaluation.fetch import fetch_logchunks
from ci_pinpoint.evaluation.harness import paired_delta, run, score, summarise
from ci_pinpoint.evaluation.report import RunInfo, build_report, render_markdown, to_json
from ci_pinpoint.evaluation.strategies import (
    GrepStrategy,
    PinpointStrategy,
    Selection,
    TailStrategy,
    build_strategies,
)
from ci_pinpoint.normalize import normalize, split_lines
from ci_pinpoint.tokens import estimate_line_tokens

from .conftest import FIXTURES

# A Travis-style log: \r\r\n line endings, colour codes, fold and time markers.
TRAVIS = (
    "travis_fold:start:install\r\x1b[0K$ bundle install\r\r\n"  # 1
    "Using rake 12.0\r\r\n"  # 2
    "travis_fold:end:install\r\x1b[0K\r\r\n"  # 3
    "travis_time:start:aa\r\x1b[0K$ bundle exec rspec\r\r\n"  # 4
    "\x1b[31mFailures:\x1b[0m\r\r\n"  # 5
    "\r\r\n"  # 6
    "  1) Widget renders the title\r\r\n"  # 7
    "\x1b[37m     Failure/Error: expect(page).to have_content('Hello')\x1b[0m\r\r\n"  # 8
    '\x1b[37m       expected to find text "Hello" in "Bye"\x1b[0m\r\r\n'  # 9
    "Finished in 1.2 seconds\r\r\n"  # 10
    "1 example, 1 failure\r\r\n"  # 11
    "travis_time:end:aa:start=1,finish=2,duration=1\r\x1b[0K\r\r\n"  # 12
    '\x1b[31;1mThe command "bundle exec rspec" exited with 1.\x1b[0m\r\r\n'  # 13
)
# How LogChunks stores a chunk: escape bytes dropped, "[37m" left behind.
CHUNK = (
    "  1) Widget renders the title\n\n"
    "[37m     Failure/Error: expect(page).to have_content('Hello')[0m\n\n"
    '[37m       expected to find text "Hello" in "Bye"[0m'
)


def _lines(text: str = TRAVIS) -> list[str]:
    return split_lines(text)


def test_label_text_drops_real_and_orphaned_escapes() -> None:
    assert label_text("\x1b[37m  a   b\x1b[0m") == "a b"
    assert label_text("[m[37m      Expected: 1") == "Expected: 1"
    assert chunk_targets("a\r\n\r\nb\rc\n  \n") == ["a", "b", "c"]


def test_locate_chunk_aligns_across_blank_lines_and_colour_codes() -> None:
    requirements, found, wanted = locate_chunk(CHUNK, _lines())
    assert (found, wanted) == (3, 3)
    assert requirements == (frozenset({7}), frozenset({8}), frozenset({9}))


def test_locate_chunk_accepts_a_partial_first_line() -> None:
    chunk = "renders the title\nFailure/Error: expect(page).to have_content('Hello')"
    requirements, _, _ = locate_chunk(chunk, _lines())
    assert requirements == (frozenset({7}), frozenset({8}))


def test_locate_chunk_keeps_every_occurrence() -> None:
    text = TRAVIS + "Summary:\n  1) Widget renders the title\n"
    requirements, found, wanted = locate_chunk("  1) Widget renders the title", _lines(text))
    assert (found, wanted) == (1, 1)
    assert requirements == (frozenset({7, 15}),)


def test_locate_chunk_short_lines_must_match_exactly() -> None:
    lines = ["ok", "1 example, 1 failure", "1"]
    requirements, _, _ = locate_chunk("1", lines)
    assert requirements == (frozenset({3}),)


def test_locate_chunk_reports_what_it_could_not_find() -> None:
    requirements, found, wanted = locate_chunk("nothing like this\nor this either", _lines())
    assert requirements == ()
    assert (found, wanted) == (0, 2)
    assert locate_chunk("  \n", _lines()) == ((), 0, 0)


def test_split_is_deterministic_and_by_repository() -> None:
    repos = [f"owner/repo-{i}" for i in range(40)]
    splits = [split_of(r) for r in repos]
    assert splits == [split_of(r) for r in repos]
    assert {"dev", "test"} == set(splits)


def _write_logchunks(root: Path) -> Path:
    base = root / "LogChunks"
    (base / "build-failure-reason" / "Ruby").mkdir(parents=True)
    logs = base / "logs" / "Ruby" / "acme@widgets" / "failed"
    logs.mkdir(parents=True)
    (logs / "100.log").write_bytes(TRAVIS.encode())
    (logs / "101.log").write_bytes(b"all good here\n")
    examples = [
        ("Ruby/acme@widgets/failed/100.log", CHUNK, "0"),
        ("Ruby/acme@widgets/failed/101.log", "Failure/Error: boom", "1"),
        ("Ruby/acme@widgets/failed/102.log", "anything", "0"),
    ]
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', "<Examples>"]
    for log, chunk, category in examples:
        escaped = chunk.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        xml.append(
            f"<Example><Log>{log}</Log><Keywords>Failure</Keywords>"
            f"<Category>{category}</Category><Chunk>{escaped}</Chunk></Example>"
        )
    xml.append("</Examples>")
    (base / "build-failure-reason" / "Ruby" / "acme@widgets.xml").write_text("\n".join(xml))
    return root


def test_load_logchunks(tmp_path: Path) -> None:
    dataset = load_logchunks(_write_logchunks(tmp_path))
    assert [c.id for c in dataset.cases] == ["Ruby/acme@widgets/failed/100"]
    case = dataset.cases[0]
    assert case.repo == "acme/widgets"
    assert case.language == "Ruby"
    assert case.category == "acme/widgets#0"
    assert case.label_lines == {7, 8, 9}
    assert case.split == split_of("acme/widgets")
    reasons = {u.id.rsplit("/", 1)[1]: u.reason for u in dataset.unlocated}
    assert reasons == {"101": "chunk not found in log", "102": "log file missing"}
    assert dataset.license == "CC-BY-4.0"
    assert "LogChunks" in dataset.citation


def test_load_logchunks_split_limit_and_layout(tmp_path: Path) -> None:
    root = _write_logchunks(tmp_path)
    other: Split = "test" if split_of("acme/widgets") == "dev" else "dev"
    assert load_logchunks(root, split=other).cases == ()
    limited = load_logchunks(root / "LogChunks", limit=1)
    assert len(limited.cases) + len(limited.unlocated) == 1
    with pytest.raises(DatasetError, match="--fetch"):
        load_logchunks(tmp_path / "nowhere")


def test_load_logchunks_rejects_broken_xml(tmp_path: Path) -> None:
    root = _write_logchunks(tmp_path)
    (root / "LogChunks" / "build-failure-reason" / "Ruby" / "bad.xml").write_text("<Examples>")
    with pytest.raises(DatasetError, match=r"bad\.xml"):
        load_logchunks(root)


def test_load_manifest_fixtures() -> None:
    dataset = load_manifest(FIXTURES)
    assert dataset.name == "kafka-llm-enricher-fixtures"
    assert len(dataset.cases) == 6
    assert dataset.unlocated == ()
    assert all(case.requirements and all(case.requirements) for case in dataset.cases)


def test_load_manifest_reports_missing_anchor_and_file(tmp_path: Path) -> None:
    (tmp_path / "a.log").write_text("line one\nline two\n")
    manifest = {
        "cases": [
            {"file": "a.log", "anchors": ["line two"]},
            {"file": "a.log", "anchors": ["line two", "line three"]},
            {"file": "missing.log", "anchors": ["x"]},
        ]
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    dataset = load_manifest(tmp_path / "manifest.json")
    assert [c.requirements for c in dataset.cases] == [(frozenset({2}),)]
    assert [(u.reason, u.found, u.wanted) for u in dataset.unlocated] == [
        ("anchor not found in log", 1, 2),
        ("log file missing", 0, 0),
    ]
    with pytest.raises(DatasetError):
        load_manifest(tmp_path / "empty-dir")


def test_tail_and_grep_stay_within_budget() -> None:
    log = normalize(TRAVIS)
    budget = 3 * estimate_line_tokens("1 example, 1 failure")
    tail = TailStrategy().select(log, [budget])[budget]
    assert tail.tokens <= budget
    assert max(tail.lines) == 13  # the last visible line
    assert 12 not in tail.lines  # a blank travis_time line is never shown
    grep = GrepStrategy().select(log, [10_000])[10_000]
    assert grep.lines == {5, 8, 11}
    assert GrepStrategy().select(normalize("ok\n"), [100])[100] == Selection(frozenset(), 0)


def test_pinpoint_strategy_matches_its_excerpt() -> None:
    log = normalize(TRAVIS)
    selections = PinpointStrategy().select(log, [300, 2000])
    assert {7, 8, 9} <= selections[2000].lines
    assert selections[300].tokens <= 300


def test_build_strategies() -> None:
    assert [s.name for s in build_strategies(["tail", "grep", "pinpoint"])] == [
        "tail",
        "grep",
        "pinpoint",
    ]
    with pytest.raises(ValueError, match="embedder"):
        build_strategies(["pinpoint+emb"])
    with pytest.raises(ValueError, match="unknown"):
        build_strategies(["head"])


def test_score_counts_requirements_not_lines() -> None:
    case = Case(
        id="c",
        path=Path("unused"),
        repo="r",
        split="test",
        requirements=(frozenset({7, 15}), frozenset({8}), frozenset({9})),
    )
    log = normalize(TRAVIS)
    s = score(case, log, Selection(frozenset({6, 7, 10, 11}), 40))
    assert s.hit
    assert s.recall == pytest.approx(1 / 3, abs=1e-3)
    assert s.shown == 3  # line 6 is blank
    assert s.precision == pytest.approx(1 / 3, abs=1e-3)
    miss = score(case, log, Selection(frozenset(), 0))
    assert (miss.hit, miss.recall, miss.precision) == (False, 0.0, 0.0)


def test_run_in_parallel_matches_sequential() -> None:
    cases = load_manifest(FIXTURES).cases
    strategies = build_strategies(["tail", "grep", "pinpoint"])
    sequential = run(cases, strategies, [400, 2000], jobs=1)
    parallel = run(cases, strategies, [400, 2000], jobs=2)
    assert [r.scores for r in sequential] == [r.scores for r in parallel]
    assert summarise(sequential, "pinpoint", 2000).mean_recall == 1.0


def test_paired_delta_interval() -> None:
    cases = load_manifest(FIXTURES).cases
    results = run(cases, build_strategies(["tail", "pinpoint"]), [2000])
    delta = paired_delta(results, "pinpoint", "tail", 2000)
    assert delta.low <= delta.mean <= delta.high
    assert delta.mean > 0
    same = paired_delta(results, "pinpoint", "pinpoint", 2000)
    assert (same.mean, same.low, same.high) == (0.0, 0.0, 0.0)
    assert paired_delta([], "a", "b", 1).mean == 0.0
    assert summarise([], "pinpoint", 2000).cases == 0


def test_report_json_and_markdown() -> None:
    dataset = load_manifest(FIXTURES)
    results = run(dataset.cases, build_strategies(["tail", "grep", "pinpoint"]), [500, 2000])
    info = RunInfo("all", (500, 2000), 2000, ("tail", "grep", "pinpoint"))
    report = build_report(dataset, results, info)
    assert json.loads(to_json(report))["summary"]["pinpoint"]["2000"]["cases"] == 6
    assert {(d["a"], d["b"]) for d in report["recall_deltas"]} == {
        ("pinpoint", "tail"),
        ("pinpoint", "grep"),
    }
    md = render_markdown(report)
    assert "| ci-pinpoint selection | 100.0% |" in md
    assert "grep -iE 'error\\|fail'" in md  # the pipe must not split the table cell
    assert "Line recall by budget" in md
    assert report["diagnosis"] is None


def test_cli_eval_on_manifest(tmp_path: Path) -> None:
    out, err = io.StringIO(), io.StringIO()
    code = cli_run(
        [
            "eval",
            "--dataset",
            "manifest",
            "--data",
            str(FIXTURES),
            "--out",
            str(tmp_path),
            "--budgets",
            "500",
            "--jobs",
            "1",
            "--diagnose",
            "2",
            "--provider",
            "fake",
            "--fail-under",
            "0.9",
        ],
        env={},
        stdout=out,
        stderr=err,
    )
    assert code == 0, err.getvalue()
    report = json.loads((tmp_path / "eval.json").read_text())
    assert report["settings"]["budgets"] == [500, 2000]
    assert report["diagnosis"]["cases"] == 2
    assert (tmp_path / "eval.md").read_text() == out.getvalue()


def test_cli_eval_with_embeddings_and_threshold(tmp_path: Path) -> None:
    err = io.StringIO()
    args = ["eval", "--dataset", "manifest", "--data", str(FIXTURES), "--out", str(tmp_path)]
    code = cli_run(
        [*args, "--embedder", "fake", "--fail-under", "1.01", "--budgets", "800"],
        env={},
        stdout=io.StringIO(),
        stderr=err,
    )
    assert code == 1
    assert "below 1.01" in err.getvalue()
    report = json.loads((tmp_path / "eval.json").read_text())
    assert report["settings"]["strategies"] == ["tail", "grep", "pinpoint", "pinpoint+emb"]
    assert report["settings"]["embedder"]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--dataset", "manifest"], "needs --data"),
        (["--data", "/nonexistent/dir"], "does not contain LogChunks"),
        (
            ["--dataset", "manifest", "--data", str(FIXTURES), "--strategies", "pinpoint+emb"],
            "embedder",
        ),
    ],
)
def test_cli_eval_usage_errors(args: list[str], message: str, tmp_path: Path) -> None:
    err = io.StringIO()
    code = cli_run(
        ["eval", *args, "--out", str(tmp_path)], env={}, stdout=io.StringIO(), stderr=err
    )
    assert code == 2
    assert message in err.getvalue()


def test_cli_eval_rejects_bad_lists() -> None:
    for bad in (["--budgets", "a,b"], ["--budgets", "0"], ["--strategies", "head"]):
        with pytest.raises(SystemExit):
            cli_run(["eval", *bad], env={}, stdout=io.StringIO(), stderr=io.StringIO())


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _zenodo(archive: bytes, checksum: str | None = None) -> tuple[httpx.Client, list[str]]:
    calls: list[str] = []
    digest = checksum or hashlib.md5(archive).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.startswith("/api/records/"):
            return httpx.Response(
                200,
                json={
                    "files": [
                        {
                            "key": "LogChunks.zip",
                            "size": len(archive),
                            "checksum": f"md5:{digest}",
                            "links": {"self": "https://zenodo.test/files/LogChunks.zip"},
                        }
                    ]
                },
            )
        return httpx.Response(200, content=archive)

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_fetch_downloads_verifies_unpacks_and_is_idempotent(tmp_path: Path) -> None:
    archive = _zip({"LogChunks/logs/x.log": b"hello\n"})
    client, calls = _zenodo(archive)
    fetch_logchunks(tmp_path, client=client, api_url="https://zenodo.test/api/records/{record}")
    assert (tmp_path / "LogChunks" / "logs" / "x.log").read_bytes() == b"hello\n"
    assert len(calls) == 2
    fetch_logchunks(tmp_path, client=client, api_url="https://zenodo.test/api/records/{record}")
    assert len(calls) == 3  # metadata only: the file is present and verified


def test_fetch_rejects_a_bad_checksum(tmp_path: Path) -> None:
    client, _ = _zenodo(_zip({"a": b"b"}), checksum="0" * 32)
    with pytest.raises(DatasetError, match="expected"):
        fetch_logchunks(tmp_path, client=client, api_url="https://zenodo.test/api/records/{record}")
    assert not list(tmp_path.iterdir())


def test_fetch_refuses_unsafe_archive_paths(tmp_path: Path) -> None:
    client, _ = _zenodo(_zip({"../escape.txt": b"x"}))
    with pytest.raises(DatasetError, match="unsafe path"):
        fetch_logchunks(
            tmp_path / "d", client=client, api_url="https://zenodo.test/api/records/{record}"
        )
    assert not (tmp_path / "escape.txt").exists()


def test_fetch_reports_unreachable_record(tmp_path: Path) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(DatasetError, match="could not read Zenodo record"):
        fetch_logchunks(tmp_path, client=client, api_url="https://zenodo.test/api/records/{record}")
