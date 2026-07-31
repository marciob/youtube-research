# youtube-research

A [Claude Code](https://docs.claude.com/en/docs/claude-code) **agent skill** that turns a
topic or question into an answer grounded in YouTube video content — it searches for
relevant videos, pulls their transcripts, and synthesizes a cited answer with timestamps.
Also handles narrow asks: find videos by keyword/channel, or grab the transcript of a
single video.

Two self-contained `yt-dlp`-backed scripts do the fetching; [`SKILL.md`](SKILL.md) drives
the research loop on top of them. **No API key needed.**

## Requirements

`yt-dlp` — `brew install yt-dlp` (macOS) or `pipx install yt-dlp`. Python 3.

## Scripts

```bash
# search
python3 scripts/search_youtube.py "QUERY" [--limit N] [--json] [--no-dates] \
    [--after YYYY-MM-DD] [--before YYYY-MM-DD] [--recent day|week|month|year]

# transcript
python3 scripts/get_transcript.py "URL_OR_ID" [--timestamps] [--lang XX] [--json] [--no-header]
```

`search_youtube.py` returns `id, title, channel, duration, view_count, upload_date, url`
per result. `get_transcript.py` prints the transcript (`--timestamps` prefixes `[MM:SS]`)
under a header with title, channel, upload date, and caption type.

### Date filtering

```bash
python3 scripts/search_youtube.py "claude code skills" --recent month
python3 scripts/search_youtube.py "llm" --after 2023-01-01 --before 2023-12-31
```

Filtering examines `max(40, --limit x 2)` candidates and then trims to `--limit`, because a
narrow window can eliminate all but one or two of the top relevance hits. `--max-candidates`
overrides that depth. The run reports `candidates_examined` and `depth_capped`, so a genuinely
sparse window is distinguishable from a search that simply stopped digging. Videos with an
undeterminable upload date are excluded and counted, never silently kept.

**Request volume is the real constraint.** The flat search is one request at any `--limit`, but
each upload date is a separate per-video request, and a burst of a few hundred trips YouTube's
bot detection. When that happens the scripts say so — and a filtered search exits non-zero
rather than returning an empty list, since "no dates could be read" is not "no videos matched".
For wide sweeps, search with `--no-dates` (one request, ~2s at any size) and take dates from
`get_transcript.py` for the videos you actually read.

YouTube's fast flat search carries no dates, so they are hydrated per video concurrently —
about 4s for a search instead of 2s, versus ~17s for the sequential alternative. Use
`--no-dates` to skip it.

## License

MIT
