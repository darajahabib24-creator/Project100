"""tools.py - the agent's hands. All local: filesystem, shell, memory, web-free.

No network access in any tool. If you need the internet, add your own tool.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from typing import Any

from .core import Tool, ToolError
from .browse import BrowseError, browse, format_page
from .search import (SearchError, direct_answer, format_answer, format_results,
                     web_search)
from .verify import verify_claim


# --------------------------------------------------------------------------- #
# Web: search the whole web, then read pages
# --------------------------------------------------------------------------- #

class WebSearch(Tool):
    name = "web_search"
    description = ("Search the web and return result titles + URLs + snippets. "
                   "No API key. Tries DuckDuckGo, Brave, Wikipedia.")
    params = {"query": "search query", "count": "max results (default 8)",
              "engine": "force one engine: ddg-lite | brave | wikipedia"}

    def run(self, query: str, count: int = 8, engine: str = "") -> str:
        try:
            engines = [engine] if engine else None
            payload = web_search(query, count=count, engines=engines)
        except SearchError as exc:
            raise ToolError(str(exc)) from exc
        return format_results(payload, limit=count)


class VerifyAnswer(Tool):
    name = "verify_answer"
    description = ("Check a claimed answer against pages you already read. "
                   "Returns confidence, supporting sources and contradictions.")
    params = {"claim": "the statement to check",
              "sources": "JSON array of {title,url,text} you browsed"}

    def run(self, claim: str, sources: str = "[]") -> str:
        import json
        try:
            srcs = json.loads(sources) if isinstance(sources, str) else sources
        except json.JSONDecodeError as exc:
            raise ToolError(f"sources must be a JSON array: {exc}") from exc
        if not isinstance(srcs, list) or not srcs:
            raise ToolError("sources must be a non-empty JSON array of "
                            "{title,url,text}")
        v = verify_claim(claim, srcs)
        return v.as_prompt()


class AnswerQuestion(Tool):
    name = "answer_question"
    description = ("Answer a factual question with a cited source. Uses "
                   "Wikipedia summaries and the open web. Returns provenance "
                   "(URL + last-edited date) with the answer.")
    params = {"question": "the question to answer",
              "read_page": "also read the source page (true/false)"}

    def run(self, question: str, read_page: bool = False) -> str:
        ans = direct_answer(question)
        if not ans:
            try:
                res = web_search(question, count=5)
            except SearchError as exc:
                raise ToolError(str(exc)) from exc
            out = ["No direct encyclopedic answer. Top web results:"]
            out.append(format_results(res, limit=5))
            return "\n".join(out)
        out = format_answer(ans)
        if read_page:
            try:
                page = browse(ans["url"])
                out += "\n\n--- PAGE LEAD ---\n" + page["text"][:1500]
            except BrowseError:
                pass
        return out


class BrowseURL(Tool):
    name = "browse_url"
    description = "Fetch a page and return its readable text plus links."
    params = {"url": "http(s) URL", "max_chars": "truncate text (default 3000)"}

    def run(self, url: str, max_chars: int = 3000) -> str:
        try:
            page = browse(url)
        except BrowseError as exc:
            raise ToolError(str(exc)) from exc
        return format_page(page, max_chars=max_chars)


class FetchURL(Tool):
    name = "fetch_url"
    description = "Fetch a URL with urllib and return decoded text (no API key)."
    params = {"url": "http(s) URL", "max_bytes": "cap (default 200000)"}

    def run(self, url: str, max_bytes: int = 200_000) -> str:
        import urllib.request
        import urllib.error
        req = urllib.request.Request(
            url, headers={"User-Agent": "local-agent/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read(int(max_bytes))
        except (urllib.error.URLError, OSError) as exc:
            raise ToolError(str(exc)) from exc
        enc = resp.headers.get_content_charset() if hasattr(resp, "headers") else None
        return data.decode(enc or "utf-8", errors="replace")

class ReadFile(Tool):
    name = "read_file"
    description = "Read a text file. Returns numbered lines."
    params = {"path": "absolute or relative path", "offset": "line to start at (1-indexed)",
              "limit": "max lines (default 200)"}

    def run(self, path: str, offset: int = 1, limit: int = 200) -> str:
        if not os.path.isfile(path):
            raise ToolError(f"not a file: {path}")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError as exc:
            raise ToolError(str(exc)) from exc
        start = max(1, int(offset))
        chunk = lines[start - 1:start - 1 + int(limit)]
        if not chunk:
            return f"(no lines from {start}; file has {len(lines)})"
        return "\n".join(f"{start + i}|{ln}" for i, ln in enumerate(chunk))


class WriteFile(Tool):
    name = "write_file"
    description = "Write text to a file, creating parent dirs. Overwrites."
    params = {"path": "path", "content": "full file content"}

    def run(self, path: str, content: str) -> str:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        except OSError as exc:
            raise ToolError(str(exc)) from exc
        return f"wrote {len(content)} bytes to {path}"


class EditFile(Tool):
    name = "edit_file"
    description = "Replace one exact string in a file. Fails if not unique."
    params = {"path": "path", "old": "exact text to find", "new": "replacement text"}

    def run(self, path: str, old: str, new: str) -> str:
        if not os.path.isfile(path):
            raise ToolError(f"not a file: {path}")
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        n = src.count(old)
        if n == 0:
            raise ToolError("old text not found in file")
        if n > 1:
            raise ToolError(f"old text occurs {n} times; make it unique")
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(src.replace(old, new, 1))
        except OSError as exc:
            raise ToolError(str(exc)) from exc
        return f"edited {path}"


class ListDir(Tool):
    name = "list_dir"
    description = "List a directory's entries."
    params = {"path": "directory (default '.')"}

    def run(self, path: str = ".") -> str:
        if not os.path.isdir(path):
            raise ToolError(f"not a directory: {path}")
        try:
            entries = sorted(os.listdir(path))
        except OSError as exc:
            raise ToolError(str(exc)) from exc
        if not entries:
            return "(empty)"
        out = []
        for e in entries:
            full = os.path.join(path, e)
            kind = "d" if os.path.isdir(full) else "f"
            size = "-" if kind == "d" else str(os.path.getsize(full))
            out.append(f"{kind} {size:>10} {e}")
        return "\n".join(out)


class SearchFiles(Tool):
    name = "search_files"
    description = "Search file contents with a regex. Returns file:line matches."
    params = {"pattern": "regex", "path": "root dir (default '.')",
              "glob": "file glob, e.g. '*.py' (default '*')", "limit": "max matches"}

    def run(self, pattern: str, path: str = ".", glob: str = "*", limit: int = 50) -> str:
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            raise ToolError(f"bad regex: {exc}") from exc
        import fnmatch
        hits = []
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if not fnmatch.fnmatch(f, glob):
                    continue
                full = os.path.join(root, f)
                try:
                    if os.path.getsize(full) > 2_000_000:
                        continue
                    with open(full, "r", encoding="utf-8", errors="replace") as fh:
                        for i, line in enumerate(fh, 1):
                            if rx.search(line):
                                hits.append(f"{full}:{i}:{line.rstrip()[:200]}")
                                if len(hits) >= limit:
                                    return "\n".join(hits)
                except OSError:
                    continue
        return "\n".join(hits) if hits else "(no matches)"


# --------------------------------------------------------------------------- #
# Shell
# --------------------------------------------------------------------------- #

class Shell(Tool):
    name = "shell"
    description = "Run a shell command. Returns stdout+stderr. cwd persists per call."
    params = {"command": "shell command string", "cwd": "working dir (default '.')",
              "timeout": "seconds (default 60)"}

    def run(self, command: str, cwd: str = ".", timeout: int = 60) -> str:
        if not os.path.isdir(cwd):
            raise ToolError(f"bad cwd: {cwd}")
        try:
            p = subprocess.run(
                command, shell=True, cwd=cwd, capture_output=True, text=True,
                timeout=int(timeout),
            )
        except subprocess.TimeoutExpired:
            raise ToolError(f"timed out after {timeout}s")
        except OSError as exc:
            raise ToolError(str(exc)) from exc
        out = (p.stdout or "") + (p.stderr or "")
        if p.returncode != 0:
            out += f"\n[exit {p.returncode}]"
        return out.strip() or "(no output)"


# --------------------------------------------------------------------------- #
# Memory
# --------------------------------------------------------------------------- #

class MemoryTool(Tool):
    """Two tools in one wrapper is awkward; expose put/get via separate names."""

    def __init__(self, memory, mode: str):
        self._mem = memory
        self._mode = mode
        if mode == "put":
            self.name = "memory_put"
            self.description = "Store a durable fact."
            self.params = {"key": "name", "value": "JSON-serialisable value (string ok)"}
        else:
            self.name = "memory_get"
            self.description = "Recall a fact, or search by substring."
            self.params = {"key": "exact key, or 'search:<needle>'"}

    def run(self, key: str, value: Any = None) -> str:
        if self._mode == "put":
            self._mem.put(key, value)
            return f"stored {key}"
        if key.startswith("search:"):
            hits = self._mem.search(key[7:])
            if not hits:
                return "(no matches)"
            import json
            return "\n".join(json.dumps(h, ensure_ascii=False) for h in hits)
        got = self._mem.get(key)
        if got is None:
            return f"(no fact {key!r})"
        import json
        return json.dumps(got, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

def default_tools(memory=None) -> list[Tool]:
    tools: list[Tool] = [
        ReadFile(), WriteFile(), EditFile(), ListDir(), SearchFiles(), Shell(),
        WebSearch(), AnswerQuestion(), VerifyAnswer(), BrowseURL(), FetchURL(),
    ]
    if memory is not None:
        tools.append(MemoryTool(memory, "put"))
        tools.append(MemoryTool(memory, "get"))
    return tools
