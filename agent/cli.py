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
    from .core import Agent, Memory, ToolBus
    from .models import make_model
    from .policy import ScriptedModel
    from .tools import default_tools
    from .ui import Spinner
except ImportError:  # run as a plain script: python3 agent/cli.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
    p.add_argument("--dump", help="write the finished transcript to this JSON")
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


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        model = _select_model(args)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not args.goal and not args.replay:
        # No goal: show how to use it rather than a bare error.
        make_parser().print_help()
        print("\nexample: agent \"what is the speed of light\"")
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
                  verbose=not args.quiet, spinner=spinner)
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
