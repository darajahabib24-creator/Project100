#!/usr/bin/env python3
"""search_agent.py -- no-model, no-API-key search over web engines and code hosts.

Searches DuckDuckGo, Google and Bing by parsing their public result pages, and
finds scripts/repositories on GitHub and GitLab through their public HTML/JSON
endpoints. No LLM is involved and no API keys are required.

Usage:
    python3 search_agent.py web "python web scraper" --engines bing
    python3 search_agent.py github "web scraper"
    python3 search_agent.py gitlab "web scraper"
    python3 search_agent.py scripts "web scraper" --emit-script out.sh
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

BING_SEARCH = "https://www.bing.com/search"
DDG_SEARCH = "https://html.duckduckgo.com/html/"
GOOGLE_SEARCH = "https://www.google.com/search"
GITHUB_HTML_SEARCH = "https://github.com/search"
GITHUB_API_SEARCH = "https://api.github.com/search/repositories"
GITLAB_API_PROJECTS = "https://gitlab.com/api/v4/projects"

WEB_ENGINES: dict[str, Callable[..., list["SearchResult"]]] = {}
DEFAULT_ENGINES = ("bing", "duckduckgo", "google")

# GitHub search pages link to many non-repository paths; skip those.
_GH_NOISE = {
    "search", "login", "logout", "signup", "explore", "topics", "trending",
    "collections", "sponsors", "features", "enterprise", "team", "pricing",
    "marketplace", "settings", "notifications", "new", "issues", "pulls",
    "discussions", "about", "contact", "security", "readme", "site", "orgs",
    "users", "apps", "services", "customer-stories", "events", "git-guides",
}


# --------------------------------------------------------------------------- #
# errors and data model
# --------------------------------------------------------------------------- #

class SearchError(Exception):
    """Base class for search failures."""


class FetchError(SearchError):
    """An HTTP fetch failed."""


class BlockedError(FetchError):
    """The host answered with a challenge / rate-limit / block page."""


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    engine: str = ""
    source: str = "web"
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "engine": self.engine,
            "source": self.source,
            "meta": self.meta,
        }


@dataclass
class SearchResponse:
    results: list[SearchResult] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.results)

    def as_dict(self) -> dict:
        return {
            "results": [r.as_dict() for r in self.results],
            "errors": self.errors,
        }


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(value: str) -> str:
    """Strip tags, unescape entities and collapse whitespace."""
    if not value:
        return ""
    value = _TAG_RE.sub(" ", value)
    value = html.unescape(value)
    return _WS_RE.sub(" ", value).strip()


def fetch(url: str, timeout: float = 15.0,
          headers: dict[str, str] | None = None) -> str:
    """GET a URL and return decoded text, raising on transport/HTTP blocks."""
    hdrs = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            body = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (202, 401, 403, 429, 503):
            raise BlockedError(
                f"HTTP {exc.code} from {url} (challenge or rate limit)") from exc
        raise FetchError(f"HTTP {exc.code} for {url}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise FetchError(f"{type(exc).__name__}: {exc} ({url})") from exc
    text = body.decode("utf-8", "replace")
    if status in (202, 401, 403, 429, 503):
        raise BlockedError(f"HTTP {status} from {url} (challenge or rate limit)")
    return text


def _dedupe(results: Iterable[SearchResult]) -> list[SearchResult]:
    seen: set[str] = set()
    out: list[SearchResult] = []
    for item in results:
        key = item.url.rstrip("/") or item.url
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


# --------------------------------------------------------------------------- #
# redirect decoders
# --------------------------------------------------------------------------- #

def decode_bing_url(href: str) -> str:
    """Turn a bing.com/ck/a?...&u=a1<base64> wrapper into the real URL."""
    if "bing.com/ck/a" not in href:
        return href
    match = re.search(r"[?&]u=a1([^&]+)", href)
    if not match:
        return href
    raw = match.group(1).replace(" ", "+").replace("-", "+").replace("_", "/")
    raw += "=" * (-len(raw) % 4)
    try:
        decoded = base64.b64decode(raw, validate=True).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - malformed wrappers fall back to the href
        return href
    return decoded or href


def decode_ddg_url(href: str) -> str:
    """Turn a duckduckgo.com/l/?uddg=<encoded> redirect into the real URL."""
    if "uddg=" not in href:
        return href
    parsed = urllib.parse.urlparse(href)
    params = urllib.parse.parse_qs(parsed.query)
    target = params.get("uddg", [None])[0]
    if target:
        return target
    return href


def decode_google_url(href: str) -> str:
    """Turn /url?q=... or a relative link into an absolute URL."""
    if href.startswith("/url?"):
        parsed = urllib.parse.urlparse("https://www.google.com" + href)
        target = urllib.parse.parse_qs(parsed.query).get("q", [None])[0]
        return target or href
    if href.startswith("http"):
        return href
    return urllib.parse.urljoin("https://www.google.com/", href.lstrip("/"))


# --------------------------------------------------------------------------- #
# web engines
# --------------------------------------------------------------------------- #

def _parse_bing(text: str, limit: int) -> list[SearchResult]:
    results: list[SearchResult] = []
    if limit <= 0:
        return results
    for block in re.findall(r'<li class="b_algo".*?</li>', text, re.S):
        anchor = re.search(
            r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not anchor:
            continue
        href = html.unescape(anchor.group(1))
        title = clean_text(anchor.group(2))
        url = decode_bing_url(href)
        if not title or not url:
            continue
        snippet = ""
        snip = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        if snip:
            snippet = clean_text(snip.group(1))
        results.append(SearchResult(title, url, snippet, "bing", "web"))
        if len(results) >= limit:
            break
    return results


def _parse_duckduckgo(text: str, limit: int) -> list[SearchResult]:
    results: list[SearchResult] = []
    if limit <= 0:
        return results
    for anchor in re.finditer(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            text, re.S):
        href = html.unescape(anchor.group(1))
        title = clean_text(anchor.group(2))
        url = decode_ddg_url(href)
        if not title or not url:
            continue
        snippet = ""
        snip = re.search(
            r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
            text[anchor.end():anchor.end() + 4000], re.S)
        if snip:
            snippet = clean_text(snip.group(1))
        results.append(SearchResult(title, url, snippet, "duckduckgo", "web"))
        if len(results) >= limit:
            break
    return results


def _parse_google(text: str, limit: int) -> list[SearchResult]:
    results: list[SearchResult] = []
    if limit <= 0:
        return results
    for anchor in re.finditer(
            r'<a[^>]+href="([^"]+)"[^>]*>\s*<h3[^>]*>(.*?)</h3>', text, re.S):
        href = html.unescape(anchor.group(1))
        title = clean_text(anchor.group(2))
        url = decode_google_url(href)
        if not title or not url:
            continue
        snippet = ""
        snip = re.search(r"<span[^>]*>(.*?)</span>",
                         text[anchor.end():anchor.end() + 3000], re.S)
        if snip:
            snippet = clean_text(snip.group(1))
        results.append(SearchResult(title, url, snippet, "google", "web"))
        if len(results) >= limit:
            break
    return results


def search_bing(query: str, limit: int = 10,
                timeout: float = 15.0) -> list[SearchResult]:
    url = BING_SEARCH + "?" + urllib.parse.urlencode(
        {"q": query, "count": max(1, limit)})
    return _parse_bing(fetch(url, timeout=timeout), limit)


def search_duckduckgo(query: str, limit: int = 10,
                      timeout: float = 15.0) -> list[SearchResult]:
    url = DDG_SEARCH + "?" + urllib.parse.urlencode(
        {"q": query, "kl": "us-en"})
    return _parse_duckduckgo(fetch(url, timeout=timeout), limit)


def search_google(query: str, limit: int = 10,
                  timeout: float = 15.0) -> list[SearchResult]:
    url = GOOGLE_SEARCH + "?" + urllib.parse.urlencode(
        {"q": query, "num": max(1, limit), "hl": "en"})
    return _parse_google(fetch(url, timeout=timeout), limit)


WEB_ENGINES.update({
    "bing": search_bing,
    "duckduckgo": search_duckduckgo,
    "google": search_google,
})


def search_web(query: str, engines: Sequence[str] | None = None,
               limit: int = 10, timeout: float = 15.0) -> SearchResponse:
    """Query one or more engines and aggregate de-duplicated results."""
    chosen = list(engines) if engines else list(DEFAULT_ENGINES)
    results: list[SearchResult] = []
    errors: dict[str, str] = {}
    for name in chosen:
        func = WEB_ENGINES.get(name.lower())
        if func is None:
            errors[name] = f"unknown engine {name!r}"
            continue
        try:
            results.extend(func(query, limit=limit, timeout=timeout))
        except SearchError as exc:
            errors[name] = str(exc)
        except Exception as exc:  # noqa: BLE001 - never abort the whole search
            errors[name] = f"{type(exc).__name__}: {exc}"
    return SearchResponse(_dedupe(results), errors)


# --------------------------------------------------------------------------- #
# code hosts
# --------------------------------------------------------------------------- #

def _parse_github_html(text: str, limit: int) -> list[SearchResult]:
    start = text.find('data-testid="results-list"')
    chunk = text[start:] if start != -1 else text
    results: list[SearchResult] = []
    if limit <= 0:
        return results
    pattern = re.compile(
        r'<a[^>]+href="(/[^"]+)"[^>]*>\s*<span[^>]*>(.*?)</span>',
        re.S)
    for match in pattern.finditer(chunk):
        path = html.unescape(match.group(1))
        name = clean_text(match.group(2))
        if not name or path.count("/") != 2:
            continue
        owner, repo = path.strip("/").split("/")
        if owner.lower() in _GH_NOISE or repo.lower() in _GH_NOISE:
            continue
        if not re.fullmatch(r"[\w.-]+", repo):
            continue
        window = chunk[match.end():match.end() + 2000]
        desc_match = re.search(
            r'<span[^>]*class="[^"]*searchMatchText[^"]*"[^>]*>(.*?)</span>',
            window, re.S)
        snippet = clean_text(desc_match.group(1)) if desc_match else ""
        stars = None
        star_match = re.search(r'aria-label="([\d,]+) stars"', window)
        if star_match:
            stars = int(star_match.group(1).replace(",", ""))
        results.append(SearchResult(
            name, "https://github.com" + path, snippet, "github", "github",
            {"stars": stars}))
        if len(results) >= limit:
            break
    return results


def search_github(query: str, limit: int = 10, timeout: float = 15.0,
                  use_api: bool = False) -> list[SearchResult]:
    """Search GitHub repositories. HTML by default; API optional (rate-limited)."""
    if use_api:
        url = GITHUB_API_SEARCH + "?" + urllib.parse.urlencode(
            {"q": query, "per_page": max(1, limit), "sort": "stars"})
        data = json.loads(fetch(
            url, timeout=timeout,
            headers={"Accept": "application/vnd.github+json"}))
        return [
            SearchResult(
                item.get("full_name", ""),
                item.get("html_url", ""),
                item.get("description") or "",
                "github", "github",
                {"stars": item.get("stargazers_count"),
                 "language": item.get("language")},
            )
            for item in data.get("items", [])
        ]
    url = GITHUB_HTML_SEARCH + "?" + urllib.parse.urlencode(
        {"q": query, "type": "repositories"})
    return _parse_github_html(fetch(url, timeout=timeout), limit)


def search_gitlab(query: str, limit: int = 10,
                  timeout: float = 15.0) -> list[SearchResult]:
    """Search GitLab projects through the unauthenticated REST API."""
    url = GITLAB_API_PROJECTS + "?" + urllib.parse.urlencode(
        {"search": query, "per_page": max(1, limit), "order_by": "id"})
    data = json.loads(fetch(url, timeout=timeout))
    if not isinstance(data, list):
        return []
    return [
        SearchResult(
            item.get("path_with_namespace", ""),
            item.get("web_url", ""),
            item.get("description") or "",
            "gitlab", "gitlab",
            {"stars": item.get("star_count"),
             "language": item.get("language")},
        )
        for item in data
    ]


def search_scripts(query: str, limit: int = 10,
                   timeout: float = 15.0) -> SearchResponse:
    """Find scripts/repositories on GitHub and GitLab."""
    results: list[SearchResult] = []
    errors: dict[str, str] = {}
    for name, func in (("github", search_github), ("gitlab", search_gitlab)):
        try:
            results.extend(func(query, limit=limit, timeout=timeout))
        except SearchError as exc:
            errors[name] = str(exc)
        except Exception as exc:  # noqa: BLE001
            errors[name] = f"{type(exc).__name__}: {exc}"
    return SearchResponse(_dedupe(results), errors)


# --------------------------------------------------------------------------- #
# script generation
# --------------------------------------------------------------------------- #

def render_script(query: str, results: Sequence[SearchResult],
                  header: str = "") -> str:
    """Render a self-contained shell script that reproduces the result list."""
    lines = [
        "#!/usr/bin/env bash",
        "# Generated by search_agent.py -- no API key, no model.",
        f"# query: {query}",
    ]
    if header:
        lines.append(f"# {header}")
    lines += [
        "set -euo pipefail",
        "",
        'echo "Search results for: %s"' % shlex.quote(query),
        'echo "----------------------------------------"',
        "",
    ]
    for item in results:
        lines.append(f"# [{item.engine or item.source}] {item.title}")
        if item.snippet:
            lines.append(f"# {item.snippet}")
        lines.append(f"printf '%s\\n' {shlex.quote(item.url)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_script(path: str, query: str,
                 results: Sequence[SearchResult]) -> str:
    """Write the generated script to *path* and return the path."""
    text = render_script(query, results)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o755)
    return path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="search_agent.py",
        description="Web + code-host search with no model and no API key.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(sp: argparse.ArgumentParser, engines: bool = False) -> None:
        sp.add_argument("query")
        sp.add_argument("--limit", type=int, default=10)
        sp.add_argument("--timeout", type=float, default=15.0)
        sp.add_argument("--json", action="store_true", dest="as_json")
        sp.add_argument("--emit-script", metavar="PATH", default=None)
        if engines:
            sp.add_argument("--engines", default=",".join(DEFAULT_ENGINES),
                            help="comma-separated: bing,duckduckgo,google")

    p_web = sub.add_parser("web", help="search DuckDuckGo/Google/Bing")
    add_common(p_web, engines=True)

    p_gh = sub.add_parser("github", help="search GitHub repositories")
    add_common(p_gh)
    p_gh.add_argument("--api", action="store_true",
                      help="use the REST API instead of HTML (rate-limited)")

    p_gl = sub.add_parser("gitlab", help="search GitLab projects")
    add_common(p_gl)

    p_scripts = sub.add_parser("scripts", help="search GitHub + GitLab")
    add_common(p_scripts)
    return parser


def _emit(response: SearchResponse, args: argparse.Namespace) -> None:
    if getattr(args, "emit_script", None):
        path = write_script(args.emit_script, args.query, response.results)
        print(f"wrote script: {path}", file=sys.stderr)


def _print(response: SearchResponse, args: argparse.Namespace) -> int:
    if args.as_json:
        print(json.dumps(response.as_dict(), indent=2, ensure_ascii=False))
    else:
        if not response.results:
            print("no results")
        for index, item in enumerate(response.results, 1):
            print(f"{index}. [{item.engine or item.source}] {item.title}")
            print(f"   {item.url}")
            if item.snippet:
                print(f"   {item.snippet}")
        for name, message in response.errors.items():
            print(f"warning: {name}: {message}", file=sys.stderr)
    _emit(response, args)
    return 0 if response.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "web":
        engines = [e.strip() for e in args.engines.split(",") if e.strip()]
        response = search_web(args.query, engines=engines, limit=args.limit,
                              timeout=args.timeout)
    elif args.command == "github":
        try:
            found = search_github(args.query, limit=args.limit,
                                  timeout=args.timeout, use_api=args.api)
            response = SearchResponse(found, {})
        except SearchError as exc:
            response = SearchResponse([], {"github": str(exc)})
    elif args.command == "gitlab":
        try:
            found = search_gitlab(args.query, limit=args.limit,
                                  timeout=args.timeout)
            response = SearchResponse(found, {})
        except SearchError as exc:
            response = SearchResponse([], {"gitlab": str(exc)})
    elif args.command == "scripts":
        response = search_scripts(args.query, limit=args.limit,
                                  timeout=args.timeout)
    else:  # pragma: no cover - argparse enforces the choices
        raise SystemExit(f"unknown command {args.command!r}")
    return _print(response, args)


if __name__ == "__main__":
    raise SystemExit(main())
