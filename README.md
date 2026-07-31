# youtube-research

A [Claude Code](https://docs.claude.com/en/docs/claude-code) **agent skill** that turns a
topic or question into an answer grounded in YouTube video content — it searches for
relevant videos, pulls their transcripts, and synthesizes a cited answer with timestamps.
Also handles narrow asks: find videos by keyword/channel, or grab the transcript of a
single video.

Two `yt-dlp`-backed scripts do the fetching, sharing a small `_throttle.py` helper that
tracks YouTube rate limiting; [`SKILL.md`](SKILL.md) drives the research loop on top of
them. **No API key needed.**

## Requirements

`yt-dlp` — `brew install yt-dlp` (macOS) or `pipx install yt-dlp`. Python 3.

## Scripts

```bash
# search
python3 scripts/search_youtube.py "QUERY" [--limit N] [--json] [--no-dates] \
    [--after YYYY-MM-DD] [--before YYYY-MM-DD] [--recent day|week|month|year]

# transcript
python3 scripts/get_transcript.py "URL_OR_ID" [--timestamps] [--lang XX|orig] [--json] \
    [--no-header] [--cookies-from-browser BROWSER]
```

One transcript costs one extraction and one subtitle download: manual and automatic
captions are requested together with a language pattern, and the best track is chosen from
whatever lands. YouTube offers ~157 machine-translated caption languages per video, so
`--lang en` on a Portuguese video returns a translation — the header names the actual track
(`[en-orig]`, or `[en] — machine-translated from pt`) and `--json` exposes `track`,
`original_lang`, and `translated`. Age-restricted videos need `--cookies-from-browser chrome`.

**Use `--lang orig` for non-English videos.** Translated caption tracks are generated on
demand and throttled far harder than original ones: a video whose `en` track returns HTTP
429 serves its `pt-orig` track seconds later. When a translated track is throttled the
script retries once against the original — that is a per-track limit, not an IP block, so
it starts no cooldown.

Metadata (title, channel, upload date, duration, views) is printed by the extraction itself
rather than read only from `.info.json`, which yt-dlp writes near the end of a run and skips
entirely when anything fails afterwards. Transcripts therefore keep their metadata even when
the run half-fails. Where `.info.json` is missing, the manual-vs-automatic inventory is
unknown and the header says `captions [xx]` instead of asserting either.

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
each upload date is a separate per-video request, and a burst of them trips YouTube's bot
detection — observed at roughly 40 in a few minutes. For wide sweeps, search with `--no-dates`
(one request, ~2s at any size) and take dates from `get_transcript.py` for the videos you
actually read.

### Rate-limit handling

- **Paced**: 4 concurrent workers, `--sleep-requests 0.75`, `--extractor-retries 1`.
- **Circuit breaker**: the first block trips it and the run stops issuing requests instead of
  firing the rest into a wall — measured 4 requests spent instead of 40.
- **Cooldown**: a block is recorded to `~/.cache/youtube-research/state.json` for 15 minutes.
  While it is active, dated searches degrade to dateless with a warning and filtered searches
  refuse outright; `--no-dates` is unaffected. A later success clears it automatically.
  Tune with `YTR_COOLDOWN_SECONDS` (0 disables) or bypass with `--ignore-cooldown`.
- **Never mislabeled**: "no dates could be read" never renders as "no videos matched".

YouTube's fast flat search carries no dates, so they are hydrated per video concurrently —
about 4s for a search instead of 2s, versus ~17s for the sequential alternative. Use
`--no-dates` to skip it.

## License

MIT
