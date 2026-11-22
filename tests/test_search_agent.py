#!/usr/bin/env python3
"""Tests for search_agent.py.

Parsing, aggregation, script generation and CLI behaviour are exercised
offline with synthetic pages and stubbed engines; fetch is exercised against
a tiny local HTTP server. Live network tests run only when
SEARCH_AGENT_LIVE=1 is set.
"""

import base64
import contextlib
import io
import json
import os
import shlex
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import search_agent as sa  # noqa: E402


# --------------------------------------------------------------------------- #
# synthetic pages
# --------------------------------------------------------------------------- #

def bing_page(real_url="https://example.com/page"):
    encoded = base64.b64encode(real_url.encode()).decode()
    return f"""
    <ol id="b_results">
      <li class="b_algo">
        <h2><a target="_blank" href="https://www.bing.com/ck/a?&amp;&amp;p=abc&amp;u=a1{encoded}&amp;ntb=1">Example &amp; Co</a></h2>
        <p class="b_lineclamp2">A <em>nice</em> page about things.</p>
      </li>
      <li class="b_algo"><h2><a href="https://plain.example.org/x">Plain</a></h2></li>
    </ol>
    """


def ddg_page():
    return """
    <div class="result">
      <h2 class="result__title">
        <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fddg&amp;rut=abc">DDG Result</a>
      </h2>
      <a class="result__snippet" href="https://example.com/ddg">Snippet here</a>
    </div>
    """


def google_page():
    return """
    <div class="g">
      <a href="/url?q=https%3A%2F%2Fexample.com%2Fg&amp;sa=U&amp;ved=1"><h3>Google Result</h3></a>
      <span>Google snippet text</span>
    </div>
    """


def github_page():
    return """
    <div data-testid="results-list">
      <div class="resultRow">
        <h3 class="header">
          <div class="search-title">
            <a href="/alice/awesome-scraper"><span>alice/awesome-scraper</span></a>
          </div>
        </h3>
        <div class="Content"><span class="search-match searchMatchText">A web scraper</span></div>
        <a href="/alice/awesome-scraper/stargazers" aria-label="1,234 stars">stars</a>
      </div>
      <div class="resultRow">
        <h3 class="header">
          <div class="search-title">
            <a href="/topics/scraper"><span>topics/scraper</span></a>
          </div>
        </h3>
      </div>
    </div>
    """


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        if self.path.startswith("/ok"):
            body, status = b"<html><body>hi</body></html>", 200
        elif self.path.startswith("/403"):
            body, status = b"blocked", 403
        elif self.path.startswith("/500"):
            body, status = b"boom", 500
        else:
            body, status = b"not found", 404
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: N802 - http.server API
        pass


class _Server:
    def __init__(self):
        self.httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #

class TestCleanText(unittest.TestCase):
    def test_strips_and_unescapes(self):
        self.assertEqual(sa.clean_text("<b>A</b> &amp; <i>B</i>"), "A & B")

    def test_collapses_whitespace(self):
        self.assertEqual(sa.clean_text("  a \n\t b  "), "a b")

    def test_empty(self):
        self.assertEqual(sa.clean_text(""), "")


class TestDecoders(unittest.TestCase):
    def test_bing_redirect(self):
        real = "https://example.com/page"
        encoded = base64.b64encode(real.encode()).decode()
        href = f"https://www.bing.com/ck/a?&&p=x&u=a1{encoded}&ntb=1"
        self.assertEqual(sa.decode_bing_url(href), real)

    def test_bing_plain_unchanged(self):
        self.assertEqual(sa.decode_bing_url("https://a.example/"), "https://a.example/")

    def test_bing_bad_base64_returns_href(self):
        href = "https://www.bing.com/ck/a?u=a1%%%"
        self.assertEqual(sa.decode_bing_url(href), href)

    def test_ddg_redirect(self):
        href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fx&rut=1"
        self.assertEqual(sa.decode_ddg_url(href), "https://example.com/x")

    def test_ddg_plain_unchanged(self):
        self.assertEqual(sa.decode_ddg_url("https://a.example/"), "https://a.example/")

    def test_google_url(self):
        self.assertEqual(
            sa.decode_google_url("/url?q=https%3A%2F%2Fexample.com%2Fg&sa=U"),
            "https://example.com/g")

    def test_google_absolute(self):
        self.assertEqual(sa.decode_google_url("https://a.example/"), "https://a.example/")

    def test_google_relative(self):
        self.assertEqual(sa.decode_google_url("/search"),
                         "https://www.google.com/search")


class TestWebParsers(unittest.TestCase):
    def test_bing(self):
        results = sa._parse_bing(bing_page(), limit=10)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].url, "https://example.com/page")
        self.assertEqual(results[0].title, "Example & Co")
        self.assertEqual(results[0].snippet, "A nice page about things.")
        self.assertEqual(results[0].engine, "bing")

    def test_bing_limit(self):
        self.assertEqual(len(sa._parse_bing(bing_page(), limit=1)), 1)

    def test_bing_empty(self):
        self.assertEqual(sa._parse_bing("<html></html>", limit=5), [])

    def test_ddg(self):
        results = sa._parse_duckduckgo(ddg_page(), limit=10)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/ddg")
        self.assertEqual(results[0].title, "DDG Result")
        self.assertEqual(results[0].snippet, "Snippet here")

    def test_google(self):
        results = sa._parse_google(google_page(), limit=10)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/g")
        self.assertEqual(results[0].title, "Google Result")

    def test_google_empty(self):
        self.assertEqual(sa._parse_google("<html></html>", limit=5), [])


class TestCodeHostParsers(unittest.TestCase):
    def test_github_html(self):
        results = sa._parse_github_html(github_page(), limit=10)
        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertEqual(item.title, "alice/awesome-scraper")
        self.assertEqual(item.url, "https://github.com/alice/awesome-scraper")
        self.assertEqual(item.snippet, "A web scraper")
        self.assertEqual(item.meta["stars"], 1234)
        self.assertEqual(item.source, "github")

    def test_github_html_limit(self):
        self.assertEqual(len(sa._parse_github_html(github_page(), limit=0)), 0)

    def test_github_api(self):
        payload = {"items": [{
            "full_name": "bob/tool", "html_url": "https://github.com/bob/tool",
            "description": "desc", "stargazers_count": 42, "language": "Python"}]}
        with mock.patch.object(sa, "fetch", return_value=json.dumps(payload)):
            results = sa.search_github("tool", use_api=True)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "bob/tool")
        self.assertEqual(results[0].meta["stars"], 42)

    def test_gitlab_api(self):
        payload = [{"path_with_namespace": "grp/proj",
                    "web_url": "https://gitlab.com/grp/proj",
                    "description": "gd", "star_count": 7, "language": None}]
        with mock.patch.object(sa, "fetch", return_value=json.dumps(payload)):
            results = sa.search_gitlab("proj")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "grp/proj")
        self.assertEqual(results[0].url, "https://gitlab.com/grp/proj")
        self.assertEqual(results[0].meta["stars"], 7)
        self.assertEqual(results[0].source, "gitlab")

    def test_gitlab_non_list_is_empty(self):
        with mock.patch.object(sa, "fetch", return_value='{"message": "nope"}'):
            self.assertEqual(sa.search_gitlab("x"), [])


class TestAggregation(unittest.TestCase):
    def _result(self, url, engine):
        return sa.SearchResult("t", url, "", engine, "web")

    def test_dedupe(self):
        results = sa._dedupe([
            self._result("https://a.example/", "bing"),
            self._result("https://a.example", "google"),
            self._result("https://b.example/", "bing"),
        ])
        self.assertEqual([r.url for r in results],
                         ["https://a.example/", "https://b.example/"])

    def test_search_web_aggregates_and_reports_errors(self):
        def fake(query, limit=10, timeout=15.0):
            return [self._result("https://a.example/", "bing")]

        def boom(query, limit=10, timeout=15.0):
            raise sa.FetchError("down")

        with mock.patch.dict(sa.WEB_ENGINES, {
                "bing": fake, "google": boom, "duckduckgo": fake}):
            resp = sa.search_web("q", engines=["bing", "google", "duckduckgo", "yahoo"])
        self.assertTrue(resp.ok)
        self.assertEqual(len(resp.results), 1)          # deduped
        self.assertIn("google", resp.errors)
        self.assertIn("yahoo", resp.errors)
        self.assertEqual(resp.errors["yahoo"], "unknown engine 'yahoo'")

    def test_search_scripts_uses_both_hosts(self):
        with mock.patch.object(sa, "search_github",
                               return_value=[self._result("https://gh.example/", "github")]), \
             mock.patch.object(sa, "search_gitlab",
                               side_effect=sa.BlockedError("limited")):
            resp = sa.search_scripts("q")
        self.assertEqual(len(resp.results), 1)
        self.assertIn("gitlab", resp.errors)


class TestFetch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _Server()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def test_ok(self):
        self.assertEqual(sa.fetch(self.server.base + "/ok"), "<html><body>hi</body></html>")

    def test_403_is_blocked(self):
        with self.assertRaises(sa.BlockedError):
            sa.fetch(self.server.base + "/403")

    def test_500_is_fetch_error(self):
        with self.assertRaises(sa.FetchError):
            sa.fetch(self.server.base + "/500")

    def test_404_is_fetch_error(self):
        with self.assertRaises(sa.FetchError):
            sa.fetch(self.server.base + "/missing")


class TestScriptGeneration(unittest.TestCase):
    def setUp(self):
        self.results = [
            sa.SearchResult("A", "https://a.example/x", "snip", "bing", "web"),
            sa.SearchResult("B", "https://b.example/y", "", "github", "github"),
        ]

    def test_render_contains_query_and_urls(self):
        text = sa.render_script("my query", self.results)
        self.assertIn("query: my query", text)
        self.assertIn("https://a.example/x", text)
        self.assertIn("https://b.example/y", text)
        self.assertIn("# [bing] A", text)
        self.assertTrue(text.startswith("#!/usr/bin/env bash"))

    def test_render_quotes_shell(self):
        text = sa.render_script("it's", self.results)
        self.assertIn(shlex.quote("it's"), text)

    def test_write_script_is_executable(self):
        path = os.path.join(tempfile.mkdtemp(), "out.sh")
        sa.write_script(path, "q", self.results)
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(os.access(path, os.X_OK))
        with open(path) as handle:
            self.assertIn("https://a.example/x", handle.read())


class TestParser(unittest.TestCase):
    def test_defaults(self):
        args = sa.build_parser().parse_args(["web", "hello"])
        self.assertEqual(args.query, "hello")
        self.assertEqual(args.limit, 10)
        self.assertFalse(args.as_json)
        self.assertEqual(args.engines, "bing,duckduckgo,google")

    def test_github_defaults(self):
        args = sa.build_parser().parse_args(["github", "tool"])
        self.assertFalse(args.api)
        self.assertIsNone(args.emit_script)

    def test_emit_script_option(self):
        args = sa.build_parser().parse_args(
            ["scripts", "tool", "--emit-script", "x.sh", "--json"])
        self.assertEqual(args.emit_script, "x.sh")
        self.assertTrue(args.as_json)


class TestMain(unittest.TestCase):
    def test_web_json(self):
        response = sa.SearchResponse(
            [sa.SearchResult("T", "https://x.example/", "", "bing", "web")], {})
        with mock.patch.object(sa, "search_web", return_value=response) as called, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            code = sa.main(["web", "q", "--json", "--engines", "bing"])
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["results"][0]["url"], "https://x.example/")
        called.assert_called_once()

    def test_no_results_returns_1(self):
        with mock.patch.object(sa, "search_web",
                               return_value=sa.SearchResponse([], {"bing": "down"})), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            code = sa.main(["web", "q"])
        self.assertEqual(code, 1)

    def test_emit_script(self):
        response = sa.SearchResponse(
            [sa.SearchResult("T", "https://x.example/", "", "bing", "web")], {})
        path = os.path.join(tempfile.mkdtemp(), "gen.sh")
        with mock.patch.object(sa, "search_github", return_value=response.results), \
             mock.patch.object(sa, "search_gitlab", return_value=[]), \
             contextlib.redirect_stdout(io.StringIO()):
            code = sa.main(["scripts", "q", "--emit-script", path])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(path))


@unittest.skipUnless(os.environ.get("SEARCH_AGENT_LIVE"), "live network test disabled")
class TestLive(unittest.TestCase):
    def test_bing_live(self):
        results = sa.search_bing("python web scraper", limit=3)
        self.assertTrue(results)

    def test_gitlab_live(self):
        results = sa.search_gitlab("web scraper", limit=3)
        self.assertTrue(results)


if __name__ == "__main__":
    unittest.main(verbosity=2)
