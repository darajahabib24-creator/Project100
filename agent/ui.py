"""ui.py - the "thinking" spinner. Pure stdlib, no curses, no dependency.

Shows a braille spinner with elapsed seconds and a live status line while the
model thinks and while tools run. On a non-TTY (pipe, redirect, CI) it degrades
to plain status lines so logs stay readable.
"""

from __future__ import annotations

import sys
import threading
import time
from contextlib import contextmanager

FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class Spinner:
    """A background-thread status line.

    Usage:
        sp = Spinner()
        sp.start()
        sp.set_text("thinking…")
        ...
        sp.stop()

    or as a context manager. `pause()` yields with the animation halted so you
    can print a real line without it being overwritten.
    """

    def __init__(self, stream=None, enabled: bool | None = None,
                 interval: float = 0.08):
        self.stream = stream or sys.stdout
        if enabled is None:
            try:
                enabled = self.stream.isatty()
            except Exception:  # noqa: BLE001
                enabled = False
        self.enabled = bool(enabled)
        self.interval = interval
        self._msg = "working"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._t0 = 0.0
        self._frame = 0
        self._running = False

    # -- state -------------------------------------------------------------- #

    @property
    def elapsed(self) -> float:
        return time.time() - self._t0 if self._t0 else 0.0

    def set_text(self, msg: str) -> None:
        with self._lock:
            self._msg = msg

    def get_text(self) -> str:
        with self._lock:
            return self._msg

    # -- lifecycle ---------------------------------------------------------- #

    def start(self) -> "Spinner":
        if not self.enabled or self._running:
            return self
        self._running = True
        self._t0 = time.time()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> "Spinner":
        if not self._running:
            return self
        self._running = False
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2 * self.interval + 0.1)
            self._thread = None
        self._clear()
        return self

    def __enter__(self) -> "Spinner":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- rendering ---------------------------------------------------------- #

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._draw()
            self._stop.wait(self.interval)

    def _draw(self) -> None:
        with self._lock:
            msg = self._msg
        frame = FRAMES[self._frame % len(FRAMES)]
        self._frame += 1
        try:
            self.stream.write(
                f"\r{frame} {msg} ({self.elapsed:.1f}s)   ")
            self.stream.flush()
        except Exception:  # noqa: BLE001 - never let UI break the agent
            pass

    def _clear(self) -> None:
        try:
            self.stream.write("\r\033[K")
            self.stream.flush()
        except Exception:  # noqa: BLE001
            pass

    @contextmanager
    def pause(self):
        """Halt the animation so a real line can be printed, then resume."""
        if not self._running:
            yield
            return
        self._clear()
        try:
            yield
        finally:
            if self._running:
                self._draw()


def print_line(text: str, spinner: Spinner | None = None) -> None:
    """Print a line that will not be clobbered by an active spinner."""
    if spinner is not None and spinner.enabled:
        with spinner.pause():
            print(text)
    else:
        print(text)
