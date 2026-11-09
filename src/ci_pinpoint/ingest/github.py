"""Fetch the logs of failed jobs from a GitHub Actions run or job URL.

Supported URLs::

    https://github.com/<owner>/<repo>/actions/runs/<run_id>
    https://github.com/<owner>/<repo>/actions/runs/<run_id>/attempts/<n>
    https://github.com/<owner>/<repo>/actions/runs/<run_id>/job/<job_id>

GitHub requires a token to download job logs, even for public repositories:
set ``GITHUB_TOKEN`` (or ``GH_TOKEN``). A fine-grained token with read access
to Actions is enough.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from ci_pinpoint.models import RawLog

RUN_URL = re.compile(
    r"^https?://(?P<host>[^/]+)/(?P<owner>[\w.\-]+)/(?P<repo>[\w.\-]+)/actions/runs/(?P<run>\d+)"
    r"(?:/attempts/(?P<attempt>\d+))?(?:/job/(?P<job>\d+))?/?(?:[?#].*)?$"
)
FAILED_CONCLUSIONS = frozenset({"failure", "timed_out"})
MAX_LOG_BYTES = 64 * 1024 * 1024


class GitHubError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RunRef:
    host: str
    owner: str
    repo: str
    run_id: int
    attempt: int | None = None
    job_id: int | None = None

    @property
    def run_url(self) -> str:
        return f"https://{self.host}/{self.owner}/{self.repo}/actions/runs/{self.run_id}"


def parse_run_url(url: str) -> RunRef | None:
    match = RUN_URL.match(url.strip())
    if not match:
        return None
    return RunRef(
        host=match["host"],
        owner=match["owner"],
        repo=match["repo"],
        run_id=int(match["run"]),
        attempt=int(match["attempt"]) if match["attempt"] else None,
        job_id=int(match["job"]) if match["job"] else None,
    )


def github_token(env: Mapping[str, str]) -> str | None:
    return env.get("GITHUB_TOKEN") or env.get("GH_TOKEN") or None


class GitHubActionsClient:
    def __init__(
        self,
        *,
        token: str | None,
        api_url: str = "https://api.github.com",
        timeout_s: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ci-pinpoint",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._has_token = bool(token)
        self._api = api_url.rstrip("/")
        self._client = httpx.Client(
            headers=headers,
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            follow_redirects=True,
            transport=transport,
        )

    def _get(self, path: str, params: Mapping[str, str | int] | None = None) -> httpx.Response:
        try:
            response = self._client.get(f"{self._api}{path}", params=params)
        except httpx.HTTPError as exc:
            raise GitHubError(f"GitHub request failed: {exc}") from exc
        if response.status_code in (401, 403, 404) and not self._has_token:
            raise GitHubError(
                f"GitHub returned HTTP {response.status_code} for {path}. Downloading Actions logs "
                "needs a token, even for public repositories: set GITHUB_TOKEN or GH_TOKEN."
            )
        if response.status_code == 404:
            raise GitHubError(
                f"not found: {path} (check the URL, or the token's repository access)"
            )
        if response.status_code >= 400:
            raise GitHubError(
                f"GitHub returned HTTP {response.status_code} for {path}: {response.text[:200]}"
            )
        return response

    def jobs(self, ref: RunRef) -> list[dict[str, Any]]:
        base = f"/repos/{ref.owner}/{ref.repo}/actions/runs/{ref.run_id}"
        path = f"{base}/attempts/{ref.attempt}/jobs" if ref.attempt else f"{base}/jobs"
        jobs: list[dict[str, Any]] = []
        page = 1
        while True:
            params: dict[str, str | int] = {"per_page": 100, "page": page}
            if not ref.attempt:
                params["filter"] = "latest"
            data = self._get(path, params).json()
            batch = data.get("jobs") or []
            jobs.extend(batch)
            if len(batch) < 100 or len(jobs) >= int(data.get("total_count") or 0):
                return jobs
            page += 1

    def job(self, ref: RunRef, job_id: int) -> dict[str, Any]:
        data: dict[str, Any] = self._get(
            f"/repos/{ref.owner}/{ref.repo}/actions/jobs/{job_id}"
        ).json()
        return data

    def job_log(self, ref: RunRef, job_id: int) -> str:
        response = self._get(f"/repos/{ref.owner}/{ref.repo}/actions/jobs/{job_id}/logs")
        if len(response.content) > MAX_LOG_BYTES:
            raise GitHubError(f"job {job_id} log is larger than {MAX_LOG_BYTES // 2**20} MiB")
        return response.content.decode("utf-8", errors="replace")

    def close(self) -> None:
        self._client.close()


def _failed_steps(job: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(step.get("name"))
        for step in job.get("steps") or []
        if step.get("conclusion") in FAILED_CONCLUSIONS
    )


def fetch_failed_logs(client: GitHubActionsClient, ref: RunRef) -> list[RawLog]:
    """Logs of the failed jobs in a run (or of the one job the URL names)."""
    if ref.job_id is not None:
        jobs = [client.job(ref, ref.job_id)]
    else:
        jobs = [j for j in client.jobs(ref) if j.get("conclusion") in FAILED_CONCLUSIONS]
        if not jobs:
            raise GitHubError(f"{ref.run_url} has no failed jobs")
    logs: list[RawLog] = []
    for job in jobs:
        job_id = int(job["id"])
        logs.append(
            RawLog(
                text=client.job_log(ref, job_id),
                source=f"{ref.run_url}/job/{job_id}",
                job_name=str(job.get("name") or job_id),
                job_url=str(job.get("html_url") or f"{ref.run_url}/job/{job_id}"),
                failed_steps=_failed_steps(job),
            )
        )
    return logs
