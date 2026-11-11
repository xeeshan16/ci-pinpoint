"""Render briefs for terminals, Markdown (job summaries, agents) and JSON."""

from __future__ import annotations

import json
from collections.abc import Sequence

from ci_pinpoint.models import Brief, CitationStatus

FORMATS = ("text", "markdown", "json")
MAX_LINE_CHARS = 240


def _clip(text: str, limit: int = MAX_LINE_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _confidence(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _md_code(text: str) -> str:
    """Inline code that survives backticks in the log line."""
    fence = "``" if "`" in text else "`"
    return f"{fence} {text} {fence}" if fence == "``" else f"`{text}`"


def _excerpt_lines(brief: Brief) -> list[str]:
    out: list[str] = []
    previous: int | None = None
    for block in brief.excerpt:
        folded = {e.after: e for e in block.elided}
        for no, text in block.lines:
            if previous is not None and no != previous + 1:
                out.append("    …")
            out.append(f"L{no:<6} {_clip(text)}")
            previous = no
            if no in folded:
                e = folded[no]
                out.append(f"{'':7} … {e.count} more line(s) like: {_clip(e.template, 160)}")
                previous = no + e.count
    return out


def _title(brief: Brief) -> str:
    return brief.job_name or brief.source


def _redactions(brief: Brief) -> str | None:
    if not brief.redactions:
        return None
    total = sum(brief.redactions.values())
    kinds = ", ".join(f"{k} x{v}" if v > 1 else k for k, v in brief.redactions.items())
    return f"{total} secret(s) redacted before analysis: {kinds}"


def _untrusted(brief: Brief) -> str:
    listed = ", ".join(f"L{no}" for no in brief.untrusted_lines[:10])
    return f"instruction-like text in the log ({listed}) was treated as data, not evidence"


def _notes(brief: Brief, shape: str) -> list[str]:
    notes = [_redactions(brief), _untrusted(brief) if brief.untrusted_lines else None]
    return [shape.format(note) for note in notes if note]


def _reduction(brief: Brief) -> str:
    s = brief.stats
    text = (
        f"{s.excerpt_tokens:,} of ~{s.raw_tokens:,} tokens "
        f"({s.reduction:.1%} smaller), {len(brief.excerpt)} block(s)"
    )
    if brief.ranked_with:
        text += f", ranked with {brief.ranked_with}"
    if brief.cached:
        text += ", from cache"
    return text


def render_text(brief: Brief, *, show_excerpt: bool = False) -> str:
    out = [f"✖ {_title(brief)}"]
    if brief.job_url:
        out.append(f"  {brief.job_url}")
    if brief.failed_steps:
        out.append(f"  failed step: {', '.join(brief.failed_steps)}")
    d = brief.diagnosis
    if d is not None:
        diag = d.diagnosis
        out += [
            "",
            f"  {diag.summary}",
            "",
            f"  category    {diag.category.value}",
            f"  confidence  {_confidence(diag.confidence)}",
            f"  model       {d.provider}/{d.model}"
            + ("  (FAKE PROVIDER: not a real diagnosis)" if d.provider == "fake" else ""),
            "",
            "  Root cause",
            *[f"    {line}" for line in diag.root_cause.splitlines()],
            "",
            "  Evidence (checked against the log)",
        ]
        if d.citations:
            for c in d.citations:
                mark = "" if c.status is CitationStatus.VERIFIED else "  [line number corrected]"
                out.append(f"    L{c.line:<6} {_clip(c.text, 200)}{mark}")
        else:
            out.append("    none of the model's citations could be verified; treat with care")
        if d.rejected_citations:
            out.append(f"    ({len(d.rejected_citations)} unverifiable citation(s) dropped)")
        out += ["", "  Suggested fix", *[f"    {line}" for line in diag.suggested_fix.splitlines()]]
        if d.warnings:
            out += ["", "  ⚠ Warnings", *[f"    - {w}" for w in d.warnings]]
    elif brief.error:
        out += [
            "",
            f"  diagnosis failed: {brief.error}",
            "  showing the selected log lines instead",
        ]
        show_excerpt = True
    else:
        show_excerpt = True
    out += ["", f"  excerpt: {_reduction(brief)}", *_notes(brief, "  {}")]
    if show_excerpt:
        out += ["", *[f"  {line}" for line in _excerpt_lines(brief)]]
    return "\n".join(out)


def render_markdown(brief: Brief, *, show_excerpt: bool = True) -> str:
    title = _title(brief)
    out = [f"### ✖ {title}"]
    if brief.job_url:
        out.append(f"[{brief.job_url}]({brief.job_url})")
    d = brief.diagnosis
    if d is not None:
        diag = d.diagnosis
        fake = " — **fake provider, not a real diagnosis**" if d.provider == "fake" else ""
        out += [
            "",
            f"**{diag.summary}**",
            "",
            f"`{diag.category.value}` · confidence {_confidence(diag.confidence)} · "
            f"`{d.provider}/{d.model}`{fake}",
            "",
            "**Root cause.** " + diag.root_cause,
            "",
            "**Evidence** (each line checked against the log):",
            "",
        ]
        if d.citations:
            out += [f"- `L{c.line}` {_md_code(_clip(c.text, 200))}" for c in d.citations]
        else:
            out.append("- none of the model's citations could be verified")
        out += ["", "**Suggested fix.** " + diag.suggested_fix]
        if d.warnings:
            out += ["", *[f"> ⚠ {w}" for w in d.warnings]]
    elif brief.error:
        out += ["", f"> diagnosis failed: {brief.error}"]
    out += ["", f"_Excerpt: {_reduction(brief)}._", *_notes(brief, "_{}._")]
    if show_excerpt and brief.excerpt:
        out += [
            "",
            "<details><summary>Selected log lines</summary>",
            "",
            "```text",
            *_excerpt_lines(brief),
            "```",
            "",
            "</details>",
        ]
    return "\n".join(out)


def render(briefs: Sequence[Brief], fmt: str, *, show_excerpt: bool = False) -> str:
    if fmt == "json":
        payload = [b.model_dump(mode="json") for b in briefs]
        return json.dumps(payload[0] if len(payload) == 1 else payload, indent=2)
    if fmt == "markdown":
        return "\n\n".join(render_markdown(b) for b in briefs)
    return "\n\n".join(render_text(b, show_excerpt=show_excerpt) for b in briefs)
