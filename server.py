#!/usr/bin/env python3
"""google-play-analytics-mcp

An MCP server exposing Google Play Console analytics — store-listing views,
conversion rates, acquisitions by traffic source (Explore / Search / Ads &
referrals), installs, ratings, crashes, subscriptions and reviews — read from
the developer's Play Console bulk-reports Cloud Storage bucket.

Data granularity: reports are monthly files of daily rows. The most recent
complete data is typically the current or previous month (Play publishes with a
lag of a day or two).
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

from mcp.server.fastmcp import FastMCP

import gp_client as gp

mcp = FastMCP("google-play-analytics")

MAX_ROWS = 200  # cap serialized rows to keep responses lean


# --------------------------------------------------------------------------- #
# Shaping helpers
# --------------------------------------------------------------------------- #

def _default_range(start_date: str | None, end_date: str | None) -> tuple[str, str]:
    end = _dt.date.fromisoformat(end_date) if end_date else _dt.date.today()
    if start_date:
        start = _dt.date.fromisoformat(start_date)
    else:
        start = end - _dt.timedelta(days=30)
    return start.isoformat(), end.isoformat()


def _is_numeric_col(rows: list[dict], col: str) -> bool:
    seen = hits = 0
    for r in rows[:50]:
        v = r.get(col)
        if v in (None, ""):
            continue
        seen += 1
        if isinstance(gp.to_number(v), (int, float)):
            hits += 1
    return seen > 0 and hits / seen >= 0.6


def _classify_columns(columns: list[str], rows: list[dict]) -> dict:
    """Split columns into date / package / label / metric buckets."""
    date_cols, pkg_cols, label_cols, metric_cols = [], [], [], []
    for c in columns:
        lc = c.lower()
        if lc in ("date",):
            date_cols.append(c)
        elif "package" in lc:
            pkg_cols.append(c)
        elif _is_numeric_col(rows, c):
            metric_cols.append(c)
        else:
            label_cols.append(c)
    return {"date": date_cols, "package": pkg_cols,
            "label": label_cols, "metric": metric_cols}


def _shape(result: dict, group_by: str | None, how: str,
           daily: bool, top: int) -> dict:
    """Turn a raw fetch_family result into an aggregated, compact response."""
    rows, columns = result["rows"], result["columns"]
    cls = _classify_columns(columns, rows)

    if not rows:
        return {**_meta(result), "note": "no rows for this package/range/dimension",
                "columns": columns, "data": []}

    if daily:
        data = rows[:MAX_ROWS]
        return {**_meta(result), "columns": columns,
                "row_count": len(rows),
                "truncated": len(rows) > MAX_ROWS,
                "data": data}

    group_col = group_by or (cls["label"][0] if cls["label"] else None)
    metrics = cls["metric"]
    if not group_col or not metrics:
        # Nothing to aggregate on — return rows as-is.
        return {**_meta(result), "columns": columns,
                "row_count": len(rows),
                "truncated": len(rows) > MAX_ROWS,
                "data": rows[:MAX_ROWS]}

    agg = gp.aggregate(rows, group_col, metrics, how=how)
    return {**_meta(result), "grouped_by": group_col,
            "metrics": metrics, "aggregation": how,
            "data": agg[:top]}


def _meta(result: dict) -> dict:
    return {"package": result["package"], "family": result["family"],
            "dimension": result["dimension"],
            "date_range": result["date_range"], "files": result["files"]}


# --------------------------------------------------------------------------- #
# Diagnostics & discovery
# --------------------------------------------------------------------------- #

@mcp.tool()
def health_check() -> dict:
    """Verify service-account auth and bucket connectivity. Run this first when
    tools fail. Returns the SA email, bucket, and top-level report prefixes."""
    try:
        email = gp.service_account_email()
        prefixes = gp.list_prefixes()
        return {"ok": True, "service_account": email, "bucket": gp.bucket(),
                "top_level_prefixes": prefixes}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e),
                "hint": "Set GOOGLE_PLAY_SA_JSON to a service-account key with "
                        "read access to the Play reports bucket, and ensure the "
                        "SA is linked in Play Console (or granted bucket IAM)."}


@mcp.tool()
def list_apps() -> dict:
    """List apps available for analysis: friendly aliases plus packages actually
    present in the reports bucket."""
    try:
        discovered = gp.discover_packages()
    except Exception as e:  # noqa: BLE001
        discovered = []
        return {"aliases": gp.APP_ALIASES, "discovered_packages": discovered,
                "warning": f"could not scan bucket: {e}"}
    return {"aliases": gp.APP_ALIASES, "discovered_packages": discovered}


@mcp.tool()
def list_report_families() -> dict:
    """List the report families this server can read, with their dimensions and a
    description of what each contains."""
    return {name: {"dimensions": [d for d in spec["dims"] if d],
                   "default_dimension": spec["default_dim"],
                   "description": spec["desc"]}
            for name, spec in gp.FAMILIES.items()}


# --------------------------------------------------------------------------- #
# Acquisition / discovery analytics (the headline questions)
# --------------------------------------------------------------------------- #

@mcp.tool()
def get_acquisitions_by_traffic_source(
        package: str, start_date: str | None = None, end_date: str | None = None,
        daily: bool = False) -> dict:
    """Store acquisitions split by traffic source — Google Play **Explore**
    (browse), Google Play **Search**, and **Ads and referrals**. This is the
    Play-Console equivalent of iOS 'App Store Browse vs Search'.

    package: alias (e.g. 'piano-companion', 'chordiq') or package name.
    date range defaults to the last 30 days. Set daily=True for per-day rows;
    otherwise totals per source with share-of-total percentages.
    """
    start, end = _default_range(start_date, end_date)
    res = gp.fetch_family("store_performance", package, start, end,
                          dimension="traffic_source")
    shaped = _shape(res, group_by="Traffic source", how="sum", daily=daily,
                    top=50)
    if not daily and shaped.get("data"):
        metric = "Total store acquisitions"
        total = sum(r.get(metric, 0) for r in shaped["data"])
        for r in shaped["data"]:
            r["share_pct"] = round(100 * r.get(metric, 0) / total, 1) if total else 0
        shaped["total_acquisitions"] = total
    return shaped


@mcp.tool()
def get_acquisitions_by_country(
        package: str, start_date: str | None = None, end_date: str | None = None,
        top: int = 25) -> dict:
    """Store acquisitions by country over a date range (default last 30 days)."""
    start, end = _default_range(start_date, end_date)
    res = gp.fetch_family("store_performance", package, start, end,
                          dimension="country")
    return _shape(res, group_by=None, how="sum", daily=False, top=top)


@mcp.tool()
def get_store_listing_conversion(
        package: str, dimension: str = "channel",
        start_date: str | None = None, end_date: str | None = None,
        top: int = 25) -> dict:
    """Store-listing visitors, installers, visitor→installer conversion rate +
    retention, from the retained-installers report. ⚠️ HISTORICAL (≤ 2021-06) —
    Google retired this export. **For CURRENT conversion rate use
    `get_store_conversion`.** Kept for historical/retention backfill only.

    dimension: 'channel' (acquisition channel), 'country', 'play_country', or
    'utm_tagged'. Visitors and installers are summed across the range and the
    conversion rate is recomputed as installers/visitors.
    """
    start, end = _default_range(start_date, end_date)
    res = gp.fetch_family("retained_installers", package, start, end,
                          dimension=dimension)
    shaped = _shape(res, group_by=None, how="sum", daily=False, top=top)
    # Recompute a true conversion rate from summed visitors/installers.
    data = shaped.get("data", [])
    vcol = _find(shaped.get("metrics", []), "visitor")
    icol = _find(shaped.get("metrics", []), "installer")
    if vcol and icol:
        for r in data:
            v = r.get(vcol, 0)
            r["conversion_rate"] = round(r.get(icol, 0) / v, 4) if v else None
    return shaped


@mcp.tool()
def get_store_conversion(
        package: str, dimension: str = "traffic_source",
        start_date: str | None = None, end_date: str | None = None,
        top: int = 25) -> dict:
    """**Store-listing conversion rate** — store-listing **visitors**,
    **acquisitions** and the **visitor→install CVR** — the CURRENT Play Console
    "Conversion analysis" data. This is the live replacement for the retired
    `get_store_listing_conversion` (retained_installers).

    dimension: 'traffic_source' (Google Play explore / search / Ads and
    referrals) or 'country'. Visitors and acquisitions are summed across the
    range and CVR is recomputed as acquisitions/visitors per group (never averaged
    — averaging the per-row rate is wrong). Returns `overall_conversion_rate` too.
    """
    start, end = _default_range(start_date, end_date)
    res = gp.fetch_family("store_conversion", package, start, end,
                          dimension=dimension)
    shaped = _shape(res, group_by=None, how="sum", daily=False, top=top)
    data = shaped.get("data", [])
    metrics = shaped.get("metrics", [])
    vcol = _find(metrics, "visitor")
    acol = _find(metrics, "acquisition")
    rate_col = _find(metrics, "conversion rate")  # raw per-row rate — summed = garbage
    tot_v = tot_a = 0
    for r in data:
        if rate_col:
            r.pop(rate_col, None)  # drop the meaningless summed rate
        v, a = r.get(vcol, 0), r.get(acol, 0)
        r["conversion_rate"] = round(a / v, 4) if v else None
        tot_v += v or 0
        tot_a += a or 0
    if tot_v:
        shaped["overall_conversion_rate"] = round(tot_a / tot_v, 4)
    return shaped


@mcp.tool()
def get_buyers(package: str, dimension: str = "channel",
               start_date: str | None = None, end_date: str | None = None,
               top: int = 25) -> dict:
    """Users who purchased within 7 days of install, by 'channel', 'country' or
    'play_country'. Useful for which acquisition source yields paying users."""
    start, end = _default_range(start_date, end_date)
    res = gp.fetch_family("buyers_7d", package, start, end, dimension=dimension)
    return _shape(res, group_by=None, how="sum", daily=False, top=top)


# --------------------------------------------------------------------------- #
# Volume / quality analytics
# --------------------------------------------------------------------------- #

@mcp.tool()
def get_installs(package: str, dimension: str = "overview",
                 start_date: str | None = None, end_date: str | None = None,
                 daily: bool = False, top: int = 25) -> dict:
    """Installs / uninstalls / active devices. dimension: 'overview' (daily
    totals), 'country', 'device', 'language', 'app_version', 'os_version',
    'carrier'. Use daily=True with dimension='overview' for a time series."""
    start, end = _default_range(start_date, end_date)
    res = gp.fetch_family("installs", package, start, end, dimension=dimension)
    return _shape(res, group_by=None, how="sum",
                  daily=daily or dimension == "overview", top=top)


@mcp.tool()
def get_ratings(package: str, dimension: str = "overview",
                start_date: str | None = None, end_date: str | None = None,
                daily: bool = False, top: int = 25) -> dict:
    """Average rating over time or by dimension ('overview', 'country', 'device',
    'language', 'app_version', 'os_version', 'carrier')."""
    start, end = _default_range(start_date, end_date)
    res = gp.fetch_family("ratings", package, start, end, dimension=dimension)
    return _shape(res, group_by=None, how="avg",
                  daily=daily or dimension == "overview", top=top)


@mcp.tool()
def get_crashes(package: str, dimension: str = "overview",
                start_date: str | None = None, end_date: str | None = None,
                daily: bool = False, top: int = 25) -> dict:
    """Crashes and ANRs. dimension: 'overview', 'device', 'os_version',
    'app_version'."""
    start, end = _default_range(start_date, end_date)
    res = gp.fetch_family("crashes", package, start, end, dimension=dimension)
    return _shape(res, group_by=None, how="sum",
                  daily=daily or dimension == "overview", top=top)


@mcp.tool()
def get_subscriptions(package: str, start_date: str | None = None,
                      end_date: str | None = None, daily: bool = False,
                      top: int = 25) -> dict:
    """Subscription activity (new / cancelled / active) by country, aggregated
    across all subscription products for the app. daily=True keeps per-day rows."""
    start, end = _default_range(start_date, end_date)
    res = gp.fetch_family("subscriptions", package, start, end,
                          dimension="country")
    return _shape(res, group_by=None, how="sum", daily=daily, top=top)


@mcp.tool()
def get_reviews(package: str, start_date: str | None = None,
                end_date: str | None = None, limit: int = 50) -> dict:
    """Individual user reviews (star rating + text) over a date range
    (default last 30 days)."""
    start, end = _default_range(start_date, end_date)
    res = gp.fetch_family("reviews", package, start, end, dimension=None)
    rows = res["rows"][:limit]
    return {**_meta(res), "columns": res["columns"],
            "row_count": len(res["rows"]),
            "truncated": len(res["rows"]) > limit, "data": rows}


# --------------------------------------------------------------------------- #
# Escape hatch
# --------------------------------------------------------------------------- #

@mcp.tool()
def fetch_raw_report(family: str, package: str, dimension: str | None = None,
                     start_date: str | None = None,
                     end_date: str | None = None) -> dict:
    """Return raw parsed rows for any report family/dimension (no aggregation).
    Use list_report_families() to see valid families and dimensions. Rows are
    capped; narrow the date range if truncated."""
    start, end = _default_range(start_date, end_date)
    dim = "__default__" if dimension is None else dimension
    res = gp.fetch_family(family, package, start, end, dimension=dim)
    rows = res["rows"]
    return {**_meta(res), "columns": res["columns"], "row_count": len(rows),
            "truncated": len(rows) > MAX_ROWS, "data": rows[:MAX_ROWS]}


def _find(cols: list[str], needle: str) -> str | None:
    for c in cols:
        if needle.lower() in c.lower():
            return c
    return None


if __name__ == "__main__":
    mcp.run()
