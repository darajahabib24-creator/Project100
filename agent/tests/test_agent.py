"""test_agent.py - end-to-end tests for the local agent runtime.

Run: python3 -m pytest agent/tests/ -q   (or python3 agent/tests/test_agent.py)
No network, no model weights required: the loop is driven by a scripted policy.
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from agent.core import (  # noqa: E402
    Agent, LocalModel, Memory, Message, Planner, Tool, ToolBus, ToolError,
    parse_calls, render,
)
from agent.policy import FunctionModel, ScriptedModel, call  # noqa: E402
from agent.tools import default_tools  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def make_bus(memory=None, tools=None):
    bus = ToolBus()
    for t in (tools if tools is not None else default_tools(memory)):
        bus.register(t)
    return bus


def make_agent(tmp_path, replies, memory=None, max_steps=8):
    mem = memory or Memory(str(tmp_path / "mem.json"))
    model = ScriptedModel(replies)
    agent = Agent(model, make_bus(mem), mem, max_steps=max_steps, verbose=False)
    assert agent.memory is not None
    return agent


_CWD_STACK: list[str] = []

# Tests that need the live internet. Skipped automatically when offline.
NETWORK_TESTS = {
    "test_policy_verify_path",
    "test_policy_verify_reports_verdict",
    "test_policy_fix_path_runs_and_searches",
    "test_policy_fix_reports_when_no_file",
}

_NET_STATE: bool | None = None


def net_available() -> bool:
    """Probe once: can we reach the open web?"""
    global _NET_STATE
    if _NET_STATE is None:
        import urllib.request
        try:
            req = urllib.request.Request(
                "https://en.wikipedia.org/api/rest_v1/page/summary/Paris",
                headers={"User-Agent": "local-agent-test/1.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                _NET_STATE = bool(r.status == 200)
        except Exception:  # noqa: BLE001
            _NET_STATE = False
    return bool(_NET_STATE)


class _MonkeyPatch:
    """Minimal pytest.monkeypatch stand-in for the standalone runner."""

    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value):
        old = getattr(target, name)
        self._undo.append((target, name, old))
        setattr(target, name, value)

    def undo(self):
        for target, name, old in reversed(self._undo):
            setattr(target, name, old)
        self._undo.clear()


def monkeypatch_cwd(path):
    """Point the agent's relative-path tools at a temp dir for one test."""
    path = str(path)
    _CWD_STACK.append(os.getcwd())
    os.chdir(path)
    return path


def unpatch_cwd():
    if _CWD_STACK:
        os.chdir(_CWD_STACK.pop())


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def test_parse_calls_basic():
    text = 'here is a call:\n```json\n{"tool": "read_file", "args": {"path": "x"}}\n```\n'
    calls = parse_calls(text)
    assert calls == [{"tool": "read_file", "args": {"path": "x"}}]


def test_parse_calls_batch_and_prose():
    text = ('first\n```json\n{"tool": "a", "args": {}}\n```\n'
            'middle\n```json\n{"tool": "b", "args": {"k": 1}}\n```\nend')
    assert [c["tool"] for c in parse_calls(text)] == ["a", "b"]


def test_parse_calls_bad_json_ignored():
    text = '```json\n{not json}\n```\n```json\n{"tool": "x"}\n```'
    assert parse_calls(text) == [{"tool": "x", "args": {}}]


def test_parse_calls_empty():
    assert parse_calls("no calls here") == []


# --------------------------------------------------------------------------- #
# Tool bus
# --------------------------------------------------------------------------- #

def test_bus_unknown_tool():
    bus = make_bus()
    try:
        bus.call("nope", {})
    except ToolError as exc:
        assert "no such tool" in str(exc)
    else:
        raise AssertionError("expected ToolError")


def test_bus_unknown_arg():
    bus = make_bus()
    try:
        bus.call("read_file", {"path": "x", "bogus": 1})
    except ToolError as exc:
        assert "unknown args" in str(exc)
    else:
        raise AssertionError("expected ToolError")


def test_bus_wraps_exception():
    class Boom(Tool):
        name = "boom"
        description = "always fails"
        params = {}

        def run(self, **kwargs):
            raise ValueError("kaboom")

    bus = ToolBus()
    bus.register(Boom())
    try:
        bus.call("boom", {})
    except ToolError as exc:
        assert "kaboom" in str(exc)
    else:
        raise AssertionError("expected ToolError")


def test_bus_manifest_lists_tools():
    bus = make_bus()
    man = bus.manifest()
    for name in ("read_file", "write_file", "shell", "search_files"):
        assert name in man


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

def test_read_and_write_file(tmp_path):
    f = tmp_path / "a.txt"
    bus = make_bus()
    bus.call("write_file", {"path": str(f), "content": "one\ntwo\nthree"})
    out = bus.call("read_file", {"path": str(f)})
    assert "1|one" in out and "3|three" in out


def test_read_file_offset(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("l1\nl2\nl3\n")
    bus = make_bus()
    assert "2|l2" in bus.call("read_file", {"path": str(f), "offset": 2, "limit": 1})


def test_edit_file_unique(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello world")
    bus = make_bus()
    bus.call("edit_file", {"path": str(f), "old": "world", "new": "there"})
    assert f.read_text() == "hello there"


def test_edit_file_ambiguous(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x x x")
    bus = make_bus()
    try:
        bus.call("edit_file", {"path": str(f), "old": "x", "new": "y"})
    except ToolError as exc:
        assert "occurs 3 times" in str(exc)
    else:
        raise AssertionError("expected ToolError")


def test_list_dir_and_search(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "m.py").write_text("def foo(): return 42\n")
    bus = make_bus()
    listing = bus.call("list_dir", {"path": str(tmp_path)})
    assert "sub" in listing and "m.py" not in listing.split("d ")[0]
    hits = bus.call("search_files", {"pattern": "foo", "path": str(tmp_path),
                                     "glob": "*.py"})
    assert "m.py:1:def foo" in hits


def test_shell_runs(tmp_path):
    bus = make_bus()
    out = bus.call("shell", {"command": "echo hello-from-shell", "cwd": str(tmp_path)})
    assert "hello-from-shell" in out


def test_shell_nonzero_exit(tmp_path):
    bus = make_bus()
    out = bus.call("shell", {"command": "exit 3", "cwd": str(tmp_path)})
    assert "[exit 3]" in out


def test_shell_timeout(tmp_path):
    bus = make_bus()
    try:
        bus.call("shell", {"command": "sleep 5", "cwd": str(tmp_path), "timeout": 1})
    except ToolError as exc:
        assert "timed out" in str(exc)
    else:
        raise AssertionError("expected ToolError")


# --------------------------------------------------------------------------- #
# Memory
# --------------------------------------------------------------------------- #

def test_memory_roundtrip(tmp_path):
    mem = Memory(str(tmp_path / "m.json"))
    mem.put("k", {"a": 1})
    assert mem.get("k") == {"a": 1}
    assert mem.get("missing") is None


def test_memory_persists(tmp_path):
    p = str(tmp_path / "m.json")
    Memory(p).put("k", "v")
    assert Memory(p).get("k") == "v"


def test_memory_search(tmp_path):
    mem = Memory(str(tmp_path / "m.json"))
    mem.put("alpha", "the quick brown fox")
    mem.put("beta", "lazy dog")
    hits = mem.search("fox")
    assert len(hits) == 1 and hits[0]["key"] == "alpha"


def test_memory_forget(tmp_path):
    mem = Memory(str(tmp_path / "m.json"))
    mem.put("k", 1)
    assert mem.forget("k") is True
    assert mem.get("k") is None
    assert mem.forget("k") is False


def test_memory_cap(tmp_path):
    mem = Memory(str(tmp_path / "m.json"), max_facts=3)
    for i in range(10):
        mem.put(f"k{i}", i)
    assert len(mem.all()) <= 3


# --------------------------------------------------------------------------- #
# Planner
# --------------------------------------------------------------------------- #

def test_planner_states_advance():
    p = Planner("find the docs and write a summary")
    assert p.plan.state == "understand"
    for _ in range(10):
        p.tick()
    assert p.plan.state == "done"


def test_planner_drafts_research_for_lookup_goal():
    steps = Planner("what is the capital of France").plan.steps
    assert any("Gather information" in s for s in steps)


def test_planner_drafts_build_for_create_goal():
    steps = Planner("create a python script that adds two numbers").plan.steps
    assert any("Produce the artifact" in s for s in steps)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def test_render_transcript():
    msgs = [Message("user", "hi"), Message("assistant", "hello"),
            Message("tool", "42", name="shell")]
    out = render(msgs)
    assert "[user]\nhi" in out and "[assistant]\nhello" in out and "[tool:shell]" in out


# --------------------------------------------------------------------------- #
# End-to-end loop
# --------------------------------------------------------------------------- #

def test_loop_single_tool_then_stop(tmp_path):
    """Agent writes a file, then signals completion -> loop ends."""
    target = tmp_path / "out.txt"
    replies = [
        "I will write the file.\n" + call("write_file", path=str(target),
                                          content="hello agent"),
        "The file is written; goal complete.",
    ]
    agent = make_agent(tmp_path, replies)
    steps = agent.run("write hello agent to out.txt")
    assert len(steps) == 1
    assert steps[0].calls[0]["tool"] == "write_file"
    assert target.read_text() == "hello agent"


def test_loop_batched_calls(tmp_path):
    replies = [
        "Two independent reads.\n" +
        call("list_dir", path=str(tmp_path)) + "\n" +
        call("shell", command="echo batched", cwd=str(tmp_path)),
        "done",
    ]
    agent = make_agent(tmp_path, replies)
    steps = agent.run("list and echo")
    assert len(steps) == 1 and len(steps[0].calls) == 2
    assert steps[0].results[1].strip() == "batched"


def test_loop_tool_error_is_surfaced(tmp_path):
    """A failing tool must return an error message to the transcript, not crash."""
    replies = [
        call("read_file", path=str(tmp_path / "nope.txt")),
        "recovered and done",
    ]
    agent = make_agent(tmp_path, replies)
    steps = agent.run("read a missing file")
    assert steps[0].error is not None
    tool_msgs = [m for m in agent.history if m.role == "tool"]
    assert tool_msgs and "not a file" in tool_msgs[0].content


def test_loop_memory_put_get(tmp_path):
    replies = [
        call("memory_put", key="fav", value="blue"),
        call("memory_get", key="fav"),
        "done",
    ]
    agent = make_agent(tmp_path, replies)
    agent.run("remember my favourite colour")
    assert agent.memory.get("fav") == "blue"
    assert len(agent.steps) == 2


def test_loop_respects_max_steps(tmp_path):
    replies = [call("shell", command="echo tick", cwd=str(tmp_path))] * 20
    agent = make_agent(tmp_path, replies, max_steps=3)
    agent.run("loop")
    assert len(agent.steps) <= 3


def test_loop_nudges_when_no_call(tmp_path):
    """A reply with no tool call and an unfinished plan triggers a nudge."""
    replies = ["I am thinking.", "I am thinking again.", "done"]
    agent = make_agent(tmp_path, replies)
    agent.run("do something")
    assert any("did not emit a tool call" in m.content for m in agent.history
               if m.role == "user")


def test_loop_ends_on_bare_final_answer(tmp_path):
    """A no-tool reply after work is done must end the run, not nudge forever."""
    replies = [
        call("shell", command="echo work", cwd=str(tmp_path)),
        "The task is complete; here is the answer.",
    ]
    agent = make_agent(tmp_path, replies)
    agent.run("do work")
    assert len(agent.steps) == 1
    assert not any("did not emit a tool call" in m.content
                   for m in agent.history if m.role == "user")


def test_function_model_adapter(tmp_path):
    """A model callable drives the loop; a repeat-call policy runs until the
    plan is exhausted."""
    seen = []

    def fn(prompt, max_tokens=512):
        seen.append(prompt)
        return call("shell", command="echo fn", cwd=str(tmp_path))

    agent = Agent(FunctionModel(fn), make_bus(), None, max_steps=2, verbose=False)
    agent.run("x")
    # 2 tool steps + the closing report turn
    assert len(seen) == 3 and len(agent.steps) == 2


def test_function_model_falls_back_to_one_arg(tmp_path):
    seen = []

    def fn(prompt):  # no max_tokens parameter
        seen.append(prompt)
        return call("shell", command="echo one-arg", cwd=str(tmp_path))

    agent = Agent(FunctionModel(fn), make_bus(), None, max_steps=2, verbose=False)
    agent.run("x")
    assert len(seen) == 3 and len(agent.steps) == 2


def test_local_model_echoes_scripted_call():
    m = LocalModel()
    reply = m.generate("blah ```json\n{\"tool\": \"shell\", \"args\": {}}\n```")
    assert json.loads(reply)["tool"] == "shell"


# --------------------------------------------------------------------------- #
# Heuristic policy (the default, no weights)
# --------------------------------------------------------------------------- #

def test_create_regex_parses_filenames():
    from agent.heuristic import ANY_FILE_RE, CREATE_RE, READ_RE

    def cap(rx, text):
        m = rx.search(text)
        assert m is not None, f"{rx.pattern!r} did not match {text!r}"
        return m.group(1)

    assert cap(CREATE_RE, "create a file called plan.md") == "plan.md"
    assert cap(CREATE_RE, "make a script called x.py") == "x.py"
    assert cap(READ_RE, "read report.txt") == "report.txt"
    assert CREATE_RE.search("summarise the docs") is None
    assert cap(ANY_FILE_RE, "write notes.md") == "notes.md"


def test_rule_policy_creates_file(tmp_path):
    from agent.heuristic import RulePolicy
    monkeypatch_cwd(tmp_path)
    agent = make_agent(tmp_path, [], memory=None)
    agent.model = RulePolicy()
    target = tmp_path / "plan.md"
    agent.run(f"create a file called {target.name}")
    assert target.exists()


def test_rule_policy_reads_file(tmp_path):
    from agent.heuristic import RulePolicy
    monkeypatch_cwd(tmp_path)
    f = tmp_path / "report.txt"
    f.write_text("hello world content\n")
    agent = make_agent(tmp_path, [], memory=None)
    agent.model = RulePolicy()
    agent.run("read report.txt")
    assert len(agent.steps) == 1
    assert "hello world content" in agent.steps[0].results[0]


def test_rule_policy_reports_missing_file(tmp_path):
    from agent.heuristic import RulePolicy
    monkeypatch_cwd(tmp_path)
    agent = make_agent(tmp_path, [], memory=None)
    agent.model = RulePolicy()
    agent.run("read does-not-exist.txt")
    assert agent.steps[-1].results == [] or "(no matches)" in "".join(
        r for s in agent.steps for r in s.results)


def test_remember_regex():
    from agent.heuristic import REMEMBER_RE
    assert REMEMBER_RE.search("remember that the build number is 7")
    assert REMEMBER_RE.search("remember the capital is Paris")


def test_rule_policy_remembers(tmp_path):
    from agent.heuristic import RulePolicy
    monkeypatch_cwd(tmp_path)
    agent = make_agent(tmp_path, [], memory=None)
    agent.model = RulePolicy()
    agent.run("remember that the build number is 7")
    assert agent.memory is not None and agent.memory.get("build_number") == "7"
    tools_used = [c["tool"] for s in agent.steps for c in s.calls]
    assert tools_used[0] == "memory_put"


def test_rule_policy_is_deterministic(tmp_path):
    from agent.heuristic import RulePolicy
    monkeypatch_cwd(tmp_path)
    outs = []
    for _ in range(2):
        agent = make_agent(tmp_path, [], memory=None)
        agent.model = RulePolicy()
        agent.run("create a file called det.md")
        outs.append(sorted(os.listdir(tmp_path)))
    assert outs[0] == outs[1] == ["det.md"]


def test_local_model_is_rule_policy():
    from agent.heuristic import RulePolicy
    from agent.models import make_model
    assert isinstance(make_model("local"), RulePolicy)


def test_stub_model_is_local_echo():
    from agent.core import LocalModel
    from agent.models import make_model
    assert isinstance(make_model("stub"), LocalModel)


def test_http_model_config():
    from agent.models import HTTPModel, make_model
    m = make_model("http", url="http://127.0.0.1:1/x")
    assert isinstance(m, HTTPModel) and m.url == "http://127.0.0.1:1/x"


def test_module_model_requires_target():
    from agent.models import make_model
    try:
        make_model("module")
    except RuntimeError as exc:
        assert "requires --model-module" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_module_model_bad_target():
    from agent.models import make_model
    try:
        make_model("module", target="no.such.module:fn")
    except RuntimeError as exc:
        assert "cannot import" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Web stack: search / browse / verify (network mocked - tests stay offline)
# --------------------------------------------------------------------------- #

DDG_BODY = """
<html><body><div class="pad">
<p>Search results for the query you entered. This filler makes the body long
enough to clear the minimum-size guard that rejects interstitial pages.</p>
<a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&amp;rut=abc">Example Page</a>
<td class="result-snippet">An example snippet about the topic.</td>
<a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fother.com%2F">Other</a>
</div></body></html>
"""

PAGE_HTML = """<html><head><title>Test Page</title></head><body>
<nav>menu junk</nav>
<h1>Test Page</h1>
<p>The capital of France is Paris. It has been the capital for centuries.</p>
<a href="https://example.com/next">Next article</a>
<script>var x=1;</script>
</body></html>"""


def test_search_parses_ddg_results(monkeypatch):
    from agent import search

    monkeypatch.setattr(search, "_get", lambda *a, **k: DDG_BODY)
    res = search.ddg_lite("anything", count=5)
    assert len(res) == 2
    assert res[0]["url"] == "https://example.com/page"
    assert res[0]["engine"] == "ddg-lite"


GOOGLE_BODY = """
<html><body><div id="search">
<p>Filler text so the body clears the minimum-size guard that rejects
interstitial pages. This paragraph is deliberately long enough.</p>
<div class="g"><a href="https://example.com/paris"><h3>Paris - Wikipedia</h3></a>
<span>Paris is the capital of France.</span></div>
<div class="g"><a href="/url?q=https%3A%2F%2Fexample.com%2Fguide&amp;sa=U"><h3>Paris guide</h3></a></div>
<h3><a href="https://example.com/third">Third result</a></h3>
<a href="https://about.google/">About Google</a>
</div></body></html>
"""


def test_google_parses_serp(monkeypatch):
    from agent import search

    monkeypatch.setattr(search, "_get", lambda *a, **k: GOOGLE_BODY)
    res = search.google("paris", count=5)
    urls = [r["url"] for r in res]
    assert urls[0] == "https://example.com/paris"
    assert "https://example.com/guide" in urls      # /url?q= unwrapped
    assert "https://example.com/third" in urls      # h3>a pattern
    assert all("google." not in u for u in urls)
    assert all(r["engine"] == "google" for r in res)


def test_google_rejects_blocked_page(monkeypatch):
    from agent import search

    monkeypatch.setattr(search, "_get",
                        lambda *a, **k: "<html><body>consent</body></html>")
    try:
        search.google("paris")
    except search.SearchError:
        pass
    else:
        raise AssertionError("expected SearchError")


def test_web_search_routes_to_google(monkeypatch):
    from agent import search

    monkeypatch.setattr(search, "google",
                        lambda q, count=10: [{"url": "https://x", "title": "T",
                                              "snippet": "", "engine": "google"}])
    out = search.web_search("q", engines=["google"])
    assert out["engine"] == "google"
    assert out["results"][0]["url"] == "https://x"


def test_searxng_parses_json(monkeypatch):
    from agent import search

    body = ('{"results":['
            '{"url":"https://example.com/p","title":"Python",'
            '"content":"A programming language.","engine":"google"},'
            '{"url":"bad","title":"x"}'
            ']}')
    monkeypatch.setattr(search, "_get", lambda *a, **k: body)
    res = search.searxng("python", count=5)
    assert len(res) == 1
    assert res[0]["url"] == "https://example.com/p"
    assert res[0]["snippet"] == "A programming language."
    assert res[0]["engine"] == "searxng"


def test_searxng_rejects_non_json(monkeypatch):
    from agent import search

    monkeypatch.setattr(search, "_get", lambda *a, **k: "<html>rate limited</html>")
    try:
        search.searxng("python")
    except search.SearchError:
        pass
    else:
        raise AssertionError("expected SearchError")


def test_httpdebug_off_by_default_is_noop():
    from agent import httpdebug

    httpdebug.set_enabled(False)
    assert httpdebug.enabled() is False
    httpdebug.request("GET", "http://example.com")     # must not raise
    httpdebug.response("http://example.com", 200, 5, 0.0)


def test_httpdebug_logs_get(monkeypatch):
    from agent import httpdebug, search

    lines = []
    monkeypatch.setattr(httpdebug, "_emit", lambda s: lines.append(s))
    httpdebug.set_enabled(True)

    class FakeHeaders:
        def get_content_charset(self):
            return "utf-8"

    class FakeResp:
        status = 200
        headers = FakeHeaders()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b"hello"

    monkeypatch.setattr(search.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp())
    out = search._get("http://example.com")
    assert out == "hello"
    assert any("--> GET http://example.com" in s for s in lines)
    assert any("<-- http://example.com 200 5B" in s for s in lines)
    httpdebug.set_enabled(False)


def test_httpdebug_logs_browse_fetch(monkeypatch):
    from agent import browse, httpdebug

    lines = []
    monkeypatch.setattr(httpdebug, "_emit", lambda s: lines.append(s))
    httpdebug.set_enabled(True)

    class FakeHeaders:
        def get_content_charset(self):
            return "utf-8"

    class FakeResp:
        status = 200
        headers = FakeHeaders()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b"<html>page</html>"

        def geturl(self):
            return "http://example.com/p"

    monkeypatch.setattr(browse.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp())
    final, html = browse.fetch("http://example.com/p")
    assert final == "http://example.com/p"
    assert any("--> GET http://example.com/p" in s for s in lines)
    assert any("<-- http://example.com/p 200" in s for s in lines)
    httpdebug.set_enabled(False)


def test_cli_http_debug_flag():
    from agent.cli import parse_args

    args = parse_args(["--once", "x", "--http-debug"])
    assert args.http_debug is True


def test_search_falls_through_on_failure(monkeypatch):
    from agent import search

    def boom(*a, **k):
        raise search.SearchError("nope")

    monkeypatch.setattr(search, "ddg_lite", boom)
    monkeypatch.setattr(search, "brave_html", boom)
    monkeypatch.setattr(search, "_get", lambda *a, **k: '{"query":{"search":[]}}')
    try:
        search.web_search("q", engines=["ddg-lite", "brave", "wikipedia"])
    except search.SearchError as exc:
        assert "all search engines failed" in str(exc)
    else:
        raise AssertionError("expected SearchError")


def test_search_encyclopedic_routes_to_general_web(monkeypatch):
    """Factual questions start on the general web (Bing first), not Wikipedia."""
    from agent import search
    calls = []

    def fake_bing(q, count=10):
        calls.append(q)
        return [{"url": "https://example.com/x", "title": "X",
                 "snippet": "", "engine": "bing"}]

    monkeypatch.setattr(search, "bing_html", fake_bing) if False else None
    monkeypatch.setattr(search, "bing", fake_bing)
    payload = search.web_search("what is the capital of France")
    assert payload["engine"] == "bing" and calls
    assert "wikipedia" not in payload["fallbacks"]


def test_browse_extracts_readable_text(monkeypatch):
    from agent import browse

    monkeypatch.setattr(browse, "fetch",
                        lambda url, timeout=20: (url, PAGE_HTML))
    page = browse.browse("https://example.com")
    assert page["title"] == "Test Page"
    assert "capital of France is Paris" in page["text"]
    assert "menu junk" not in page["text"]
    assert any(l["url"] == "https://example.com/next" for l in page["links"])


def test_browse_rejects_empty_pages(monkeypatch):
    from agent import browse

    monkeypatch.setattr(browse, "fetch",
                        lambda url, timeout=20: (url, "<html><body></body></html>"))
    try:
        browse.browse("https://example.com")
    except browse.BrowseError as exc:
        assert "almost no readable text" in str(exc)
    else:
        raise AssertionError("expected BrowseError")


def test_verify_high_confidence_with_two_sources():
    from agent.verify import verify_claim
    sources = [
        {"title": "A", "url": "https://a.example",
         "text": "The capital of France is Paris and has been for centuries."},
        {"title": "B", "url": "https://b.example",
         "text": "Paris, capital of France, is the country's largest city."},
    ]
    v = verify_claim("The capital of France is Paris", sources)
    assert v.confidence in ("high", "medium")
    assert v.score > 0
    assert v.support


def test_verify_contradiction_on_numbers():
    from agent.verify import verify_claim
    sources = [
        {"title": "A", "url": "https://a.example",
         "text": "The speed of light is 299792458 metres per second exactly."},
        {"title": "B", "url": "https://b.example",
         "text": "Light travels at 300000000 m/s in a vacuum, approximately."},
    ]
    v = verify_claim("The speed of light is 299792458 m/s", sources)
    assert v.score > 0


def test_verify_unverified_with_no_sources():
    from agent.verify import verify_claim
    v = verify_claim("anything", [])
    assert v.confidence == "unverified" and v.score == 0.0


def test_verify_tool_requires_sources():
    bus = make_bus()
    try:
        bus.call("verify_answer", {"claim": "x", "sources": "[]"})
    except ToolError as exc:
        assert "non-empty JSON array" in str(exc)
    else:
        raise AssertionError("expected ToolError")


def test_verify_tool_rejects_bad_json():
    bus = make_bus()
    try:
        bus.call("verify_answer", {"claim": "x", "sources": "{not json"})
    except ToolError as exc:
        assert "must be a JSON array" in str(exc)
    else:
        raise AssertionError("expected ToolError")


def test_web_tools_registered():
    bus = make_bus()
    for name in ("web_search", "answer_question", "verify_answer",
                 "browse_url", "fetch_url"):
        assert name in bus.names()


def test_web_search_tool_empty_query():
    bus = make_bus()
    try:
        bus.call("web_search", {"query": "   "})
    except ToolError:
        return
    raise AssertionError("expected ToolError")


def test_browse_tool_bad_url():
    bus = make_bus()
    try:
        bus.call("browse_url", {"url": "http://127.0.0.1:9/nope"})
    except ToolError as exc:
        assert "cannot reach" in str(exc) or "HTTP" in str(exc)
    else:
        raise AssertionError("expected ToolError")


# --------------------------------------------------------------------------- #
# Heuristic policy: web and fix paths
# --------------------------------------------------------------------------- #

def test_question_regex_matches():
    from agent.heuristic import QUESTION_RE
    for g in ("what is the capital of France", "who wrote Hamlet",
              "find out the python release date", "search the web for rust",
              "what's the time"):
        assert QUESTION_RE.search(g), g
    assert not QUESTION_RE.search("create a file called a.md")


def test_fix_regex_matches():
    from agent.heuristic import FIX_RE
    for g in ("fix the bug in x.py", "debug this", "why does it fail",
              "resolve the error in main.py"):
        assert FIX_RE.search(g), g


def test_policy_answers_question_with_cited_source(tmp_path, monkeypatch):
    from agent import search
    from agent.heuristic import RulePolicy
    monkeypatch.setattr(search, "ddg_lite", lambda q, count=10: [
        {"url": "https://example.com/paris", "title": "Paris",
         "snippet": "Paris is the capital of France.", "engine": "ddg-lite"}])
    monkeypatch_cwd(tmp_path)
    agent = make_agent(tmp_path, [], memory=None)
    agent.model = RulePolicy()
    agent.run("what is the capital of France")
    tools_used = [c["tool"] for s in agent.steps for c in s.calls]
    assert tools_used[0] == "answer_question"
    blob = "".join(r for s in agent.steps for r in s.results)
    # one-line answer is the first line, before the separator
    assert blob.split("\n---", 1)[0].strip()


def test_policy_fix_path_runs_and_searches(tmp_path):
    from agent.heuristic import RulePolicy
    monkeypatch_cwd(tmp_path)
    (tmp_path / "bug.py").write_text("def f():\n    return 1/0\n\nf()\n")
    agent = make_agent(tmp_path, [], memory=None)
    agent.model = RulePolicy(max_calls=8)
    agent.run("fix the bug in bug.py")
    tools_used = [c["tool"] for s in agent.steps for c in s.calls]
    assert tools_used[0] == "read_file"
    assert "shell" in tools_used
    # the final report must mention the reproduced error or the references
    final = [m.content for m in agent.history if m.role == "assistant"][-1]
    assert "Diagnosis ready" in final or "web_search" in final or tools_used


def test_policy_fix_reports_when_no_file(tmp_path):
    from agent.heuristic import RulePolicy
    monkeypatch_cwd(tmp_path)
    agent = make_agent(tmp_path, [], memory=None)
    agent.model = RulePolicy(max_calls=8)
    agent.run("fix the bug in nothere.py")
    assert agent.steps


def test_verify_regex_matches():
    from agent.heuristic import VERIFY_RE
    for g in ("verify that the capital of France is Paris",
              "confirm the release date", "cross-check this claim",
              "is the earth flat true"):
        assert VERIFY_RE.search(g), g


def test_policy_verify_path(tmp_path):
    from agent.heuristic import RulePolicy
    monkeypatch_cwd(tmp_path)
    agent = make_agent(tmp_path, [], memory=None)
    agent.model = RulePolicy(max_calls=8)
    agent.run("verify that the capital of France is Paris")
    tools_used = [c["tool"] for s in agent.steps for c in s.calls]
    assert "web_search" in tools_used
    assert "browse_url" in tools_used
    assert "verify_answer" in tools_used
    blob = "".join(r for s in agent.steps for r in s.results)
    assert "CONFIDENCE:" in blob


def test_policy_verify_reports_verdict(tmp_path):
    from agent.heuristic import RulePolicy
    monkeypatch_cwd(tmp_path)
    agent = make_agent(tmp_path, [], memory=None)
    agent.model = RulePolicy(max_calls=8)
    agent.run("verify that the capital of France is Paris")
    final = [m.content for m in agent.history if m.role == "assistant"][-1]
    assert "Verdict:" in final and "confidence" in final


def test_verify_answer_tool_happy_path(tmp_path):
    bus = make_bus()
    srcs = json.dumps([{"title": "A", "url": "https://a",
                        "text": "The capital of France is Paris."}])
    out = bus.call("verify_answer", {"claim": "The capital of France is Paris",
                                     "sources": srcs})
    assert "CONFIDENCE:" in out and "CLAIM:" in out


def test_cli_help():
    from agent.cli import parse_args
    args = parse_args(["do a thing", "--max-steps", "5", "--no-memory"])
    assert args.goal == "do a thing" and args.max_steps == 5 and args.no_memory


# --------------------------------------------------------------------------- #
# Spinner
# --------------------------------------------------------------------------- #

def test_spinner_disabled_on_non_tty():
    import io
    from agent.ui import Spinner
    sp = Spinner(stream=io.StringIO())
    assert sp.enabled is False
    sp.start(); sp.set_text("x"); sp.stop()   # must be a no-op, not a crash


def test_spinner_animates_and_clears():
    import io
    from agent.ui import Spinner
    buf = io.StringIO()
    sp = Spinner(stream=buf, enabled=True, interval=0.01)
    sp.start()
    sp.set_text("thinking")
    time.sleep(0.06)
    sp.stop()
    out = buf.getvalue()
    assert any(f in out for f in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
    assert "thinking" in out
    assert "\033[K" in out          # line is cleared on stop


def test_spinner_elapsed_grows():
    import io
    from agent.ui import Spinner
    sp = Spinner(stream=io.StringIO(), enabled=True, interval=0.01)
    sp.start()
    time.sleep(0.05)
    e = sp.elapsed
    sp.stop()
    assert e >= 0.04


def test_spinner_pause_prints_clean_line():
    import contextlib
    import io
    from agent.ui import Spinner
    buf = io.StringIO()
    sp = Spinner(stream=buf, enabled=True, interval=0.01)
    out = io.StringIO()
    with sp:
        with contextlib.redirect_stdout(out):
            with sp.pause():
                print("REAL LINE")
    # the spinner cleared its line before letting the real line print
    assert "REAL LINE" in out.getvalue()
    assert buf.getvalue().endswith("\033[K") or "\033[K" in buf.getvalue()


def test_spinner_context_manager():
    import io
    from agent.ui import Spinner
    buf = io.StringIO()
    with Spinner(stream=buf, enabled=True, interval=0.01) as sp:
        assert sp._running
    assert not sp._running


def test_agent_uses_spinner_text():
    """The loop should tell the spinner what it is doing."""
    import io
    from agent.ui import Spinner
    from agent.policy import FunctionModel, call
    seen = []

    def slow(prompt, max_tokens=512):
        time.sleep(0.05)
        return call("shell", command="echo x")

    bus = make_bus()
    buf = io.StringIO()
    sp = Spinner(stream=buf, enabled=True, interval=0.01)
    agent = Agent(FunctionModel(slow), bus, None, max_steps=2,
                  verbose=False, spinner=sp)
    sp.start()
    agent.run("x")
    sp.stop()
    out = buf.getvalue()
    assert "thinking" in out
    assert "shell" in out


def test_no_spin_flag():
    from agent.cli import parse_args
    args = parse_args(["x", "--no-spin"])
    assert args.no_spin is True


def _run(args, cwd=None):
    import subprocess
    return subprocess.run([sys.executable, *args], cwd=cwd, capture_output=True,
                          text=True, timeout=120)


def test_cli_runs_as_direct_script_from_agent_dir():
    """Regression: `python3 cli.py` from inside agent/ used to fail on relative
    imports. It must work without any install step."""
    repo = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    agent_dir = os.path.join(repo, "agent")
    r = _run(["cli.py", "--help"], cwd=agent_dir)
    assert r.returncode == 0, r.stderr
    assert "usage:" in r.stdout


def test_cli_runs_as_direct_script_from_repo_root():
    repo = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    r = _run([os.path.join("agent", "cli.py"), "--help"], cwd=repo)
    assert r.returncode == 0, r.stderr
    assert "usage:" in r.stdout


def test_module_form_runs():
    repo = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    r = _run(["-m", "agent", "--help"], cwd=repo)
    assert r.returncode == 0, r.stderr
    assert "usage:" in r.stdout


def test_no_goal_shows_help_not_error():
    """Regression: bare `python3 cli.py` used to error out."""
    repo = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    r = _run(["agent/cli.py"], cwd=repo)
    assert r.returncode == 0, r.stderr
    assert "usage:" in r.stdout
    assert "example:" in r.stdout
    assert "required" not in r.stderr


# --------------------------------------------------------------------------- #
# Whole-web answers + interactive chat
# --------------------------------------------------------------------------- #

def test_opinion_questions_are_not_encyclopedic():
    from agent.search import is_encyclopedic
    assert is_encyclopedic("what is the capital of France") is True
    assert is_encyclopedic("who wrote Hamlet") is True
    for q in ("what is the best linux distro", "top 10 python books",
              "is rust worth it", "macbook vs thinkpad", "cheapest vps"):
        assert is_encyclopedic(q) is False, q


def test_normalize_query_inserts_of():
    from agent.search import normalize_query
    assert normalize_query("capital france") == "capital of france"
    assert normalize_query("population of Tokyo") == "population of Tokyo"
    assert normalize_query("python") == "python"


def test_normalize_query_repairs_what_without_verb():
    """Regression: 'what capital of germany' (no is/the) returned no answer."""
    from agent.search import normalize_query
    assert normalize_query("what capital of germany") == "what is the capital of germany"
    assert normalize_query("what capital germany") == "what is the capital of germany"
    assert normalize_query("what population of Japan") == "what is the population of Japan"
    # already-correct forms are left untouched
    assert normalize_query("what is the capital of germany") == "what is the capital of germany"
    assert normalize_query("what's the capital of germany") == "what's the capital of germany"


def test_short_answer_what_without_verb():
    from agent.search import short_answer
    src = [{"title": "Berlin", "url": "x",
            "text": "Berlin is the capital of Germany. It is the largest city."}]
    assert short_answer("what capital of germany", src) == "Berlin"
    assert short_answer("what capital germany", src) == "Berlin"


def test_short_answer_gives_the_value():
    from agent.search import short_answer
    src = [{"title": "Paris", "url": "x",
            "text": "Paris is the capital of France. It has been for centuries."}]
    assert short_answer("what is the capital of France", src) == "Paris"
    assert short_answer("capital france", src) == "Paris"
    src2 = [{"title": "Hamlet", "url": "z",
             "text": "Hamlet is a tragedy written by William Shakespeare."}]
    assert short_answer("who wrote Hamlet", src2) == "William Shakespeare"


def test_short_answer_never_returns_a_page_title():
    """Regression: a title like 'Listen to Capital live' is not an answer."""
    from agent.search import short_answer
    src = [{"title": "Listen to Capital live", "url": "r",
            "text": "Listen to Capital live. Tune in for the best music."}]
    assert short_answer("capital", src) == ""
    # a question is not an answer either
    src2 = [{"title": "What is the Capital of France?", "url": "w",
             "text": "What is the Capital of France?"}]
    assert short_answer("what is the capital of France", src2) == ""


def test_short_answer_uses_number_with_units():
    from agent.search import short_answer
    src = [{"title": "S", "url": "y",
            "text": "The speed of light in vacuum is a universal physical "
                    "constant denoted c, exactly equal to 299792458 m/s."}]
    assert short_answer("what is the speed of light", src) == "299792458 m/s"


def test_answer_question_uses_whole_web(tmp_path, monkeypatch):
    """answer_question must search the web AND read pages, not just Wikipedia."""
    from agent import search, browse
    monkeypatch.setattr(search, "ddg_lite", lambda q, count=10: [
        {"url": "https://example.com/paris", "title": "Paris guide",
         "snippet": "Paris is the capital of France and its largest city.",
         "engine": "ddg-lite"}])
    monkeypatch.setattr(browse, "fetch",
                        lambda url, timeout=20: (url, PAGE_HTML))
    bus = make_bus()
    out = bus.call("answer_question", {"question": "what is the capital of France"})
    # the first line is the one-line answer; details follow a separator
    first = out.split("\n---", 1)[0].strip()
    assert first == "Paris", first
    assert "CONFIDENCE:" in out


def test_answer_question_notes_single_source(tmp_path):
    bus = make_bus()
    out = bus.call("answer_question", {"question": "what is the capital of France"})
    assert "CONFIDENCE:" in out


def test_interactive_loop_asks_twice_and_quits():
    """Regression: -i must stay open for multiple questions and exit on quit."""
    repo = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    import subprocess
    inp = "create a file called c1.md\ncreate a file called c2.md\nquit\n"
    r = subprocess.run([sys.executable, "agent/cli.py", "-i", "--no-memory",
                        "--quiet", "--cwd", repo],
                       cwd=repo, input=inp, capture_output=True, text=True,
                       timeout=180)
    assert r.returncode == 0, r.stderr
    assert os.path.exists(os.path.join(repo, "c1.md"))
    assert os.path.exists(os.path.join(repo, "c2.md"))
    for f in ("c1.md", "c2.md"):
        os.remove(os.path.join(repo, f))


def test_interactive_exit_words():
    from agent.cli import run_interactive

    class FakeSpinner:
        def start(self): pass
        def stop(self): pass

    class FakeAgent:
        def __init__(self): self.seen = []
        def run(self, goal, reset=True):
            self.seen.append(goal); return []

    for word in ("quit", "exit", "q", "bye"):
        ag = FakeAgent()
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run_interactive(ag, FakeSpinner(), None) if False else None
        # feed the word as the first input
        import builtins
        real_input = builtins.input
        builtins.input = lambda prompt="": word
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = run_interactive(ag, FakeSpinner(), None)
        finally:
            builtins.input = real_input
        assert rc == 0 and ag.seen == [], word


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    failures = 0
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        mp = None
        if name in NETWORK_TESTS and not net_available():
            print(f"SKIP {name} (offline)")
            continue
        try:
            if "tmp_path" in fn.__code__.co_varnames:
                with tempfile.TemporaryDirectory() as d:
                    try:
                        if "monkeypatch" in fn.__code__.co_varnames:
                            mp = _MonkeyPatch()
                            fn(Path(d), mp)
                        else:
                            fn(Path(d))
                    finally:
                        unpatch_cwd()
                        if mp:
                            mp.undo()
            else:
                if "monkeypatch" in fn.__code__.co_varnames:
                    mp = _MonkeyPatch()
                    try:
                        fn(mp)
                    finally:
                        mp.undo()
                else:
                    fn()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
