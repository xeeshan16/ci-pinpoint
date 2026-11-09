"""On-disk caches, so explaining the same failure twice costs nothing.

* :class:`BriefCache` stores finished briefs as JSON, keyed by the SHA-256 of
  the raw log plus every setting that changes the answer (budget, provider,
  model, prompt version, embedder, tool version). Writes are atomic.
* :class:`EmbeddingStore` keeps embedding vectors in SQLite, keyed by model and
  text digest; :class:`CachedEmbedder` wraps any embedder with it, so prototype
  sentences and repeated blocks are embedded once per machine.

Location: ``CI_PINPOINT_CACHE_DIR``, else ``$XDG_CACHE_HOME/ci-pinpoint``, else
``~/.cache/ci-pinpoint``. Delete the directory to clear it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import tempfile
from array import array
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ci_pinpoint import __version__
from ci_pinpoint.models import Brief
from ci_pinpoint.providers.base import Embedder

log = logging.getLogger(__name__)


def default_cache_dir(env: Mapping[str, str]) -> Path:
    if env.get("CI_PINPOINT_CACHE_DIR"):
        return Path(env["CI_PINPOINT_CACHE_DIR"])
    base = env.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "ci-pinpoint"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def brief_key(log_text: str, parts: Mapping[str, Any]) -> str:
    """Cache key for a brief: the log's hash plus everything that changes the answer."""
    fingerprint = json.dumps(
        {"log": sha256_text(log_text), "tool": __version__, **parts}, sort_keys=True, default=str
    )
    return hashlib.sha256(fingerprint.encode()).hexdigest()


class BriefCache:
    def __init__(self, root: Path) -> None:
        self._root = root / "briefs"

    def _path(self, key: str) -> Path:
        return self._root / key[:2] / f"{key}.json"

    def get(self, key: str) -> Brief | None:
        path = self._path(key)
        try:
            return Brief.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValidationError, ValueError) as exc:
            log.warning(
                "ignoring unreadable cache entry", extra={"path": str(path), "error": str(exc)}
            )
            return None

    def put(self, key: str, brief: Brief) -> None:
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(brief.model_dump_json())
            os.replace(tmp, path)
        except OSError as exc:  # a read-only or full disk must not fail the run
            log.warning("could not write cache entry", extra={"path": str(path), "error": str(exc)})


class EmbeddingStore:
    """SQLite-backed vector cache. Safe to share between processes."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, timeout=10.0)
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS vectors ("
                " model TEXT NOT NULL, digest TEXT NOT NULL, data BLOB NOT NULL,"
                " PRIMARY KEY (model, digest))"
            )
            self._db.commit()
        except sqlite3.Error:
            self._db.close()
            raise

    def get_many(self, model: str, digests: Sequence[str]) -> dict[str, list[float]]:
        found: dict[str, list[float]] = {}
        unique = list(dict.fromkeys(digests))
        for start in range(0, len(unique), 500):
            chunk = unique[start : start + 500]
            marks = ",".join("?" * len(chunk))
            rows = self._db.execute(
                f"SELECT digest, data FROM vectors WHERE model = ? AND digest IN ({marks})",  # noqa: S608 - placeholders only
                [model, *chunk],
            )
            for digest, blob in rows:
                vec = array("f")
                vec.frombytes(blob)
                found[digest] = vec.tolist()
        return found

    def put_many(self, model: str, items: Mapping[str, Sequence[float]]) -> None:
        self._db.executemany(
            "INSERT OR REPLACE INTO vectors (model, digest, data) VALUES (?, ?, ?)",
            [(model, digest, array("f", vec).tobytes()) for digest, vec in items.items()],
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()


class CachedEmbedder:
    """Wraps an embedder; only texts not seen before (for this model) are sent."""

    def __init__(self, inner: Embedder, store: EmbeddingStore) -> None:
        self._inner = inner
        self._store = store
        self.hits = 0
        self.misses = 0

    @property
    def model(self) -> str:
        return self._inner.model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        digests = [sha256_text(t) for t in texts]
        cached = self._store.get_many(self.model, digests)
        missing = list(dict.fromkeys(d for d in digests if d not in cached))
        if missing:
            first_text = {d: t for d, t in zip(digests, texts, strict=True)}
            fresh = self._inner.embed([first_text[d] for d in missing])
            new = dict(zip(missing, fresh, strict=True))
            self._store.put_many(self.model, new)
            cached.update(new)
        self.hits += len(digests) - len(missing)
        self.misses += len(missing)
        return [cached[d] for d in digests]
