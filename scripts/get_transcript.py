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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _throttle

# Long talks plus --sleep-requests and a retry can outlast a short timeout, and
# a timeout wastes the whole extraction. Generous, since it only bounds a hang.
TRANSCRIPT_TIMEOUT = 180

# Metadata is printed to stdout as one tagged line, and only *then* read from
# .info.json. yt-dlp writes that file near the end of a run, so anything that
# fails late -- a no-formats video, or a 429 from the subtitle CDN after the
# extraction itself succeeded -- leaves subtitles on disk and no metadata at all.
# Observed: an error on the subtitle download aborted the run with zero files
# written, despite the page extraction having already returned title and date.
#
# --print alone would be a trap: it implies --simulate, which stops yt-dlp
# writing any subtitle file, yielding perfect metadata and an empty transcript.
# --no-simulate is what makes the two coexist.
#
# The separator is ASCII US (0x1f), which cannot occur in a YouTube title.
META_TAG = "YTRMETA"
META_SEP = "\x1f"
META_FIELDS = ("id", "title", "channel", "upload_date", "duration",
               "view_count", "webpage_url", "language")


def print_template():
    """One tagged, separator-delimited line carrying the core metadata."""
    return META_TAG + META_SEP + META_SEP.join(f"%({f})s" for f in META_FIELDS)


def parse_printed_meta(stdout):
    """Parse the tagged --print line. Returns {} when it is absent."""
    for line in (stdout or "").splitlines():
        if not line.startswith(META_TAG + META_SEP):
            continue
        parts = line.rstrip("\n").split(META_SEP)
        if len(parts) != len(META_FIELDS) + 1:
            continue
        out = {}
        for name, raw in zip(META_FIELDS, parts[1:]):
            value = raw.strip()
            # yt-dlp prints "NA" for fields it could not resolve.
            out[name] = None if value in ("", "NA", "None") else value
        upload = out.get("upload_date")
        if upload and len(upload) == 8 and upload.isdigit():
            out["upload_date"] = f"{upload[:4]}-{upload[4:6]}-{upload[6:]}"
        else:
            out["upload_date"] = None
        for numeric in ("duration", "view_count"):
            try:
                out[numeric] = int(float(out[numeric])) if out[numeric] else None
            except (TypeError, ValueError):
                out[numeric] = None
        return {
            "title": out["title"],
            "channel": out["channel"],
            "upload_date": out["upload_date"],
            "duration": out["duration"],
            "duration_string": format_duration(out["duration"]),
            "view_count": out["view_count"],
            "url": out["webpage_url"],
            # Audio language, used to flag a translated caption track without
            # spending a request on the original.
            "spoken_lang": out["language"],
        }
    return {}


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


ORIGINAL_PATTERN = ".*-orig"


def sub_lang_pattern(lang):
    """Build the --sub-langs pattern for a requested language.

    Only the requested language is downloaded. Pulling `.*-orig` alongside it
    would double the requests against the subtitle CDN, which is the endpoint
    that rate-limits: a video whose `en` track 429s serves its `pt-orig` track
    fine seconds later. Knowing a transcript is translated needs the video's
    language, not the original track's bytes, and metadata already carries that.
    """
    if lang.lower() in ("orig", "original"):
        return ORIGINAL_PATTERN
    return f"{lang}.*"


def track_langs(tmpdir, video_id):
    """Map landed subtitle files to their language codes: {lang: path}."""
    found = {}
    prefix = f"{video_id}."
    for fname in os.listdir(tmpdir):
        if not fname.endswith(".json3") or not fname.startswith(prefix):
            continue
        code = fname[len(prefix):-len(".json3")]
        found[code] = os.path.join(tmpdir, fname)
    return found


def choose_track(found, lang, manual_langs):
    """Pick the best landed track. Returns (path, code, kind) or (None, None, None).

    An explicit --lang wins over fidelity: asking for English on a Portuguese
    video returns the English track, flagged as translated, rather than silently
    handing back Portuguese. Within the requested language, manual captions beat
    the original ASR, which beats a machine translation. The original track is
    the fallback only when the requested language did not arrive at all -- that
    is what keeps a language miss from costing a second extraction.
    """
    if not found:
        return None, None, None
    base = lang.lower()
    asked_original = base in ("orig", "original")

    def kind_of(code):
        if code in manual_langs:
            return "manual"
        return "auto-original" if code.endswith("-orig") else "auto"

    order = []
    if not asked_original:
        order += [c for c in found if c in manual_langs and c == lang]
        order += [c for c in found if c in manual_langs and c.startswith(base)]
        order += [c for c in found if c in manual_langs]
        order += [c for c in found if c == f"{lang}-orig"]
        order += [c for c in found if c == lang]
        order += [c for c in found if c.startswith(base)]
    order += [c for c in found if c.endswith("-orig")]
    order += sorted(found)

    seen = set()
    for code in order:
        if code in seen or code not in found:
            continue
        seen.add(code)
        return found[code], code, kind_of(code)
    return None, None, None


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
        manual_langs = sorted((d.get("subtitles") or {}).keys())
        auto_langs = sorted((d.get("automatic_captions") or {}).keys())
        original = next((c for c in auto_langs if c.endswith("-orig")), None)
        return {
            "title": d.get("title"),
            "channel": d.get("channel") or d.get("uploader"),
            "upload_date": upload_date,
            "duration": d.get("duration"),
            "duration_string": format_duration(d.get("duration")),
            "view_count": d.get("view_count"),
            "url": d.get("webpage_url"),
            "manual_langs": manual_langs,
            "auto_langs": auto_langs,
            # e.g. "pt-orig" -> the video was actually spoken in Portuguese
            "original_lang": original[: -len("-orig")] if original else None,
        }
    return {}


def fetch_transcript(video_id, lang="en", cookies_from_browser=None):
    """Fetch transcript using yt-dlp with json3 format.

    Returns (entries, source, error, meta). One extraction, not three: requesting
    manual and automatic captions together with a language pattern lands every
    candidate track in a single pass, and the best one is chosen from what
    arrived. The previous try-manual-then-auto-then-any loop re-extracted the
    whole video page each round, costing 2 requests for the common auto-caption
    case and 3 for a language miss.

    Metadata comes from --write-info-json, which costs nothing extra: the
    subtitle fetch already extracts the video page.
    """
    yt_dlp = shutil.which("yt-dlp")
    if not yt_dlp:
        print("Error: yt-dlp not found. Install with: brew install yt-dlp", file=sys.stderr)
        sys.exit(1)

    url = f"https://www.youtube.com/watch?v={video_id}"

    with tempfile.TemporaryDirectory() as tmpdir:
        out_template = os.path.join(tmpdir, "%(id)s")
        cmd = [
            yt_dlp,
            "--print", print_template(),
            # --print implies --simulate, which would suppress every subtitle
            # file. Without this line the script returns metadata and an empty
            # transcript.
            "--no-simulate",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs", sub_lang_pattern(lang),
            "--sub-format", "json3",
            "--write-info-json",
            # A video exposing no downloadable formats otherwise hard-errors
            # before any subtitle is written, losing a transcript that was
            # perfectly reachable. --skip-download means we want none of them.
            "--ignore-no-formats-error",
            "--skip-download",
            "--no-warnings",
            "--no-progress",
            "--sleep-requests", "0.75",
            "--extractor-retries", "1",
            "-o", out_template,
            url,
        ]
        if cookies_from_browser:
            cmd[1:1] = ["--cookies-from-browser", cookies_from_browser]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=TRANSCRIPT_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            return [], None, f"timed out after {TRANSCRIPT_TIMEOUT}s", {}

        # Printed metadata is primary because it survives late failures;
        # info.json only fills gaps and supplies the caption inventory.
        info = read_info_json(tmpdir)
        meta = dict(info)
        meta["inventory_known"] = bool(info)
        for key, value in parse_printed_meta(result.stdout).items():
            if value is not None:
                meta[key] = value
        err = result.stderr.strip()

        # Age restriction is a per-video permissions failure, not a rate signal:
        # never record a block for it, and point at the fix.
        if _throttle.is_age_restricted(err):
            hint = ("" if cookies_from_browser else
                    " Retry with --cookies-from-browser BROWSER (e.g. chrome).")
            return [], None, f"age-restricted video.{hint}", meta

        # A 429 confined to the subtitle CDN is not an IP-wide block: the
        # original track and the extraction endpoint keep working. Fall through
        # to the retry below instead of starting a cooldown over it.
        sub_limited = _throttle.is_subtitle_rate_limit(err)
        if _throttle.is_block_error(err) and not sub_limited:
            _throttle.record_block()
            return [], None, err, meta

        found = track_langs(tmpdir, video_id)

        # Either the requested language does not exist for this video, or its
        # machine-translated track was throttled. Both are worth one retry
        # against the original track, which YouTube serves far more reliably --
        # and both are rare, so this beats paying an extra subtitle download on
        # every video to pre-empt them.
        if not found and lang.lower() not in ("orig", "original"):
            retry = list(cmd)
            retry[retry.index("--sub-langs") + 1] = ORIGINAL_PATTERN
            try:
                result = subprocess.run(
                    retry, capture_output=True, text=True,
                    timeout=TRANSCRIPT_TIMEOUT
                )
            except subprocess.TimeoutExpired:
                return [], None, f"timed out after {TRANSCRIPT_TIMEOUT}s", meta
            err = result.stderr.strip()
            # The original track failing too means the pressure is not confined
            # to translated captions after all.
            if _throttle.is_block_error(err):
                _throttle.record_block()
                return [], None, err, meta
            found = track_langs(tmpdir, video_id)

        # Original language, cheapest source first: the info.json inventory, then
        # the printed audio language, then whatever `*-orig` file landed.
        if not meta.get("original_lang"):
            landed_orig = next((c for c in found if c.endswith("-orig")), None)
            meta["original_lang"] = (
                meta.get("spoken_lang")
                or (landed_orig[: -len("-orig")] if landed_orig else None)
            )
        manual = set(meta.get("manual_langs") or [])

        fpath, code, kind = choose_track(found, lang, manual)

        while fpath:
            try:
                with open(fpath, "r") as f:
                    entries = parse_json3(json.load(f))
            except (OSError, json.JSONDecodeError):
                entries = []
            if entries:
                # Proof the IP is usable again; let searches resume dates.
                _throttle.clear_block()
                meta["track"] = code
                meta["track_kind"] = kind
                return entries, kind, None, meta
            # Empty track: drop it and fall through to the next best candidate.
            found.pop(code, None)
            fpath, code, kind = choose_track(found, lang, manual)

        return [], None, err or None, meta


def main():
    parser = argparse.ArgumentParser(description="Get YouTube video transcript")
    parser.add_argument("video", help="YouTube URL or video ID")
    parser.add_argument("--timestamps", action="store_true", help="Include timestamps")
    parser.add_argument(
        "--lang",
        default="en",
        help="Language code (default: en), or 'orig' for the original spoken track",
    )
    parser.add_argument(
        "--cookies-from-browser",
        metavar="BROWSER",
        help="Load cookies from a browser (chrome, firefox, safari, ...) to reach "
             "age-restricted videos",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument(
        "--no-header",
        action="store_true",
        help="Omit the title/channel/upload-date header from text output",
    )
    args = parser.parse_args()

    video_id = extract_video_id(args.video)

    # One transcript is a single extraction, so a cooldown warns rather than
    # blocks -- the caller asked for this specific video. It does tell the agent
    # to stop fanning out across many videos.
    cooling = _throttle.cooldown_remaining()
    if cooling:
        print(f"Warning: YouTube blocked requests from this IP recently "
              f"({_throttle.format_cooldown(cooling)} of cooldown left). "
              f"Fetching this one anyway; avoid batching more transcripts until "
              f"it clears.", file=sys.stderr)

    entries, source, error, meta = fetch_transcript(
        video_id, args.lang, args.cookies_from_browser
    )

    if not entries:
        if error and _throttle.is_age_restricted(error):
            print(f"Error: {video_id} is age-restricted and needs an "
                  f"authenticated account." +
                  ("" if args.cookies_from_browser else
                   " Retry with --cookies-from-browser chrome (or firefox, "
                   "safari, edge)."), file=sys.stderr)
        elif error and _throttle.is_block_error(error):
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

    track = meta.get("track")
    original_lang = meta.get("original_lang")
    # A track that is neither manual nor the original ASR is a machine
    # translation of speech in another language -- say so rather than passing it
    # off as a plain transcript.
    translated = bool(
        source == "auto" and original_lang
        and not (track or "").startswith(original_lang)
    )

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
            "track": track,
            "original_lang": original_lang,
            "translated": translated,
            "entries": len(entries),
            "transcript": [
                {"start": s, "text": t} for s, t in entries
            ],
        }))
        return

    if not args.no_header and meta:
        if meta.get("inventory_known", True):
            captions = {
                "manual": "manual captions",
                "auto-original": "auto captions (original)",
            }.get(source, "auto captions")
        else:
            # No info.json: the manual/automatic inventory is unknown, so do not
            # assert which one this is.
            captions = ("captions (original)" if source == "auto-original"
                        else "captions")
        if track:
            captions += f" [{track}]"
        if translated:
            captions += f" — machine-translated from {original_lang}"
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
