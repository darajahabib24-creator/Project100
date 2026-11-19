"""policy.py - scripted policies for testing the loop without a model.

A policy is a Model whose generate() returns the next canned reply. It lets us
prove the loop, tools, memory and planner work before real weights exist, and
gives you a way to replay transcripts.
"""

from __future__ import annotations

import json
from typing import Callable, Sequence

from .core import Model


class ScriptedModel(Model):
    """Replies come from a list, in order, then a default."""

    def __init__(self, replies: Sequence[str], default: str = ""):
        self.replies = list(replies)
        self.default = default
        self.prompts: list[str] = []

    def generate(self, prompt: str, max_tokens: int = 512) -> str:
        self.prompts.append(prompt)
        if self.replies:
            return self.replies.pop(0)
        return self.default


class FunctionModel(Model):
    """Reply from a python function of the prompt."""

    def __init__(self, fn: Callable[[str], str]):
        self.fn = fn

    def generate(self, prompt: str, max_tokens: int = 512) -> str:
        return self.fn(prompt)


def call(tool: str, **args) -> str:
    return "```json\n" + json.dumps({"tool": tool, "args": args}) + "\n```"
