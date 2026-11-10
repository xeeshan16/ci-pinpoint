from __future__ import annotations

from ci_pinpoint.models import LineKind
from ci_pinpoint.normalize import clean_line, normalize, split_lines


def test_clean_line_strips_bom_timestamp_and_ansi() -> None:
    raw = "﻿2026-10-21T13:08:54.5581533Z \x1b[31;1mBUILD FAILURE\x1b[0m"
    assert clean_line(raw) == "BUILD FAILURE"


def test_clean_line_keeps_last_carriage_return_segment() -> None:
    assert clean_line("Downloading 10%\rDownloading 55%\rDownloaded 100%   ") == "Downloaded 100%"


def test_clean_line_leaves_plain_text_alone() -> None:
    assert (
        clean_line("  at com.example.Foo.bar(Foo.java:12)")
        == "  at com.example.Foo.bar(Foo.java:12)"
    )


def test_github_groups_annotations_and_command_echo() -> None:
    text = "\n".join(
        [
            "2026-01-01T00:00:00.0000000Z ##[group]Run actions/checkout@v4",
            "2026-01-01T00:00:00.0000000Z with:",
            "2026-01-01T00:00:00.0000000Z ##[endgroup]",
            "2026-01-01T00:00:01.0000000Z Syncing repository",
            "2026-01-01T00:00:02.0000000Z ##[group]Run pytest",
            "2026-01-01T00:00:02.0000000Z pytest -q || echo 'error: tests failed'",
            "2026-01-01T00:00:02.0000000Z ##[endgroup]",
            "2026-01-01T00:00:03.0000000Z FAILED tests/test_x.py::test_y - assert 1 == 2",
            "2026-01-01T00:00:03.0000000Z ##[error]Process completed with exit code 1.",
        ]
    )
    log = normalize(text)
    assert [line.no for line in log.lines] == list(range(1, 10))
    assert [s.title for s in log.sections] == [
        "(log start)",
        "Run actions/checkout@v4",
        "Run pytest",
    ]
    assert [s.has_error for s in log.sections] == [False, False, True]
    kinds = {line.no: line.kind for line in log.lines}
    assert kinds[1] is LineKind.GROUP
    assert kinds[2] is LineKind.COMMAND
    assert kinds[3] is LineKind.ENDGROUP
    assert kinds[6] is LineKind.COMMAND
    assert kinds[8] is LineKind.TEXT
    assert kinds[9] is LineKind.ERROR_ANNOTATION
    assert log.lines[8].text == "Process completed with exit code 1."


def test_workflow_command_annotations_and_nested_groups() -> None:
    log = normalize("::group::Install\nok\n::endgroup::\n::error file=a.py,line=3::boom\n")
    assert log.sections[1].title == "Install"
    assert log.sections[1].has_error
    assert log.lines[3].kind is LineKind.ERROR_ANNOTATION
    assert log.lines[3].text == "boom"
    warning = normalize("##[warning]Node 16 is deprecated").lines[0]
    assert warning.kind is LineKind.WARNING_ANNOTATION


def test_gitlab_sections() -> None:
    text = (
        "\x1b[0Ksection_start:1700000000:step_script\r\x1b[0K"
        "\x1b[36;1mExecuting step_script\x1b[0;m\n"
        "$ npm test\n"
        "npm ERR! Test failed.\n"
        "\x1b[0Ksection_end:1700000001:step_script\r\x1b[0K\n"
    )
    log = normalize(text)
    assert log.sections[1].title == "step_script"
    assert log.lines[0].text == "Executing step_script"
    assert log.lines[2].text == "npm ERR! Test failed."
    assert log.lines[3].text == ""


def test_empty_log() -> None:
    log = normalize("")
    assert log.lines == ()
    assert len(log.sections) == 1


def test_real_logs_keep_line_numbers(real_case) -> None:  # type: ignore[no-untyped-def]
    text = real_case.text()
    log = normalize(text)
    assert len(log.lines) == len(split_lines(text)) == text.count("\n") + (not text.endswith("\n"))
    assert any(s.has_error for s in log.sections)


def test_carriage_returns_do_not_shift_line_numbers() -> None:
    log = normalize("a\r\nprogress 10%\rprogress 100%\nerror: boom\n")
    assert [(line.no, line.text) for line in log.lines] == [
        (1, "a"),
        (2, "progress 100%"),
        (3, "error: boom"),
    ]


def test_clean_line_drops_charset_selection_and_trailing_erase() -> None:
    assert clean_line("\x1b[36mdotnet-install:\x1b(B\x1b[m done") == "dotnet-install: done"
    assert clean_line("Downloading 10%\rDownloading 55%\r\x1b[0K") == "Downloading 55%"


TRAVIS_LOG = "\n".join(
    [
        "travis_fold:start:worker_info\r\x1b[0K\x1b[33;1mWorker information\x1b[0m",
        "hostname: worker-1",
        "travis_fold:end:worker_info\r\x1b[0Ktravis_fold:start:system_info\r\x1b[0K"
        "\x1b[33;1mBuild system information\x1b[0m\r\r",
        "Build language: csharp\r\r",
        "travis_fold:end:system_info\r\x1b[0K\r\r",
        "travis_fold:start:install\r\x1b[0Ktravis_time:start:aa\r\x1b[0K$ bundle install\r\r",
        "Using rake 12.0\r\r",
        "travis_time:end:aa:start=1,finish=2,duration=1\r\x1b[0K\r\r",
        "travis_fold:end:install\r\x1b[0K\r\r",
        "travis_time:start:bb\r\x1b[0K$ ./build.sh --verbosity=minimal\r\r",
        "An error occured while installing Cake.\r\r",
        "travis_time:end:bb:start=1,finish=2,duration=1\r\x1b[0K\r\r",
        '\x1b[31;1mThe command "./build.sh --verbosity=minimal" exited with 1.\x1b[0m\r\r',
        "Done. Your build exited with 1.\r\r",
    ]
)


def test_travis_folds_commands_and_failed_command() -> None:
    log = normalize(TRAVIS_LOG)
    assert [line.no for line in log.lines] == list(range(1, 15))
    titles = [s.title for s in log.sections]
    assert titles == [
        "(log start)",
        "worker_info",
        "system_info",
        "(top level)",
        "install",
        "(top level)",
        "./build.sh --verbosity=minimal",
    ]
    failed = [s.title for s in log.sections if s.has_error]
    assert failed == ["./build.sh --verbosity=minimal"]
    by_no = log.by_number()
    assert by_no[1].text == "Worker information"
    assert by_no[1].kind is LineKind.GROUP
    # "$ bundle install" sits inside a fold: output of that fold, not a new step.
    assert by_no[6].kind is LineKind.GROUP
    assert by_no[6].text == "$ bundle install"
    assert by_no[10].kind is LineKind.COMMAND
    assert by_no[11].text == "An error occured while installing Cake."
    assert by_no[12].text == ""
    assert by_no[13].kind is LineKind.ERROR_ANNOTATION
    assert by_no[13].text == 'The command "./build.sh --verbosity=minimal" exited with 1.'


def test_travis_failure_inside_a_fold_marks_that_fold() -> None:
    text = (
        "travis_fold:start:install.1\r\x1b[0K$ npm ci\r\n"
        "npm ERR! code ELIFECYCLE\r\n"
        'The command "npm ci" failed and exited with 1 during .\r\n'
        "travis_fold:end:install.1\r\x1b[0K\r\n"
    )
    log = normalize(text)
    assert [s.title for s in log.sections if s.has_error] == ["install.1"]


def test_dollar_lines_are_plain_text_outside_travis_logs() -> None:
    log = normalize("section_start:1:step_script\r\x1b[0K\n$ make test\nboom\n")
    assert [s.title for s in log.sections] == ["(log start)", "step_script"]
    assert log.lines[1].kind is LineKind.TEXT
