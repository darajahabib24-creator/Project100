#!/usr/bin/env python3
"""agent.py -- a live agent loop that needs no API key and no cloud model.

It is a real agent (perceive -> plan -> act -> observe -> answer) with real
tools: web search, page fetch, shell, file read/write, directory listing,
Python execution and persistent memory.  The default brain is a deterministic
rule-based planner, so the script runs anywhere with only the standard
library.  A free local model (Ollama on 127.0.0.1:11434) can be used as the
brain instead with ``--model ollama`` -- still no API key, still no cloud.

Usage:
    python3 agent.py                                  # interactive REPL
    python3 agent.py "what is the capital of France?"
    python3 agent.py "search for python web scrapers" --json
    python3 agent.py "list files in ."
    python3 agent.py --model ollama "summarize https://example.com"
    python3 agent.py --tools
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

import search_agent as sa

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #

DEFAULT_MEMORY = Path(os.environ.get(
    "AGENT_MEMORY", str(Path.home() / ".project100" / "agent_memory.json")))

OLLAMA_BASE = os.environ.get("OLLAMA_BASE", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "could",
    "did", "do", "does", "for", "from", "had", "has", "have", "how", "i", "if",
    "in", "into", "is", "it", "its", "me", "my", "of", "on", "or", "our",
    "please", "should", "so", "some", "that", "the", "their", "them", "then",
    "there", "these", "this", "to", "up", "us", "was", "we", "what", "when",
    "where", "which", "who", "why", "will", "with", "would", "you", "your",
}

# Words that say "this is code" but not which code; useless for relevance.
_CODE_STOPWORDS = {
    "python", "python3", "bash", "sh", "shell", "javascript", "js", "java",
    "cpp", "c++", "ruby", "php", "typescript", "ts", "kotlin", "swift",
    "perl", "lua", "scala", "dart", "rust", "golang", "html", "css", "sql",
    "script", "scripts", "code", "program", "example", "snippet", "file",
    "files", "language", "source", "function", "class", "object", "module",
}

# Subset worth dropping from a web query too (keep task words like "file").
_QUERY_FILLER = _CODE_STOPWORDS - {
    "file", "files", "function", "class", "object", "module",
}

VERB_PREFIXES = (
    "i want you to", "i need you to", "i'd like you to", "could you",
    "would you", "can you", "please", "help me",
    "search for", "search", "look up", "lookup", "find me", "get me",
    "give me", "show me", "google", "bing", "browse", "research",
    "create", "write", "make", "generate", "build", "need",
    "me a", "me an",
)

_VERB_RE = re.compile(
    r"^(?:" + "|".join(re.escape(p) for p in VERB_PREFIXES) + r")\b[:\s,]*",
    re.I)


class AgentError(Exception):
    """Base class for agent failures."""


class ToolError(AgentError):
    """A tool was called with bad arguments or does not exist."""


class PolicyDenied(AgentError):
    """A mutating tool call was refused by the safety policy."""


# --------------------------------------------------------------------------- #
# small text helpers
# --------------------------------------------------------------------------- #

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(
    r"<(script|style|noscript|svg|head)\b[^>]*>.*?</\1>", re.S | re.I)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_BLOCK_RE = re.compile(
    r"</?(?:p|div|br|li|ul|ol|tr|td|th|table|h[1-6]|section|article|header|"
    r"footer|pre|blockquote)[^>]*>", re.I)


def html_to_text(raw: str) -> str:
    """Very small HTML -> text converter (no external dependencies)."""
    if not raw:
        return ""
    text = _COMMENT_RE.sub(" ", raw)
    text = _SCRIPT_RE.sub(" ", text)
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    lines = [_WS_RE.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def page_title(raw: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    if not match:
        return ""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html.unescape(match.group(1)))).strip()


_BR_RE = re.compile(r"<br\s*/?>", re.I)
_CODE_BLOCK_RE = re.compile(r"<(pre|code)\b[^>]*>(.*?)</\1>", re.S | re.I)


def _code_inner(inner: str) -> str:
    inner = _BR_RE.sub("\n", inner)
    inner = _TAG_RE.sub("", inner)
    return html.unescape(inner).strip("\n")


def extract_code_blocks(raw: str, limit: int = 6,
                        max_chars: int = 1500) -> list[str]:
    """Pull <pre>/<code> blocks out of HTML, longest first, de-duplicated."""
    candidates: list[str] = []
    for match in _CODE_BLOCK_RE.finditer(raw):
        text = _code_inner(match.group(2))
        if len(text) >= 20:
            candidates.append(text)
    blocks: list[str] = []
    for text in sorted(candidates, key=len, reverse=True):
        if any(text in block for block in blocks):
            continue
        blocks.append(text[:max_chars])
        if len(blocks) >= limit:
            break
    return blocks


_GH_BLOB_RE = re.compile(r'href="(/[^"/]+/[^"/]+/blob/[^"/]+/[^"]+)"')


def extract_raw_links(raw: str, page_url: str, limit: int = 8) -> list[str]:
    """Turn GitHub blob links on a repo page into raw file URLs."""
    links: list[str] = []
    if "github.com" not in page_url:
        return links
    seen: set[str] = set()
    for match in _GH_BLOB_RE.finditer(raw):
        parts = html.unescape(match.group(1)).strip("/").split("/", 4)
        if len(parts) != 5 or parts[2] != "blob":
            continue
        owner, repo, ref, file_path = parts[0], parts[1], parts[3], parts[4]
        url = (f"https://raw.githubusercontent.com/{owner}/{repo}/"
               f"{ref}/{file_path}")
        if url in seen:
            continue
        seen.add(url)
        links.append(url)
        if len(links) >= limit:
            break
    return links


_TOKEN_RE = re.compile(r"[a-z][a-z0-9+.-]{2,}")


def _stem(word: str) -> str:
    """Crude suffix stripper so 'downloads' and 'downloaded' match."""
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[:-len(suffix)]
    return word


def _word_stems(text: str) -> set[str]:
    return {_stem(word) for word in _TOKEN_RE.findall(text.lower())}


def keywords(text: str, limit: int = 12) -> list[str]:
    """Content words of *text*, lowercased, stopwords removed."""
    words = re.findall(r"[a-z][a-z0-9+._-]{2,}", text.lower())
    out: list[str] = []
    seen: set[str] = set()
    for word in words:
        if word in STOPWORDS or word in seen:
            continue
        seen.add(word)
        out.append(word)
        if len(out) >= limit:
            break
    return out


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.replace("\n", " "))
    return [p.strip() for p in parts if len(p.strip()) > 20]


_CITE_RE = re.compile(r"\[\s*\d+\s*\]|\[\s*[a-z]\s*\]")
_SPACE_PUNCT_RE = re.compile(r"\s+([,.;:])")


def _clean_sentence(sentence: str) -> str:
    """Drop citation markers and fix spacing around punctuation."""
    sentence = _CITE_RE.sub(" ", sentence)
    sentence = _WS_RE.sub(" ", sentence).strip()
    return _SPACE_PUNCT_RE.sub(r"\1", sentence)


def extract_answer(text: str, query: str, max_sentences: int = 3,
                   max_chars: int = 700) -> str:
    """Extractive answer: rank well-formed sentences by keyword overlap."""
    wanted = set(keywords(query, limit=10))
    if not wanted:
        return ""
    phrase = " ".join(keywords(query, limit=4))
    scored: list[tuple[float, int, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(split_sentences(text)):
        sentence = _clean_sentence(raw)
        if not 30 <= len(sentence) <= 300:
            continue
        low = sentence.lower()
        hits = sum(1 for word in wanted if word in low)
        if not hits:
            continue
        marker = low[:80]
        if marker in seen:
            continue
        seen.add(marker)
        score = hits * 3.0
        if phrase and phrase in low:
            score += 5.0
        if index < 10:
            score += 2.0
        if len(sentence) <= 200:
            score += 1.0
        scored.append((score, index, sentence))
    scored.sort(key=lambda item: (-item[0], item[1]))
    chosen = scored[:max_sentences]
    chosen.sort(key=lambda item: item[1])
    answer = " ".join(sentence for _, _, sentence in chosen)
    return answer[:max_chars].strip()


def strip_verbs(goal: str) -> str:
    """Remove polite/leading intent verbs so a goal becomes a clean query."""
    original = goal.strip()
    text = original.strip(" .!?")
    changed = True
    while changed:
        changed = False
        updated = _VERB_RE.sub("", text, count=1)
        if updated != text:
            text = updated.strip(" ,:")
            changed = True
    return text.strip(" .!?") or original


def is_question(goal: str) -> bool:
    if goal.rstrip().endswith("?"):
        return True
    return bool(re.match(
        r"^(what|who|whom|whose|why|how|when|where|which|is|are|was|were|do|"
        r"does|did|can|could|should|will|would)\b", goal.strip(), re.I))


def reformulate_query(query: str) -> str:
    """Fallback query: content words only, capped, for a second search try."""
    words = keywords(query, limit=8)
    if not words:
        return query
    shortened = " ".join(words)
    return shortened if shortened != query.lower() else query


# --------------------------------------------------------------------------- #
# data model
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    properties: dict[str, tuple[str, str]]  # name -> (json type, description)
    required: tuple[str, ...] = ()
    mutating: bool = False

    def json_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                key: {"type": kind, "description": desc}
                for key, (kind, desc) in self.properties.items()
            },
            "required": list(self.required),
        }


@dataclass
class Step:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    why: str = ""

    def as_dict(self) -> dict:
        return {"tool": self.tool, "args": self.args, "why": self.why}


@dataclass
class Observation:
    step: Step
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def summary(self) -> str:
        if not self.ok:
            return f"{self.step.tool} failed: {self.error}"
        tool = self.step.tool
        data = self.data
        if tool in ("web_search", "github_search", "gitlab_search",
                    "scripts_search"):
            return format_search_results(data)
        if tool == "fetch_page":
            return f"fetched {data.get('url', '')} ({data.get('chars', 0)} chars)"
        if tool == "shell":
            return f"exit {data.get('returncode')}"
        if tool == "read_file":
            return f"read {data.get('path', '')} ({data.get('total_lines', 0)} lines)"
        if tool == "write_file":
            return f"wrote {data.get('bytes', 0)} bytes to {data.get('path', '')}"
        if tool == "list_files":
            return f"{len(data.get('entries', []))} entries in {data.get('path', '')}"
        if tool == "python":
            return "ran python snippet"
        if tool == "remember":
            return "saved to memory"
        if tool == "recall":
            return f"{len(data.get('matches', []))} memories"
        return tool


@dataclass
class Plan:
    mode: str                      # steps | pipeline | clarify | answer
    steps: list[Step] = field(default_factory=list)
    pipeline: str = ""
    text: str = ""
    question: str = ""
    thoughts: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# memory
# --------------------------------------------------------------------------- #

def load_memory(path: Path) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def save_memory(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(entries[-500:], handle, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #

def _coerce(key: str, value: Any, kind: str) -> Any:
    if kind == "string":
        return str(value)
    if kind == "integer":
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ToolError(f"argument {key!r} must be an integer") from exc
    if kind == "number":
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ToolError(f"argument {key!r} must be a number") from exc
    if kind == "boolean":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "y", "on")
        return bool(value)
    if kind == "array":
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return [value]
    return value


class ToolRegistry:
    """Holds tool specs and implementations, validates and dispatches calls."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._funcs: dict[str, Callable[..., dict]] = {}

    def register(self, spec: ToolSpec, func: Callable[..., dict]) -> None:
        self._specs[spec.name] = spec
        self._funcs[spec.name] = func

    def spec(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise ToolError(f"unknown tool {name!r}") from exc

    def names(self) -> list[str]:
        return sorted(self._specs)

    def call(self, name: str, args: dict[str, Any] | None) -> dict:
        spec = self.spec(name)
        args = args or {}
        if not isinstance(args, dict):
            raise ToolError(f"{name}: arguments must be an object")
        unknown = sorted(set(args) - set(spec.properties))
        if unknown:
            raise ToolError(f"{name}: unknown argument(s): {', '.join(unknown)}")
        missing = [key for key in spec.required if key not in args]
        if missing:
            raise ToolError(f"{name}: missing argument(s): {', '.join(missing)}")
        coerced = {key: _coerce(key, value, spec.properties[key][0])
                   for key, value in args.items()}
        try:
            result = self._funcs[name](**coerced)
        except ToolError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - tools must not kill the agent
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(result, dict):
            return {"ok": bool(result), "value": result}
        result.setdefault("ok", True)
        return result

    def definitions(self) -> list[dict]:
        return [{"type": "function",
                 "function": {"name": spec.name,
                              "description": spec.description,
                              "parameters": spec.json_schema()}}
                for _, spec in sorted(self._specs.items())]


# --- tool implementations -------------------------------------------------- #

def tool_web_search(query: str, engines: Sequence[str] | None = None,
                    limit: int = 8) -> dict:
    engine_list = list(engines) if engines else None
    resp = sa.search_web(query, engines=engine_list, limit=limit)
    return {
        "ok": resp.ok,
        "query": query,
        "count": len(resp.results),
        "results": [{"title": r.title, "url": r.url, "snippet": r.snippet,
                     "engine": r.engine or r.source, "source": r.source}
                    for r in resp.results],
        "errors": resp.errors,
    }


def tool_github_search(query: str, limit: int = 8, use_api: bool = False) -> dict:
    try:
        found = sa.search_github(query, limit=limit, use_api=use_api)
    except sa.SearchError as exc:
        return {"ok": False, "error": str(exc), "count": 0, "results": []}
    return {"ok": bool(found), "query": query, "count": len(found),
            "results": [{"title": r.title, "url": r.url, "snippet": r.snippet,
                         "engine": "github", "source": "github",
                         "meta": r.meta} for r in found],
            "errors": {}}


def tool_gitlab_search(query: str, limit: int = 8) -> dict:
    try:
        found = sa.search_gitlab(query, limit=limit)
    except sa.SearchError as exc:
        return {"ok": False, "error": str(exc), "count": 0, "results": []}
    return {"ok": bool(found), "query": query, "count": len(found),
            "results": [{"title": r.title, "url": r.url, "snippet": r.snippet,
                         "engine": "gitlab", "source": "gitlab",
                         "meta": r.meta} for r in found],
            "errors": {}}


def tool_scripts_search(query: str, limit: int = 8) -> dict:
    resp = sa.search_scripts(query, limit=limit)
    return {
        "ok": resp.ok,
        "query": query,
        "count": len(resp.results),
        "results": [{"title": r.title, "url": r.url, "snippet": r.snippet,
                     "engine": r.engine or r.source, "source": r.source}
                    for r in resp.results],
        "errors": resp.errors,
    }


def tool_fetch_page(url: str, max_chars: int = 8000,
                    code_max_chars: int = 4000, raw: bool = False) -> dict:
    url = url.strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url
    url = url.rstrip(".,);\"'")
    try:
        body = sa.fetch(url, timeout=20.0)
    except sa.SearchError as exc:
        return {"ok": False, "url": url, "error": str(exc)}
    if raw:
        return {"ok": True, "url": url, "title": "", "text": body[:max_chars],
                "chars": len(body), "code": [], "raw_links": [],
                "truncated": len(body) > max_chars}
    text = html_to_text(body)
    return {"ok": True, "url": url, "title": page_title(body),
            "text": text[:max_chars], "chars": len(body),
            "code": extract_code_blocks(body, max_chars=code_max_chars),
            "raw_links": extract_raw_links(body, url),
            "truncated": len(text) > max_chars}


def tool_shell(command: str, timeout: int = 60) -> dict:
    try:
        proc = subprocess.run(command, shell=True, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "command": command,
                "error": f"timed out after {timeout}s"}
    except OSError as exc:
        return {"ok": False, "command": command, "error": str(exc)}
    return {
        "ok": proc.returncode == 0,
        "command": command,
        "stdout": proc.stdout[-8000:],
        "stderr": proc.stderr[-4000:],
        "returncode": proc.returncode,
    }


def tool_read_file(path: str, offset: int = 1, limit: int = 2000) -> dict:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError as exc:
        return {"ok": False, "path": path, "error": str(exc)}
    start = max(0, offset - 1)
    chunk = lines[start:start + max(0, limit)]
    return {"ok": True, "path": path, "content": "".join(chunk),
            "total_lines": len(lines), "offset": offset,
            "truncated": start + len(chunk) < len(lines)}


def tool_write_file(path: str, content: str) -> dict:
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(content)
    except OSError as exc:
        return {"ok": False, "path": path, "error": str(exc)}
    return {"ok": True, "path": path, "bytes": len(content.encode("utf-8"))}


def tool_list_files(path: str = ".", pattern: str | None = None) -> dict:
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name)
    except OSError as exc:
        return {"ok": False, "path": path, "error": str(exc)}
    regex = re.compile(pattern) if pattern else None
    out = []
    for entry in entries:
        if regex and not regex.search(entry.name):
            continue
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
            size = 0 if is_dir else entry.stat(follow_symlinks=False).st_size
        except OSError:
            is_dir, size = False, 0
        out.append({"name": entry.name, "is_dir": is_dir, "size": size})
    return {"ok": True, "path": path, "count": len(out), "entries": out}


def tool_python(code: str, timeout: int = 30) -> dict:
    try:
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timed out after {timeout}s"}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": proc.returncode == 0, "stdout": proc.stdout[-8000:],
            "stderr": proc.stderr[-4000:], "returncode": proc.returncode}


def tool_remember(text: str, memory_path: Path | None = None) -> dict:
    path = memory_path or DEFAULT_MEMORY
    entries = load_memory(path)
    entries.append({"text": text.strip(), "ts": int(time.time())})
    save_memory(path, entries)
    return {"ok": True, "count": len(entries), "path": str(path)}


def tool_recall(query: str = "", memory_path: Path | None = None) -> dict:
    path = memory_path or DEFAULT_MEMORY
    entries = load_memory(path)
    needle = query.strip().lower()
    matches = [e.get("text", "") for e in entries
               if not needle or needle in e.get("text", "").lower()]
    return {"ok": True, "count": len(matches), "matches": matches[-20:],
            "path": str(path)}


def build_registry(memory_path: Path | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    memory = memory_path or DEFAULT_MEMORY

    registry.register(ToolSpec(
        "web_search",
        "Search the web with DuckDuckGo/Bing/Google and return result list.",
        {"query": ("string", "search query"),
         "engines": ("array", "engines to use: bing, duckduckgo, google"),
         "limit": ("integer", "max results (default 8)")},
        required=("query",)), tool_web_search)

    registry.register(ToolSpec(
        "github_search", "Search GitHub repositories (public HTML results).",
        {"query": ("string", "repository search query"),
         "limit": ("integer", "max results"),
         "use_api": ("boolean", "use the rate-limited REST API instead")},
        required=("query",)), tool_github_search)

    registry.register(ToolSpec(
        "gitlab_search", "Search GitLab projects through the public API.",
        {"query": ("string", "project search query"),
         "limit": ("integer", "max results")},
        required=("query",)), tool_gitlab_search)

    registry.register(ToolSpec(
        "scripts_search", "Search GitHub and GitLab for scripts/repositories.",
        {"query": ("string", "search query"),
         "limit": ("integer", "max results")},
        required=("query",)), tool_scripts_search)

    registry.register(ToolSpec(
        "fetch_page", "Fetch a URL and return its text content and title.",
        {"url": ("string", "http(s) URL to fetch"),
         "max_chars": ("integer", "truncate text to this many chars"),
         "code_max_chars": ("integer", "truncate each code block to this"),
         "raw": ("boolean", "return the body as plain text, no HTML parsing")},
        required=("url",)), tool_fetch_page)

    registry.register(ToolSpec(
        "shell", "Run a shell command and return stdout/stderr/exit code.",
        {"command": ("string", "command line to run"),
         "timeout": ("integer", "seconds before killing the command")},
        required=("command",), mutating=True), tool_shell)

    registry.register(ToolSpec(
        "read_file", "Read a text file with 1-based line offset and limit.",
        {"path": ("string", "file path"),
         "offset": ("integer", "first line to read (1-based)"),
         "limit": ("integer", "max lines to return")},
        required=("path",)), tool_read_file)

    registry.register(ToolSpec(
        "write_file", "Write text content to a file, creating parent dirs.",
        {"path": ("string", "file path"),
         "content": ("string", "full file content")},
        required=("path", "content"), mutating=True), tool_write_file)

    registry.register(ToolSpec(
        "list_files", "List directory entries, optionally filtered by regex.",
        {"path": ("string", "directory path"),
         "pattern": ("string", "regex filter on entry names")},
        required=(), ), tool_list_files)

    registry.register(ToolSpec(
        "python", "Run a Python snippet in a subprocess and return its output.",
        {"code": ("string", "python source to execute"),
         "timeout": ("integer", "seconds before killing the snippet")},
        required=("code",)), tool_python)

    registry.register(ToolSpec(
        "remember", "Store a fact in the persistent memory file.",
        {"text": ("string", "fact to remember")},
        required=("text",), mutating=True),
        lambda text: tool_remember(text, memory))

    registry.register(ToolSpec(
        "recall", "Search the persistent memory file for matching facts.",
        {"query": ("string", "substring to match (empty matches all)")},
        required=()), lambda query="": tool_recall(query, memory))

    return registry


# --------------------------------------------------------------------------- #
# output formatting
# --------------------------------------------------------------------------- #

def format_search_results(data: dict) -> str:
    results = data.get("results", [])
    if not results:
        return f"no results for {data.get('query', '')!r}"
    lines = [f"{len(results)} result(s) for {data.get('query', '')!r}:"]
    for index, item in enumerate(results, 1):
        lines.append(f"{index}. {item.get('title', '')}")
        lines.append(f"   {item.get('url', '')}")
        snippet = (item.get("snippet") or "").strip()
        if snippet:
            lines.append(f"   {snippet}")
    errors = data.get("errors") or {}
    for name, message in errors.items():
        lines.append(f"warning: {name}: {message}")
    return "\n".join(lines)


def format_entries(data: dict) -> str:
    entries = data.get("entries", [])
    if not entries:
        return f"no entries in {data.get('path', '')!r}"
    lines = [f"{len(entries)} entries in {data.get('path', '')!r}:"]
    for entry in entries:
        suffix = "/" if entry.get("is_dir") else f" ({entry.get('size', 0)} bytes)"
        lines.append(f"  {entry.get('name', '')}{suffix}")
    return "\n".join(lines)


def compose_answer(goal: str, observations: Sequence[Observation]) -> str:
    """Turn a finished step list into the agent's final answer."""
    parts: list[str] = []
    for obs in observations:
        if not obs.ok:
            parts.append(f"{obs.step.tool} failed: {obs.error}")
            continue
        tool, data = obs.step.tool, obs.data
        if tool in ("web_search", "github_search", "gitlab_search",
                    "scripts_search"):
            parts.append(format_search_results(data))
        elif tool == "fetch_page":
            parts.append(f"# {data.get('title') or data.get('url')}\n"
                         f"{data.get('url')}\n\n{data.get('text', '')[:2000]}")
        elif tool == "shell":
            body = (data.get("stdout") or "").strip() or \
                   (data.get("stderr") or "").strip()
            parts.append(f"$ {data.get('command')}\n{body[:2000]}")
        elif tool == "read_file":
            parts.append(data.get("content", "")[:2000])
        elif tool == "write_file":
            parts.append(f"Wrote {data.get('bytes', 0)} bytes to "
                         f"{data.get('path', '')}")
        elif tool == "list_files":
            parts.append(format_entries(data))
        elif tool == "python":
            body = (data.get("stdout") or "").strip() or \
                   (data.get("stderr") or "").strip()
            parts.append(body[:2000])
        elif tool == "remember":
            parts.append("Noted: " + obs.step.args.get("text", ""))
        elif tool == "recall":
            matches = data.get("matches", [])
            parts.append("\n".join(matches) if matches else "No matching memories.")
        else:
            parts.append(obs.summary())
    text = "\n\n".join(part for part in parts if part)
    return text or "Nothing to report."


# --------------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------------- #

class Policy:
    """Decides whether mutating tool calls are allowed."""

    def __init__(self, assume_yes: bool = False,
                 interactive: bool | None = None) -> None:
        self.assume_yes = assume_yes
        self.interactive = sys.stdin.isatty() if interactive is None else interactive

    def approve(self, step: Step, spec: ToolSpec) -> bool:
        if not spec.mutating:
            return True
        if self.assume_yes:
            return True
        if not self.interactive:
            return False
        prompt = f"allow {step.tool} {json.dumps(step.args)[:160]}? [y/N] "
        try:
            answer = input(prompt)
        except EOFError:
            return False
        return answer.strip().lower() in ("y", "yes")


# --------------------------------------------------------------------------- #
# planners
# --------------------------------------------------------------------------- #

_INTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("remember", re.compile(
        r"\b(remember|note that|keep in mind|don't forget|dont forget)\b")),
    ("recall", re.compile(
        r"\b(recall|what did i (say|tell|ask)|do you remember)\b")),
    ("code", re.compile(
        r"\b(?:create|write|make|generate|build|find)\b[^.?!]*"
        r"\b(?:script|code|program|class|object|function|module|snippet)\b",
        re.I)),
    ("github", re.compile(r"\bgithub\b")),
    ("gitlab", re.compile(r"\bgitlab\b")),
    ("read", re.compile(r"\b(read|cat|view)\b")),
    ("fetch", re.compile(
        r"\b(fetch|open|download|scrape|crawl|summarize|summarise)\b")),
    ("python", re.compile(
        r"\b(calculate|compute|parse|regex|snippet|count|sum|convert|"
        r"evaluate)\b|\bpython\s+(?:code|snippet|script)\b")),
    ("shell", re.compile(
        r"\b(run|execute|exec|shell|command|terminal|bash|install|build|"
        r"compile|deploy)\b")),
    ("list", re.compile(
        r"\b(list|ls)\b|\b(?:show|what(?:'s| is)|files?)\s+(?:in|inside|of)\b"
        r"|\bdir(?:ectory)?\b|\bcontents? of\b")),
    ("write", re.compile(
        r"\b(write|create|save|generate|make|export|dump)\b")),
    ("search", re.compile(
        r"\b(search|find|look up|lookup|google|bing|browse|research|"
        r"repositories|repos|scripts|tools)\b")),
)

_URL_RE = re.compile(r"https?://[^\s\"'<>]+")


def _code_query_variant(query: str) -> str:
    """Drop language/'script' filler so the web search targets the task."""
    kept = [word.strip(".,?!")
            for word in query.split()
            if word.strip(".,?!").lower() not in _QUERY_FILLER
            and word.strip(".,?!").lower() not in STOPWORDS]
    return " ".join(kept)


def _extract_url(goal: str) -> str | None:
    match = _URL_RE.search(goal)
    return match.group(0).rstrip(".,);\"'") if match else None


def _pipeline_query(text: str, path: str) -> str:
    """Search query for a write/save goal, with the action clause removed."""
    head = text.replace(path, " ")
    parts = re.split(r"\b(?:save|export|dump|write|create|generate|make)\b",
                     head, maxsplit=1, flags=re.I)
    head = parts[0].strip() or (parts[1] if len(parts) > 1 else "")
    query = strip_verbs(head)
    query = re.sub(
        r"^(?:a|the)?\s*(?:script|tool|program)\s+(?:that\s+)?"
        r"(?:searches?|finds?|queries?|lists?)\s+(?:for|of|about)?\s*",
        "", query, flags=re.I)
    query = re.sub(r"\s+(?:and|then|to|the results?|results?|output|list)$",
                   "", query, flags=re.I)
    query = query.strip(" ,-")
    if len(query) < 3:
        query = strip_verbs(text.replace(path, " ")).strip(" ,-")
    return query or strip_verbs(text)


def _extract_dir(goal: str) -> str:
    """Directory argument for the list tool (paths need no extension)."""
    match = re.search(r"\b(?:in|inside|of)\s+([^\s,;]+)", goal)
    if match:
        candidate = match.group(1).strip("\"'")
        if candidate.startswith(("/", "~", ".")) or "/" in candidate or "." in candidate:
            return os.path.expanduser(candidate)
    match = re.search(r"^\s*(?:list|ls)\s+([^\s,;]+)\s*$", goal, re.I)
    if match:
        return os.path.expanduser(match.group(1).strip("\"'"))
    return "."


def _extract_path(goal: str) -> str | None:
    match = re.search(
        r"\b(?:to|in|into|at|called|named)\s+"
        r"([~\w./-]+\.[A-Za-z0-9]{1,8})\b", goal)
    if match:
        return os.path.expanduser(match.group(1))
    match = re.search(r"\b(?:called|named|file)\s+([~\w./-]+)\b", goal)
    if match:
        return os.path.expanduser(match.group(1))
    match = re.search(r"\b([~\w./-]+\.[A-Za-z0-9]{1,8})\b", goal)
    return os.path.expanduser(match.group(1)) if match else None


class RuleBasedPlanner:
    """Deterministic planner: intent matching plus a few multi-step pipelines."""

    name = "rules"

    def plan(self, goal: str, observations: Sequence[Observation]) -> Plan:
        if observations:
            return Plan("answer", text=compose_answer(goal, observations))
        return self._plan_fresh(goal)

    # -- intent classification --------------------------------------------- #

    def _plan_fresh(self, goal: str) -> Plan:
        text = goal.strip()
        low = text.lower()

        url = _extract_url(text)
        if url and re.fullmatch(r"\s*https?://\S+\s*", text):
            return Plan("steps", steps=[Step("fetch_page", {"url": url},
                                                "goal is a bare URL")])

        for intent, pattern in _INTENT_PATTERNS:
            if not pattern.search(low):
                continue
            plan = self._build(intent, text, url)
            if plan is not None:
                return plan

        if is_question(text):
            return Plan("pipeline", pipeline="research",
                        steps=[Step("web_search", {"query": strip_verbs(text)},
                                    "question: search first")],
                        thoughts=["question detected -> research pipeline"])
        return Plan("pipeline", pipeline="search_content",
                    steps=[Step("web_search", {"query": strip_verbs(text)},
                                "default intent: search")])

    def _build(self, intent: str, text: str, url: str | None) -> Plan | None:
        query = strip_verbs(text)

        if intent == "remember":
            text = re.sub(
                r"^(please\s+)?(remember|note that|keep in mind|"
                r"don't forget|dont forget)\s*(that\s*)?",
                "", text, flags=re.I).strip()
            return Plan("steps", steps=[
                Step("remember", {"text": text}, "store fact")])

        if intent == "recall":
            query = re.sub(
                r"^(please\s+)?(recall|what did i (say|tell|ask)|"
                r"do you remember)\s*(about|regarding|on)?\s*",
                "", text, flags=re.I).strip()
            return Plan("steps", steps=[
                Step("recall", {"query": query}, "search memory")])

        if intent == "code":
            if _extract_path(text):
                return None          # writing to a file -> let "write" handle it
            return Plan("pipeline", pipeline="find_code",
                        steps=[Step("web_search",
                                    {"query": strip_verbs(text)},
                                    "find code on the web")])

        if intent == "github":
            query = re.sub(r"\bgithub\b", "", query, flags=re.I).strip(" ,-") \
                or query
            return Plan("pipeline", pipeline="search_content",
                        steps=[Step("github_search", {"query": query},
                                    "GitHub search")])

        if intent == "gitlab":
            query = re.sub(r"\bgitlab\b", "", query, flags=re.I).strip(" ,-") \
                or query
            return Plan("pipeline", pipeline="search_content",
                        steps=[Step("gitlab_search", {"query": query},
                                    "GitLab search")])

        if intent == "read":
            path = _extract_path(text)
            if path:
                return Plan("steps", steps=[
                    Step("read_file", {"path": path}, "read file")])
            return None

        if intent == "fetch":
            if url:
                return Plan("pipeline", pipeline="fetch_extract",
                            steps=[Step("fetch_page", {"url": url},
                                        "fetch the requested URL")])
            return Plan("pipeline", pipeline="search_content",
                        steps=[Step("web_search", {"query": query},
                                    "fetch intent, no URL")])

        if intent == "list":
            return Plan("steps", steps=[
                Step("list_files", {"path": _extract_dir(text)},
                     "list directory")])

        if intent == "shell":
            command = re.sub(r"^(please\s+)?(run|execute|exec|shell|command|"
                             r"terminal|bash)\s*", "", text, flags=re.I).strip()
            return Plan("steps", steps=[
                Step("shell", {"command": command or text}, "run shell command")])

        if intent == "python":
            code = re.sub(r"^(please\s+)?(calculate|compute|parse|run)\s*",
                          "", text, flags=re.I).strip()
            return Plan("steps", steps=[
                Step("python", {"code": code}, "run python snippet")])

        if intent == "write":
            path = _extract_path(text)
            if not path:
                if re.search(r"\b(script|code|program|class|object|function|"
                             r"module|snippet|app|tool)\b", text, re.I):
                    return Plan("pipeline", pipeline="find_code",
                                steps=[Step("web_search",
                                            {"query": strip_verbs(text)},
                                            "find code on the web")])
                return Plan("clarify",
                            question="Which file path should I write to?")
            query = _pipeline_query(text, path)
            if re.search(r"\b(script|sh|bash|py|python)\b", text, re.I) and \
                    re.search(r"\b(search|find|quer|result)", text, re.I):
                return Plan("pipeline", pipeline="make_search_script",
                            steps=[Step("web_search", {"query": query},
                                        "search before scripting"),
                                   Step("write_file", {"path": path},
                                        "write generated script")])
            if re.search(r"\b(results|output|list|repos|repositories)\b",
                         text, re.I):
                return Plan("pipeline", pipeline="save_results",
                            steps=[Step("web_search", {"query": query},
                                        "search before saving"),
                                   Step("write_file", {"path": path},
                                        "write results")])
            return Plan("clarify",
                        question=f"What content should I write into {path}?")

        if intent == "search":
            if re.search(r"\b(script|code|program|class|object|function|"
                         r"module|snippet)\b", text, re.I) and \
                    re.search(r"\b(create|write|make|generate|find)\b",
                              text, re.I):
                return Plan("pipeline", pipeline="find_code",
                            steps=[Step("web_search", {"query": query},
                                        "find code on the web")])
            if re.search(r"\b(script|scripts|repo|repos|repository|"
                         r"repositories|tool|tools)\b", text, re.I):
                return Plan("pipeline", pipeline="search_content",
                            steps=[Step("scripts_search", {"query": query},
                                        "code search")])
            return Plan("pipeline", pipeline="search_content",
                        steps=[Step("web_search", {"query": query},
                                    "web search")])

        return None


# --------------------------------------------------------------------------- #
# optional local-model brain (Ollama, no API key)
# --------------------------------------------------------------------------- #

class OllamaBrain:
    """Talks to a local Ollama server.  Nothing is sent to the cloud."""

    def __init__(self, base_url: str = OLLAMA_BASE, model: str = OLLAMA_MODEL,
                 timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def available(self) -> bool:
        try:
            with urllib.request.urlopen(self.base_url + "/api/tags",
                                        timeout=3.0) as resp:
                return 200 <= getattr(resp, "status", 200) < 300
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        payload = json.dumps({"model": self.model, "messages": messages,
                              "tools": tools, "stream": False}).encode()
        req = urllib.request.Request(
            self.base_url + "/api/chat", data=payload,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))


class ModelPlanner:
    """Brain-driven planner.  The model chooses tools; the agent loop runs them."""

    name = "ollama"

    SYSTEM = (
        "You are a resourceful agent with tools. Use them to accomplish the "
        "user's goal. Prefer search and fetch for questions. When the goal is "
        "met, reply with the final answer as plain text. Never invent tool "
        "results.")

    def __init__(self, brain: OllamaBrain, registry: ToolRegistry) -> None:
        self.brain = brain
        self.registry = registry

    def plan(self, goal: str, observations: Sequence[Observation]) -> Plan:
        messages: list[dict] = [{"role": "system", "content": self.SYSTEM},
                                {"role": "user", "content": goal}]
        for obs in observations:
            messages.append({"role": "user", "content": (
                f"tool: {obs.step.tool} args: {json.dumps(obs.step.args)}\n"
                f"ok: {obs.ok}\n{obs.summary()[:2000]}\n"
                f"Continue or give the final answer.")})
        try:
            reply = self.brain.chat(messages, self.registry.definitions())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return Plan("answer", text=f"local model error: {exc}")
        message = reply.get("message", {}) if isinstance(reply, dict) else {}
        steps = self._parse_steps(message)
        if steps:
            return Plan("steps", steps=steps)
        return Plan("answer", text=(message.get("content") or "").strip()
                    or "The model returned no answer.")

    def _parse_steps(self, message: dict) -> list[Step]:
        steps: list[Step] = []
        for call in message.get("tool_calls") or []:
            function = call.get("function", {}) if isinstance(call, dict) else {}
            name = function.get("name", "")
            args = function.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            if not isinstance(args, dict):
                args = {}
            try:
                self.registry.spec(name)
            except ToolError:
                continue
            steps.append(Step(name, args))
        return steps


# --------------------------------------------------------------------------- #
# agent
# --------------------------------------------------------------------------- #

class Agent:
    """The agent loop: plan -> execute -> observe -> repeat."""

    def __init__(self, registry: ToolRegistry, planner: Any,
                 policy: Policy | None = None, max_steps: int = 12,
                 trace: bool = True, memory_path: Path | None = None,
                 script_path: Path | None = None,
                 force_code: bool = False) -> None:
        self.registry = registry
        self.planner = planner
        self.policy = policy or Policy()
        self.max_steps = max(1, max_steps)
        self.trace_enabled = trace
        self.memory_path = memory_path or DEFAULT_MEMORY
        self.script_path = script_path
        self.force_code = force_code

    # -- tracing ------------------------------------------------------------ #

    def trace(self, message: str) -> None:
        if self.trace_enabled:
            print(f"[agent] {message}", file=sys.stderr)

    # -- execution ---------------------------------------------------------- #

    def execute(self, step: Step) -> Observation:
        spec = self.registry.spec(step.tool)
        if not self.policy.approve(step, spec):
            self.trace(f"denied {step.tool} {json.dumps(step.args)[:120]}")
            return Observation(step, False, error="denied by policy")
        self.trace(f"{step.tool} {json.dumps(step.args)[:160]}")
        try:
            data = self.registry.call(step.tool, step.args)
        except ToolError as exc:
            return Observation(step, False, error=str(exc))
        ok = bool(data.get("ok"))
        error = "" if ok else str(data.get("error", "tool failed"))
        obs = Observation(step, ok, data, error)
        self.trace(f"-> {'ok' if ok else 'error: ' + error}"
                   f" ({obs.summary()[:80]})")
        return obs

    # -- loop --------------------------------------------------------------- #

    def run(self, goal: str) -> str:
        goal = goal.strip()
        if not goal:
            return "Tell me what you want done."
        self.trace(f"goal: {goal}")
        if self.force_code:
            plan = Plan("pipeline", pipeline="find_code",
                        steps=[Step("web_search",
                                    {"query": strip_verbs(goal)})])
            return self._run_pipeline(plan, goal)
        observations: list[Observation] = []
        for _ in range(self.max_steps):
            plan = self.planner.plan(goal, observations)
            for thought in plan.thoughts:
                self.trace(f"think: {thought}")
            if plan.mode == "answer":
                return plan.text
            if plan.mode == "clarify":
                return plan.question
            if plan.mode == "pipeline":
                return self._run_pipeline(plan, goal)
            if not plan.steps:
                return "I could not decide how to proceed."
            step = plan.steps[0]
            observations.append(self.execute(step))
            if len(plan.steps) > 1:
                for extra in plan.steps[1:]:
                    observations.append(self.execute(extra))
        final = self.planner.plan(goal, observations)
        return final.text if final.mode == "answer" else \
            compose_answer(goal, observations)

    def _script_from_results(self, obs: Observation,
                             goal: str) -> str | None:
        """Render a search observation as a script and write it to --script."""
        results = [sa.SearchResult(
            item.get("title", ""), item.get("url", ""),
            item.get("snippet", ""), item.get("engine", ""),
            item.get("source", "web"))
            for item in obs.data.get("results", [])]
        if not results:
            return None
        query = obs.data.get("query") or goal
        content = sa.render_script(query, results)
        write = self.execute(Step("write_file",
                                  {"path": str(self.script_path),
                                   "content": content}))
        if write.ok:
            return f"Created script: {self.script_path}"
        return f"Could not write {self.script_path}: {write.error}"

    # -- pipelines ---------------------------------------------------------- #

    def _run_pipeline(self, plan: Plan, goal: str) -> str:
        handler = getattr(self, f"_pipe_{plan.pipeline}", None)
        if handler is None:
            return f"unknown pipeline {plan.pipeline!r}"
        return handler(plan, goal)

    def _pipe_search_content(self, plan: Plan, goal: str) -> str:
        """Search, then fetch the top results and show their content/code."""
        search_step = plan.steps[0]
        obs = self.execute(search_step)
        results = obs.data.get("results", [])
        query = obs.data.get("query") or goal
        if not results:
            retry = reformulate_query(query)
            if retry != query:
                self.trace(f"retrying search with {retry!r}")
                args = dict(search_step.args)
                args["query"] = retry
                obs = self.execute(Step(search_step.tool, args))
                results = obs.data.get("results", [])
        if not results:
            return f"I could not find anything for {query!r}."
        if self.script_path:
            scripted = self._script_from_results(obs, goal)
            if scripted is not None:
                return scripted
        parts: list[str] = []
        for item in results[:3]:
            url = item.get("url", "")
            page = self.execute(Step("fetch_page",
                                     {"url": url, "max_chars": 12000}))
            if not page.ok:
                parts.append(f"# {item.get('title', '')}\n{url}\n"
                             f"(could not fetch: {page.error})")
                continue
            data = page.data
            code = data.get("code") or []
            body = "\n\n".join(code[:4])[:3000] if code \
                else (data.get("text") or "")[:1500]
            parts.append(f"# {data.get('title') or item.get('title', '')}\n"
                         f"{url}\n\n{body}")
        return "\n\n---\n\n".join(parts)

    def _pipe_find_code(self, plan: Plan, goal: str) -> str:
        """Search the web, fetch the best matching script, print only the code."""
        query = plan.steps[0].args.get("query") or goal
        obs = self.execute(plan.steps[0])
        query = obs.data.get("query") or query
        results = list(obs.data.get("results", []))
        variant = _code_query_variant(query)
        if variant and variant != query:
            self.trace(f"also searching {variant!r}")
            args = dict(plan.steps[0].args)
            args["query"] = variant
            alt = self.execute(Step(plan.steps[0].tool, args))
            seen = {item.get("url") for item in results}
            results += [item for item in alt.data.get("results", [])
                        if item.get("url") not in seen]
        best_block, best_score = self._best_code(results, query)
        if (best_block is None or best_score < 20.0) and \
                plan.steps[0].tool != "scripts_search":
            alt = self.execute(Step("scripts_search", {"query": query}))
            alt_block, alt_score = self._best_code(
                alt.data.get("results", []), query)
            if alt_block is not None and (best_block is None
                                          or alt_score > best_score):
                best_block, best_score = alt_block, alt_score
        if best_block is None:
            return ("I could not find a script for that. Try describing what it "
                    "should do, the language, and any library.")
        return best_block

    def _best_code(self, results: Sequence[dict],
                   query: str) -> tuple[str | None, float]:
        wanted = [word for word in keywords(query, limit=12)
                  if word not in _CODE_STOPWORDS]
        if not wanted:
            wanted = keywords(query, limit=12)
        wanted_stems = {_stem(word) for word in wanted}
        best_score: float | None = None
        best_block: str | None = None
        for item in results[:8]:
            page = self.execute(Step("fetch_page", {
                "url": item.get("url", ""),
                "max_chars": 20000, "code_max_chars": 8000}))
            if not page.ok:
                continue
            context_stems = _word_stems(item.get("title", "") + " " +
                                        item.get("snippet", ""))
            boost = 3.0 * len(wanted_stems & context_stems)
            for block in page.data.get("code") or []:
                score = self._score_code(block, wanted_stems) + boost
                if best_score is None or score > best_score:
                    best_score, best_block = score, block
            for raw_url in (page.data.get("raw_links") or [])[:3]:
                file_page = self.execute(Step("fetch_page", {
                    "url": raw_url, "max_chars": 20000, "raw": True}))
                if not file_page.ok:
                    continue
                content = file_page.data.get("text") or ""
                if len(content) < 40:
                    continue
                score = self._score_code(content, wanted_stems) + boost
                if best_score is None or score > best_score:
                    best_score, best_block = score, content[:8000]
        return best_block, best_score if best_score is not None else 0.0

    @staticmethod
    def _score_code(block: str, wanted_stems: set[str]) -> float:
        hits = len(wanted_stems & _word_stems(block))
        score = hits * 20.0
        if re.search(r"(?m)^\s*(def |class |function |func |import |from |"
                     r"#include|public |private |const |let |var |async def )",
                     block):
            score += 10.0
        if block.count("\n") >= 3:
            score += 3.0
        return score + min(len(block), 4000) / 800.0

    def _pipe_research(self, plan: Plan, goal: str) -> str:
        query = (plan.steps[0].args.get("query") if plan.steps else goal) or goal
        obs = self.execute(Step("web_search", {"query": query, "limit": 8}))
        urls = [item["url"] for item in obs.data.get("results", [])]
        if not urls:
            retry = reformulate_query(query)
            if retry != query:
                self.trace(f"retrying search with {retry!r}")
                obs = self.execute(Step("web_search",
                                        {"query": retry, "limit": 8}))
                urls = [item["url"] for item in obs.data.get("results", [])]
        if not urls:
            return f"I could not find anything for {query!r}."

        found: list[tuple[str, str]] = []
        for url in urls[:3]:
            page = self.execute(Step("fetch_page", {"url": url}))
            if not page.ok or not page.data.get("text"):
                continue
            answer = extract_answer(page.data["text"], query)
            if answer:
                found.append((url, answer))
                break
        if found:
            url, answer = found[0]
            return f"{answer}\n\nSource: {url}"
        return compose_answer(goal, [obs])

    def _pipe_fetch_extract(self, plan: Plan, goal: str) -> str:
        step = plan.steps[0]
        obs = self.execute(step)
        if not obs.ok:
            return f"Could not fetch {step.args.get('url')}: {obs.error}"
        text = obs.data.get("text", "")
        answer = extract_answer(text, strip_verbs(goal)) if is_question(goal) \
            else ""
        body = answer or text[:1200]
        return f"# {obs.data.get('title') or step.args.get('url')}\n" \
               f"{step.args.get('url')}\n\n{body}"

    def _pipe_save_results(self, plan: Plan, goal: str) -> str:
        search_step, write_step = plan.steps[0], plan.steps[1]
        obs = self.execute(search_step)
        if not obs.ok or not obs.data.get("results"):
            return f"No results to save for {search_step.args.get('query')!r}."
        path = write_step.args.get("path", "results.txt")
        content = format_search_results(obs.data) + "\n"
        write = self.execute(Step("write_file", {"path": path,
                                                 "content": content}))
        if write.ok:
            return f"Saved {obs.data.get('count', 0)} result(s) to {path}"
        return f"Could not write {path}: {write.error}"

    def _pipe_make_search_script(self, plan: Plan, goal: str) -> str:
        search_step, write_step = plan.steps[0], plan.steps[1]
        obs = self.execute(search_step)
        query = search_step.args.get("query", goal)
        results = [sa.SearchResult(
            item.get("title", ""), item.get("url", ""),
            item.get("snippet", ""), item.get("engine", ""),
            item.get("source", "web")) for item in obs.data.get("results", [])]
        path = write_step.args.get("path", "search.sh")
        write = self.execute(Step("write_file",
                                  {"path": path,
                                   "content": sa.render_script(query, results)}))
        if write.ok:
            return f"Created script: {path}"
        return f"Could not write {path}: {write.error}"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

HELP_TEXT = """commands:
  /help          show this text
  /tools         list available tools
  /trace         toggle the action trace on stderr
  /yes           auto-approve mutating tools (shell, write_file, remember)
  /exit          leave the REPL
anything else is sent to the agent as a goal."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent.py",
        description="Live agent loop with no API key and no cloud model.")
    parser.add_argument("goal", nargs="?", default=None,
                        help="goal to accomplish; omit for an interactive REPL")
    parser.add_argument("--model", choices=("rules", "ollama"), default="rules",
                        help="brain to use (default: rules, no model at all)")
    parser.add_argument("--ollama-base", default=OLLAMA_BASE)
    parser.add_argument("--ollama-model", default=OLLAMA_MODEL)
    parser.add_argument("--memory", default=str(DEFAULT_MEMORY),
                        help="path to the persistent memory JSON file")
    parser.add_argument("--script", metavar="PATH", default=None,
                        help="write search results to this script file and "
                             "print only its path (no URL list)")
    parser.add_argument("--code", action="store_true",
                        help="find a script for the description and print "
                             "only the code (no links)")
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--yes", action="store_true",
                        help="auto-approve mutating tools")
    parser.add_argument("--trace", action="store_true", default=None,
                        help="print the action trace to stderr")
    parser.add_argument("--no-trace", dest="trace", action="store_false")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="print the answer as JSON")
    parser.add_argument("--tools", action="store_true",
                        help="list tools and exit")
    return parser


def _make_agent(args: argparse.Namespace) -> Agent:
    registry = build_registry(Path(args.memory))
    trace = args.trace if args.trace is not None else args.goal is None
    if args.model == "ollama":
        brain = OllamaBrain(args.ollama_base, args.ollama_model)
        if not brain.available():
            raise SystemExit(
                f"ollama brain not reachable at {args.ollama_base} "
                f"(start it or use --model rules)")
        planner: Any = ModelPlanner(brain, registry)
    else:
        planner = RuleBasedPlanner()
    script_path = Path(args.script) if args.script else None
    policy = Policy(assume_yes=args.yes or script_path is not None,
                    interactive=True if args.goal is None else None)
    return Agent(registry, planner, policy=policy, max_steps=args.max_steps,
                 trace=trace, memory_path=Path(args.memory),
                 script_path=script_path, force_code=args.code)


def _print_tools(registry: ToolRegistry) -> None:
    for name in registry.names():
        spec = registry.spec(name)
        flags = " [mutating]" if spec.mutating else ""
        print(f"{name}{flags}\n  {spec.description}")
        for key, (kind, desc) in spec.properties.items():
            req = "*" if key in spec.required else ""
            print(f"  - {key}{req}: {desc} ({kind})")


def repl(agent: Agent) -> int:
    registry = agent.registry
    print("agent.py -- live agent, no API key, no cloud model.")
    print(f"brain: {agent.planner.name} | memory: {agent.memory_path}")
    print("type /help for commands, /exit to quit.\n")
    while True:
        try:
            line = input("agent> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in ("/exit", "/quit", "exit", "quit"):
            return 0
        if line == "/help":
            print(HELP_TEXT)
            continue
        if line == "/tools":
            _print_tools(registry)
            continue
        if line == "/trace":
            agent.trace_enabled = not agent.trace_enabled
            print(f"trace {'on' if agent.trace_enabled else 'off'}")
            continue
        if line == "/yes":
            agent.policy.assume_yes = True
            print("mutating tools auto-approved")
            continue
        answer = agent.run(line)
        print()
        print(answer)
        print()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    registry = build_registry(Path(args.memory))
    if args.tools:
        _print_tools(registry)
        return 0
    agent = _make_agent(args)
    if args.goal is None:
        return repl(agent)
    answer = agent.run(args.goal)
    if args.as_json:
        print(json.dumps({"goal": args.goal, "brain": agent.planner.name,
                          "answer": answer}, indent=2, ensure_ascii=False))
    else:
        print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
