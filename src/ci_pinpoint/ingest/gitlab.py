"""Fetch the logs of failed jobs from a GitLab job or pipeline URL.

Supported URLs (gitlab.com or a self-managed host)::

    https://gitlab.com/<namespace>/<project>/-/jobs/<job_id>[/raw]
    https://gitlab.com/<namespace>/<project>/-/pipelines/<pipeline_id>

Downloading a job log needs a token, even for a public project: gitlab.com lists a
public pipeline's jobs anonymously but answers 401 for the job and its trace (checked
against the live API in November 2026). Set ``GITLAB_TOKEN`` (a personal, project or
group access token with ``read_api``); inside a GitLab job, ``CI_JOB_TOKEN`` is used if
no other token is set. The API
base defaults to ``https://<host>/api/v4``; set ``GITLAB_API_URL`` (or GitLab's own
``CI_API_V4_URL``) when the instance lives under a path prefix.

A token is only ever sent to gitlab.com or to the host named by ``GITLAB_API_URL``,
``CI_API_V4_URL``, ``CI_SERVER_HOST`` or ``GITLAB_HOST``. The URL to fetch can come
from an agent that has read untrusted text, and a crafted self-managed URL must not
be able to collect the token.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from ci_pinpoint.models import RawLog

GITLAB_URL = re.compile(
    r"^https?://(?P<host>[^/]+)/(?P<project>[\w.\-]+(?:/[\w.\-]+)+)/-/"
    r"(?P<kind>jobs|pipelines)/(?P<id>\d+)(?:/raw)?/?(?:[?#].*)?$"
)
MAX_LOG_BYTES = 64 * 1024 * 1024
MAX_JOBS = 50


class GitLabError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GitLabRef:
    host: str
    project: str  # full path, e.g. "gitlab-org/gitlab-runner"
    job_id: int | None = None
    pipeline_id: int | None = None

    @property
    def project_url(self) -> str:
        return f"https://{self.host}/{self.project}"

    @property
    def api_project(self) -> str:
        return quote(self.project, safe="")


def parse_gitlab_url(url: str) -> GitLabRef | None:
    match = GITLAB_URL.match(url.strip())
    if not match or match["host"] == "github.com":
        return None
    ident = int(match["id"])
    if match["kind"] == "jobs":
        return GitLabRef(host=match["host"], project=match["project"], job_id=ident)
    return GitLabRef(host=match["host"], project=match["project"], pipeline_id=ident)


def _explicit_api(env: Mapping[str, str]) -> str | None:
    return env.get("GITLAB_API_URL") or env.get("CI_API_V4_URL") or None


def trusted_hosts(env: Mapping[str, str]) -> frozenset[str]:
    """Hosts that may receive a GitLab token."""
    hosts = {"gitlab.com"}
    explicit = _explicit_api(env)
    if explicit:
        hosts.add((urlsplit(explicit).hostname or "").lower())
    for key in ("CI_SERVER_HOST", "GITLAB_HOST"):
        if env.get(key):
            hosts.add(env[key].strip().lower())
    hosts.discard("")
    return frozenset(hosts)


def gitlab_auth_headers(env: Mapping[str, str], host: str | None = None) -> dict[str, str]:
    """Auth headers for ``host``; none if the host is not trusted with the token."""
    if host is not None and host.lower().split(":")[0] not in trusted_hosts(env):
        return {}
    token = env.get("GITLAB_TOKEN") or env.get("GL_TOKEN")
    if token:
        return {"PRIVATE-TOKEN": token}
    job_token = env.get("CI_JOB_TOKEN")
    if job_token:
        return {"JOB-TOKEN": job_token}
    return {}


def gitlab_api_url(ref: GitLabRef, env: Mapping[str, str]) -> str:
    """The configured API base when it serves the URL's host, else ``https://<host>/api/v4``."""
    explicit = _explicit_api(env)
    if explicit and (urlsplit(explicit).netloc or "").lower() == ref.host.lower():
        return explicit.rstrip("/")
    return f"https://{ref.host}/api/v4"


class GitLabClient:
    def __init__(
        self,
        *,
        api_url: str,
        headers: Mapping[str, str] | None = None,
        timeout_s: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._has_token = bool(headers)
        self._api = api_url.rstrip("/")
        self._client = httpx.Client(
            headers={"User-Agent": "ci-pinpoint", **(headers or {})},
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            follow_redirects=True,
            transport=transport,
        )

    def _get(self, path: str, params: Mapping[str, str | int] | None = None) -> httpx.Response:
        try:
            response = self._client.get(f"{self._api}{path}", params=params)
        except httpx.HTTPError as exc:
            raise GitLabError(f"GitLab request failed: {exc}") from exc
        if response.status_code in (401, 403, 404) and not self._has_token:
            raise GitLabError(
                f"GitLab returned HTTP {response.status_code} for {path}. Reading job logs "
                "needs a token, even for public projects: set GITLAB_TOKEN (read_api scope)."
            )
        if response.status_code == 404:
            raise GitLabError(f"not found: {path} (check the URL, or the token's project access)")
        if response.status_code >= 400:
            raise GitLabError(
                f"GitLab returned HTTP {response.status_code} for {path}: {response.text[:200]}"
            )
        return response

    def job(self, ref: GitLabRef, job_id: int) -> dict[str, Any]:
        data: dict[str, Any] = self._get(f"/projects/{ref.api_project}/jobs/{job_id}").json()
        return data

    def failed_jobs(self, ref: GitLabRef, pipeline_id: int) -> list[dict[str, Any]]:
        path = f"/projects/{ref.api_project}/pipelines/{pipeline_id}/jobs"
        jobs: list[dict[str, Any]] = []
        page = 1
        while len(jobs) < MAX_JOBS:
            response = self._get(path, {"scope[]": "failed", "per_page": 100, "page": page})
            batch = response.json() or []
            jobs.extend(batch)
            next_page = response.headers.get("x-next-page", "").strip()
            if not batch or not next_page:
                break
            page = int(next_page)
        return jobs[:MAX_JOBS]

    def trace(self, ref: GitLabRef, job_id: int) -> str:
        response = self._get(f"/projects/{ref.api_project}/jobs/{job_id}/trace")
        if len(response.content) > MAX_LOG_BYTES:
            raise GitLabError(f"job {job_id} log is larger than {MAX_LOG_BYTES // 2**20} MiB")
        return response.content.decode("utf-8", errors="replace")

    def close(self) -> None:
        self._client.close()


def _blocking(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Failed jobs that failed the pipeline; allowed failures only if nothing else failed."""
    hard = [j for j in jobs if not j.get("allow_failure")]
    return hard or jobs


def _raw(client: GitLabClient, ref: GitLabRef, job: Mapping[str, Any]) -> RawLog:
    job_id = int(job["id"])
    web_url = str(job.get("web_url") or f"{ref.project_url}/-/jobs/{job_id}")
    name = str(job.get("name") or job_id)
    stage = job.get("stage")
    return RawLog(
        text=client.trace(ref, job_id),
        source=f"{ref.project_url}/-/jobs/{job_id}",
        job_name=f"{stage}: {name}" if stage else name,
        job_url=web_url,
        # GitLab has no step list; the trace's own section markers stand in for it.
        failed_steps=(),
    )


def fetch_gitlab_logs(client: GitLabClient, ref: GitLabRef) -> list[RawLog]:
    """Logs of the failed jobs in a pipeline, or of the one job the URL names."""
    if ref.job_id is not None:
        return [_raw(client, ref, client.job(ref, ref.job_id))]
    assert ref.pipeline_id is not None
    jobs = _blocking(client.failed_jobs(ref, ref.pipeline_id))
    if not jobs:
        raise GitLabError(f"{ref.project_url}/-/pipelines/{ref.pipeline_id} has no failed jobs")
    return [_raw(client, ref, job) for job in jobs]
