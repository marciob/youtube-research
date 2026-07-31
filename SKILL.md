---
name: youtube-research
description: >
  Research and learn any topic from YouTube. Use when the user wants to understand, study, or
  get up to speed on a subject using video content — e.g. "what does YouTube say about X",
  "learn/research X from videos", "summarize the best videos on X", "find and explain a
  tutorial/talk/interview about X". Searches for relevant videos, pulls their transcripts, and
  synthesizes an answer with citations and timestamps. Also handles narrow requests: find videos
  by keyword/channel, or get the transcript of a single YouTube URL or video ID. Supports
  filtering the search by upload date ("only videos from this year", "since March 2025").
metadata:
  author: m
  version: "2.1"
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
`channel`, `duration`, `view_count`, `upload_date`, `url` per result in relevance order. Use
`--json` when programming against the results (the research loop below does); use plain text when
showing the user a list to choose from.

Every result carries `upload_date` (`YYYY-MM-DD`) so you can judge recency yourself. Getting it
costs a per-video lookup, so search takes ~4s rather than ~2s; pass `--no-dates` for a faster
dateless list when recency is irrelevant.

**Date filtering** — `--after YYYY-MM-DD`, `--before YYYY-MM-DD` (combinable for a closed range),
or `--recent day|week|month|year` as shorthand. Filtering examines 20-40 candidates and then trims
to `--limit`, so it costs ~10-15s; that depth is deliberate, since a narrow window can eliminate
all but one or two of the top relevance hits. The run reports how many candidates it examined and
whether more matched beyond the limit. Videos whose upload date can't be determined are excluded
from filtered results and reported, never silently kept.

A filtered search returning fewer than `--limit` results means one of two things, and the output
distinguishes them: `depth_capped: false` means the window is genuinely sparse — say so rather
than padding the answer with out-of-window videos. `depth_capped: true` means the candidate pool
ran out first and more probably exist; re-run with a higher `--max-candidates` before concluding
anything about scarcity.

**Transcript** — `scripts/get_transcript.py "URL_OR_ID" [--timestamps] [--lang XX] [--json]
[--no-header]`. `--timestamps` prefixes each line with `[MM:SS]`; `--lang XX` sets caption
language (default `en`); `--json` gives per-line start times. Text output opens with a `#` header
carrying title, channel, upload date, duration, and caption type (manual vs auto) — pass
`--no-header` for the bare transcript. `--json` returns the same metadata as fields.
**Never modify transcript wording** — you may re-wrap plain text into paragraphs, nothing more.

## Default path: research a topic

This is the main use. When the user asks to learn, research, or understand something (not a
single named video), follow this loop:

1. **Frame the question.** Restate what the user actually wants to know. Note the angle (overview,
   how-to, comparison, latest developments) and any recency need.
2. **Search wide** with `--json`. Searching is cheap and reading transcripts is expensive, so
   cast a wide net here and be selective in the next step — never the reverse. Scale `--limit`
   to the ask (see the breadth table below), and for anything broader than a quick question run
   2-4 searches with different phrasings; the same topic surfaces different videos under
   different wording, and one query's top 10 is not the field. Add a date filter when the topic
   is one where old answers are wrong, not merely dated — fast-moving tooling, model releases,
   prices, "latest"/"in 2026" questions, anything the user framed as current. Otherwise search
   unfiltered and let the `upload_date` on each result inform selection, since a narrow window
   can exclude the one canonical explainer on a subject.
3. **Select from the pool.** Favor relevance, substantial duration (skip Shorts for depth),
   higher view counts as a weak quality signal, and channel credibility when recognizable. Weigh
   `upload_date` against the topic's decay rate — a 2019 talk is fine for fundamentals and
   near-worthless for current tooling. Diversify — don't pick near-duplicates, and prefer
   covering distinct angles over the top N by relevance. State which you picked and why,
   including dates when recency matters.
4. **Fetch transcripts** for the selected videos (use `--timestamps` so you can cite moments).
   Skip and replace any video whose transcript fails (bot-block / no captions).
5. **Synthesize.** Write the answer in your own words, grounded in the transcripts:
   - Lead with the direct answer / key takeaways.
   - Attribute claims to sources: *(Channel — "title", 2025-11-14, [MM:SS])*. Include the upload
     date whenever the claim could have expired.
   - Note agreement, disagreement, and anything only one source claims. When sources conflict,
     check their dates before splitting the difference — the disagreement is often just the
     older video describing a superseded state.
   - Flag gaps ("no source covered X") and recency caveats — state the actual dates you relied
     on, e.g. "newest source is from 2024-03, so anything after that is uncovered." Do not
     invent content not in the transcripts.
6. **Offer depth.** End by offering the full transcript, more sources, or a deeper dive on a
   subtopic.

### Breadth

Search breadth and reading breadth are separate budgets. Widen the search freely; ration the
transcripts, which are what actually cost context.

| Ask | `--limit` | Queries | Transcripts to read |
|-----|-----------|---------|---------------------|
| "gist of X", quick question | 10 | 1 | 1-2 |
| Normal research question | 15-20 | 2-3 | 3-5 |
| "research X thoroughly", "look at a lot of videos", comparisons, literature-style sweeps | 30-50 | 3-4 | 8-12 |

These are defaults, not ceilings. `--limit` is passed straight through to YouTube, so go higher
when the user asks for exhaustive coverage.

**The one real limit is request volume, not `--limit`.** The flat search is a single request no
matter how large `--limit` is, but each upload date is a separate per-video request, and a few
hundred of those in a burst trips YouTube's bot detection — after which *every* lookup fails
until the block clears. So for wide sweeps:

- **Go wide with `--no-dates`** (one request, ~2s at any `--limit`), shortlist from titles and
  channels, and let the dates arrive with the transcripts — `get_transcript.py` reports the
  upload date for each video you actually read.
- **Reserve dated or filtered searches for narrower passes.** A filtered search examines
  `max(40, --limit x 2)` videos; at `--limit 50` that is 100 requests, which is near the safe
  ceiling. Push past it with `--max-candidates` only deliberately.
- If lookups start failing en masse the scripts say so and a filtered search refuses outright —
  do not retry in a tight loop, and never report a throttled empty result as "no videos found".

When the user explicitly asks for a wide search, say how wide you went — queries run, results
seen, transcripts read — so the coverage is inspectable rather than implied.

## Narrow paths

- **Single video** (user gives a URL/ID or names one video): fetch its transcript, then do what
  was asked — display, summarize, or extract quotes/timestamps.
- **Just find videos** (user wants a list, not an answer): run search with plain-text output and
  present the results; don't fetch transcripts unless asked. Translate any date wording in the
  request into a filter — "from this year" → `--recent year`, "since March" → `--after`, "2023
  videos" → `--after 2023-01-01 --before 2023-12-31`.

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
- **"No results ... (within DATE to DATE)"** — the query matched videos but none inside the date
  window. Report the window you used and how many candidates were examined, then offer to widen
  it. Do not quietly re-run unfiltered and present the results as if they met the constraint.
- **"N skipped (upload date unavailable)"** — those videos were excluded from a filtered result
  because their date couldn't be confirmed. Mention it if the count is material.
- **"every upload-date lookup failed" / "N/M upload-date lookups failed"** — YouTube is
  throttling the per-video requests. A filtered search exits non-zero here rather than returning
  an empty list, because "no dates" and "no matches" are not the same claim. Fall back to
  `--no-dates` for the wide pass and take dates from the transcripts, or wait and retry with a
  smaller `--limit`. Never restate this as "there are no recent videos on this topic".
- **"stopped at the N-candidate ceiling"** (`depth_capped: true`) — the pool ran out before
  `--limit` was filled, so more matches probably exist deeper. Raise `--max-candidates` if the
  count matters; otherwise report the result as partial, not exhaustive.
- **"yt-dlp not found"** — surface the install command.
- Other errors — show the message to the user.
