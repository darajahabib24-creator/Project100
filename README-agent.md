# local-agent

An autonomous agent runtime that runs entirely on your machine. No cloud API,
no API key, no telemetry. The model layer is a single callable you can replace
with your own weights.

```
goal -> plan -> [ think -> tool call -> observe ]* -> report
```

## Quick start

```bash
# from the repo root
python3 agent/cli.py "create a file called plan.md" --cwd /tmp/work
python3 -m agent "what is the speed of light"        # same thing, shorter
python3 -m agent.cli "goal" --model http            # explicit module form

# or install once and use the `agent` command anywhere
pip install -e .
agent "read report.txt" --cwd /tmp/work
```

All three invocation forms work from any directory, including running
`cli.py` directly - no install step required.

Run the tests (no network, no model):

```bash
python3 agent/tests/test_agent.py
```

## The spinner

On a TTY the agent animates a status line while it works, so it reads like a
real agent and not a hung terminal:

```
⠹ thinking  [step 2/12] (1.4s)
⠦ read_file  [step 2] (1.5s)
✓ done in 3.2s · 2 step(s) · 2 tool call(s) · model calls=3
```

The label tracks what is actually happening - `thinking  [step n/N]` while the
model runs, the tool name while a tool executes - with elapsed seconds. On a
pipe, redirect or CI it degrades to plain log lines (no ANSI), and `--no-spin`
turns the animation off while keeping the step log.

## What it actually does with no model weights

`--model local` (the default) is **not** a language model. It is a
deterministic, transparent policy (`agent/heuristic.py`) that reads the goal and
emits the next sensible tool call: create/read a named file, list a directory,
search contents. That makes the whole loop real and testable offline. It cannot
reason, so for anything beyond "touch this file", attach a model.

## The model seam

Everything model-shaped implements one method:

```python
class Model(abc.ABC):
    def generate(self, prompt: str, max_tokens: int = 512) -> str: ...
```

The prompt is a plain-text transcript: system prompt, tool manifest, plan,
transcript, and the goal. The expected reply is free text plus optional fenced
tool calls:

    ```json
    {"tool": "read_file", "args": {"path": "report.txt"}}
    ```

Three adapters ship in `agent/models.py`:

| `--model` | what it is | how to use |
|---|---|---|
| `local` | deterministic policy, no weights | default; no setup |
| `stub` | echoes scripted calls from the prompt | tests |
| `http` | POSTs to a local server, no key | `--model-url http://127.0.0.1:8080/completion` |
| `module` | imports your own callable | `--model-module mypkg.mymodel:generate` |

Run your own weights and point the agent at them:

```bash
# llama.cpp server, local only
llama-server -m model.gguf --port 8080
agent "summarise notes.md" --model http --model-url http://127.0.0.1:8080/completion

# or your own python inference
agent "goal" --model module --model-module mymodel:generate
```

`HTTPModel` expects a llama.cpp-style JSON response (`{"content": ...}`) and
reads `AGENT_MODEL_URL` if `--model-url` is omitted.

## Tools

All local except the web tools. No tool uses an API key.

`read_file`, `write_file`, `edit_file`, `list_dir`, `search_files`, `shell`,
`memory_put`, `memory_get`.

Web: `web_search`, `answer_question`, `verify_answer`, `browse_url`, `fetch_url`.

## The web: find things and get right answers

Three stages, and the agent uses them from the default policy with no weights:

1. **Find** - `web_search` queries the open web with no key. It tries
   DuckDuckGo Lite, then Brave, then Wikipedia, and records which engines
   failed so you can see provenance. Encyclopedic questions
   ("what is X", "who wrote Y") route to Wikipedia first.
2. **Read** - `browse_url` fetches a page and reduces it to readable text
   (stdlib HTML pass, no headless browser). It rejects pages that yield almost
   no text, which is how bot walls and JS-only sites announce themselves.
3. **Verify** - `verify_answer` scores a claim against the pages actually read
   and returns a confidence label (`high`/`medium`/`low`/`contradicted`/
   `unverified`) plus which sources support or contradict it.

`answer_question` combines 1+2 and returns the answer **with its source URL and
last-edited date**. It never presents a snippet as the answer.

```bash
agent "what is the speed of light"                 # cited answer + reads the page
agent "verify that the capital of France is Paris" # confidence verdict
agent "search the web for rust programming"        # raw results
```

### Fixing things

`agent "fix the bug in bug.py"` runs a grounded loop: read the file, **run it to
reproduce the failure**, search the web for that specific error, then report the
diagnosis with references. It does not edit your code - a rules policy cannot
judge which change is correct. Attach a model backend and it can.

### Honest limits

- Search is only as good as the free HTML endpoints. Brave rate-limits (429);
  DuckDuckGo Lite changes markup and may need the parser updated. Wikipedia is
  the most stable and is the fallback for factual questions.
- `verify_answer` measures textual agreement, not truth. Two pages copying the
  same wrong fact will agree. It is a confidence signal, not proof.
- No search API key means no SERP API and no guaranteed freshness. For anything
  time-sensitive, confirm against a primary source.


Register your own:

```python
from agent import Agent, Tool, ToolBus, Memory

class Now(Tool):
    name = "now"
    description = "Current time."
    params = {}
    def run(self, **kw):
        import datetime
        return datetime.datetime.now().isoformat()

bus = ToolBus(); bus.register(Now())
```

## Layout

```
agent/core.py       messages, tool bus, memory, planner, the loop
agent/ui.py         the thinking spinner (TTY-aware, non-TTY degrades to log lines)
agent/tools.py      filesystem / shell / memory / web tools
agent/search.py      web search (DuckDuckGo Lite, Brave, Wikipedia) + direct answers
agent/browse.py     fetch a page -> readable text
agent/verify.py     cross-source agreement + confidence
agent/heuristic.py  deterministic policy (the no-weights default)
agent/models.py     model adapters (http / module / local)
agent/policy.py     scripted models for tests and replay
agent/cli.py        CLI, --replay, --dump
agent/tests/        70 tests, offline (live-web tests skip when offline)
```

## Memory

`--memory PATH` (default `~/.agent/memory.json`) persists facts across runs as
JSON with substring search. No embedding model.

## Safety

- The shell tool runs real commands as your user. There is no sandbox; `--cwd`
  is the only boundary.
- Web tools make outbound requests only when you ask; no key, no telemetry.
  `fetch_url` is the raw one - prefer `browse_url` for pages.
- A model failure exits non-zero; it never silently reports success.
