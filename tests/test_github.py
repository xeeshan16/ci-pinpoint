from __future__ import annotations

import os

import httpx
import pytest

from ci_pinpoint.ingest import GitHubError, SourceError, load_sources, parse_run_url
from ci_pinpoint.ingest.github import GitHubActionsClient, fetch_failed_logs

RUN = "https://github.com/acme/app/actions/runs/123"
LOG = "2026-01-01T00:00:00.0000000Z ##[error]Process completed with exit code 1.\n"


def test_parse_run_urls() -> None:
    ref = parse_run_url(RUN)
    assert ref is not None
    assert (ref.owner, ref.repo, ref.run_id, ref.job_id, ref.attempt) == (
        "acme",
        "app",
        123,
        None,
        None,
    )
    job = parse_run_url(f"{RUN}/job/456?pr=7")
    assert job is not None
    assert job.job_id == 456
    attempt = parse_run_url(f"{RUN}/attempts/2")
    assert attempt is not None
    assert attempt.attempt == 2
    assert parse_run_url("https://github.com/acme/app/pull/1") is None


def _api(
    jobs: list[dict[str, object]], *, status: int = 200, seen: list[httpx.Request] | None = None
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        path = request.url.path
        if status != 200:
            return httpx.Response(status, text="denied")
        if path.endswith("/runs/123/jobs") or path.endswith("/attempts/2/jobs"):
            return httpx.Response(200, json={"total_count": len(jobs), "jobs": jobs})
        if path.endswith("/logs"):
            # GitHub answers with a redirect to blob storage.
            return httpx.Response(302, headers={"location": "https://blob.example/log.txt"})
        if request.url.host == "blob.example":
            return httpx.Response(200, content=LOG.encode())
        if path.endswith("/actions/jobs/456"):
            return httpx.Response(200, json=jobs[0])
        return httpx.Response(404)

    return httpx.MockTransport(handler)


JOBS = [
    {"id": 1, "name": "lint", "conclusion": "success", "steps": []},
    {
        "id": 2,
        "name": "test (3.12)",
        "conclusion": "failure",
        "html_url": "https://github.com/acme/app/actions/runs/123/job/2",
        "steps": [
            {"name": "Set up job", "conclusion": "success"},
            {"name": "pytest", "conclusion": "failure"},
        ],
    },
    {"id": 3, "name": "e2e", "conclusion": "timed_out", "steps": []},
]


def test_fetches_only_failed_jobs_with_token() -> None:
    seen: list[httpx.Request] = []
    logs = load_sources(RUN, env={"GITHUB_TOKEN": "ghs_x"}, transport=_api(JOBS, seen=seen))
    assert [log.job_name for log in logs] == ["test (3.12)", "e2e"]
    assert logs[0].failed_steps == ("pytest",)
    assert logs[0].text == LOG
    assert logs[0].job_url == "https://github.com/acme/app/actions/runs/123/job/2"
    assert logs[1].source == f"{RUN}/job/3"
    api_calls = [r for r in seen if r.url.host == "api.github.com"]
    assert all(r.headers["authorization"] == "Bearer ghs_x" for r in api_calls)
    assert api_calls[0].url.params["filter"] == "latest"


def test_job_url_fetches_one_job() -> None:
    job = {**JOBS[1], "id": 456}
    logs = load_sources(f"{RUN}/job/456", env={"GH_TOKEN": "t"}, transport=_api([job]))
    assert len(logs) == 1
    assert logs[0].job_name == "test (3.12)"


def test_attempt_url_uses_attempt_endpoint() -> None:
    seen: list[httpx.Request] = []
    load_sources(f"{RUN}/attempts/2", env={"GITHUB_TOKEN": "t"}, transport=_api(JOBS, seen=seen))
    assert seen[0].url.path.endswith("/attempts/2/jobs")
    assert "filter" not in seen[0].url.params


def test_run_without_failures() -> None:
    with pytest.raises(GitHubError, match="no failed jobs"):
        load_sources(RUN, env={"GITHUB_TOKEN": "t"}, transport=_api([JOBS[0]]))


def test_missing_token_gets_a_helpful_error() -> None:
    with pytest.raises(GitHubError, match="set GITHUB_TOKEN"):
        load_sources(RUN, env={}, transport=_api(JOBS, status=403))


def test_not_found_with_token() -> None:
    with pytest.raises(GitHubError, match="not found"):
        load_sources(RUN, env={"GITHUB_TOKEN": "t"}, transport=_api(JOBS, status=404))


def test_server_error() -> None:
    with pytest.raises(GitHubError, match="HTTP 500"):
        load_sources(RUN, env={"GITHUB_TOKEN": "t"}, transport=_api(JOBS, status=500))


def test_transport_error() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(GitHubError, match="request failed"):
        load_sources(RUN, env={"GITHUB_TOKEN": "t"}, transport=httpx.MockTransport(boom))


def test_unsupported_url_and_missing_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(SourceError, match="unsupported URL"):
        load_sources("https://example.com/x/y/builds/1", env={})
    with pytest.raises(SourceError, match="no such file"):
        load_sources(str(tmp_path / "nope.log"), env={})


def test_pagination() -> None:
    page1 = [{"id": i, "name": f"j{i}", "conclusion": "failure", "steps": []} for i in range(100)]
    page2 = [{"id": 100, "name": "j100", "conclusion": "failure", "steps": []}]

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        jobs = page1 if page == "1" else page2
        return httpx.Response(200, json={"total_count": 101, "jobs": jobs})

    client = GitHubActionsClient(token="t", transport=httpx.MockTransport(handler))
    ref = parse_run_url(RUN)
    assert ref is not None
    assert len(client.jobs(ref)) == 101


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("CI_PINPOINT_LIVE_GITHUB"), reason="set CI_PINPOINT_LIVE_GITHUB=1"
)
def test_live_public_run() -> None:
    """Real API: a failed run of a public repository (needs GITHUB_TOKEN)."""
    ref = parse_run_url("https://github.com/xeeshan16/kafka-llm-enricher/actions/runs/40330806643")
    assert ref is not None
    client = GitHubActionsClient(token=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    logs = fetch_failed_logs(client, ref)
    assert [log.job_name for log in logs] == ["docker image, compose demo smoke test"]
    assert "MISSING              gen_ai_request_model" in logs[0].text
    assert logs[0].failed_steps == ("Smoke test",)
