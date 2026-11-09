from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "logs"


@dataclass(frozen=True)
class RealCase:
    file: str
    run_url: str
    job: str
    category: str
    anchors: list[str]

    @property
    def path(self) -> Path:
        return FIXTURES / self.file

    def text(self) -> str:
        return gzip.decompress(self.path.read_bytes()).decode("utf-8", errors="replace")


def real_cases() -> list[RealCase]:
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    return [RealCase(**case) for case in manifest["cases"]]


@pytest.fixture(params=real_cases(), ids=lambda c: c.file.removesuffix(".log.gz"))
def real_case(request: pytest.FixtureRequest) -> RealCase:
    case: RealCase = request.param
    return case
