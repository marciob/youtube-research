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


def fetch_transcript(video_id, lang="en"):
    """Fetch transcript using yt-dlp with json3 format."""
    yt_dlp = shutil.which("yt-dlp")
    if not yt_dlp:
        print("Error: yt-dlp not found. Install with: brew install yt-dlp", file=sys.stderr)
        sys.exit(1)

    url = f"https://www.youtube.com/watch?v={video_id}"
    last_error = None

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
                "--skip-download",
                "--no-warnings",
                "--no-progress",
                "-o", out_template,
                url,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0 and result.stderr.strip():
                last_error = result.stderr.strip()

            for fname in os.listdir(tmpdir):
                if fname.endswith(".json3"):
                    fpath = os.path.join(tmpdir, fname)
                    with open(fpath, "r") as f:
                        data = json.load(f)
                    entries = parse_json3(data)
                    if entries:
                        source = "manual" if "--write-sub" in sub_args else "auto"
                        return entries, source, None
                    os.remove(fpath)

    return [], None, last_error


def main():
    parser = argparse.ArgumentParser(description="Get YouTube video transcript")
    parser.add_argument("video", help="YouTube URL or video ID")
    parser.add_argument("--timestamps", action="store_true", help="Include timestamps")
    parser.add_argument("--lang", default="en", help="Language code (default: en)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    video_id = extract_video_id(args.video)
    entries, source, error = fetch_transcript(video_id, args.lang)

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
            "source": source,
            "entries": len(entries),
            "transcript": [
                {"start": s, "text": t} for s, t in entries
            ],
        }))
    elif args.timestamps:
        for start, text in entries:
            print(f"{format_timestamp(start)} {text}")
    else:
        for _, text in entries:
            print(text)


if __name__ == "__main__":
    main()
