#!/usr/bin/env python3
"""agent.py -- describe a script, get the script.

Searches the web and GitHub/GitLab, fetches the best matching page or raw
source file, and prints only the code: no links, no titles, no commentary.
No API key, no model, standard library only.

Usage:
    python3 agent.py "create me a python script that downloads a webpage"
    python3 agent.py                     # interactive: type a description
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from dataclasses import dataclass, field
from typing import Callable, Sequence

import search_agent as sa

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "could",
    "did", "do", "does", "for", "from", "had", "has", "have", "how", "i", "if",
    "in", "into", "is", "it", "its", "me", "my", "of", "on", "or", "our",
    "please", "should", "so", "some", "that", "the", "their", "them", "then",
    "there", "these", "this", "to", "than", "up", "us", "was", "we", "what", "when",
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


# --------------------------------------------------------------------------- #
# text helpers
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
    for suffix in ("ing", "ed", "s"):
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


def _code_query_variant(query: str) -> str:
    """Drop language/'script' filler so the web search targets the task."""
    kept = [word.strip(".,?!")
            for word in query.split()
            if word.strip(".,?!").lower() not in _QUERY_FILLER
            and word.strip(".,?!").lower() not in STOPWORDS]
    return " ".join(kept)


# --------------------------------------------------------------------------- #
# data model
# --------------------------------------------------------------------------- #

@dataclass
class Step:
    tool: str
    args: dict = field(default_factory=dict)


@dataclass
class Observation:
    step: Step
    ok: bool
    data: dict = field(default_factory=dict)
    error: str = ""


# --------------------------------------------------------------------------- #
# tools (each returns {"ok": bool, ...} or {"ok": False, "error": str})
# --------------------------------------------------------------------------- #

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


def build_tools() -> dict[str, Callable[..., dict]]:
    return {
        "web_search": tool_web_search,
        "scripts_search": tool_scripts_search,
        "fetch_page": tool_fetch_page,
    }


# --------------------------------------------------------------------------- #
# agent
# --------------------------------------------------------------------------- #

class Agent:
    """Search the web, fetch the best matching script, print only the code."""

    def __init__(self, tools: dict[str, Callable[..., dict]],
                 trace: bool = True) -> None:
        self.tools = tools
        self.trace_enabled = trace

    def trace(self, message: str) -> None:
        if self.trace_enabled:
            print(f"[agent] {message}", file=sys.stderr)

    def execute(self, step: Step) -> Observation:
        self.trace(f"{step.tool} {step.args}")
        try:
            data = self.tools[step.tool](**step.args)
        except Exception as exc:  # noqa: BLE001 - a tool must not kill the loop
            return Observation(step, False, {},
                               f"{type(exc).__name__}: {exc}")
        ok = bool(data.get("ok"))
        error = "" if ok else str(data.get("error", "tool failed"))
        self.trace(f"-> {'ok' if ok else 'error: ' + error}")
        return Observation(step, ok, data, error)

    def run(self, goal: str) -> str:
        goal = goal.strip()
        if not goal:
            return ""
        query = strip_verbs(goal)
        self.trace(f"goal: {goal}")

        queries = [query]
        variant = _code_query_variant(query)
        if variant and variant != query:
            queries.append(variant)

        results: list[dict] = []
        seen: set[str] = set()
        for candidate in queries:
            obs = self.execute(Step("web_search", {"query": candidate}))
            if not obs.ok:
                continue
            for item in obs.data.get("results", []):
                url = item.get("url")
                if url and url not in seen:
                    seen.add(url)
                    results.append(item)

        best_block, best_score = self._best_code(results, query)
        if best_block is None or best_score < 20.0:
            obs = self.execute(Step("scripts_search", {"query": query}))
            if obs.ok:
                alt_block, alt_score = self._best_code(
                    obs.data.get("results", []), query)
                if alt_block is not None and (best_block is None
                                              or alt_score > best_score):
                    best_block, best_score = alt_block, alt_score
        return best_block or ""

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


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

HELP_TEXT = """type a description of the script you want, e.g.
  create me a python script that downloads a webpage
commands: /help shows this, /trace toggles the working trace, /exit leaves."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent.py",
        description="Describe a script, get the script. No links, no model.")
    parser.add_argument("goal", nargs="?", default=None,
                        help="description of the script; omit for interactive")
    parser.add_argument("--trace", action="store_true", default=None,
                        help="print the working trace to stderr")
    parser.add_argument("--no-trace", dest="trace", action="store_false")
    return parser


def repl(agent: Agent) -> int:
    print("agent.py -- describe a script, get the script (no links, no model).")
    print("type /exit to quit.\n")
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
        if line == "/trace":
            agent.trace_enabled = not agent.trace_enabled
            print(f"trace {'on' if agent.trace_enabled else 'off'}")
            continue
        if line == "/help":
            print(HELP_TEXT)
            continue
        answer = agent.run(line)
        if answer:
            print()
            print(answer)
            print()
        else:
            print("no script found", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    agent = Agent(build_tools(),
                  trace=args.trace if args.trace is not None
                  else args.goal is None)
    if args.goal is None:
        return repl(agent)
    answer = agent.run(args.goal)
    if answer:
        print(answer)
    else:
        print("no script found", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
