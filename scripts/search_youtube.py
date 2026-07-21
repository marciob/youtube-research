#!/usr/bin/env python3
"""Search YouTube and return video metadata using yt-dlp.

Uses yt-dlp's `ytsearchN:` query with --flat-playlist so results come back fast
(one metadata record per video, no per-video extraction). Prints a human-readable
list by default, or structured JSON with --json.
"""

import sys
import json
import shutil
import subprocess
import argparse

# yt-dlp's flat search extraction usually finishes in a few seconds; 60s covers
# slow networks and larger --limit values without hanging indefinitely.
SEARCH_TIMEOUT = 60


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


def search(query, limit):
    """Run a flat YouTube search. Returns (results, error).

    results is a list of dicts; error is a string (or None on success).
    """
    yt_dlp = shutil.which("yt-dlp")
    if not yt_dlp:
        return None, "yt-dlp not found. Install with: brew install yt-dlp"

    cmd = [
        yt_dlp,
        f"ytsearch{limit}:{query}",
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


def main():
    parser = argparse.ArgumentParser(description="Search YouTube for videos")
    parser.add_argument("query", help="Search query")
    parser.add_argument(
        "--limit", type=int, default=10, help="Number of results (default: 10)"
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    results, error = search(args.query, args.limit)

    if error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)

    if not results:
        print(f"No results found for: {args.query}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps({"query": args.query, "count": len(results), "results": results}))
        return

    for i, r in enumerate(results, 1):
        views = format_views(r["view_count"])
        print(f"{i}. {r['title']}")
        print(f"   {r['channel']}  ·  {r['duration_string']}  ·  {views} views")
        print(f"   {r['url']}  (id: {r['id']})")
        print()


if __name__ == "__main__":
    main()
