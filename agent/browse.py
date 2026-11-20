"""browse.py - fetch a page and reduce it to readable text, no headless browser.

Extraction is a small readability pass over stdlib HTML: drop script/style/
nav boilerplate, keep headings and paragraphs, preserve link targets.
"""

from __future__ import annotations

import html as html_mod
import re
import urllib.error
import urllib.parse
import urllib.request

from . import httpdebug

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
MAX_BYTES = 800_000
MIN_TEXT = 80


class BrowseError(Exception):
    pass


DROP_TAGS = ("script", "style", "noscript", "svg", "iframe", "form", "nav",
             "footer", "header", "aside", "template")


def fetch(url: str, timeout: int = 20) -> tuple[str, str]:
    """Return (final_url, html)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml"})
    started = httpdebug.request("GET", url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BYTES)
            status = resp.status
            charset = resp.headers.get_content_charset() or "utf-8"
            final = resp.geturl()
    except Exception as exc:
        httpdebug.response(url, None, 0, started,
                           err=f"{type(exc).__name__}: {exc}")
        raise
    httpdebug.response(final, status, len(raw), started)
    return final, raw.decode(charset, errors="replace")


def extract(html_text: str) -> dict:
    """Reduce HTML to {title, text, links}."""
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.S | re.I)
    title = _clean(title_m.group(1)) if title_m else ""

    body = html_text
    for tag in DROP_TAGS:
        body = re.sub(rf"<{tag}\b[^>]*>.*?</{tag}>", " ", body,
                      flags=re.S | re.I)
    # keep link targets as markdown-ish references
    body = re.sub(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                  lambda m: f"{_clean(m.group(2))} [{_abs(m.group(1), None)}]",
                  body, flags=re.S | re.I)
    body = re.sub(r"<(h[1-6]|p|li|br|div|tr)\b[^>]*>", "\n", body, flags=re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    text = html_mod.unescape(body)
    lines = []
    for ln in text.split("\n"):
        ln = _clean(ln)
        if len(ln) >= 2:
            lines.append(ln)
    out = "\n".join(lines)

    links = []
    seen = set()
    for m in re.finditer(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                         html_text, re.S | re.I):
        href = _abs(m.group(1), None)
        label = _clean(m.group(2))
        if href and href not in seen and href.startswith("http"):
            seen.add(href)
            links.append({"url": href, "label": label[:120]})
    return {"title": title, "text": out[:20_000], "links": links[:40]}


def _abs(url: str, base: str | None) -> str:
    if url.startswith("//"):
        return "https:" + url
    return url


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def browse(url: str) -> dict:
    """Fetch + extract. Raises BrowseError with a readable cause."""
    try:
        final, html_text = fetch(url)
    except urllib.error.HTTPError as exc:
        raise BrowseError(f"HTTP {exc.code} for {url}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise BrowseError(f"cannot reach {url}: {exc}") from exc
    page = extract(html_text)
    if len(page["text"]) < MIN_TEXT:
        raise BrowseError(
            f"{url} yielded almost no readable text "
            f"({len(page['text'])} chars) - likely JS-only or a bot wall")
    page["url"] = final
    return page


def format_page(page: dict, max_chars: int = 3000) -> str:
    lines = [f"PAGE: {page.get('title', '')}", f"URL: {page.get('url', '')}", ""]
    lines.append(page["text"][:max_chars])
    if page.get("links"):
        lines.append("")
        lines.append("LINKS:")
        lines += [f"- {l['label'][:60]} {l['url']}" for l in page["links"][:10]]
    return "\n".join(lines)
