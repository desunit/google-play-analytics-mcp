#!/usr/bin/env python3
"""Google Play bulk-reports client.

Reads the monthly CSV reports that Google Play Console exports to the developer's
Cloud Storage bucket (`pubsite_prod_rev_<n>`). Authenticates with a service-account
key (non-interactive) scoped for read-only Storage access.

Report families exposed (top-level bucket prefixes):
  stats/store_performance/  -> store acquisitions by traffic source / country
  acquisition/retained_installers/ -> store-listing visitors, conversion rate, retention
  acquisition/buyers_7d/    -> buyers within 7 days of install (channel / country)
  stats/installs/           -> installs / uninstalls by dimension
  stats/ratings/, ratings_v2/ -> ratings by dimension
  stats/crashes/            -> crashes / ANRs by dimension
  financial-stats/subscriptions/ -> subscription activity by country
  reviews/                  -> individual reviews

Each report file is named:
  <fileprefix><package>_<YYYYMM>[_<product>]_<dimension>.csv
and holds daily rows (a `Date` column) for one calendar month.

Config (env vars, all optional):
  GOOGLE_PLAY_SA_JSON        path to the service-account key JSON
  GOOGLE_PLAY_REPORTS_BUCKET reports bucket name (default: the Songtive bucket)
  GOOGLE_PLAY_CACHE_DIR      local cache dir for downloaded report files
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

STORAGE_SCOPE = ["https://www.googleapis.com/auth/devstorage.read_only"]
CONFIG_DIR = Path.home() / ".config" / "google-play-analytics-mcp"

# Candidate locations for the service-account key, in priority order.
_SA_CANDIDATES = [
    os.environ.get("GOOGLE_PLAY_SA_JSON"),
    str(CONFIG_DIR / "google-play.json"),
    str(Path(__file__).resolve().parent / "google-play.json"),
]


def bucket() -> str:
    """The Play Console reports bucket (`pubsite_prod_rev_<n>`). Required config —
    find it in Play Console > Download reports > any report > 'Copy Cloud Storage URI'
    (the `gs://` prefix before the first slash)."""
    b = os.environ.get("GOOGLE_PLAY_REPORTS_BUCKET", "").strip()
    if not b:
        raise RuntimeError(
            "GOOGLE_PLAY_REPORTS_BUCKET is not set. Set it to your Play Console "
            "reports bucket (e.g. 'pubsite_prod_rev_1234567890'). Find it under "
            "Play Console > Download reports > Copy Cloud Storage URI.")
    return b.removeprefix("gs://").split("/")[0]


def cache_dir() -> Path:
    d = Path(os.environ.get(
        "GOOGLE_PLAY_CACHE_DIR",
        str(Path.home() / ".cache" / "google-play-analytics-mcp")))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_aliases() -> dict[str, str]:
    """Optional friendly app aliases -> package name, merged from (1) an
    apps.json next to this file or in the config dir, and (2) the
    GOOGLE_PLAY_APP_ALIASES env var (JSON object). Entirely optional — full
    package names always work without any aliases configured."""
    aliases: dict[str, str] = {}
    for p in (Path(__file__).resolve().parent / "apps.json", CONFIG_DIR / "apps.json"):
        if p.is_file():
            try:
                aliases.update(json.loads(p.read_text()))
            except (ValueError, OSError):
                pass
    env = os.environ.get("GOOGLE_PLAY_APP_ALIASES", "").strip()
    if env:
        try:
            aliases.update(json.loads(env))
        except ValueError:
            pass
    return {k.lower(): v for k, v in aliases.items()}


APP_ALIASES = _load_aliases()


def resolve_package(name: str) -> str:
    """Map an alias to a package name; pass through anything that looks like one."""
    if not name:
        raise ValueError("package/app is required")
    return APP_ALIASES.get(name.strip().lower(), name.strip())


# --------------------------------------------------------------------------- #
# Report family specifications
# --------------------------------------------------------------------------- #

FAMILIES: dict[str, dict] = {
    "store_performance": {
        "prefix": "stats/store_performance/",
        "fileprefix": "total_store_performance_",
        "dims": ["traffic_source", "country"],
        "default_dim": "traffic_source",
        "desc": "Store acquisitions split by traffic source (Explore / Search / "
                "Ads and referrals) or by country. Acquisitions (installs) only — "
                "for visitors + conversion rate use 'store_conversion'.",
    },
    "store_conversion": {
        "prefix": "stats/store_performance/",
        "fileprefix": "store_performance_",  # the non-'total_' sibling
        "dims": ["traffic_source", "country"],
        "default_dim": "traffic_source",
        "desc": "Store-listing VISITORS, acquisitions and visitor->install "
                "CONVERSION RATE by traffic source (Explore / Search / Ads and "
                "referrals) or country (also broken down by search term & UTM in "
                "the raw rows). This is the live replacement for the retired "
                "retained_installers CVR report.",
    },
    "retained_installers": {
        "prefix": "acquisition/retained_installers/",
        "fileprefix": "retained_installers_",
        "dims": ["channel", "country", "play_country", "utm_tagged"],
        "default_dim": "channel",
        "desc": "Store-listing visitors, installers, visitor->installer conversion "
                "rate and 1/7/15/30-day retention, by acquisition channel or country. "
                "HISTORICAL ONLY: Google discontinued this bulk report after "
                "2021-06 — use store_performance for current acquisition data.",
    },
    "buyers_7d": {
        "prefix": "acquisition/buyers_7d/",
        "fileprefix": "buyers_7d_",
        "dims": ["channel", "country", "play_country"],
        "default_dim": "channel",
        "desc": "Users who made a purchase within 7 days of installing, by channel "
                "or country. HISTORICAL ONLY: discontinued after 2021-06.",
    },
    "installs": {
        "prefix": "stats/installs/",
        "fileprefix": "installs_",
        "dims": ["overview", "country", "device", "language", "app_version",
                 "os_version", "carrier"],
        "default_dim": "overview",
        "desc": "Daily/active-device installs and uninstalls by dimension.",
    },
    "ratings": {
        "prefix": "stats/ratings/",
        "fileprefix": "ratings_",
        "dims": ["overview", "country", "device", "language", "app_version",
                 "os_version", "carrier"],
        "default_dim": "overview",
        "desc": "Daily and total average rating by dimension.",
    },
    "ratings_v2": {
        "prefix": "stats/ratings_v2/",
        "fileprefix": "ratings_",
        "dims": ["overview", "country", "device", "language", "app_version",
                 "os_version", "carrier"],
        "default_dim": "overview",
        "desc": "Ratings (v2 schema) by dimension. HISTORICAL ONLY: only "
                "2022-12..2023-03 exist — use 'ratings' for current data.",
    },
    "crashes": {
        "prefix": "stats/crashes/",
        "fileprefix": "crashes_",
        "dims": ["overview", "device", "os_version", "app_version"],
        "default_dim": "overview",
        "desc": "Daily crashes and ANRs by dimension.",
    },
    "subscriptions": {
        "prefix": "financial-stats/subscriptions/",
        "fileprefix": "subscriptions_",
        "dims": ["country"],
        "default_dim": "country",
        "desc": "Subscription activity (new / cancelled / active) by country. "
                "One file per product.",
    },
    "cancellations": {
        "prefix": "subscriptions/cancellations/",
        "fileprefix": "freeform_",
        "dims": [None],
        "default_dim": None,
        "single_file": True,
        "desc": "Subscription CANCELLATION-SURVEY free-text answers (the 'Other' "
                "box of Play's cancel survey): cancellation date, SKU, country, "
                "response. One ZIP-in-.csv file per package holding the full "
                "history, rewritten daily. Google writes most rows 3x — read it "
                "through get_cancellation_reasons (dedupes). The multiple-choice "
                "reason counts are NOT in the bulk export.",
    },
    "reviews": {
        "prefix": "reviews/",
        "fileprefix": "reviews_",
        "dims": [None],
        "default_dim": None,
        "desc": "Individual user reviews with star rating and text.",
    },
}


def family_spec(family: str) -> dict:
    if family not in FAMILIES:
        raise ValueError(
            f"unknown report family '{family}'. "
            f"Known: {', '.join(sorted(FAMILIES))}")
    return FAMILIES[family]


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

_CREDS = None  # cached google.oauth2 Credentials


def _sa_path() -> str:
    for cand in _SA_CANDIDATES:
        if cand and Path(cand).expanduser().is_file():
            return str(Path(cand).expanduser())
    raise FileNotFoundError(
        "No service-account key found. Set GOOGLE_PLAY_SA_JSON to the key path, "
        "or place google-play.json next to this file / in "
        "~/.config/google-play-analytics-mcp/.")


def _credentials():
    global _CREDS
    if _CREDS is None:
        from google.oauth2 import service_account
        _CREDS = service_account.Credentials.from_service_account_file(
            _sa_path(), scopes=STORAGE_SCOPE)
    return _CREDS


def access_token() -> str:
    import google.auth.transport.requests as gtr
    creds = _credentials()
    if not creds.valid:
        creds.refresh(gtr.Request())
    return creds.token


def service_account_email() -> str:
    with open(_sa_path()) as f:
        return json.load(f).get("client_email", "?")


# --------------------------------------------------------------------------- #
# Bucket access
# --------------------------------------------------------------------------- #

_LIST_CACHE: dict[str, list[dict]] = {}


def _request(url: str) -> bytes:
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {access_token()}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def list_objects(prefix: str, use_cache: bool = True) -> list[dict]:
    """List all objects under a bucket prefix (paginated). Cached per process."""
    if use_cache and prefix in _LIST_CACHE:
        return _LIST_CACHE[prefix]
    items: list[dict] = []
    page = None
    while True:
        qs = {"prefix": prefix, "maxResults": "1000",
              "fields": "items(name,size,updated),nextPageToken"}
        if page:
            qs["pageToken"] = page
        url = (f"https://storage.googleapis.com/storage/v1/b/{bucket()}/o?"
               + urllib.parse.urlencode(qs))
        data = json.loads(_request(url))
        items.extend(data.get("items", []))
        page = data.get("nextPageToken")
        if not page:
            break
    if use_cache:
        _LIST_CACHE[prefix] = items
    return items


def list_prefixes(prefix: str = "") -> list[str]:
    """List immediate 'directory' prefixes under `prefix` (delimiter=/)."""
    qs = {"delimiter": "/", "maxResults": "1000", "fields": "prefixes"}
    if prefix:
        qs["prefix"] = prefix
    url = (f"https://storage.googleapis.com/storage/v1/b/{bucket()}/o?"
           + urllib.parse.urlencode(qs))
    return json.loads(_request(url)).get("prefixes", [])


def object_meta(name: str) -> dict | None:
    """Bucket-side {size, updated} for one object, read from the (per-process
    cached) listing of its parent prefix. None if the object isn't listed."""
    prefix = name.rsplit("/", 1)[0] + "/"
    try:
        for it in list_objects(prefix):
            if it.get("name") == name:
                return {"size": it.get("size"), "updated": it.get("updated")}
    except Exception:  # noqa: BLE001 — listing failure => unverifiable
        return None
    return None


def _meta_path(cached: Path) -> Path:
    return cached.with_name(cached.name + ".meta.json")


def download_object(name: str, use_cache: bool = True) -> bytes:
    """Download one object's bytes, caching to the local cache dir.

    The cache is validated against the object's bucket-side `updated`/`size`
    metadata on every read. Play REWRITES the current month's report file each
    day, so an unvalidated cache silently serves a truncated month — the reason
    this check exists. If the metadata can't be fetched the object is
    re-downloaded, falling back to the cached copy only if that fails.
    """
    safe = name.replace("/", "__")
    cached = cache_dir() / safe
    stamp = _meta_path(cached)
    have_cache = cached.is_file() and cached.stat().st_size > 0
    meta = object_meta(name) if use_cache else None

    if use_cache and have_cache and meta is not None:
        try:
            saved = json.loads(stamp.read_text())
            # Bucket metadata must match AND the file must still be the size we
            # wrote. The bucket reports the *compressed* size while `alt=media`
            # returns it decompressed, so the on-disk length is stamped
            # separately — this is what catches a truncated/corrupt cache file.
            if (saved.get("size") == meta["size"]
                    and saved.get("updated") == meta["updated"]
                    and saved.get("local_size") == cached.stat().st_size):
                return cached.read_bytes()
        except (OSError, ValueError):
            pass  # missing/corrupt stamp => treat as stale

    enc = urllib.parse.quote(name, safe="")
    url = (f"https://storage.googleapis.com/storage/v1/b/{bucket()}/o/{enc}"
           "?alt=media")
    try:
        raw = _request(url)
    except Exception:  # noqa: BLE001
        if have_cache:
            return cached.read_bytes()
        raise
    if use_cache:
        cached.write_bytes(raw)
        if meta is not None:
            stamp.write_text(json.dumps({**meta, "local_size": len(raw)}))
        else:
            stamp.unlink(missing_ok=True)
    return raw


def cache_stats() -> dict:
    """Entry count and size of the local report cache."""
    d = cache_dir()
    files = [p for p in d.glob("*") if p.is_file()
             and not p.name.endswith(".meta.json")]
    unverified = [p.name for p in files if not _meta_path(p).is_file()]
    return {
        "dir": str(d),
        "entries": len(files),
        "bytes": sum(p.stat().st_size for p in files),
        "unverified_entries": len(unverified),
    }


def purge_cache(pattern: str | None = None) -> dict:
    """Delete cached report files (and their validation stamps).

    `pattern` is a substring match on the cache filename, e.g. '202607' for one
    month or a package name. None purges everything.
    """
    removed = []
    for p in cache_dir().glob("*"):
        if not p.is_file():
            continue
        if pattern and pattern not in p.name:
            continue
        p.unlink(missing_ok=True)
        if not p.name.endswith(".meta.json"):
            removed.append(p.name)
    _LIST_CACHE.clear()
    return {"removed": len(removed), "pattern": pattern, "files": removed[:50]}


# --------------------------------------------------------------------------- #
# Decoding & parsing
# --------------------------------------------------------------------------- #

def unzip_if_needed(raw: bytes) -> bytes:
    """Some Play exports are a ZIP archive under a `.csv` name (the
    subscription-cancellation file is). Return the first member's bytes."""
    if raw[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            members = [n for n in z.namelist() if not n.endswith("/")]
            return z.read(members[0]) if members else b""
    return raw


def decode_report(raw: bytes) -> str:
    """Play reports are UTF-16 (installs/ratings/crashes) or UTF-8 (acquisition).
    Detect via BOM, then fall back. ZIP-wrapped files are unpacked first."""
    raw = unzip_if_needed(raw)
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    if raw[:3] == b"\xef\xbb\xbf":
        return raw.decode("utf-8-sig")
    for enc in ("utf-8", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_csv(text: str) -> list[dict]:
    # newline="" keeps line breaks INSIDE quoted fields (free-text answers).
    reader = csv.DictReader(io.StringIO(text, newline=""))
    return [dict(row) for row in reader]


# --------------------------------------------------------------------------- #
# Month-window helpers
# --------------------------------------------------------------------------- #

def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def months_in_range(start: date, end: date) -> list[str]:
    """List YYYYMM strings covering [start, end] inclusive."""
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y}{m:02d}")
        m += 1
        if m == 13:
            m, y = 1, y + 1
    return out


# --------------------------------------------------------------------------- #
# Core fetch
# --------------------------------------------------------------------------- #

def _matching_files(spec: dict, package: str, months: set[str],
                    dimension: str | None) -> list[str]:
    prefix = spec["prefix"]
    fileprefix = spec["fileprefix"]
    names = [it["name"] for it in list_objects(prefix)]
    matched = []
    for n in names:
        base = n.rsplit("/", 1)[-1]
        if not base.startswith(fileprefix):
            continue
        if package not in base:
            continue
        mo = re.search(r"_(\d{6})", base)
        if not mo or mo.group(1) not in months:
            continue
        if dimension:
            if not base.endswith(f"_{dimension}.csv"):
                continue
        else:
            # No dimension (e.g. reviews): file ends right after the month.
            if not re.search(r"_\d{6}\.csv$", base):
                continue
        matched.append(n)
    return sorted(matched)


def fetch_family(family: str, package: str, start_date: str, end_date: str,
                 dimension: str | None = "__default__") -> dict:
    """Return parsed, date-filtered rows for a report family over a date range.

    Returns { package, family, dimension, date_range, files, columns, rows }.
    """
    spec = family_spec(family)
    pkg = resolve_package(package)
    if dimension == "__default__":
        dimension = spec["default_dim"]
    if dimension is not None and dimension not in spec["dims"]:
        raise ValueError(
            f"dimension '{dimension}' not valid for '{family}'. "
            f"Valid: {[d for d in spec['dims'] if d]}")

    start = _parse_date(start_date)
    end = _parse_date(end_date)
    months = set(months_in_range(start, end))

    if spec.get("single_file"):
        files = [it["name"] for it in list_objects(spec["prefix"])
                 if it["name"].rsplit("/", 1)[-1].startswith(spec["fileprefix"] + pkg + ".")]
    else:
        files = _matching_files(spec, pkg, months, dimension)
    rows: list[dict] = []
    columns: list[str] = []
    seen_dates: set[date] = set()
    for name in files:
        text = decode_report(download_object(name))
        parsed = parse_csv(text)
        if parsed and not columns:
            columns = list(parsed[0].keys())
        for row in parsed:
            d = _row_date(row)
            if d is None or start <= d <= end:
                rows.append(row)
                if d is not None:
                    seen_dates.add(d)
    return {
        "package": pkg,
        "family": family,
        "dimension": dimension,
        "date_range": {"start": start_date, "end": end_date},
        "files": [f.rsplit("/", 1)[-1] for f in files],
        "columns": columns,
        "rows": rows,
        "coverage": _coverage(seen_dates, start, end),
    }


def _coverage(seen: set[date], start: date, end: date) -> dict:
    """Which requested days actually have rows.

    Play publishes with a lag and occasionally drops a day. Comparing two
    windows without checking this silently compares e.g. 7 days against 4.
    """
    from datetime import timedelta
    expected = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    missing = [d.isoformat() for d in expected if d not in seen]
    latest = max(seen).isoformat() if seen else None
    cov = {
        "data_through": latest,
        "requested_through": end.isoformat(),
        "days_expected": len(expected),
        "days_present": len(expected) - len(missing),
        "missing_dates": missing[:40],
    }
    if latest and latest < end.isoformat():
        cov["lag_days"] = (end - max(seen)).days
    if missing:
        interior = [d for d in missing if latest and d < latest]
        if interior:
            cov["warning"] = (
                f"{len(interior)} day(s) missing INSIDE the range "
                f"({', '.join(interior[:5])}) — per-day averages, not sums, "
                "are needed to compare this window with another.")
    return cov


def _row_date(row: dict) -> date | None:
    for key in ("Date", "date", "Cancellation Date"):
        if key in row and row[key]:
            try:
                return _parse_date(row[key][:10])
            except ValueError:
                return None
    return None


# --------------------------------------------------------------------------- #
# Aggregation helpers
# --------------------------------------------------------------------------- #

def to_number(v: str):
    if v is None:
        return 0
    s = str(v).strip().replace(",", "")
    if s in ("", "NA", "N/A"):
        return 0
    try:
        f = float(s)
        return int(f) if f.is_integer() else f
    except ValueError:
        return s


def aggregate(rows: list[dict], group_col: str, value_cols: list[str],
              how: str = "sum") -> list[dict]:
    """Group rows by `group_col`, summing/averaging `value_cols`."""
    buckets: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for row in rows:
        key = row.get(group_col, "")
        b = buckets.setdefault(key, {group_col: key})
        counts[key] = counts.get(key, 0) + 1
        for col in value_cols:
            if col not in row:
                continue
            val = to_number(row[col])
            if isinstance(val, (int, float)):
                b[col] = b.get(col, 0) + val
    if how == "avg":
        for key, b in buckets.items():
            for col in value_cols:
                if col in b:
                    b[col] = round(b[col] / max(counts[key], 1), 4)
    return sorted(buckets.values(),
                  key=lambda r: (r.get(value_cols[0], 0)
                                 if value_cols and isinstance(
                                     r.get(value_cols[0]), (int, float)) else 0),
                  reverse=True)


def discover_packages() -> list[str]:
    """Scan the installs prefix to list packages that have reports."""
    pkgs = set()
    for it in list_objects("stats/installs/"):
        base = it["name"].rsplit("/", 1)[-1]
        mo = re.match(r"installs_(.+?)_\d{6}_", base)
        if mo:
            pkgs.add(mo.group(1))
    return sorted(pkgs)
