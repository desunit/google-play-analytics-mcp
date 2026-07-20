# AGENTS.md

Guidance for AI agents (and humans) working in this repository. `CLAUDE.md` is a
symlink to this file.

## What this is

An MCP server (`server.py` + `gp_client.py`) that reads Google Play Console **bulk
CSV reports** from a developer's Cloud Storage bucket (`pubsite_prod_rev_*`) and
exposes them as analytics tools. Python 3.11+, stdlib + `mcp`, `google-auth`,
`requests`. No build step; run `server.py` over stdio.

- `gp_client.py` — auth (service-account → Storage token), bucket list/download
  with caching, BOM-aware decode, CSV parse, the `FAMILIES` spec, and
  aggregation helpers.
- `server.py` — the FastMCP tool surface. Tools shape `fetch_family()` output
  into compact aggregated responses.

## Hard rules

1. **Never commit secrets or business data.** The service-account key
   (`google-play.json` / `*-service-account*.json`), `apps.json`, and the
   `.cache/`+`reports/` report data are all gitignored. Before any commit, run
   `git status` and confirm none are staged. This repo is **public**.
2. **No org-specific values in committed code.** The reports bucket, the
   service-account, and app aliases are supplied at runtime via env vars
   (`GOOGLE_PLAY_REPORTS_BUCKET`, `GOOGLE_PLAY_SA_JSON`,
   `GOOGLE_PLAY_APP_ALIASES`) or gitignored local files — never hardcoded.
3. **Read-only.** The service account uses `devstorage.read_only`. This server
   must never write to the bucket or to Play Console. Don't add write scopes or
   mutating tools.
4. **Be honest about coverage.** Google discontinued some bulk reports
   (`retained_installers`, `buyers_7d` after 2021-06; `ratings_v2` was
   2022–2023 only). Keep the `desc` fields and README coverage table accurate;
   flag historical-only families rather than implying live data.

## Adding a report family

1. Confirm the bucket prefix and filename pattern
   (`<fileprefix><package>_<YYYYMM>[_<product>]_<dimension>.csv`) by listing the
   prefix live.
2. Add an entry to `FAMILIES` in `gp_client.py` (`prefix`, `fileprefix`, `dims`,
   `default_dim`, `desc`).
3. Add a thin tool in `server.py` that calls `fetch_family()` and passes the
   result through `_shape()`; only special-case when a metric needs recomputing
   (e.g. a true conversion rate from summed numerator/denominator).
4. Handle encoding via the existing `decode_report()` (some reports are UTF-16).

## Testing

No network mocks — validate against a live bucket with real env vars set:

```bash
export GOOGLE_PLAY_SA_JSON=/abs/path/service-account.json
export GOOGLE_PLAY_REPORTS_BUCKET=pubsite_prod_rev_XXXXXXXXXXXXXXXX
./.venv/bin/python -c "import server, json; print(json.dumps(server.health_check(), indent=2))"
```

Then exercise individual tools by importing `server` and calling the tool
functions directly (they remain plain callables under the `@mcp.tool()`
decorator).

## Conventions

- Keep tool responses lean — aggregate by default, cap rows (`MAX_ROWS`), and set
  a `truncated` flag rather than dumping everything.
- Dates are ISO `YYYY-MM-DD`; default range is the last 30 days.
- `package` accepts an alias or a full package name; always route through
  `resolve_package()`.
