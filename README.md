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

## Tests

```
python3 -m unittest tests.test_search_agent -v
SEARCH_AGENT_LIVE=1 python3 -m unittest tests.test_search_agent -v
```

The live variants hit the real engines and are skipped unless
`SEARCH_AGENT_LIVE` is set.
