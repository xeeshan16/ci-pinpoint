"""CI Pinpoint: find the log lines that broke a CI build and explain why."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ci-pinpoint")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0"

__all__ = ["__version__"]
