#!/usr/bin/env python3
"""Extract transcripts from YouTube videos using yt-dlp."""

import sys
import os
import re
import json
import subprocess
import tempfile
import argparse
import shutil


def extract_video_id(url_or_id):
    """Extract video ID from URL or return as-is."""
    patterns = [
        r"(?:v=|/v/|youtu\.be/|/embed/)([a-zA-Z0-9_-]{11})",
        r"^([a-zA-Z0-9_-]{11})$",
    ]
    for pattern in patterns:
        match = re.search(pattern, url_or_id)
        if match:
            return match.group(1)
    return url_or_id


def parse_json3(data):
    """Parse YouTube json3 subtitle format into (start_seconds, text) entries."""
    events = data.get("events", [])
    entries = []
    for event in events:
        segs = event.get("segs")
        if not segs:
            continue
        if event.get("aAppend"):
            continue
        text = "".join(seg.get("utf8", "") for seg in segs).strip()
        if not text:
            continue
        start_ms = event.get("tStartMs", 0)
        entries.append((start_ms / 1000, text))
    return entries


def format_timestamp(seconds):
    """Format seconds into [MM:SS] or [HH:MM:SS]."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"[{h}:{m:02d}:{s:02d}]"
    return f"[{m}:{s:02d}]"


def format_duration(seconds):
    """Format a duration in seconds as M:SS or H:MM:SS. Returns None if unknown."""
    if not seconds:
        return None
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def read_info_json(tmpdir):
    """Pull video metadata from the .info.json yt-dlp wrote alongside the subs."""
    for fname in os.listdir(tmpdir):
        if not fname.endswith(".info.json"):
            continue
        try:
            with open(os.path.join(tmpdir, fname), "r") as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        upload_date = d.get("upload_date")
        if upload_date and len(upload_date) == 8 and upload_date.isdigit():
            upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
        else:
            upload_date = None
        return {
            "title": d.get("title"),
            "channel": d.get("channel") or d.get("uploader"),
            "upload_date": upload_date,
            "duration": d.get("duration"),
            "duration_string": format_duration(d.get("duration")),
            "view_count": d.get("view_count"),
            "url": d.get("webpage_url"),
        }
    return {}


def fetch_transcript(video_id, lang="en"):
    """Fetch transcript using yt-dlp with json3 format.

    Returns (entries, source, error, meta). Metadata comes from --write-info-json,
    which costs nothing extra: the subtitle fetch already extracts the video page.
    """
    yt_dlp = shutil.which("yt-dlp")
    if not yt_dlp:
        print("Error: yt-dlp not found. Install with: brew install yt-dlp", file=sys.stderr)
        sys.exit(1)

    url = f"https://www.youtube.com/watch?v={video_id}"
    last_error = None
    meta = {}

    with tempfile.TemporaryDirectory() as tmpdir:
        out_template = os.path.join(tmpdir, "%(id)s")

        for sub_args in [
            ["--write-sub", "--sub-lang", lang],
            ["--write-auto-sub", "--sub-lang", lang],
            ["--write-auto-sub"],
        ]:
            cmd = [
                yt_dlp,
                *sub_args,
                "--sub-format", "json3",
                "--write-info-json",
                "--skip-download",
                "--no-warnings",
                "--no-progress",
                "-o", out_template,
                url,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0 and result.stderr.strip():
                last_error = result.stderr.strip()

            if not meta:
                meta = read_info_json(tmpdir)

            for fname in os.listdir(tmpdir):
                if fname.endswith(".json3"):
                    fpath = os.path.join(tmpdir, fname)
                    with open(fpath, "r") as f:
                        data = json.load(f)
                    entries = parse_json3(data)
                    if entries:
                        source = "manual" if "--write-sub" in sub_args else "auto"
                        return entries, source, None, meta
                    os.remove(fpath)

    return [], None, last_error, meta


def main():
    parser = argparse.ArgumentParser(description="Get YouTube video transcript")
    parser.add_argument("video", help="YouTube URL or video ID")
    parser.add_argument("--timestamps", action="store_true", help="Include timestamps")
    parser.add_argument("--lang", default="en", help="Language code (default: en)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument(
        "--no-header",
        action="store_true",
        help="Omit the title/channel/upload-date header from text output",
    )
    args = parser.parse_args()

    video_id = extract_video_id(args.video)
    entries, source, error, meta = fetch_transcript(video_id, args.lang)

    if not entries:
        if error and "Sign in to confirm" in error:
            print(f"Error: YouTube is blocking this request (bot detection). "
                  f"Video {video_id} cannot be accessed from this IP. "
                  f"Try again later or use a different video.", file=sys.stderr)
        elif error:
            msg = error.split("\n")[-1] if "\n" in error else error
            print(f"Error: Failed to fetch transcript for {video_id}: {msg}", file=sys.stderr)
        else:
            print(f"Error: No transcript found for {video_id}. "
                  f"The video may not have captions enabled.", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps({
            "video_id": video_id,
            "title": meta.get("title"),
            "channel": meta.get("channel"),
            "upload_date": meta.get("upload_date"),
            "duration": meta.get("duration"),
            "view_count": meta.get("view_count"),
            "url": meta.get("url") or f"https://www.youtube.com/watch?v={video_id}",
            "source": source,
            "entries": len(entries),
            "transcript": [
                {"start": s, "text": t} for s, t in entries
            ],
        }))
        return

    if not args.no_header and meta:
        captions = "manual captions" if source == "manual" else "auto captions"
        line2 = [x for x in (meta.get("channel"), meta.get("upload_date"),
                             meta.get("duration_string"), captions) if x]
        if meta.get("title"):
            print(f"# {meta['title']}")
        print(f"# {'  ·  '.join(line2)}")
        print(f"# {meta.get('url') or f'https://www.youtube.com/watch?v={video_id}'}")
        print()

    if args.timestamps:
        for start, text in entries:
            print(f"{format_timestamp(start)} {text}")
    else:
        for _, text in entries:
            print(text)


if __name__ == "__main__":
    main()
