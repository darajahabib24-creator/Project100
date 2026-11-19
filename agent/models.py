"""models.py - model adapters. This is the seam where your own model plugs in.

Nothing here calls a network. Each adapter implements Model.generate(prompt).
Pick one with --model:
    local   deterministic stand-in (default, no weights needed)
    http    POST to a local server you run yourself (e.g. llama.cpp server)
    module  import a python callable you wrote: path.to.fn (fn(prompt) -> str)
"""

from __future__ import annotations

import importlib
import json
import os
import urllib.request
import urllib.error

from .core import LocalModel, Model
from .heuristic import RulePolicy


class HTTPModel(Model):
    """Talks to a local inference server. No cloud, no API key.

    Default endpoint: http://127.0.0.1:8080/completion (llama.cpp server style,
    which needs no key). Override with --model-http-url / --model-http-json.
    """

    def __init__(self, url: str = "http://127.0.0.1:8080/completion",
                 prompt_key: str = "prompt", response_key: str = "content",
                 timeout: int = 120, extra: dict | None = None):
        self.url = url
        self.prompt_key = prompt_key
        self.response_key = response_key
        self.timeout = timeout
        self.extra = extra or {}

    def generate(self, prompt: str, max_tokens: int = 512) -> str:
        body = {self.prompt_key: prompt, "n_predict": max_tokens, **self.extra}
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=data,
            headers={"Content-Type": "application/json", "User-Agent": "local-agent/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(
                f"model server unreachable at {self.url}: {exc}. "
                "Start it (e.g. llama-server) or use --model local."
            ) from exc
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return raw.strip()
        if isinstance(obj, dict):
            if self.response_key in obj:
                return str(obj[self.response_key])
            for key in ("content", "text", "response", "completion", "output"):
                if key in obj:
                    return str(obj[key])
            return json.dumps(obj)[:2000]
        return str(obj)


class ModuleModel(Model):
    """Wrap any python callable: --model module --model-module mypkg.mymodel:gen"""

    def __init__(self, target: str):
        mod_name, sep, attr = target.partition(":")
        if not sep:
            mod_name, attr = target.rsplit(".", 1)
        try:
            mod = importlib.import_module(mod_name)
            self.fn = getattr(mod, attr)
        except (ImportError, AttributeError) as exc:
            raise RuntimeError(f"cannot import model target {target!r}: {exc}") from exc
        if not callable(self.fn):
            raise RuntimeError(f"model target {target!r} is not callable")

    def generate(self, prompt: str, max_tokens: int = 512) -> str:
        try:
            out = self.fn(prompt, max_tokens=max_tokens)
        except TypeError:
            out = self.fn(prompt)  # simpler signature
        return str(out)


def make_model(kind: str = "local", **kw) -> Model:
    if kind == "local":
        # "local" is the honest default: a deterministic policy, not a model.
        # "stub" keeps the old echo-only behaviour for tests.
        return RulePolicy(max_calls=int(kw.get("max_calls", 8)))
    if kind == "stub":
        return LocalModel()
    if kind == "http":
        return HTTPModel(
            url=kw.get("url", os.environ.get("AGENT_MODEL_URL",
                                            "http://127.0.0.1:8080/completion")),
            timeout=int(kw.get("timeout", 120)),
        )
    if kind == "module":
        if not kw.get("target"):
            raise RuntimeError("--model module requires --model-module path:fn")
        return ModuleModel(kw["target"])
    raise RuntimeError(f"unknown model kind: {kind!r} (local|http|module)")
