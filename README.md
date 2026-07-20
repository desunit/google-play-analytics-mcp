# google-play-analytics-mcp

An [MCP](https://modelcontextprotocol.io) server that exposes **Google Play Console
analytics** to AI agents — store-listing acquisitions by traffic source
(Explore / Search / Ads & referrals), installs & uninstalls, ratings, crashes,
subscriptions and reviews.

It reads the **bulk CSV reports** that Google Play Console automatically exports to
your developer Cloud Storage bucket (`pubsite_prod_rev_*`), using a Google Cloud
**service account** (fully non-interactive — no browser login, no token refresh
dance). Reports are monthly files of daily rows; the server discovers, downloads,
decodes, parses and aggregates them behind a small set of tools.

> Play Console publishes these reports with a lag of ~1–2 days, so the freshest
> data is usually yesterday or the day before.

## Why this exists

The Play Console UI shows acquisition data (how many people found your app by
**browsing** vs **searching** the Play Store, and how well your store listing
converts visitors to installers), but there's no convenient API for an agent to
read it. The underlying data, however, is exported as CSV to a Cloud Storage
bucket. This server turns that bucket into typed, queryable tools — the Android
counterpart to Apple's "App Store Browse vs Search" source-type analytics.

## Tools

| Tool | What it returns |
|------|-----------------|
| `health_check` | Verifies auth + bucket connectivity; lists top-level report prefixes |
| `list_apps` | Configured aliases + packages actually present in the bucket |
| `list_report_families` | Every report family, its dimensions and description |
| `get_acquisitions_by_traffic_source` | **Store acquisitions split by Google Play Explore / Search / Ads & referrals**, with share-of-total % |
| `get_acquisitions_by_country` | Store acquisitions by country |
| `get_store_listing_conversion` | Store-listing visitors, installers, visitor→installer conversion rate, retention *(historical: ≤2021-06, see coverage)* |
| `get_buyers` | Users who purchased within 7 days of install, by channel/country *(historical: ≤2021-06)* |
| `get_installs` | Installs / uninstalls / active devices, daily or by dimension |
| `get_ratings` | Average rating over time or by dimension |
| `get_crashes` | Crashes & ANRs, daily or by dimension |
| `get_subscriptions` | New / cancelled / active subscriptions by country |
| `get_reviews` | Individual user reviews (rating + text) |
| `fetch_raw_report` | Escape hatch — raw parsed rows for any family/dimension |

Common parameters: `package` (an alias or a full package name), `start_date` /
`end_date` (ISO `YYYY-MM-DD`, default = last 30 days), and per-tool `dimension`,
`daily`, `top`.

### Report families & data coverage

| Family | Bucket prefix | Current data? | Dimensions |
|--------|---------------|---------------|-----------|
| `store_performance` | `stats/store_performance/` | ✅ live | `traffic_source`, `country` |
| `installs` | `stats/installs/` | ✅ live | `overview`, `country`, `device`, `language`, `app_version`, `os_version`, `carrier` |
| `ratings` | `stats/ratings/` | ✅ live | same as installs |
| `crashes` | `stats/crashes/` | ✅ live | `overview`, `device`, `os_version`, `app_version` |
| `subscriptions` | `financial-stats/subscriptions/` | ✅ live | `country` |
| `reviews` | `reviews/` | ✅ live | — |
| `retained_installers` | `acquisition/retained_installers/` | ⚠️ ≤ 2021-06 | `channel`, `country`, `play_country`, `utm_tagged` |
| `buyers_7d` | `acquisition/buyers_7d/` | ⚠️ ≤ 2021-06 | `channel`, `country`, `play_country` |

> Google **discontinued** the `retained_installers` and `buyers_7d` bulk exports
> after June 2021 (and `ratings_v2` was a short-lived 2022–2023 schema). For
> current store-listing conversion and acquisition data, use
> `store_performance`. The historical families remain queryable for backfill.

## Prerequisites

1. A Google Play Console account with reports being exported to a Cloud Storage
   bucket (this is on by default; the bucket is `pubsite_prod_rev_<number>`).
2. A **Google Cloud service account** with **read access** to that bucket, plus its
   JSON key. Two ways to grant access:
   - **Play Console link** (recommended): Play Console → *Users and permissions* →
     invite the service-account email → grant *View app information and download
     bulk reports*; **or**
   - **Bucket IAM**: grant the service account `roles/storage.objectViewer` on the
     `pubsite_prod_rev_*` bucket in the Google Cloud console.
3. Python 3.11+.

## Install

```bash
git clone git@github.com:desunit/google-play-analytics-mcp.git
cd google-play-analytics-mcp
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

## Configure

The server is configured entirely through environment variables (no secrets in
the repo):

| Variable | Required | Meaning |
|----------|----------|---------|
| `GOOGLE_PLAY_SA_JSON` | yes* | Path to the service-account key JSON |
| `GOOGLE_PLAY_REPORTS_BUCKET` | **yes** | Reports bucket, e.g. `pubsite_prod_rev_1234567890` |
| `GOOGLE_PLAY_APP_ALIASES` | no | JSON object of `{"alias": "com.pkg"}` |
| `GOOGLE_PLAY_CACHE_DIR` | no | Download cache dir (default `~/.cache/google-play-analytics-mcp`) |

\* If `GOOGLE_PLAY_SA_JSON` is unset, the server also looks for
`google-play.json` in `~/.config/google-play-analytics-mcp/` or next to
`server.py`. **Never commit the key** — it's gitignored.

**Finding your bucket name:** Play Console → *Download reports* → open any report
category → *Copy Cloud Storage URI*. The bucket is the part between `gs://` and the
first `/`.

**App aliases (optional):** copy `apps.example.json` → `apps.json` (gitignored) to
map short names like `my-app` to package names, or set `GOOGLE_PLAY_APP_ALIASES`.
Full package names always work without aliases.

### Register with your MCP client

Copy `.mcp.json.example` and fill in absolute paths, or add to your client config:

```json
{
  "mcpServers": {
    "google-play-analytics": {
      "command": "/abs/path/google-play-analytics-mcp/.venv/bin/python",
      "args": ["/abs/path/google-play-analytics-mcp/server.py"],
      "env": {
        "GOOGLE_PLAY_SA_JSON": "/abs/path/service-account.json",
        "GOOGLE_PLAY_REPORTS_BUCKET": "pubsite_prod_rev_XXXXXXXXXXXXXXXX"
      }
    }
  }
}
```

For **Claude Code**, the equivalent one-liner:

```bash
claude mcp add google-play-analytics \
  -e GOOGLE_PLAY_SA_JSON=/abs/path/service-account.json \
  -e GOOGLE_PLAY_REPORTS_BUCKET=pubsite_prod_rev_XXXXXXXXXXXXXXXX \
  -- /abs/path/.venv/bin/python /abs/path/server.py
```

## Verify

```bash
export GOOGLE_PLAY_SA_JSON=/abs/path/service-account.json
export GOOGLE_PLAY_REPORTS_BUCKET=pubsite_prod_rev_XXXXXXXXXXXXXXXX
./.venv/bin/python -c "import server, json; print(json.dumps(server.health_check(), indent=2))"
```

A healthy response lists your bucket's top-level prefixes
(`acquisition/`, `stats/`, `financial-stats/`, `reviews/`, …).

## Example questions it answers

- *"What share of my Play Store installs come from browsing (Explore) vs Search?"*
  → `get_acquisitions_by_traffic_source`
- *"Which countries drive the most store acquisitions this month?"*
  → `get_acquisitions_by_country`
- *"What's my daily install and uninstall trend?"* → `get_installs(daily=True)`
- *"How is my rating trending?"* → `get_ratings`
- *"Which app versions crash most?"* → `get_crashes(dimension="app_version")`

## How it works

```
service account key ──► google-auth ──► short-lived Storage token
                                             │
        GCS JSON API: list + download objects under pubsite_prod_rev_*
                                             │
     BOM-aware decode (UTF-16 / UTF-8) ──► CSV parse ──► date-filter ──► aggregate
```

Downloaded report files are cached under `GOOGLE_PLAY_CACHE_DIR` so repeat queries
don't re-fetch. Object listings are cached per process.

## Security

- **No credentials are stored in this repository.** The service-account key, the
  bucket name and any app aliases are all supplied at runtime via env vars or
  gitignored local files (`google-play.json`, `apps.json`).
- The service account only needs **read-only** Storage scope
  (`devstorage.read_only`). This server never writes to your bucket or Play
  Console.
- Downloaded reports (which contain your business data) are cached **outside** the
  repo tree by default (`~/.cache/...`).

## License

MIT — see [LICENSE](LICENSE).
