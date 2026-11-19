"""search.py - find things on the web without a search API.

No API key, no vendor SDK. `web_search` tries, in order:
  1. DuckDuckGo lite   - GET form, no JS, no key
  2. Brave HTML         - no key, different index
  3. Wikipedia API      - only for encyclopedic queries (see is_encyclopedic)
and returns normalised results. A failed engine falls through, so one blocked
host does not kill the search.
"""

from __future__ import annotations

import html as html_mod
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

MIN_BODY = 400          # bytes; below this we suspect an interstitial
TIMEOUT = 15


class SearchError(Exception):
    pass


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _get(url: str, timeout: int = TIMEOUT, headers: dict | None = None) -> str:
    hdrs = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(2_000_000)
        charset = resp.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #

def _clean(text: str) -> str:
    text = html_mod.unescape(text or "")
    return re.sub(r"\s+", " ", text).strip()


def _result(url: str, title: str, snippet: str, engine: str, **extra) -> dict:
    url = _abs(url)
    return {
        "url": url,
        "title": _clean(title)[:200],
        "snippet": _clean(snippet)[:400],
        "engine": engine,
        **extra,
    }


def _abs(url: str, base: str | None = None) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    if base and url.startswith("/"):
        m = re.match(r"(https?://[^/]+)", base)
        if m:
            url = m.group(1) + url
    return url.split("#")[0]


def _valid(url: str) -> bool:
    return bool(re.match(r"https?://", url or ""))


# --------------------------------------------------------------------------- #
# Engines
# --------------------------------------------------------------------------- #

def ddg_lite(query: str, count: int = 10) -> list[dict]:
    """DuckDuckGo lite: a plain HTML form, no JavaScript required."""
    url = "https://lite.duckduckgo.com/lite/?" + urllib.parse.urlencode(
        {"q": query, "kl": "wt-wt"})
    body = _get(url)
    if len(body) < MIN_BODY:
        raise SearchError("ddg-lite: suspiciously short body")
    results = []
    # lite.duckduckgo.com markup: every result is an <a rel="nofollow"
    # href="//duckduckgo.com/l/?uddg=...">TITLE</a>, followed by a snippet in
    # the same table row. The class names rotate, so match on rel/href shape.
    rows = re.findall(
        r'<a\b[^>]*href="(//duckduckgo\.com/l/\?uddg=[^"]+)"[^>]*>(.*?)</a>',
        body, re.S)
    snippets = re.findall(
        r'<td\b[^>]*>(?:\s*<a\b[^>]*>.*?</a>)?\s*(.*?)</td>', body, re.S)
    snip_texts = [_strip_tags(s) for s in snippets if _strip_tags(s)]
    for i, (href, title) in enumerate(rows[:count]):
        real = urllib.parse.unquote(html_mod.unescape(href))
        m = re.search(r"uddg=([^&]+)", real)
        if m:
            real = urllib.parse.unquote(m.group(1))
        if not _valid(real):
            continue
        snip = snip_texts[i] if i < len(snip_texts) else ""
        results.append(_result(real, _strip_tags(title), snip, "ddg-lite"))
    if not results:
        raise SearchError("ddg-lite: no results parsed")
    return results


def mojeek(query: str, count: int = 10) -> list[dict]:
    """Mojeek - independent web index, plain HTML, no key, generous limits."""
    url = "https://www.mojeek.com/search?" + urllib.parse.urlencode({"q": query})
    body = _get(url, headers={"Accept": "text/html"})
    if len(body) < MIN_BODY:
        raise SearchError("mojeek: suspiciously short body")
    results = []
    # result blocks: <a class="title" href="URL">TITLE</a> ... <p class="s">SNIP</p>
    for m in re.finditer(
            r'<a[^>]+class="title"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', body, re.S):
        href, title = m.group(1), _strip_tags(m.group(2))
        if not _valid(href) or len(title) < 5:
            continue
        tail = body[m.end():m.end() + 600]
        sm = re.search(r'<p class="s">(.*?)</p>', tail, re.S)
        snip = _strip_tags(sm.group(1)) if sm else ""
        results.append(_result(href, title, snip, "mojeek"))
        if len(results) >= count:
            break
    if not results:
        raise SearchError("mojeek: no results parsed")
    return results


def brave_html(query: str, count: int = 10) -> list[dict]:
    """Brave search HTML. No key, but the markup shifts; parse defensively."""
    url = "https://search.brave.com/search?" + urllib.parse.urlencode(
        {"q": query, "source": "web"})
    body = _get(url, headers={"Accept": "text/html"})
    if len(body) < MIN_BODY:
        raise SearchError("brave: suspiciously short body")
    results = []
    # Anchors inside result blocks, class contains "h" or "result"
    for m in re.finditer(
            r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', body, re.S):
        href, inner = m.group(1), m.group(2)
        if any(bad in href for bad in ("brave.com", "search.brave", "go.brave")):
            continue
        title = _strip_tags(inner)
        if len(title) < 8:
            continue
        results.append(_result(href, title, "", "brave"))
        if len(results) >= count:
            break
    if not results:
        raise SearchError("brave: no results parsed")
    return results


def wikipedia(query: str, count: int = 5) -> list[dict]:
    """Wikipedia opensearch — the reliable fallback for encyclopedic queries."""
    api = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "list": "search", "srsearch": query,
        "format": "json", "srlimit": count, "srprop": "snippet|timestamp",
    })
    body = _get(api, headers={"Accept": "application/json"})
    try:
        obj = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SearchError(f"wikipedia: bad JSON: {exc}") from exc
    hits = (obj.get("query") or {}).get("search") or []
    out = []
    for h in hits:
        title = h.get("title", "")
        snippet = _strip_tags(h.get("snippet", ""))
        ts = h.get("timestamp", "")
        out.append(_result(
            f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
            title, snippet, "wikipedia", timestamp=ts))
    if not out:
        raise SearchError("wikipedia: no results")
    return out


def wikipedia_summary(title: str) -> dict:
    """Lead paragraph + facts for a Wikipedia title. The most reliable place
    for a 'what is X' answer, since it is prose written to define the subject."""
    api = "https://en.wikipedia.org/api/rest_v1/page/summary/" + \
        urllib.parse.quote(title.replace(" ", "_"))
    try:
        body = _get(api, headers={"Accept": "application/json"})
    except Exception:  # noqa: BLE001 - fall back to the action API
        return {}
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        return {}
    if obj.get("type") == "disambiguation":
        return {}
    return {
        "title": obj.get("title", title),
        "extract": _clean(obj.get("extract", "")),
        "url": (obj.get("content_urls") or {}).get("desktop", {}).get("page", ""),
        "timestamp": obj.get("timestamp", ""),
    }


def wikipedia_opensearch(query: str, count: int = 5) -> list[str]:
    """Candidate Wikipedia titles for a query."""
    api = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "list": "search", "srsearch": query,
        "format": "json", "srlimit": count, "srprop": "",
    })
    body = _get(api, headers={"Accept": "application/json"})
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        return []
    return [h.get("title", "") for h in
            (obj.get("query") or {}).get("search") or [] if h.get("title")]


def direct_answer(query: str) -> dict:
    """Best-effort direct answer for a factual question.

    Strategy: pick the most likely Wikipedia title for the query, take its
    summary lead, and return it with provenance. Returns {} when nothing fits.
    """
    q = query.strip()
    titles = []
    # a quoted or capitalised subject is a strong title signal
    m = re.search(r"what(?: is| are| was| were)\s+(?:the\s+|a\s+|an\s+)?(.+?)"
                  r"\s*\??\s*$", q, re.I)
    if m:
        titles.append(m.group(1).strip())
    titles += wikipedia_opensearch(q, count=3)
    for t in titles:
        t = t.strip()
        if not t or len(t) > 120:
            continue
        s = wikipedia_summary(t)
        if s.get("extract"):
            s["query"] = q
            return s
    return {}


def format_answer(ans: dict) -> str:
    if not ans:
        return "(no direct answer found)"
    lines = [f"ANSWER: {ans['title']}", f"SOURCE: {ans['url']}"]
    if ans.get("timestamp"):
        lines.append(f"LAST EDITED: {ans['timestamp']}")
    lines += ["", ans["extract"]]
    return "\n".join(lines)


def _strip_tags(text: str) -> str:
    return _clean(re.sub(r"<[^>]+>", " ", text or ""))


# --------------------------------------------------------------------------- #
# Query routing
# --------------------------------------------------------------------------- #

def is_encyclopedic(query: str) -> bool:
    """Should this query go to Wikipedia first?

    Only genuinely factual, definitional questions. Opinion, comparison,
    recommendation and current-event questions belong to the open web, where
    there are multiple sources to cross-check.
    """
    q = query.lower()
    # these are web questions, not encyclopedia questions
    opinion = ("best", "worst", "top ", "top 10", " vs ", "versus", "review",
               "recommend", "which should", "is it worth", "worth it",
               "cheapest", "fastest", "easiest", "alternative to", "like ",
               "opinion", "rating", "price", "buy", "deals", "news today")
    if any(w in q for w in opinion):
        return False
    head = r"^(what|who|when|where|why|how|which|whose)\b"
    if re.search(head, q):
        return True
    return any(w in q for w in ("definition", "history of", "wikipedia",
                                "named after"))


# Maps engine name -> module-level function name, resolved at call time so
# tests (and callers) can monkeypatch the module attribute.
ENGINES = {"ddg-lite": "ddg_lite", "brave": "brave_html",
           "wikipedia": "wikipedia", "mojeek": "mojeek"}


def _engine(name: str):
    fn = globals().get(ENGINES.get(name, name))
    return fn if callable(fn) else None


def web_search(query: str, count: int = 10, engines: list[str] | None = None,
               sleep: float = 0.5) -> dict:
    """Search the open web. Returns {"query", "engine", "results", "fallbacks"}.

    Tries engines in order and returns the first that yields results; engines
    that fail are recorded in `fallbacks` so the caller can see provenance.
    """
    query = (query or "").strip()
    if not query:
        raise SearchError("empty query")

    order = engines or ["ddg-lite", "mojeek", "brave", "wikipedia"]
    if is_encyclopedic(query):
        order = ["wikipedia"] + [e for e in order if e != "wikipedia"]

    fallbacks, last_err = [], None
    retried: set[str] = set()
    for name in order:
        fn = _engine(name)
        if fn is None:
            fallbacks.append({"engine": name, "error": "unknown engine"})
            continue
        try:
            res = fn(query, count=count)
            return {"query": query, "engine": name, "results": res,
                    "fallbacks": fallbacks}
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and name not in retried:
                # rate-limited or briefly unavailable: wait, retry once
                retried.add(name)
                time.sleep(3.0)
                try:
                    res = fn(query, count=count)
                    return {"query": query, "engine": name, "results": res,
                            "fallbacks": fallbacks}
                except Exception as exc2:  # noqa: BLE001
                    fallbacks.append({"engine": name,
                                      "error": f"{type(exc2).__name__}: {exc2}"})
            else:
                fallbacks.append({"engine": name,
                                  "error": f"{type(exc).__name__}: {exc}"})
        except (SearchError, urllib.error.URLError, OSError, Exception) as exc:
            fallbacks.append({"engine": name, "error": f"{type(exc).__name__}: {exc}"})
            last_err = exc
        time.sleep(sleep)
    raise SearchError(f"all search engines failed ({last_err})")


def format_results(payload: dict, limit: int = 5) -> str:
    """Render search results as compact agent-readable text."""
    lines = [f"search: {payload['query']}  (engine: {payload['engine']})"]
    for i, r in enumerate(payload["results"][:limit], 1):
        lines.append(f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}")
    if payload.get("fallbacks"):
        lines.append("engines tried and failed: " + ", ".join(
            f"{f['engine']}" for f in payload["fallbacks"]))
    return "\n".join(lines)
