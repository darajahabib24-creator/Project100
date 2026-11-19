"""core.py - the agent runtime: message types, tool bus, memory, planner, loop.

No network, no external model. The model layer is a callable that takes a
prompt (list of Message) and returns text; LocalModel is a deterministic
retrieval stand-in you can replace with your own weights later.
"""

from __future__ import annotations

import abc
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """You are an autonomous agent running locally, with no access to any
cloud API. You reason by writing short plans, and you act only through the
tools available to you.

Rules:
- Think before you act. State the next step in one line, then call a tool.
- One tool call per turn unless calls are independent; then batch them.
- Never invent tool names or arguments. Use only the schema given.
- When you have the answer, stop. Do not keep calling tools to look busy.
- If a tool fails, read the error and adapt; do not repeat it unchanged.

Output format:
- Plain text for narration.
- A tool call is a single JSON object on its own, fenced in ```json ... ```
  with keys: {"tool": <name>, "args": {<object>}}.
- You may emit several fenced JSON blocks in one turn to batch independent calls.
"""


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    name: str | None = None  # tool name, for role == "tool"
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = {"role": self.role, "content": self.content}
        if self.name:
            d["name"] = self.name
        return d


def render(messages: Sequence[Message]) -> str:
    """Render a transcript as plain text for a local model prompt."""
    out = []
    for m in messages:
        if m.role == "system":
            out.append(f"[system]\n{m.content}")
        elif m.role == "user":
            out.append(f"[user]\n{m.content}")
        elif m.role == "assistant":
            out.append(f"[assistant]\n{m.content}")
        elif m.role == "tool":
            out.append(f"[tool:{m.name}]\n{m.content}")
    return "\n\n".join(out)


# --------------------------------------------------------------------------- #
# Tool bus
# --------------------------------------------------------------------------- #

class ToolError(Exception):
    pass


class Tool(abc.ABC):
    """A tool the agent can call. Subclass and register on the bus."""

    name: str = ""
    description: str = ""
    # JSON-schema-ish parameter spec, used to build the prompt and validate args.
    params: dict[str, str] = {}

    @abc.abstractmethod
    def run(self, **kwargs: Any) -> str:  # pragma: no cover - interface
        """Execute. Return text (may be multi-line). Raise ToolError on failure."""

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        known = set(self.params)
        unknown = set(args) - known
        if unknown:
            raise ToolError(f"unknown args for {self.name}: {sorted(unknown)}")
        return args


class ToolBus:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("tool must define a name")
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ToolError(
                f"no such tool: {name!r}. available: {', '.join(self.names())}"
            )
        return self._tools[name]

    def manifest(self) -> str:
        lines = []
        for name in self.names():
            t = self._tools[name]
            ps = ", ".join(f"{k}:{v}" for k, v in t.params.items()) or "(none)"
            lines.append(f"- {name}({ps}) - {t.description}")
        return "\n".join(lines)

    def call(self, name: str, args: dict[str, Any]) -> str:
        tool = self.get(name)
        args = tool.validate(args)
        try:
            out = tool.run(**args)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface anything to the agent
            raise ToolError(f"{name} raised {type(exc).__name__}: {exc}") from exc
        return "" if out is None else str(out)


# --------------------------------------------------------------------------- #
# Memory: short-term transcript + long-term JSON store with search
# --------------------------------------------------------------------------- #

class Memory:
    """Durable scratch memory. Survives process exit.

    Facts are JSON-serialisable dicts; each gets a key you choose. Search is
    substring over the serialised record (no embedding model needed).
    """

    def __init__(self, path: str, max_facts: int = 500):
        self.path = os.path.abspath(path)
        self.max_facts = max_facts
        self._facts: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    self._facts = json.load(fh)
            except (json.JSONDecodeError, OSError):
                self._facts = {}

    def _flush(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._facts, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def put(self, key: str, value: Any) -> None:
        self._facts[key] = {"key": key, "value": value, "ts": time.time()}
        if len(self._facts) > self.max_facts:
            oldest = min(self._facts.items(), key=lambda kv: kv[1].get("ts", 0))
            self._facts.pop(oldest[0], None)
        self._flush()

    def get(self, key: str, default: Any = None) -> Any:
        rec = self._facts.get(key)
        return default if rec is None else rec.get("value", default)

    def search(self, needle: str, limit: int = 8) -> list[dict[str, Any]]:
        needle = needle.lower()
        hits = []
        for key, rec in self._facts.items():
            blob = json.dumps(rec, ensure_ascii=False).lower()
            if needle and needle in blob:
                hits.append(rec)
        hits.sort(key=lambda r: r.get("ts", 0), reverse=True)
        return hits[:limit]

    def forget(self, key: str) -> bool:
        if key in self._facts:
            del self._facts[key]
            self._flush()
            return True
        return False

    def all(self) -> list[dict[str, Any]]:
        return sorted(self._facts.values(), key=lambda r: r.get("key", ""))


# --------------------------------------------------------------------------- #
# Planner: deterministic state machine that proposes the next action
# --------------------------------------------------------------------------- #

PLAN_STATES = ("understand", "research", "execute", "verify", "report", "done")


@dataclass
class Plan:
    goal: str
    steps: list[str] = field(default_factory=list)
    state: str = "understand"
    idx: int = 0

    def advance(self) -> None:
        try:
            i = PLAN_STATES.index(self.state)
            self.state = PLAN_STATES[min(i + 1, len(PLAN_STATES) - 1)]
        except ValueError:
            self.state = "understand"

    def as_prompt(self) -> str:
        done = self.steps[: self.idx]
        todo = self.steps[self.idx :]
        lines = [f"Goal: {self.goal}", f"State: {self.state}"]
        if done:
            lines.append("Done:")
            lines += [f"  [x] {s}" for s in done]
        if todo:
            lines.append("Next:")
            lines += [f"  [ ] {s}" for s in todo]
        return "\n".join(lines)


class Planner:
    """Rule-based planner: turns the goal into an ordered step list, then walks
    it. The model can revise the plan via the plan_tool; otherwise this drives
    the loop's narration and state transitions."""

    def __init__(self, goal: str):
        self.plan = Plan(goal=goal, steps=self._draft(goal), state="understand")

    @staticmethod
    def _draft(goal: str) -> list[str]:
        g = goal.lower()
        steps = [f"Restate the goal: {goal}"]
        if any(w in g for w in ("find", "search", "look up", "what is", "who")):
            steps.append("Gather information with available tools")
        if any(w in g for w in ("write", "create", "build", "make", "code", "fix")):
            steps.append("Produce the artifact or edit")
        if any(w in g for w in ("check", "verify", "test", "run", "validate")):
            steps.append("Verify the result")
        steps.append("Report the outcome concisely")
        return steps

    def tick(self) -> None:
        if self.plan.idx < len(self.plan.steps):
            self.plan.idx += 1
            if self.plan.idx >= len(self.plan.steps):
                self.plan.state = "done"
            else:
                self.plan.advance()

    @property
    def idx(self) -> int:
        return self.plan.idx

    @idx.setter
    def idx(self, v: int) -> None:
        self.plan.idx = v


# --------------------------------------------------------------------------- #
# Model layer
# --------------------------------------------------------------------------- #

class Model(abc.ABC):
    """A local language model. Implement generate() with your own weights."""

    @abc.abstractmethod
    def generate(self, prompt: str, max_tokens: int = 512) -> str:
        """Given a rendered transcript, return the model's continuation."""


class LocalModel(Model):
    """Deterministic retrieval stand-in. NOT a language model.

    It looks for a tool call in the prompt, or echoes a short structured
    response, so the loop is exercisable end-to-end before you supply real
    weights. Replace generate() with your own inference.
    """

    def __init__(self, echo: bool = False):
        self.echo = echo
        self.calls = 0

    def generate(self, prompt: str, max_tokens: int = 512) -> str:
        self.calls += 1
        # If the transcript already contains a fenced tool call, echo it back
        # so a scripted/recorded policy can drive the loop.
        m = re.findall(r"```json\s*(\{.*?\})\s*```", prompt, re.S)
        if m:
            return m[-1]
        return "No tool call emitted. Continue reasoning toward the goal."


# --------------------------------------------------------------------------- #
# Parsing model output
# --------------------------------------------------------------------------- #

CALL_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


def parse_calls(text: str) -> list[dict[str, Any]]:
    """Extract tool calls from a model reply. Tolerates stray prose."""
    calls = []
    for block in CALL_RE.findall(text):
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "tool" in obj:
            args = obj.get("args", {})
            if not isinstance(args, dict):
                args = {}
            calls.append({"tool": str(obj["tool"]), "args": args})
    return calls


# --------------------------------------------------------------------------- #
# The agent loop
# --------------------------------------------------------------------------- #

@dataclass
class Step:
    n: int
    thought: str
    calls: list[dict[str, Any]] = field(default_factory=list)
    results: list[str] = field(default_factory=list)
    error: str | None = None


class Agent:
    def __init__(
        self,
        model: Model,
        bus: ToolBus,
        memory: Memory | None = None,
        max_steps: int = 12,
        verbose: bool = True,
        spinner=None,
    ):
        self.model = model
        self.bus = bus
        self.memory = memory
        self.max_steps = max_steps
        self.verbose = verbose
        self.history: list[Message] = []
        self.steps: list[Step] = []
        self.model_error: str | None = None
        self.spinner = spinner
        self._empty_turns = 0
        self._say_fn: Callable[[str], None] = print

    def _looks_finished(self, reply: str, planner: "Planner") -> bool:
        """Heuristic: the model says it is done, or the plan is exhausted."""
        low = reply.lower()
        markers = ("goal complete", "is complete", "i am done", "task done",
                   "finished", "that's everything", "nothing more to do")
        if any(m in low for m in markers):
            return True
        return planner.plan.state == "done"

    def _spin(self, msg: str | None) -> None:
        """Update the spinner text (or restore the default) if one is active."""
        if self.spinner is None or not self.spinner.enabled:
            return
        self.spinner.set_text(msg if msg else "working")

    def _say(self, msg: str) -> None:
        if not self.verbose:
            return
        if self.spinner is not None and self.spinner.enabled:
            with self.spinner.pause():
                self._say_fn(msg)
        else:
            self._say_fn(msg)

    def _build_prompt(self, goal: str, planner: Planner, extra: str = "") -> str:
        parts = [SYSTEM_PROMPT, ""]
        parts.append("TOOLS:\n" + self.bus.manifest())
        parts.append("")
        parts.append("PLAN:\n" + planner.plan.as_prompt())
        if self.memory:
            parts.append("")
            parts.append("MEMORY KEYS: " + ", ".join(
                r["key"] for r in self.memory.all()[:20]
            ) or "(empty)")
        parts.append("")
        parts.append("TRANSCRIPT:")
        parts.append(render(self.history))
        if extra:
            parts.append(extra)
        parts.append("")
        parts.append(f"Goal: {goal}\nRespond now.")
        return "\n".join(parts)

    def run(self, goal: str) -> list[Step]:
        planner = Planner(goal)
        self.history = [Message("user", goal)]
        self._say(f"[goal] {goal}")

        for n in range(1, self.max_steps + 1):
            prompt = self._build_prompt(goal, planner)
            self._spin(f"thinking  [step {n}/{self.max_steps}]")
            try:
                reply = self.model.generate(prompt)
            except Exception as exc:  # noqa: BLE001
                self._say(f"[model error] {type(exc).__name__}: {exc}")
                self.model_error = f"{type(exc).__name__}: {exc}"
                return self.steps
            finally:
                self._spin(None)

            step = Step(n=n, thought=reply)
            self.history.append(Message("assistant", reply))

            calls = parse_calls(reply)
            if not calls:
                self._say(f"[{n}] {reply.strip()[:200]}")
                # No tool call. Stop if the model says it is done, else nudge
                # it once to act; a second consecutive empty reply also ends
                # the run, so a silent model cannot spin forever.
                if self._looks_finished(reply, planner):
                    planner.plan.state = "done"
                    break
                if self._empty_turns >= 1 or n >= self.max_steps - 1:
                    planner.plan.state = "done"
                    break
                self._empty_turns += 1
                self.history.append(Message(
                    "user",
                    "You did not emit a tool call. Emit one now, or state that "
                    "the goal is complete.",
                ))
                continue
            self._empty_turns = 0

            self._say(f"[{n}] {len(calls)} call(s): " +
                      ", ".join(c["tool"] for c in calls))
            for call in calls:
                self._spin(f"{call['tool']}  [step {n}]")
                try:
                    result = self.bus.call(call["tool"], call["args"])
                    step.results.append(result)
                    self.history.append(Message("tool", result, name=call["tool"]))
                    self._say(f"    -> {call['tool']}: {result[:300]}")
                except ToolError as exc:
                    step.error = str(exc)
                    self.history.append(Message("tool", str(exc), name=call["tool"]))
                    self._say(f"    !! {call['tool']}: {exc}")

            step.calls = calls
            self.steps.append(step)
            planner.tick()
            self._say(f"    state -> {planner.plan.state} "
                      f"({planner.idx}/{len(planner.plan.steps)})")

            if planner.plan.state == "done":
                # One closing turn so the agent reports its result instead of
                # ending silently on the last tool step.
                closing = self._build_prompt(
                    goal, planner,
                    extra="The plan is complete. Report the outcome to the user "
                          "in one short message. Do not call any more tools.")
                try:
                    reply = self.model.generate(closing)
                except Exception:  # noqa: BLE001
                    break
                self.history.append(Message("assistant", reply))
                self._say(f"[final] {reply.strip()[:400]}")
                break

        self._say(f"[done] {len(self.steps)} tool steps, "
                  f"state={planner.plan.state}")
        return self.steps


# --------------------------------------------------------------------------- #
# Convenience
# --------------------------------------------------------------------------- #

def build_agent(model: Model | None = None, memory_path: str | None = None,
                tools: Iterable[Tool] | None = None, **kw) -> Agent:
    bus = ToolBus()
    for t in (tools or []):
        bus.register(t)
    mem = Memory(memory_path) if memory_path else None
    return Agent(model or LocalModel(), bus, mem, **kw)
