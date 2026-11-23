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

Describe a script, get the script. You write one sentence; the agent searches
the web and GitHub/GitLab, fetches the best matching page or raw source file,
and prints only the code -- no links, no titles, no commentary. No API key, no
model, standard library only.

```
python3 agent.py "create me a python script that downloads a webpage"
python3 agent.py "write me a bash script that finds files larger than 1MB"
python3 agent.py "find me a python script that scrapes a website"
python3 agent.py                     # interactive: type a description
```

How it works: it searches the web twice (your description, then a de-noised
version with language/"script" filler removed), falls back to GitHub/GitLab,
fetches the top pages, extracts `<pre>`/`<code>` blocks, and for GitHub repos
follows the file links to fetch the raw source file. Candidates are scored by
stemmed keyword overlap ("downloads" matches "downloaded"), code markers and
length; the winner is printed.

Options: `--trace/--no-trace` prints what it is doing on stderr.

If nothing is found, nothing is printed on stdout and a short note goes to
stderr.

Note: results depend on the public search engines. From some networks Bing,
DuckDuckGo and Google return challenge pages or unrelated results, so a query
may come back empty or with whatever code the fetched pages happen to contain.

## Tests

```
python3 -m unittest tests.test_search_agent -v
python3 -m unittest tests.test_agent -v
SEARCH_AGENT_LIVE=1 python3 -m unittest tests.test_search_agent -v
```

The live variants hit the real engines and are skipped unless
`SEARCH_AGENT_LIVE` is set. `tests/test_agent.py` is fully offline: search
and fetch are stubbed.

Note: `tests/test_rdp_scan.py` and `tests/test_telegram_phone.py` are stale
leftovers that import modules no longer present in the repository; they fail
to import and are unrelated to the modules above.
