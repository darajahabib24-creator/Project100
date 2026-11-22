#!/usr/bin/env python3
"""Tests for agent.py.

Everything runs offline: search and fetch are stubbed, tools run in temp
directories, and the optional Ollama brain is exercised against a tiny local
HTTP server.  Live network behaviour of the underlying search module is
covered by tests/test_search_agent.py.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
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

    def test_extract_answer_prefers_keyword_sentence(self):
        text = ("Python is a language. The Eiffel Tower is a wrought iron "
                "tower on the Champ de Mars in Paris. It is named after "
                "Gustave Eiffel.")
        answer = ag.extract_answer(text, "where is the eiffel tower")
        self.assertIn("Eiffel Tower", answer)
        self.assertNotIn("Python is a language", answer)

    def test_extract_answer_skips_boilerplate(self):
        text = ("Jump to content Main menu Navigation page contents events "
                "random article. Paris is the capital of France. "
                "Coordinates and other tables follow here.")
        answer = ag.extract_answer(text, "capital of France")
        self.assertIn("Paris is the capital of France", answer)

    def test_extract_answer_empty_text(self):
        self.assertEqual(ag.extract_answer("", "anything"), "")

    def test_strip_verbs(self):
        self.assertEqual(ag.strip_verbs("please search for cats"), "cats")
        self.assertEqual(ag.strip_verbs("can you find a repo"), "find a repo")
        self.assertEqual(ag.strip_verbs("research the french revolution"),
                         "the french revolution")
        self.assertEqual(ag.strip_verbs("findings show growth"),
                         "findings show growth")

    def test_is_question(self):
        self.assertTrue(ag.is_question("what is the capital of France?"))
        self.assertTrue(ag.is_question("Who wrote Hamlet"))
        self.assertFalse(ag.is_question("search for hamlet"))

    def test_reformulate_query(self):
        self.assertEqual(ag.reformulate_query("what is the capital of france"),
                         "capital france")


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #

class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.registry = ag.build_registry(Path(tempfile.mkdtemp()) / "mem.json")

    def test_coercion(self):
        self.assertEqual(ag._coerce("n", "5", "integer"), 5)
        self.assertEqual(ag._coerce("xs", "a, b", "array"), ["a", "b"])
        self.assertIs(ag._coerce("b", "false", "boolean"), False)
        self.assertIs(ag._coerce("b", "yes", "boolean"), True)
        with self.assertRaises(ag.ToolError):
            ag._coerce("n", "abc", "integer")

    def test_unknown_tool(self):
        with self.assertRaises(ag.ToolError):
            self.registry.spec("nope")

    def test_unknown_and_missing_args(self):
        with self.assertRaises(ag.ToolError):
            self.registry.call("web_search", {"query": "x", "bogus": 1})
        with self.assertRaises(ag.ToolError):
            self.registry.call("web_search", {})

    def test_tool_failure_is_ok_false(self):
        result = self.registry.call("read_file", {"path": "/definitely/missing"})
        self.assertFalse(result["ok"])
        self.assertIn("error", result)

    def test_definitions_shape(self):
        defs = self.registry.definitions()
        names = {item["function"]["name"] for item in defs}
        self.assertIn("web_search", names)
        self.assertIn("shell", names)


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #

class TestTools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.memory = os.path.join(self.tmp, "memory.json")
        self.registry = ag.build_registry(Path(self.memory))

    def test_write_and_read_file(self):
        path = os.path.join(self.tmp, "sub", "out.txt")
        written = self.registry.call("write_file",
                                     {"path": path, "content": "hello\nworld\n"})
        self.assertTrue(written["ok"])
        read = self.registry.call("read_file", {"path": path})
        self.assertTrue(read["ok"])
        self.assertEqual(read["content"], "hello\nworld\n")
        self.assertEqual(read["total_lines"], 2)

    def test_read_file_offset(self):
        path = os.path.join(self.tmp, "lines.txt")
        with open(path, "w") as handle:
            handle.write("a\nb\nc\n")
        read = self.registry.call("read_file", {"path": path, "offset": 2})
        self.assertEqual(read["content"], "b\nc\n")

    def test_list_files(self):
        with open(os.path.join(self.tmp, "b.txt"), "w") as handle:
            handle.write("x")
        os.mkdir(os.path.join(self.tmp, "dir"))
        result = self.registry.call("list_files", {"path": self.tmp})
        self.assertTrue(result["ok"])
        names = {entry["name"] for entry in result["entries"]}
        self.assertEqual(names, {"b.txt", "dir"})
        filtered = self.registry.call("list_files",
                                      {"path": self.tmp, "pattern": r"\.txt$"})
        self.assertEqual([e["name"] for e in filtered["entries"]], ["b.txt"])

    def test_python(self):
        result = self.registry.call("python", {"code": "print(6 * 7)"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["stdout"].strip(), "42")

    def test_python_error(self):
        result = self.registry.call("python", {"code": "raise SystemExit(3)"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["returncode"], 3)

    def test_shell(self):
        result = self.registry.call("shell", {"command": "echo agent-ok"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["stdout"].strip(), "agent-ok")

    def test_shell_failure(self):
        result = self.registry.call("shell", {"command": "exit 7"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["returncode"], 7)

    def test_remember_recall(self):
        self.registry.call("remember", {"text": "I like oolong tea"})
        self.registry.call("remember", {"text": "I live in Lisbon"})
        found = self.registry.call("recall", {"query": "tea"})
        self.assertTrue(found["ok"])
        self.assertIn("oolong", found["matches"][0])
        everything = self.registry.call("recall", {})
        self.assertEqual(everything["count"], 2)
        self.assertTrue(os.path.isfile(self.memory))

    def test_fetch_page(self):
        with mock.patch.object(sa, "fetch",
                               return_value="<html><title>Hi</title>"
                                           "<body><p>Body text</p></body></html>"):
            result = self.registry.call("fetch_page", {"url": "example.com/x"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["url"], "https://example.com/x")
        self.assertEqual(result["title"], "Hi")
        self.assertIn("Body text", result["text"])

    def test_fetch_page_blocked(self):
        with mock.patch.object(sa, "fetch", side_effect=sa.BlockedError("403")):
            result = self.registry.call("fetch_page", {"url": "https://x.example/"})
        self.assertFalse(result["ok"])
        self.assertIn("403", result["error"])


# --------------------------------------------------------------------------- #
# planner
# --------------------------------------------------------------------------- #

class TestRuleBasedPlanner(unittest.TestCase):
    def setUp(self):
        self.planner = ag.RuleBasedPlanner()

    def plan(self, goal):
        return self.planner.plan(goal, [])

    def tools_of(self, plan):
        return [step.tool for step in plan.steps]

    def test_bare_url(self):
        plan = self.plan("https://example.com/article")
        self.assertEqual(plan.mode, "steps")
        self.assertEqual(plan.steps[0].tool, "fetch_page")
        self.assertEqual(plan.steps[0].args["url"], "https://example.com/article")

    def test_search(self):
        plan = self.plan("search for python web scrapers")
        self.assertEqual(self.tools_of(plan), ["web_search"])
        self.assertIn("python web scrapers", plan.steps[0].args["query"])

    def test_question_is_research_pipeline(self):
        plan = self.plan("what is the capital of France?")
        self.assertEqual(plan.mode, "pipeline")
        self.assertEqual(plan.pipeline, "research")

    def test_code_search(self):
        plan = self.plan("find me a repo for web scraping")
        self.assertEqual(self.tools_of(plan), ["scripts_search"])

    def test_github_and_gitlab(self):
        self.assertEqual(self.tools_of(self.plan("github web scraper")),
                         ["github_search"])
        self.assertEqual(self.tools_of(self.plan("gitlab web scraper")),
                         ["gitlab_search"])

    def test_list_and_read(self):
        plan = self.plan("list files in /tmp")
        self.assertEqual(plan.steps[0].tool, "list_files")
        self.assertEqual(plan.steps[0].args["path"], "/tmp")
        plan = self.plan("read config.yaml")
        self.assertEqual(plan.steps[0].tool, "read_file")
        self.assertEqual(plan.steps[0].args["path"], "config.yaml")

    def test_shell(self):
        plan = self.plan("run ls -la")
        self.assertEqual(plan.steps[0].tool, "shell")
        self.assertEqual(plan.steps[0].args["command"], "ls -la")

    def test_python(self):
        plan = self.plan("calculate 12 * 12")
        self.assertEqual(plan.steps[0].tool, "python")

    def test_remember_and_recall(self):
        plan = self.plan("remember that I like tea")
        self.assertEqual(plan.steps[0].tool, "remember")
        self.assertEqual(plan.steps[0].args["text"], "I like tea")
        plan = self.plan("what did I say about tea")
        self.assertEqual(plan.steps[0].tool, "recall")
        self.assertEqual(plan.steps[0].args["query"], "tea")

    def test_save_results_pipeline(self):
        plan = self.plan("search for rust cli tools and save the results to out.txt")
        self.assertEqual(plan.mode, "pipeline")
        self.assertEqual(plan.pipeline, "save_results")
        self.assertEqual(plan.steps[0].args["query"], "rust cli tools")
        self.assertEqual(plan.steps[1].args["path"], "out.txt")

    def test_make_script_pipeline(self):
        plan = self.plan("write a script that searches for pdf tools to tools.sh")
        self.assertEqual(plan.mode, "pipeline")
        self.assertEqual(plan.pipeline, "make_search_script")
        self.assertEqual(plan.steps[0].args["query"], "pdf tools")
        self.assertEqual(plan.steps[1].args["path"], "tools.sh")

    def test_write_without_path_clarifies(self):
        plan = self.plan("write a poem about the sea")
        self.assertEqual(plan.mode, "clarify")
        self.assertTrue(plan.question)

    def test_fallback_is_search(self):
        plan = self.plan("tell me something interesting")
        self.assertEqual(plan.mode, "steps")
        self.assertEqual(plan.steps[0].tool, "web_search")

    def test_second_call_answers(self):
        obs = [ag.Observation(ag.Step("web_search", {"query": "x"}), True,
                              {"query": "x", "count": 1,
                               "results": [{"title": "T", "url": "https://x/",
                                            "snippet": "s"}]})]
        plan = self.planner.plan("x", obs)
        self.assertEqual(plan.mode, "answer")
        self.assertIn("https://x/", plan.text)


# --------------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------------- #

class TestPolicy(unittest.TestCase):
    def test_non_interactive_denies_mutating(self):
        policy = ag.Policy(assume_yes=False, interactive=False)
        spec = ag.ToolSpec("shell", "", {}, required=(), mutating=True)
        self.assertFalse(policy.approve(ag.Step("shell", {"command": "rm"}), spec))

    def test_assume_yes_allows(self):
        policy = ag.Policy(assume_yes=True, interactive=False)
        spec = ag.ToolSpec("shell", "", {}, required=(), mutating=True)
        self.assertTrue(policy.approve(ag.Step("shell"), spec))

    def test_readonly_always_allowed(self):
        policy = ag.Policy(assume_yes=False, interactive=False)
        spec = ag.ToolSpec("read_file", "", {}, required=("path",))
        self.assertTrue(policy.approve(ag.Step("read_file"), spec))


# --------------------------------------------------------------------------- #
# agent loop
# --------------------------------------------------------------------------- #

class TestAgentLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.registry = ag.build_registry(Path(self.tmp) / "mem.json")
        self.agent = ag.Agent(self.registry, ag.RuleBasedPlanner(),
                              policy=ag.Policy(assume_yes=True, interactive=False),
                              trace=False,
                              memory_path=Path(self.tmp) / "mem.json")

    def test_search_goal(self):
        with mock.patch.object(sa, "search_web",
                               return_value=web_response(["https://a.example/"])):
            answer = self.agent.run("search for python web scrapers")
        self.assertIn("https://a.example/", answer)

    def test_research_pipeline_fetches(self):
        html = ("<html><head><title>France</title></head><body>"
                "<p>Paris is the capital of France. It has many museums.</p>"
                "</body></html>")
        with mock.patch.object(sa, "search_web",
                               return_value=web_response(["https://a.example/"])), \
             mock.patch.object(sa, "fetch", return_value=html):
            answer = self.agent.run("what is the capital of France?")
        self.assertIn("Paris", answer)
        self.assertIn("https://a.example/", answer)

    def test_research_falls_back_to_results_when_fetch_blocked(self):
        with mock.patch.object(sa, "search_web",
                               return_value=web_response(["https://a.example/"])), \
             mock.patch.object(sa, "fetch",
                               side_effect=sa.BlockedError("403 blocked")):
            answer = self.agent.run("what is the capital of France?")
        self.assertIn("https://a.example/", answer)

    def test_research_no_results(self):
        with mock.patch.object(sa, "search_web",
                               return_value=sa.SearchResponse([], {})):
            answer = self.agent.run("what is the capital of France?")
        self.assertIn("could not find", answer.lower())

    def test_save_results_pipeline(self):
        out = os.path.join(self.tmp, "results.txt")
        with mock.patch.object(sa, "search_web",
                               return_value=web_response(["https://a.example/"])):
            answer = self.agent.run(f"search for rust tools and save the "
                                    f"results to {out}")
        self.assertIn("Saved", answer)
        with open(out) as handle:
            self.assertIn("https://a.example/", handle.read())

    def test_make_script_pipeline(self):
        out = os.path.join(self.tmp, "find.sh")
        with mock.patch.object(sa, "search_web",
                               return_value=web_response(["https://a.example/"])):
            answer = self.agent.run(f"write a script that searches for pdf "
                                    f"tools to {out}")
        self.assertIn("Created script", answer)
        self.assertIn(out, answer)
        with open(out) as handle:
            content = handle.read()
        self.assertTrue(content.startswith("#!/usr/bin/env bash"))
        self.assertIn("https://a.example/", content)

    def test_script_mode_hides_urls(self):
        out = os.path.join(self.tmp, "search.sh")
        agent = ag.Agent(self.registry, ag.RuleBasedPlanner(),
                         policy=ag.Policy(assume_yes=True, interactive=False),
                         trace=False, memory_path=Path(self.tmp) / "mem.json",
                         script_path=Path(out))
        with mock.patch.object(sa, "search_web",
                               return_value=web_response(["https://a.example/"])):
            answer = agent.run("search for python web scrapers")
        self.assertEqual(answer, f"Created script: {out}")
        self.assertNotIn("http", answer)
        with open(out) as handle:
            self.assertIn("https://a.example/", handle.read())

    def test_fetch_extract_pipeline(self):
        html = ("<html><head><title>Doc</title></head><body>"
                "<p>Example domain page body.</p></body></html>")
        with mock.patch.object(sa, "fetch", return_value=html):
            answer = self.agent.run("summarize https://example.com")
        self.assertIn("example.com", answer)

    def test_shell_denied_by_policy(self):
        agent = ag.Agent(self.registry, ag.RuleBasedPlanner(),
                         policy=ag.Policy(assume_yes=False, interactive=False),
                         trace=False)
        answer = agent.run("run echo hello")
        self.assertIn("denied", answer)

    def test_remember_goal(self):
        answer = self.agent.run("remember that I like tea")
        self.assertIn("Noted", answer)
        found = self.registry.call("recall", {"query": "tea"})
        self.assertIn("I like tea", found["matches"][0])


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

class TestCLI(unittest.TestCase):
    def test_tools_listing(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = ag.main(["--tools"])
        self.assertEqual(code, 0)
        self.assertIn("web_search", out.getvalue())
        self.assertIn("shell", out.getvalue())

    def test_one_shot_json(self):
        with mock.patch.object(sa, "search_web",
                               return_value=web_response(["https://a.example/"])), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            code = ag.main(["search for x", "--json", "--no-trace"])
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertIn("https://a.example/", data["answer"])
        self.assertEqual(data["brain"], "rules")

    def test_script_option(self):
        path = os.path.join(tempfile.mkdtemp(), "out.sh")
        with mock.patch.object(sa, "search_web",
                               return_value=web_response(["https://a.example/"])), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            code = ag.main(["search for x", "--script", path, "--no-trace"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), f"Created script: {path}")
        self.assertTrue(os.path.isfile(path))

    def test_one_shot_repl_exit(self):
        with contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            with mock.patch("builtins.input", side_effect=["/exit"]):
                code = ag.main([])
        self.assertEqual(code, 0)

    def test_repl_help(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, \
             contextlib.redirect_stderr(io.StringIO()):
            with mock.patch("builtins.input", side_effect=["/help", "/exit"]):
                ag.main([])
        self.assertIn("/tools", out.getvalue())


# --------------------------------------------------------------------------- #
# optional local-model brain
# --------------------------------------------------------------------------- #

class _OllamaHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        if self.path == "/api/tags":
            body, status = json.dumps({"models": []}).encode(), 200
        else:
            body, status = b"{}", 404
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps({"message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": "web_search",
                                         "arguments": {"query": "x"}}}]}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: N802 - http.server API
        pass


class _Server:
    def __init__(self, handler=_OllamaHandler):
        self.httpd = HTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


class TestOllamaBrain(unittest.TestCase):
    def setUp(self):
        self.server = _Server()
        self.brain = ag.OllamaBrain(self.server.base, model="test-model")
        self.registry = ag.build_registry(Path(tempfile.mkdtemp()) / "m.json")

    def tearDown(self):
        self.server.close()

    def test_available(self):
        self.assertTrue(self.brain.available())

    def test_unavailable(self):
        brain = ag.OllamaBrain("http://127.0.0.1:1", timeout=1.0)
        self.assertFalse(brain.available())

    def test_model_planner_parses_tool_call(self):
        planner = ag.ModelPlanner(self.brain, self.registry)
        plan = planner.plan("find x", [])
        self.assertEqual(plan.mode, "steps")
        self.assertEqual(plan.steps[0].tool, "web_search")
        self.assertEqual(plan.steps[0].args, {"query": "x"})

    def test_model_planner_ignores_unknown_tool(self):
        class Handler(_OllamaHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                body = json.dumps({"message": {
                    "role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "bogus",
                                                 "arguments": "{}"}}]}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

        server = _Server(Handler)
        try:
            planner = ag.ModelPlanner(ag.OllamaBrain(server.base), self.registry)
            plan = planner.plan("x", [])
            self.assertEqual(plan.mode, "answer")
        finally:
            server.close()

    def test_model_planner_final_answer(self):
        class Handler(_OllamaHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                body = json.dumps({"message": {"role": "assistant",
                                               "content": "all done"}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

        server = _Server(Handler)
        try:
            planner = ag.ModelPlanner(ag.OllamaBrain(server.base), self.registry)
            plan = planner.plan("x", [])
            self.assertEqual(plan.mode, "answer")
            self.assertEqual(plan.text, "all done")
        finally:
            server.close()

    def test_model_planner_connection_error(self):
        planner = ag.ModelPlanner(ag.OllamaBrain("http://127.0.0.1:1", timeout=1.0),
                                  self.registry)
        plan = planner.plan("x", [])
        self.assertEqual(plan.mode, "answer")
        self.assertIn("error", plan.text.lower())

    def test_model_cli_requires_server(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                ag.main(["--model", "ollama", "--ollama-base",
                         "http://127.0.0.1:1", "x"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
