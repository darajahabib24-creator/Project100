#!/usr/bin/env python3
"""Tests for agent.py.

Everything runs offline: search and fetch are stubbed. Live network behaviour
of the underlying search module is covered by tests/test_search_agent.py.
"""

import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as ag  # noqa: E402
import search_agent as sa  # noqa: E402


def web_response(urls):
    return sa.SearchResponse(
        [sa.SearchResult(f"Title {index}", url, f"snippet {index}", "bing", "web")
         for index, url in enumerate(urls)], {})


# --------------------------------------------------------------------------- #
# text helpers
# --------------------------------------------------------------------------- #

class TestTextHelpers(unittest.TestCase):
    def test_html_to_text_strips_markup(self):
        raw = ("<html><head><title>T</title><style>x{}</style></head>"
               "<body><p>Hello &amp; <b>world</b></p>"
               "<script>bad()</script></body></html>")
        text = ag.html_to_text(raw)
        self.assertIn("Hello & world", text)
        self.assertNotIn("bad()", text)
        self.assertNotIn("x{}", text)

    def test_page_title(self):
        self.assertEqual(ag.page_title("<title>  My &amp; Page </title>"),
                         "My & Page")

    def test_keywords_drop_stopwords(self):
        words = ag.keywords("the quick brown fox jumps over the lazy dog")
        self.assertEqual(words[:4], ["quick", "brown", "fox", "jumps"])

    def test_stem_matches_variants(self):
        self.assertEqual(ag._stem("downloads"), "download")
        self.assertEqual(ag._stem("downloaded"), "download")
        self.assertEqual(ag._stem("finds"), "find")
        self.assertEqual(ag._stem("files"), "file")

    def test_extract_code_blocks(self):
        raw = ("<p>text</p><pre><code>import os\nprint(os.getcwd())</code></pre>"
               "<pre>short</pre><code>x = 1</code>")
        blocks = ag.extract_code_blocks(raw)
        self.assertTrue(any("import os" in block for block in blocks))
        self.assertFalse(any(block.strip() == "short" for block in blocks))

    def test_extract_raw_links(self):
        raw = ('<a href="/o/r/blob/main/a.py">a</a>'
               '<a href="/o/r/blob/main/sub/b.py">b</a>'
               '<a href="/o/r/blob/main/a.py">a</a>')
        self.assertEqual(
            ag.extract_raw_links(raw, "https://github.com/o/r"),
            ["https://raw.githubusercontent.com/o/r/main/a.py",
             "https://raw.githubusercontent.com/o/r/main/sub/b.py"])

    def test_extract_raw_links_non_github(self):
        self.assertEqual(
            ag.extract_raw_links('<a href="/x">', "https://example.com"), [])

    def test_strip_verbs(self):
        self.assertEqual(ag.strip_verbs("please search for cats"), "cats")
        self.assertEqual(ag.strip_verbs("create me a python object"),
                         "python object")
        self.assertEqual(ag.strip_verbs("findings show growth"),
                         "findings show growth")

    def test_code_query_variant(self):
        self.assertEqual(
            ag._code_query_variant("bash script that finds files larger than 1MB"),
            "finds files larger 1MB")
        self.assertEqual(ag._code_query_variant("python script"), "")


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #

class TestTools(unittest.TestCase):
    def test_web_search(self):
        with mock.patch.object(sa, "search_web",
                               return_value=web_response(["https://a/"])) as called:
            result = ag.tool_web_search("hello")
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["url"], "https://a/")
        called.assert_called_once()

    def test_scripts_search(self):
        with mock.patch.object(sa, "search_scripts",
                               return_value=web_response(["https://gh/"])):
            result = ag.tool_scripts_search("hello")
        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["url"], "https://gh/")

    def test_fetch_page(self):
        with mock.patch.object(sa, "fetch", return_value=(
                "<html><title>Hi</title><body><p>Body &amp; text</p>"
                "<pre>import os\nprint(os.getcwd())</pre></body></html>")):
            result = ag.tool_fetch_page("example.com/x")
        self.assertTrue(result["ok"])
        self.assertEqual(result["url"], "https://example.com/x")
        self.assertEqual(result["title"], "Hi")
        self.assertIn("Body & text", result["text"])
        self.assertTrue(any("import os" in block for block in result["code"]))

    def test_fetch_page_raw_keeps_body(self):
        with mock.patch.object(sa, "fetch", return_value="a < b and c > d"):
            result = ag.tool_fetch_page("https://x/raw", raw=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "a < b and c > d")

    def test_fetch_page_blocked(self):
        with mock.patch.object(sa, "fetch", side_effect=sa.BlockedError("403")):
            result = ag.tool_fetch_page("https://x.example/")
        self.assertFalse(result["ok"])
        self.assertIn("403", result["error"])


# --------------------------------------------------------------------------- #
# agent
# --------------------------------------------------------------------------- #

class TestAgent(unittest.TestCase):
    def setUp(self):
        self.agent = ag.Agent(ag.build_tools(), trace=False)

    def test_run_prints_only_code(self):
        code = ("def scrape(url):\n"
                "    import requests\n"
                "    return requests.get(url).text\n")
        html = ("<html><head><title>T</title></head><body>"
                f"<pre>{code}</pre></body></html>")
        with mock.patch.object(sa, "search_web",
                               return_value=web_response(["https://a.example/"])), \
             mock.patch.object(sa, "fetch", return_value=html), \
             mock.patch.object(sa, "search_scripts",
                               return_value=sa.SearchResponse([], {})):
            answer = self.agent.run("create me a python script that downloads it")
        self.assertEqual(answer, code.strip("\n"))
        self.assertNotIn("http", answer)

    def test_run_falls_back_to_scripts_search(self):
        code = ("class Parser:\n"
                "    def parse(self, text):\n"
                "        return text.split()\n")
        html = f"<html><body><pre>{code}</pre></body></html>"
        with mock.patch.object(sa, "search_web",
                               return_value=sa.SearchResponse([], {})), \
             mock.patch.object(sa, "search_scripts",
                               return_value=web_response(["https://github.com/o/r"])), \
             mock.patch.object(sa, "fetch", return_value=html):
            answer = self.agent.run("create me a python class that splits text")
        self.assertEqual(answer, code.strip("\n"))

    def test_run_follows_raw_source_file(self):
        repo_html = ('<html><body><a href="/o/r/blob/main/app.py">app</a>'
                     '</body></html>')
        source = "import os\n\ndef main():\n    print(os.getcwd())\n"

        def fake_fetch(url, timeout=15.0):
            return source if "raw.githubusercontent" in url else repo_html

        with mock.patch.object(sa, "search_web",
                               return_value=web_response(["https://github.com/o/r"])), \
             mock.patch.object(sa, "fetch", side_effect=fake_fetch), \
             mock.patch.object(sa, "search_scripts",
                               return_value=sa.SearchResponse([], {})):
            answer = self.agent.run("create me a python script that prints the cwd")
        self.assertEqual(answer, source)

    def test_run_no_results_returns_empty(self):
        with mock.patch.object(sa, "search_web",
                               return_value=sa.SearchResponse([], {})), \
             mock.patch.object(sa, "search_scripts",
                               return_value=sa.SearchResponse([], {})):
            self.assertEqual(self.agent.run("create me something"), "")

    def test_score_code_prefers_keyword_hits(self):
        relevant = "def download(url):\n    return url\n"
        irrelevant = "def sum(a, b):\n    return a + b\n"
        wanted = {"download"}
        self.assertGreater(ag.Agent._score_code(relevant, wanted),
                           ag.Agent._score_code(irrelevant, wanted))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

class TestCLI(unittest.TestCase):
    def test_one_shot_prints_only_code(self):
        code = "class Parser:\n    def parse(self, text):\n        return text.split()\n"
        html = f"<html><body><pre>{code}</pre></body></html>"
        with mock.patch.object(sa, "search_web",
                               return_value=web_response(["https://a.example/"])), \
             mock.patch.object(sa, "fetch", return_value=html), \
             mock.patch.object(sa, "search_scripts",
                               return_value=sa.SearchResponse([], {})), \
             contextlib.redirect_stdout(io.StringIO()) as out, \
             contextlib.redirect_stderr(io.StringIO()):
            rc = ag.main(["a python class that splits text"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), code.strip("\n"))

    def test_repl_exit(self):
        with contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()), \
             mock.patch("builtins.input", side_effect=["/exit"]):
            self.assertEqual(ag.main([]), 0)

    def test_repl_help(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, \
             contextlib.redirect_stderr(io.StringIO()), \
             mock.patch("builtins.input", side_effect=["/help", "/exit"]):
            ag.main([])
        self.assertIn("create me a python script", out.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
