"""Shared rate-limit state for the youtube-research scripts.

YouTube tolerates the flat search (one request at any --limit) but blocks an IP
after a burst of per-video extractions. Observed in practice at roughly 40
per-video requests inside a few minutes, and the block then fails *every*
subsequent lookup until it clears.

Two failure modes follow, and this module exists to prevent both:

  1. A blocked run keeps issuing its remaining requests, each one failing and
     deepening the block. The Breaker stops that within a run.
  2. The next invocation starts a fresh burst against a still-blocked IP. The
     cooldown file stops that across runs.

State lives in ~/.cache/youtube-research/state.json and is best-effort: any
filesystem problem degrades to "no cooldown known" rather than failing a search.
"""

import os
import json
import time
import threading

STATE_DIR = os.path.expanduser("~/.cache/youtube-research")
STATE_FILE = os.path.join(STATE_DIR, "state.json")

# Observed blocks cleared in well under 20 minutes. Long enough to matter,
# short enough that a real research session is not dead in the water.
DEFAULT_COOLDOWN_SECONDS = 900

# Substrings that mean YouTube is refusing us for *rate* reasons.
#
# These are matched against text YouTube writes, not text yt-dlp writes, so the
# phrasing is outside our control -- which is why none of them may be a prefix
# of a longer, unrelated message. "sign in to confirm" was exactly that mistake:
# it also matches the age gate, "Sign in to confirm your age", so a single
# age-restricted video would record a block and stall every later run for 15
# minutes over something cookies solve outright. Match "not a bot", which lands
# after the apostrophe and so survives both ASCII ' and U+2019.
BOT_BLOCK_MARKERS = (
    "not a bot",
    "http error 429",
    "too many requests",
    "rate-limit exceeded",
)

# Age restriction is a per-video authorization problem, never a rate problem.
# It must never trip the breaker: the IP is fine and other videos still work.
AGE_GATE_MARKERS = (
    "confirm your age",
    "age-restricted",
    "age restricted",
    "inappropriate for some users",
)


def is_block_error(text):
    """True when stderr indicates throttling/blocking, not a missing field.

    Age-gated videos are explicitly excluded: they say "Sign in to confirm your
    age", which is a permissions failure on one video, not a signal about the IP.
    """
    if not text:
        return False
    low = text.lower()
    if is_age_restricted(low):
        return False
    return any(marker in low for marker in BOT_BLOCK_MARKERS)


def is_age_restricted(text):
    """True when a video needs an authenticated account (cookies), not a wait."""
    if not text:
        return False
    low = text.lower()
    return any(marker in low for marker in AGE_GATE_MARKERS)


def cooldown_seconds():
    """Cooldown length; override with YTR_COOLDOWN_SECONDS (0 disables)."""
    raw = os.environ.get("YTR_COOLDOWN_SECONDS")
    if raw is None:
        return DEFAULT_COOLDOWN_SECONDS
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_COOLDOWN_SECONDS


def record_block():
    """Remember that we were blocked, so the next run does not burst again."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump({"blocked_at": time.time()}, f)
    except OSError:
        pass


def clear_block():
    """Called after a successful per-video lookup: the block is demonstrably over."""
    try:
        os.remove(STATE_FILE)
    except OSError:
        pass


def cooldown_remaining():
    """Seconds until per-video requests are safe again. 0 when clear."""
    window = cooldown_seconds()
    if window <= 0:
        return 0
    try:
        with open(STATE_FILE) as f:
            blocked_at = float(json.load(f)["blocked_at"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return 0
    remaining = window - (time.time() - blocked_at)
    return int(remaining) if remaining > 0 else 0


def format_cooldown(seconds):
    """Render a cooldown for humans: '7m 30s'."""
    minutes, secs = divmod(max(0, int(seconds)), 60)
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


class Breaker:
    """Trips on the first observed block so concurrent work stops requesting.

    Threads check `tripped` before spending a request. Tasks already queued
    still run, but return immediately without touching the network.
    """

    def __init__(self):
        self._event = threading.Event()

    def trip(self):
        if not self._event.is_set():
            self._event.set()
            record_block()

    @property
    def tripped(self):
        return self._event.is_set()
