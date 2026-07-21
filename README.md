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
python3 scripts/search_youtube.py "QUERY" [--limit N] [--json]

# transcript
python3 scripts/get_transcript.py "URL_OR_ID" [--timestamps] [--lang XX] [--json]
```

`search_youtube.py` returns `id, title, channel, duration, view_count, url` per result.
`get_transcript.py` prints the transcript (`--timestamps` prefixes `[MM:SS]`).

## License

MIT
