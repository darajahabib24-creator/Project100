"""httpdebug.py - optional terminal debugging for outbound HTTP.

When enabled (AGENT_HTTP_DEBUG=1, or the CLI's --http-debug), every web
request the agent makes prints one line to stderr:

    [http] --> GET https://example.com/search?q=x
    [http] <-- https://example.com/search?q=x 200 12345B (87ms)

Off by default: normal runs print nothing.
"""

from __future__ import annotations

import os
import sys
import time

_ENABLED = os.environ.get("AGENT_HTTP_DEBUG", "").strip().lower() in (
    "1", "true", "yes", "on")


def enabled() -> bool:
    return _ENABLED


def set_enabled(value: bool) -> None:
    global _ENABLED
    _ENABLED = bool(value)


def _emit(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def request(method: str, url: str) -> float:
    """Log an outgoing request; return a timestamp to pass to response()."""
    if not _ENABLED:
        return 0.0
    _emit(f"[http] --> {method} {url}")
    return time.perf_counter()


def response(url: str, status, nbytes: int, started: float,
             err: str | None = None) -> None:
    """Log the response for a request started with request()."""
    if not _ENABLED:
        return
    ms = (time.perf_counter() - started) * 1000.0 if started else 0.0
    if err is not None:
        _emit(f"[http] <-- {url} ERROR {err} ({ms:.0f}ms)")
    else:
        _emit(f"[http] <-- {url} {status} {nbytes}B ({ms:.0f}ms)")
