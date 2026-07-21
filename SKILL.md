---
name: youtube-research
description: >
  Research and learn any topic from YouTube. Use when the user wants to understand, study, or
  get up to speed on a subject using video content — e.g. "what does YouTube say about X",
  "learn/research X from videos", "summarize the best videos on X", "find and explain a
  tutorial/talk/interview about X". Searches for relevant videos, pulls their transcripts, and
  synthesizes an answer with citations and timestamps. Also handles narrow requests: find videos
  by keyword/channel, or get the transcript of a single YouTube URL or video ID.
metadata:
  author: m
  version: "2.0"
---

# YouTube Research

Turn a topic or question into an answer grounded in YouTube video content. Two self-contained
`yt-dlp`-backed scripts do the fetching; this skill drives the research loop on top of them.

## Requirements

`yt-dlp` must be installed (`brew install yt-dlp` on macOS, or `pipx install yt-dlp`). Both
scripts print a clear install hint if it is missing. No API key is needed.

## The scripts (the engine)

| Task | Script |
|------|--------|
| Find videos by keyword/topic | `scripts/search_youtube.py` |
| Get a video's transcript | `scripts/get_transcript.py` |

**Search** — `scripts/search_youtube.py "QUERY" [--limit N] [--json]`. Returns `id`, `title`,
`channel`, `duration`, `view_count`, `url` per result in relevance order. Use `--json` when
programming against the results (the research loop below does); use plain text when showing the
user a list to choose from.

**Transcript** — `scripts/get_transcript.py "URL_OR_ID" [--timestamps] [--lang XX] [--json]`.
`--timestamps` prefixes each line with `[MM:SS]`; `--lang XX` sets caption language (default
`en`); `--json` gives per-line start times. **Never modify transcript wording** — you may
re-wrap plain text into paragraphs, nothing more.

## Default path: research a topic

This is the main use. When the user asks to learn, research, or understand something (not a
single named video), follow this loop:

1. **Frame the question.** Restate what the user actually wants to know. Note the angle (overview,
   how-to, comparison, latest developments) and any recency need.
2. **Search** with `--json`, `--limit 8-10`. For broad topics, run 2-3 searches with different
   phrasings to widen coverage.
3. **Select 3-5 sources.** Favor relevance, substantial duration (skip Shorts for depth), higher
   view counts as a weak quality signal, and channel credibility when recognizable. Diversify —
   don't pick five near-duplicates. State which you picked and why.
4. **Fetch transcripts** for the selected videos (use `--timestamps` so you can cite moments).
   Skip and replace any video whose transcript fails (bot-block / no captions).
5. **Synthesize.** Write the answer in your own words, grounded in the transcripts:
   - Lead with the direct answer / key takeaways.
   - Attribute claims to sources: *(Channel — "title", [MM:SS])*.
   - Note agreement, disagreement, and anything only one source claims.
   - Flag gaps ("no source covered X") and recency caveats. Do not invent content not in the
     transcripts.
6. **Offer depth.** End by offering the full transcript, more sources, or a deeper dive on a
   subtopic.

Scale effort to the ask: a quick "what's the gist of X" needs 1-2 sources; "research X
thoroughly" warrants the full loop with more sources and cross-checking.

## Narrow paths

- **Single video** (user gives a URL/ID or names one video): fetch its transcript, then do what
  was asked — display, summarize, or extract quotes/timestamps.
- **Just find videos** (user wants a list, not an answer): run search with plain-text output and
  present the results; don't fetch transcripts unless asked.

## Saving transcripts

- Save to the user's path if they gave one; if they asked to save but gave no path, use
  `{video_id}-transcript.txt` in the current directory.
- If they just want to read or want a summary, don't save — display/summarize directly.

## Error handling

Both scripts exit non-zero and print to stderr on failure:

- **"bot detection" / "Sign in to confirm"** — YouTube is blocking this IP. Tell the user, try a
  different video if researching, and suggest retrying later. Do not retry in a tight loop.
- **"No transcript found" / "No results found"** — no captions, or the query matched nothing.
  In the research loop, substitute another source; otherwise report it plainly.
- **"yt-dlp not found"** — surface the install command.
- Other errors — show the message to the user.
