"""Turn a raw CI log into clean, numbered lines grouped into sections.

Handles the formats that show up in practice:

* GitHub Actions job logs: a UTF-8 BOM, an ISO-8601 timestamp on every line,
  ``##[group]``/``##[endgroup]`` and ``::group::``/``::endgroup::`` markers and
  ``##[error]`` / ``::error ...::`` annotations.
* GitLab job traces: ``section_start:<ts>:<name>`` / ``section_end:<ts>:<name>``
  markers, usually wrapped in ANSI escape codes.
* Travis CI job logs: ``travis_fold:start:<name>`` / ``travis_fold:end:<name>``
  and ``travis_time:...`` markers hidden behind ``\r\x1b[0K``; every top-level
  ``$ command`` starts a step, and ``The command "..." exited with N.`` marks the
  step that failed.
* Anything else: ANSI colours and carriage-return progress bars are removed.

Line numbers always refer to the raw log, so a citation can be checked against
the bytes the user actually has.
"""

from __future__ import annotations

import re

from ci_pinpoint.models import LineKind, LogLine, NormalizedLog, Section

ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI: colours, cursor movement, erase line
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC: window titles, hyperlinks
    r"|\x1b[()][0-9A-Za-z]"  # character set selection, e.g. tput sgr0's \x1b(B
    r"|\x1b[@-Z\\-_]"
)
GHA_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ?")
GROUP_RE = re.compile(r"^(?:##\[group\]|::group::)(.*)$")
ENDGROUP_RE = re.compile(r"^(?:##\[endgroup\]|::endgroup::)\s*$")
GHA_ANNOTATION_RE = re.compile(r"^##\[(error|warning|notice|debug)\](.*)$")
WORKFLOW_COMMAND_RE = re.compile(r"^::(error|warning|notice)(?:\s[^:]*)?::(.*)$")
GITLAB_SECTION_START_RE = re.compile(r"section_start:\d+:([\w.\-]+)(?:\[[^\]]*\])?\r?(.*)")
GITLAB_SECTION_END_RE = re.compile(r"section_end:\d+:[\w.\-]+\r?")
TRAVIS_MARKER_RE = re.compile(r"travis_(fold|time):(start|end):([\w.\-]+)[^\r\n]*?(?=\r|$)")
TRAVIS_COMMAND_RE = re.compile(r"^\$ (\S.*)$")
TRAVIS_EXIT_RE = re.compile(r'^The command "(.*)" (?:failed and )?exited with ([1-9]\d*)')

PREAMBLE_TITLE = "(log start)"


def clean_line(raw: str) -> str:
    """Strip ANSI codes, a leading GitHub timestamp and carriage-return redraws."""
    line = raw.lstrip("﻿")
    line = GHA_TIMESTAMP_RE.sub("", line, count=1)
    if "\r" in line:
        # Progress bars redraw with \r; what the user last saw is the final
        # segment that still has text once escape codes are gone.
        segments = [t for t in (ANSI_RE.sub("", s) for s in line.split("\r")) if t.strip()]
        line = segments[-1] if segments else ""
    line = ANSI_RE.sub("", line)
    return line.rstrip()


def split_lines(text: str) -> list[str]:
    """Split on ``\n`` only. ``str.splitlines`` would also split on ``\r`` (progress
    bars) and other separators, and line numbers would drift from the raw file."""
    if not text:
        return []
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


def normalize(text: str) -> NormalizedLog:
    """Clean every line and split the log into sections."""
    lines: list[LogLine] = []
    section_titles: list[str] = [PREAMBLE_TITLE]
    section_first: list[int] = [1]
    section_errors: list[bool] = [False]

    def open_section(title: str, line_no: int) -> int:
        section_titles.append(title.strip() or "(untitled)")
        section_first.append(line_no)
        section_errors.append(False)
        return len(section_titles) - 1

    current = 0
    in_run_header = False
    travis = "travis_fold:" in text or "travis_time:" in text
    in_fold = False
    for no, raw in enumerate(split_lines(text), start=1):
        if travis:
            markers = TRAVIS_MARKER_RE.findall(raw)
            cleaned = clean_line(TRAVIS_MARKER_RE.sub("", raw))
            folds = [(edge, name) for kind_, edge, name in markers if kind_ == "fold"]
            if folds:
                edge, name = folds[-1]
                in_fold = edge == "start"
                current = open_section(name if in_fold else "(top level)", no)
                lines.append(
                    LogLine(
                        no=no,
                        text=cleaned,
                        section=current,
                        kind=LineKind.GROUP if in_fold else LineKind.ENDGROUP,
                    )
                )
                continue
            command = TRAVIS_COMMAND_RE.match(cleaned)
            if command and not in_fold:
                current = open_section(command.group(1), no)
                lines.append(LogLine(no=no, text=cleaned, section=current, kind=LineKind.COMMAND))
                continue
            if TRAVIS_EXIT_RE.match(cleaned):
                section_errors[current] = True
                lines.append(
                    LogLine(no=no, text=cleaned, section=current, kind=LineKind.ERROR_ANNOTATION)
                )
                continue
            lines.append(LogLine(no=no, text=cleaned, section=current, kind=LineKind.TEXT))
            continue
        # GitLab markers live inside ANSI sequences, so look before stripping colours.
        gl_start = GITLAB_SECTION_START_RE.search(raw)
        if gl_start:
            current = open_section(gl_start.group(1), no)
            rest = clean_line(gl_start.group(2))
            lines.append(LogLine(no=no, text=rest, section=current, kind=LineKind.GROUP))
            continue
        cleaned = clean_line(GITLAB_SECTION_END_RE.sub("", raw))

        group = GROUP_RE.match(cleaned)
        if group:
            current = open_section(group.group(1), no)
            in_run_header = group.group(1).startswith("Run ")
            lines.append(LogLine(no=no, text=group.group(1), section=current, kind=LineKind.GROUP))
            continue
        if ENDGROUP_RE.match(cleaned):
            in_run_header = False
            lines.append(LogLine(no=no, text="", section=current, kind=LineKind.ENDGROUP))
            continue

        kind = LineKind.TEXT
        annotation = GHA_ANNOTATION_RE.match(cleaned) or WORKFLOW_COMMAND_RE.match(cleaned)
        if annotation:
            level, message = annotation.group(1), annotation.group(2)
            if level == "error":
                kind = LineKind.ERROR_ANNOTATION
                section_errors[current] = True
            elif level == "warning":
                kind = LineKind.WARNING_ANNOTATION
            cleaned = message
        elif in_run_header:
            kind = LineKind.COMMAND
        lines.append(LogLine(no=no, text=cleaned, section=current, kind=kind))

    total = len(lines)
    sections: list[Section] = []
    for index, title in enumerate(section_titles):
        first = section_first[index]
        last = section_first[index + 1] - 1 if index + 1 < len(section_titles) else total
        sections.append(
            Section(
                index=index,
                title=title,
                first_line=first,
                last_line=max(first, last),
                has_error=section_errors[index],
            )
        )
    return NormalizedLog(lines=tuple(lines), sections=tuple(sections))
