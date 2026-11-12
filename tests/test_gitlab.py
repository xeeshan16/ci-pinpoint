from __future__ import annotations

import os

import httpx
import pytest

from ci_pinpoint.config import Settings
from ci_pinpoint.ingest import GitLabError, SourceError, load_sources, parse_gitlab_url
from ci_pinpoint.ingest.gitlab import GitLabClient, gitlab_api_url, gitlab_auth_headers
from ci_pinpoint.models import LineKind
from ci_pinpoint.normalize import normalize
from ci_pinpoint.pipeline import explain

PROJECT = "https://gitlab.com/acme/platform/api"
JOB = f"{PROJECT}/-/jobs/4242"
PIPELINE = f"{PROJECT}/-/pipelines/77"

# Shaped like a real gitlab-runner trace: ANSI colours, section markers wrapped in
# erase-line codes, a failing pytest run, then the runner's own epilogue.
TRACE = (
    "\x1b[0KRunning with gitlab-runner 17.5.0 (abc123)\x1b[0;m\n"
    "\x1b[0K  on blue-3.saas-linux-small-amd64.runners-manager.gitlab.com/default x\x1b[0;m\n"
    "section_start:1731400000:prepare_executor\r\x1b[0K\x1b[0K\x1b[36;1m"
    'Preparing the "docker+machine" executor\x1b[0;m\x1b[0;m\n'
    "\x1b[0KUsing Docker executor with image python:3.12 ...\x1b[0;m\n"
    "section_end:1731400010:prepare_executor\r\x1b[0K\n"
    "section_start:1731400010:get_sources\r\x1b[0K\x1b[0K\x1b[36;1mGetting source from Git "
    "repository\x1b[0;m\x1b[0;m\n"
    "\x1b[32;1mFetching changes with git depth set to 20...\x1b[0;m\n"
    "section_end:1731400012:get_sources\r\x1b[0K\n"
    'section_start:1731400012:step_script\r\x1b[0K\x1b[0K\x1b[36;1mExecuting "step_script" '
    "stage of the job script\x1b[0;m\x1b[0;m\n"
    "\x1b[32;1m$ pip install -e '.[test]'\x1b[0;m\n"
    "Successfully installed api-0.4.0\n"
    "\x1b[32;1m$ pytest -q\x1b[0;m\n"
    "....F....                                                    [100%]\n"
    "=================================== FAILURES ===================================\n"
    "_____________________________ test_rate_limit_header _____________________________\n"
    "    def test_rate_limit_header(client):\n"
    "        response = client.get('/v1/items')\n"
    ">       assert response.headers['X-RateLimit-Limit'] == '100'\n"
    "E       KeyError: 'x-ratelimit-limit'\n"
    "tests/test_items.py:41: KeyError\n"
    "=========================== short test summary info ============================\n"
    "FAILED tests/test_items.py::test_rate_limit_header - KeyError: 'x-ratelimit-limit'\n"
    "1 failed, 8 passed in 2.31s\n"
    "section_end:1731400030:step_script\r\x1b[0K\n"
    "section_start:1731400030:upload_artifacts_on_failure\r\x1b[0K\x1b[0K\x1b[36;1mUploading "
    "artifacts for failed job\x1b[0;m\x1b[0;m\n"
    "\x1b[0;33mWARNING: report.xml: no matching files\x1b[0;m\n"
    "section_end:1731400031:upload_artifacts_on_failure\r\x1b[0K\n"
    "section_start:1731400031:cleanup_file_variables\r\x1b[0K\x1b[0K\x1b[36;1mCleaning up "
    "project directory and file based variables\x1b[0;m\x1b[0;m\n"
    "section_end:1731400032:cleanup_file_variables\r\x1b[0K\n"
    "\x1b[31;1mERROR: Job failed: exit code 1\n"
    "\x1b[0;m\n"
)
CAUSE_LINE = TRACE.split("\n").index("E       KeyError: 'x-ratelimit-limit'") + 1

JOB_JSON = {
    "id": 4242,
    "name": "pytest",
    "stage": "test",
    "status": "failed",
    "allow_failure": False,
    "web_url": f"{PROJECT}/-/jobs/4242",
}


def _api(
    *,
    jobs: list[dict[str, object]] | None = None,
    status: int = 200,
    seen: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if status != 200:
            return httpx.Response(status, json={"message": "404 Project Not Found"})
        path = request.url.raw_path.decode()
        if "/projects/acme%2Fplatform%2Fapi/" not in path:
            return httpx.Response(404)
        if path.split("?")[0].endswith("/pipelines/77/jobs"):
            return httpx.Response(200, json=jobs or [])
        if path.endswith("/trace"):
            return httpx.Response(200, content=TRACE.encode())
        if path.split("?")[0].endswith("/jobs/4242"):
            return httpx.Response(200, json=JOB_JSON)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_parse_gitlab_urls() -> None:
    job = parse_gitlab_url(JOB)
    assert job is not None
    assert (job.host, job.project, job.job_id, job.pipeline_id) == (
        "gitlab.com",
        "acme/platform/api",
        4242,
        None,
    )
    assert job.api_project == "acme%2Fplatform%2Fapi"
    raw = parse_gitlab_url(f"{JOB}/raw")
    assert raw is not None
    assert raw.job_id == 4242
    pipe = parse_gitlab_url(f"{PIPELINE}?tab=failures")
    assert pipe is not None
    assert pipe.pipeline_id == 77
    self_managed = parse_gitlab_url("https://git.example.org/team/svc/-/jobs/9")
    assert self_managed is not None
    assert self_managed.host == "git.example.org"
    assert parse_gitlab_url(f"{PROJECT}/-/merge_requests/3") is None
    assert parse_gitlab_url("https://github.com/acme/app/-/jobs/1") is None


def test_auth_and_api_url() -> None:
    assert gitlab_auth_headers({}) == {}
    assert gitlab_auth_headers({"GITLAB_TOKEN": "glpat"}) == {"PRIVATE-TOKEN": "glpat"}
    assert gitlab_auth_headers({"CI_JOB_TOKEN": "j"}) == {"JOB-TOKEN": "j"}
    assert gitlab_auth_headers({"GITLAB_TOKEN": "g", "CI_JOB_TOKEN": "j"}) == {"PRIVATE-TOKEN": "g"}
    ref = parse_gitlab_url(JOB)
    assert ref is not None
    assert gitlab_api_url(ref, {}) == "https://gitlab.com/api/v4"
    assert gitlab_api_url(ref, {"CI_API_V4_URL": "https://h/gl/api/v4/"}) == ("https://h/gl/api/v4")


def test_job_url_fetches_trace_without_token() -> None:
    seen: list[httpx.Request] = []
    logs = load_sources(JOB, env={}, transport=_api(seen=seen))
    assert len(logs) == 1
    assert logs[0].job_name == "test: pytest"
    assert logs[0].job_url == f"{PROJECT}/-/jobs/4242"
    assert logs[0].source == f"{PROJECT}/-/jobs/4242"
    assert "KeyError" in logs[0].text
    assert all("PRIVATE-TOKEN" not in r.headers for r in seen)


def test_token_is_sent() -> None:
    seen: list[httpx.Request] = []
    load_sources(JOB, env={"GITLAB_TOKEN": "glpat-x"}, transport=_api(seen=seen))
    assert {r.headers["PRIVATE-TOKEN"] for r in seen} == {"glpat-x"}


def test_pipeline_takes_failed_jobs_that_failed_the_pipeline() -> None:
    jobs: list[dict[str, object]] = [
        {**JOB_JSON, "id": 4242},
        {**JOB_JSON, "id": 4243, "name": "flaky-e2e", "allow_failure": True},
    ]
    seen: list[httpx.Request] = []
    logs = load_sources(PIPELINE, env={}, transport=_api(jobs=jobs, seen=seen))
    assert [log.source for log in logs] == [f"{PROJECT}/-/jobs/4242"]
    listing = next(r for r in seen if r.url.path.endswith("/pipelines/77/jobs"))
    assert listing.url.params.get("scope[]") == "failed"


def test_pipeline_with_only_allowed_failures_still_reports_them() -> None:
    jobs: list[dict[str, object]] = [{**JOB_JSON, "allow_failure": True}]
    logs = load_sources(PIPELINE, env={}, transport=_api(jobs=jobs))
    assert len(logs) == 1


def test_pipeline_without_failures() -> None:
    with pytest.raises(GitLabError, match="no failed jobs"):
        load_sources(PIPELINE, env={}, transport=_api(jobs=[]))


def test_pipeline_pagination() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        if page == "1":
            batch = [{**JOB_JSON, "id": i} for i in range(100)]
            return httpx.Response(200, json=batch, headers={"x-next-page": "2"})
        return httpx.Response(200, json=[{**JOB_JSON, "id": 100}], headers={"x-next-page": ""})

    client = GitLabClient(
        api_url="https://gitlab.com/api/v4", transport=httpx.MockTransport(handler)
    )
    ref = parse_gitlab_url(PIPELINE)
    assert ref is not None
    # Capped: a pipeline with hundreds of failures is not diagnosed job by job.
    assert len(client.failed_jobs(ref, 77)) == 50


def test_private_project_without_token_gets_a_helpful_error() -> None:
    with pytest.raises(GitLabError, match="GITLAB_TOKEN"):
        load_sources(JOB, env={}, transport=_api(status=404))


def test_errors_with_token() -> None:
    with pytest.raises(GitLabError, match="not found"):
        load_sources(JOB, env={"GITLAB_TOKEN": "t"}, transport=_api(status=404))
    with pytest.raises(GitLabError, match="HTTP 500"):
        load_sources(JOB, env={"GITLAB_TOKEN": "t"}, transport=_api(status=500))

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(GitLabError, match="request failed"):
        load_sources(JOB, env={}, transport=httpx.MockTransport(boom))


def test_unsupported_gitlab_shaped_url() -> None:
    with pytest.raises(SourceError, match="gitlab-host"):
        load_sources(f"{PROJECT}/-/merge_requests/3", env={})


def test_trace_marks_the_script_section_and_commands() -> None:
    log = normalize(TRACE)
    failed = [s.title for s in log.sections if s.has_error]
    assert failed == ["step_script"]
    by_no = log.by_number()
    commands = [line.text for line in log.lines if line.kind is LineKind.COMMAND]
    assert commands == ["$ pip install -e '.[test]'", "$ pytest -q"]
    last = log.lines[-2]
    assert last.text == "ERROR: Job failed: exit code 1"
    assert last.kind is LineKind.ERROR_ANNOTATION
    assert by_no[CAUSE_LINE].text == "E       KeyError: 'x-ratelimit-limit'"


def test_dollar_lines_outside_gitlab_traces_are_plain_text() -> None:
    log = normalize("$ make test\nboom\n")
    assert {line.kind for line in log.lines} == {LineKind.TEXT}


def test_excerpt_of_a_gitlab_trace_contains_the_cause() -> None:
    logs = load_sources(JOB, env={}, transport=_api())
    brief = explain(logs[0], Settings(budget_tokens=200), None)
    shown = {no for block in brief.excerpt for no, _ in block.lines}
    assert CAUSE_LINE in shown
    assert brief.failed_steps == ["step_script"]


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("CI_PINPOINT_LIVE_GITLAB"), reason="set CI_PINPOINT_LIVE_GITLAB=1"
)
def test_live_public_gitlab_job() -> None:
    """Real API, no token: a recent failed job of a public gitlab.com project."""
    project = os.environ.get("CI_PINPOINT_LIVE_GITLAB_PROJECT", "gitlab-org/gitlab-runner")
    api = "https://gitlab.com/api/v4/projects/" + project.replace("/", "%2F")
    with httpx.Client(timeout=30, headers={"User-Agent": "ci-pinpoint-tests"}) as client:
        listing = client.get(f"{api}/jobs", params={"scope[]": "failed", "per_page": 5})
    if listing.status_code in (401, 403):
        pytest.skip(f"gitlab.com does not list {project} jobs anonymously ({listing.status_code})")
    listing.raise_for_status()
    jobs = listing.json()
    assert jobs, f"no failed jobs listed for {project}"
    logs = load_sources(jobs[0]["web_url"], env={})
    assert len(logs) == 1
    brief = explain(logs[0], Settings(), None)
    assert brief.stats.raw_lines > 10
    assert brief.excerpt
    print(f"\n{jobs[0]['web_url']}: {brief.stats.raw_lines} lines, failed {brief.failed_steps}")
