# Project100

Small, dependency-free Python tools. Each module is stdlib-only and ships with
unittests under `tests/`.

## search_agent.py

Search the web and find scripts/repositories without an LLM and without API
keys. DuckDuckGo, Google and Bing are queried by parsing their public result
pages; GitHub is searched through its public HTML results (REST API optional);
GitLab is searched through its unauthenticated REST API.

```
python3 search_agent.py web "python web scraper" --engines bing
python3 search_agent.py web "python web scraper" --limit 10 --json
python3 search_agent.py github "web scraper" --limit 10
python3 search_agent.py github "web scraper" --api        # REST (rate-limited)
python3 search_agent.py gitlab "web scraper" --limit 10
python3 search_agent.py scripts "web scraper" --emit-script out.sh
```

Options shared by every subcommand: `--limit`, `--timeout`, `--json`,
`--emit-script PATH`. `--emit-script` writes a runnable bash script that
reproduces the result list.

Exit status is `0` when at least one result was found, `1` otherwise. Engine
failures (blocks, rate limits) are reported as warnings on stderr and do not
abort the other engines.

### Notes

- Google often serves a JavaScript-only page to datacenter IPs and DuckDuckGo
  may answer with a `202` anomaly challenge; both are detected and reported
  rather than silently returning garbage. Bing is the most reliable of the
  three from a plain HTTP client.
- GitHub's unauthenticated REST API is capped at 60 requests/hour per IP; the
  HTML path avoids that cap.
- Only public data is accessed; no credentials are read or stored.

## agent.py

A live agent loop that needs no API key and no cloud model. It perceives a
goal, plans, calls real tools, observes the results and answers -- the same
shape as a model-driven agent, but the default brain is a deterministic
rule-based planner, so it runs anywhere with only the standard library.

```
python3 agent.py                                  # interactive REPL
python3 agent.py "what is the capital of France?"
python3 agent.py "search for python web scraper" --json
python3 agent.py "find me a repo for web scraping"
python3 agent.py "write a script that searches for pdf tools to find.sh" --yes
python3 agent.py "search for rust tools and save the results to out.txt" --yes
python3 agent.py "search for pdf tools" --script pdf_search.sh
python3 agent.py "remember that I like oolong tea" --yes
python3 agent.py "what did I say about tea"
python3 agent.py "run echo hello" --yes
python3 agent.py --tools
```

Tools: `web_search`, `github_search`, `gitlab_search`, `scripts_search`,
`fetch_page`, `shell`, `read_file`, `write_file`, `list_files`, `python`,
`remember`, `recall`. `shell`, `write_file` and `remember` are marked
mutating: in a terminal they ask for confirmation, and in a non-interactive
session they are refused unless `--yes` is passed.

Options: `--model {rules,ollama}` (default `rules`), `--memory PATH`,
`--max-steps N`, `--yes`, `--trace/--no-trace`, `--json`, `--tools`,
`--script PATH`, `--code`.

`--script PATH` writes the search results to a runnable bash script and the
answer is only that path -- no URL list is printed. The same is true when the
goal itself asks for a script ("write a script that searches for X to PATH"):
the file is created and the answer is just `Created script: PATH`. Passing
`--script` implies `--yes` for that one file write.

Search goals (`search ...`, `find me a repo ...`, `github ...`, `gitlab ...`)
default to the `search_content` pipeline: the agent searches, fetches the top
few results and prints each page's text or extracted code blocks, so you get
the actual content rather than a bare list of links. Add `--script PATH` if
you want the results written to a script instead.

### Finding a whole script

Goals that ask for code -- "create me a python script that downloads a
webpage", "write me a bash script that finds files larger than 1MB", "find me
a python script that scrapes a website" -- run the `find_code` pipeline. The
agent searches the web (and GitHub), fetches the best matching page, and for
GitHub repos follows the file links to fetch the raw source file. It prints
only the script itself: no title, no URL, no commentary. `--code` forces this
mode for any description.

```
python3 agent.py "create me a python script that downloads a webpage"
python3 agent.py "write me a bash script that finds files larger than 1MB" --code
```

Note: results depend on the public search engines. From some networks Bing,
DuckDuckGo and Google return challenge pages or unrelated results, so a query
may come back empty or with whatever code the fetched pages happen to contain.

### Brains

- `rules` (default): no model at all. Intent matching plus multi-step
  pipelines: `find_code` (search the web, fetch the best matching page or
  GitHub source file, print only the whole script -- no links), `search_content`
  (search, fetch the top results, print their text or code blocks),
  `research` (search, fetch, extract an answer with a citation),
  `fetch_extract`, `save_results`, `make_search_script`.
- `ollama`: uses a free local model through `http://127.0.0.1:11434` (set
  `OLLAMA_BASE` / `--ollama-base` and `OLLAMA_MODEL` / `--ollama-model`).
  Still no API key and nothing leaves the machine. The script exits with a
  clear message if the server is not reachable.

Memory lives in `~/.project100/agent_memory.json` (override with `--memory`
or `AGENT_MEMORY`) and survives between runs.

## Tests

```
python3 -m unittest tests.test_search_agent -v
python3 -m unittest tests.test_agent -v
SEARCH_AGENT_LIVE=1 python3 -m unittest tests.test_search_agent -v
```

The live variants hit the real engines and are skipped unless
`SEARCH_AGENT_LIVE` is set. `tests/test_agent.py` is fully offline: search
and fetch are stubbed, tools run in temp directories, and the optional
Ollama brain is tested against a tiny local HTTP server.

Note: `tests/test_rdp_scan.py` and `tests/test_telegram_phone.py` are stale
leftovers that import modules no longer present in the repository; they fail
to import and are unrelated to the modules above.
