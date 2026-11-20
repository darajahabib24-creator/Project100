#!/usr/bin/env python3
"""cli.py - run the local agent.

Works from any directory, in all three of these forms:

    python3 agent/cli.py "goal text"        # direct script (adds parent to path)
    python3 -m agent.cli "goal text"        # module form
    agent "goal text"                       # after: pip install -e .

No cloud API, no key. --model local is a deterministic stand-in so the loop can
be exercised before you attach your own weights.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

try:  # normal case: imported as part of the package
    from . import httpdebug
    from .core import Agent, Memory, ToolBus
    from .models import make_model
    from .policy import ScriptedModel
    from .tools import default_tools
    from .ui import Spinner
except ImportError:  # run as a plain script: python3 agent/cli.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from agent import httpdebug
    from agent.core import Agent, Memory, ToolBus
    from agent.models import make_model
    from agent.policy import ScriptedModel
    from agent.tools import default_tools
    from agent.ui import Spinner


def make_parser():
    p = argparse.ArgumentParser(prog="agent", description="Local autonomous agent.")
    p.add_argument("goal", nargs="?", help="what the agent should do")
    p.add_argument("--model", default="local", choices=["local", "stub", "http", "module"],
                   help="model backend (default: local deterministic policy)")
    p.add_argument("--model-module", help="module:fn for --model module")
    p.add_argument("--model-url", help="local inference server URL for --model http")
    p.add_argument("--memory", default=os.path.expanduser("~/.agent/memory.json"),
                   help="path to durable memory JSON")
    p.add_argument("--no-memory", action="store_true", help="do not load memory")
    p.add_argument("--max-steps", type=int, default=12)
    p.add_argument("--cwd", default=".", help="working directory for the agent")
    p.add_argument("--replay", help="JSON list of assistant replies (scripted run)")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--no-spin", action="store_true",
                   help="disable the animated spinner (keeps the step log)")
    p.add_argument("-i", "--interactive", action="store_true",
                   help="stay in a chat loop: ask more than one question")
    p.add_argument("--once", action="store_true",
                   help="answer one goal and exit (no chat loop)")
    p.add_argument("--dump", help="write the finished transcript to this JSON")
    p.add_argument("--http-debug", action="store_true",
                   help="print every outbound HTTP request/response to stderr")
    return p


def parse_args(argv=None):
    return make_parser().parse_args(argv)


def _coerce_replies(payload) -> list[str]:
    """Accept either a bare list of reply strings, or the dump format produced
    by --dump ({"transcript": [...]} / {"messages": [...]}) whose entries are
    {"role","content"} dicts. Assistant entries become the reply script."""
    if isinstance(payload, dict):
        for key in ("transcript", "messages", "history"):
            if key in payload:
                payload = payload[key]
                break
        else:
            raise SystemExit(
                f"error: --replay JSON object needs a 'transcript' or "
                f"'messages' key, got keys: {sorted(payload)}"
            )
    if not isinstance(payload, list):
        raise SystemExit("error: --replay must be a JSON list of reply strings "
                         "(or a transcript dump)")
    out = []
    for item in payload:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict) and "content" in item:
            if item.get("role", "assistant") in ("assistant", "tool"):
                out.append(str(item["content"]))
        else:
            out.append(str(item))
    return out


def _select_model(args):
    """Build the model backend. A replay file wins over --model."""
    if args.replay:
        with open(args.replay, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return ScriptedModel(_coerce_replies(payload))
    return make_model(args.model, target=args.model_module, url=args.model_url)


def one_line(text: str) -> str:
    """The answer is everything before the '---' separator."""
    return text.split("\n---", 1)[0].strip()


def last_answer(agent, start: int = 0) -> str:
    """One-line answer from the tool results added during THIS turn."""
    for m in reversed(agent.history[start:]):
        if m.role == "tool" and m.name == "answer_question":
            line = one_line(m.content)
            if line:
                return line
    return ""


def run_interactive(agent, spinner, first_goal, quiet=False, show_log=False):
    """Chat loop: thinking -> working -> one-line answer -> stay open.

    The transcript is NOT reset between turns, so the agent remembers what you
    asked earlier. Type quit / exit / q, or press Ctrl-D / Ctrl-C, to leave.
    """
    print("agent - ask anything. 'quit' to exit.")
    print("-" * 60)
    goal = first_goal
    first = True
    while True:
        if goal is None:
            try:
                goal = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nbye")
                return 0
        if not goal:
            goal = None
            continue
        if goal.lower() in ("quit", "exit", "q", "bye", "exit()"):
            print("bye")
            return 0

        t0 = time.time()
        n0 = len(agent.history)      # only look at this turn's results
        if hasattr(agent.model, "reset_turn"):
            agent.model.reset_turn()
        spinner.start()
        try:
            steps = agent.run(goal, reset=first)
        except RuntimeError as exc:
            print(f"error: {exc}")
            goal = None
            continue
        finally:
            spinner.stop()
        first = False
        if agent.model_error:
            print(f"error: model failed: {agent.model_error}")
            agent.model_error = None
            goal = None
            continue

        ans = last_answer(agent, n0)
        if ans:
            print(f"\n{ans}\n")
        elif not show_log:
            final = [m.content for m in agent.history if m.role == "assistant"]
            if final:
                print(f"\n{one_line(final[-1])}\n")
        if show_log and not quiet:
            n_tools = sum(len(s.calls) for s in steps)
            print(f"✓ {time.time() - t0:.1f}s · {len(steps)} step(s) · "
                  f"{n_tools} tool call(s)")
        goal = None


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.http_debug:
        httpdebug.set_enabled(True)
    try:
        model = _select_model(args)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Chat is the default when attached to a terminal; --once forces single-shot.
    interactive = args.interactive or (not args.once and sys.stdin.isatty())

    if interactive and not args.goal and not args.replay:
        pass  # interactive with no opening goal just starts the prompt
    elif not args.goal and not args.replay:
        # No goal: show how to use it rather than a bare error.
        make_parser().print_help()
        print("\nexample: agent \"what is the speed of light\"")
        print("         agent -i          (chat mode, stays open)")
        return 0
    goal = args.goal or "replay"

    memory = None if args.no_memory else Memory(args.memory)
    tools = default_tools(memory)
    bus = ToolBus()
    for t in tools:
        bus.register(t)

    os.makedirs(os.path.abspath(args.cwd), exist_ok=True)
    os.chdir(os.path.abspath(args.cwd))

    spinner = Spinner(enabled=False if (args.quiet or args.no_spin) else None)
    agent = Agent(model, bus, memory, max_steps=args.max_steps,
                  verbose=not args.quiet and not interactive,
                  spinner=spinner)

    if interactive:
        return run_interactive(agent, spinner, args.goal, quiet=args.quiet,
                               show_log=args.quiet)

    t0 = time.time()
    spinner.start()
    try:
        steps = agent.run(goal)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        spinner.stop()
    if agent.model_error:
        print(f"error: model failed: {agent.model_error}", file=sys.stderr)
        return 1

    if not args.quiet:
        n_tools = sum(len(s.calls) for s in steps)
        print(f"✓ done in {time.time() - t0:.1f}s · "
              f"{len(steps)} step(s) · {n_tools} tool call(s) · "
              f"model calls={getattr(model, 'calls', 'n/a')}")

    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as fh:
            json.dump([m.to_dict() for m in agent.history], fh, indent=2,
                      ensure_ascii=False)
        if not args.quiet:
            print(f"[transcript] {args.dump}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
