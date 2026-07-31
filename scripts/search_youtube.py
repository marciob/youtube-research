#!/usr/bin/env python3
"""Search YouTube and return video metadata (including upload date) using yt-dlp.

Two stages, because yt-dlp's fast `--flat-playlist` search does not carry upload
dates at all:

  1. `ytsearchN:` with --flat-playlist -> id / title / channel / duration / views
  2. per-video `--print %(upload_date)s`, run concurrently -> the upload date

Measured on a 12-result search: flat + concurrent hydration = 4.3s, versus 17.4s
for a single sequential non-flat extraction of the same 12 videos.

Prints a human-readable list by default, or structured JSON with --json.
"""

import os
import sys
import json
import shutil
import subprocess
import argparse
import datetime
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _throttle

# yt-dlp's flat search extraction usually finishes in a few seconds; 60s covers
# slow networks and larger --limit values without hanging indefinitely.
SEARCH_TIMEOUT = 60

# Per-video metadata extraction is a separate, much smaller request.
METADATA_TIMEOUT = 45

# Concurrency for date hydration. 12 workers is measurably faster and reliably
# gets this IP blocked; 4 plus --sleep-requests keeps the burst under YouTube's
# tolerance. Dates are a convenience -- losing them to a block costs far more
# than the seconds saved. Wide sweeps should use --no-dates, not more workers.
MAX_WORKERS = 4

# Seconds yt-dlp waits between its own HTTP requests inside one extraction.
SLEEP_REQUESTS = 0.75

# Per-video requests in a single run beyond which a warning is warranted.
LARGE_RUN_REQUESTS = 50

# Date filters drop candidates, so over-fetch before filtering or a --limit 5
# search over a narrow window would return almost nothing. The floor matters as
# much as the factor: a narrow window can eliminate all but one of the top 12
# relevance hits, so a small --limit must still look deep.
OVERFETCH_FACTOR = 4
MIN_CANDIDATES = 20

# Default ceiling on how deep a filtered search digs. It scales with --limit so
# that asking for more results always widens the pool -- a fixed ceiling would
# silently make large filtered searches unsatisfiable. It scales at 2x rather
# than OVERFETCH_FACTOR because every candidate is one more request: a 200-video
# pool reliably trips YouTube's bot detection. Override with --max-candidates.
DEFAULT_MAX_CANDIDATES = 40
CANDIDATE_SCALE = 2

# Share of failed date lookups that means "throttled", not "undated videos".
DATE_FAILURE_THRESHOLD = 0.5

RECENT_WINDOWS = {"day": 1, "week": 7, "month": 30, "year": 365}


def format_duration(seconds):
    """Format a duration in seconds as M:SS or H:MM:SS. Returns '?' if unknown."""
    if not seconds:
        return "?"
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_views(views):
    """Format a view count compactly (e.g. 1.4M, 288K). Returns '?' if unknown."""
    if views is None:
        return "?"
    if views >= 1_000_000:
        return f"{views / 1_000_000:.1f}M"
    if views >= 1_000:
        return f"{views / 1_000:.0f}K"
    return str(views)


def parse_date_arg(value, flag):
    """Parse YYYY-MM-DD or YYYYMMDD into a comparable YYYYMMDD string."""
    raw = value.strip().replace("-", "")
    if len(raw) != 8 or not raw.isdigit():
        raise ValueError(f"{flag} expects YYYY-MM-DD (got: {value})")
    try:
        datetime.date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
    except ValueError:
        raise ValueError(f"{flag} is not a real date: {value}")
    return raw


def iso_date(compact):
    """20251114 -> 2025-11-14. Returns None for missing/unknown dates."""
    if not compact or len(compact) != 8 or not compact.isdigit():
        return None
    return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"


def resolve_window(args):
    """Turn --after/--before/--recent into (after, before) YYYYMMDD strings."""
    if args.recent and (args.after or args.before):
        raise ValueError("--recent cannot be combined with --after/--before")

    if args.recent:
        days = RECENT_WINDOWS[args.recent]
        start = datetime.date.today() - datetime.timedelta(days=days)
        return start.strftime("%Y%m%d"), None

    after = parse_date_arg(args.after, "--after") if args.after else None
    before = parse_date_arg(args.before, "--before") if args.before else None
    if after and before and after > before:
        raise ValueError(
            f"--after ({iso_date(after)}) is later than --before ({iso_date(before)})"
        )
    return after, before


def flat_search(yt_dlp, query, count):
    """Fast flat search. Returns (results, error); results carry no upload date."""
    cmd = [
        yt_dlp,
        f"ytsearch{count}:{query}",
        "--flat-playlist",
        "--dump-json",
        "--skip-download",
        "--no-warnings",
        "--no-progress",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=SEARCH_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        return None, f"Search timed out after {SEARCH_TIMEOUT}s."

    results = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        results.append(
            {
                "id": d.get("id"),
                "title": d.get("title"),
                "channel": d.get("channel") or d.get("uploader"),
                "channel_id": d.get("channel_id"),
                "duration": d.get("duration"),
                "duration_string": format_duration(d.get("duration")),
                "view_count": d.get("view_count"),
                "upload_date": None,
                "url": d.get("url") or d.get("webpage_url"),
            }
        )

    if not results:
        err = result.stderr.strip()
        if err and "Sign in to confirm" in err:
            return None, (
                "YouTube is blocking this request (bot detection) from this IP. "
                "Try again later."
            )
        if err:
            return None, err.split("\n")[-1]
        return [], None
    return results, None


def fetch_upload_date(yt_dlp, video_id, breaker):
    """Extract one video's upload date as YYYYMMDD. Returns None if unavailable.

    Once `breaker` has tripped this spends no request at all: continuing to hammer
    a blocked endpoint only prolongs the block.
    """
    if breaker.tripped:
        return None
    cmd = [
        yt_dlp,
        f"https://www.youtube.com/watch?v={video_id}",
        "--print", "%(upload_date)s",
        "--skip-download",
        "--no-warnings",
        "--no-progress",
        "--sleep-requests", str(SLEEP_REQUESTS),
        "--extractor-retries", "1",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=METADATA_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0 and _throttle.is_block_error(result.stderr):
        breaker.trip()
        return None
    value = result.stdout.strip().splitlines()
    if not value:
        return None
    value = value[0].strip()
    if len(value) != 8 or not value.isdigit():
        return None
    return value


def hydrate_dates(yt_dlp, results):
    """Fill in upload_date for each result, concurrently. Preserves input order.

    Returns (results, failures, blocked). Failures are counted rather than
    swallowed: when YouTube throttles, every lookup returns None, and a filtered
    search would otherwise report "nothing matched your window" when the truth is
    "no dates could be determined". Those two must never look alike.
    """
    if not results:
        return results, 0, False
    breaker = _throttle.Breaker()
    workers = min(MAX_WORKERS, len(results))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        dates = list(
            pool.map(lambda r: fetch_upload_date(yt_dlp, r["id"], breaker), results)
        )
    for result, date in zip(results, dates):
        result["upload_date"] = date
    if any(dates) and not breaker.tripped:
        # A successful lookup proves any *earlier* block has lifted. Never clear
        # on a run that tripped: partial success before the block still means we
        # are blocked now, and clearing would let the next run burst straight in.
        _throttle.clear_block()
    return results, sum(1 for d in dates if not d), breaker.tripped


def main():
    parser = argparse.ArgumentParser(description="Search YouTube for videos")
    parser.add_argument("query", help="Search query")
    parser.add_argument(
        "--limit", type=int, default=10, help="Number of results (default: 10)"
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--after", help="Only videos uploaded on/after YYYY-MM-DD")
    parser.add_argument("--before", help="Only videos uploaded on/before YYYY-MM-DD")
    parser.add_argument(
        "--recent",
        choices=sorted(RECENT_WINDOWS),
        help="Shorthand window: day | week | month | year",
    )
    parser.add_argument(
        "--no-dates",
        action="store_true",
        help="Skip upload-date lookup for a faster, dateless list",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        help="Cap how many videos a filtered search examines "
             "(default: max(40, --limit x 2))",
    )
    parser.add_argument(
        "--ignore-cooldown",
        action="store_true",
        help="Attempt per-video lookups even if YouTube blocked us recently",
    )
    args = parser.parse_args()

    if args.limit < 1:
        print("Error: --limit must be at least 1", file=sys.stderr)
        sys.exit(1)

    try:
        after, before = resolve_window(args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    filtering = bool(after or before)
    if filtering and args.no_dates:
        print("Error: --no-dates cannot be combined with a date filter",
              file=sys.stderr)
        sys.exit(1)

    yt_dlp = shutil.which("yt-dlp")
    if not yt_dlp:
        print("Error: yt-dlp not found. Install with: brew install yt-dlp",
              file=sys.stderr)
        sys.exit(1)

    # A recent block means per-video lookups will fail and each attempt prolongs
    # it. The flat search itself survives throttling, so degrade to it rather
    # than refusing outright -- except for a filter, which needs real dates.
    cooling = 0 if (args.no_dates or args.ignore_cooldown) else \
        _throttle.cooldown_remaining()
    if cooling:
        left = _throttle.format_cooldown(cooling)
        if filtering:
            print(f"Error: YouTube blocked per-video requests from this IP "
                  f"recently; {left} of cooldown left. A date filter needs real "
                  f"dates, and retrying now would extend the block. Wait, use "
                  f"--no-dates without a filter, or override with "
                  f"--ignore-cooldown.", file=sys.stderr)
            sys.exit(1)
        print(f"Warning: skipping upload-date lookups — blocked recently, {left} "
              f"of cooldown left. Results have no dates; --ignore-cooldown "
              f"overrides.", file=sys.stderr)
        args.no_dates = True

    # Filtering discards candidates, so search wider than the caller asked for.
    candidates = args.limit
    if filtering:
        # The ceiling scales with --limit, so a larger request always digs deeper
        # rather than hitting a fixed wall it cannot report its way out of.
        ceiling = args.max_candidates or max(
            DEFAULT_MAX_CANDIDATES, args.limit * CANDIDATE_SCALE
        )
        candidates = min(max(args.limit * OVERFETCH_FACTOR, MIN_CANDIDATES), ceiling)

    results, error = flat_search(yt_dlp, args.query, candidates)

    if error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)

    if not results:
        print(f"No results found for: {args.query}", file=sys.stderr)
        sys.exit(1)

    examined = len(results)
    unknown_date = 0
    failures = 0
    blocked = False

    if not args.no_dates:
        if len(results) > LARGE_RUN_REQUESTS:
            print(f"Warning: this run will make {len(results)} per-video requests, "
                  f"which risks a temporary block. --no-dates does the same search "
                  f"in one request.", file=sys.stderr)
        results, failures, blocked = hydrate_dates(yt_dlp, results)

    # Mass lookup failure means YouTube is throttling, not that the videos are
    # undated. Filtering on top of that would manufacture a confident empty
    # result, so refuse rather than answer from data we do not have.
    if failures and failures >= max(1, len(results) * DATE_FAILURE_THRESHOLD):
        detail = (f"{failures}/{len(results)} upload-date lookups failed"
                  if failures < len(results) else "every upload-date lookup failed")
        if blocked:
            detail += (f"; stopped early on bot detection and started a "
                       f"{_throttle.format_cooldown(_throttle.cooldown_seconds())} "
                       f"cooldown")
        if filtering:
            print(f"Error: {detail}. A date filter cannot be applied without "
                  f"dates; results would look like 'no matches' when they are "
                  f"really 'unknown'. Wait out the cooldown, or search with "
                  f"--no-dates and no filter.", file=sys.stderr)
            sys.exit(1)
        print(f"Warning: {detail}. Those results show no date; use --no-dates to "
              f"skip the lookups entirely, or wait and retry.", file=sys.stderr)

    if filtering:
        kept = []
        for r in results:
            date = r["upload_date"]
            if not date:
                unknown_date += 1
                continue
            if after and date < after:
                continue
            if before and date > before:
                continue
            kept.append(r)
        results = kept

    truncated = max(0, len(results) - args.limit)

    # A short result set has two very different causes: the window is genuinely
    # sparse, or the candidate pool ran out first. Only the second one means
    # "more probably exist" -- never let them look alike.
    depth_capped = filtering and len(results) < args.limit and examined >= candidates

    results = results[: args.limit]

    for r in results:
        r["upload_date"] = iso_date(r["upload_date"])

    if args.json:
        payload = {"query": args.query, "count": len(results), "results": results}
        if filtering:
            payload["filter"] = {
                "after": iso_date(after),
                "before": iso_date(before),
                "recent": args.recent,
                "candidates_examined": examined,
                "candidate_ceiling": candidates,
                "excluded_unknown_date": unknown_date,
                "matched_beyond_limit": truncated,
                "depth_capped": depth_capped,
            }
        print(json.dumps(payload))
        return

    if not results:
        window = " to ".join(x for x in (iso_date(after), iso_date(before)) if x)
        print(f"No results for: {args.query} (within {window}; "
              f"examined {examined} candidates)", file=sys.stderr)
        sys.exit(1)

    for i, r in enumerate(results, 1):
        views = format_views(r["view_count"])
        date = r["upload_date"] or "date ?"
        print(f"{i}. {r['title']}")
        print(f"   {r['channel']}  ·  {r['duration_string']}  ·  {views} views"
              f"  ·  {date}")
        print(f"   {r['url']}  (id: {r['id']})")
        print()

    if filtering:
        notes = [f"examined {examined} candidates"]
        if unknown_date:
            notes.append(f"{unknown_date} skipped (upload date unavailable)")
        if truncated:
            notes.append(f"{truncated} more matched beyond --limit")
        if depth_capped:
            notes.append(
                f"stopped at the {candidates}-candidate ceiling with only "
                f"{len(results)}/{args.limit} filled — more may exist, "
                f"raise --max-candidates to dig deeper"
            )
        print(f"({'; '.join(notes)})", file=sys.stderr)


if __name__ == "__main__":
    main()
