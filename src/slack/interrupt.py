"""Per-thread run-interrupt registry + the interrupt-phrase matcher.

The Ctrl-C analog for Slack: while a run is in flight, the worker registers its
Interrupt token here under the run's (agent, thread) key; a "!stop"/"stop"/
"ctrl-c" control phrase looks the token up and signals it (SIGINT to the live CLI
plus a graceful-settle flag the runner reads). See src.runners.common.Interrupt.

In-memory only (subprocess handles are not serializable) and single-process, like
the seen_before dedup. Thread-safe. One run per (agent, thread): BOTH the message
path and the cron path claim the slot atomically with try_register (the busy
guard) and decline when it is taken, so a live token is never overwritten.
register (unconditional, last-writer-wins) remains only as the raw primitive.
"""

from __future__ import annotations

import threading

from src.runners.common import Interrupt

_LOCK = threading.Lock()
_RUNNING: dict[tuple[str, str], Interrupt] = {}

# A de-mentioned message equal (case-insensitively, stripped) to one of these is an
# interrupt request, not a prompt. Both "!"-prefixed and bare forms, since a user
# reaching for "stop" mid-run will not prefix it. Mirrors claude-wormhole's matcher.
# ponytail: exact whole-message match keeps the false-positive risk near zero (a
# real prompt is rarely the single word "stop"), and when nothing is running the
# handler just replies "nothing to interrupt", so even a stray match is harmless.
_INTERRUPT_PHRASES = frozenset(
    {
        "!stop",
        "stop",
        "!interrupt",
        "interrupt",
        "/interrupt",
        "ctrl+c",
        "ctrl-c",
        "ctrlc",
        "control+c",
        "control-c",
        "^c",
    }
)


def is_interrupt_phrase(text):
    """True if `text` (a de-mentioned message) is an interrupt request."""
    return (text or "").strip().lower() in _INTERRUPT_PHRASES


def register(agent_name, thread_ts):
    """Create + register an Interrupt token for this run; return it."""
    token = Interrupt()
    with _LOCK:
        _RUNNING[(agent_name, thread_ts)] = token
    return token


def try_register(agent_name, thread_ts):
    """Register a token ONLY if this thread has no run in flight (the busy guard).

    Returns the new token, or None if a run is already registered for this
    (agent, thread). Atomic under _LOCK, so two near-simultaneous messages to one
    thread cannot both pass: the first claims the slot, the second gets None and
    the caller declines to start a competing run (which would --resume the same
    session id concurrently). Unlike register(), this never overwrites a live
    token. The caller passes the returned token into the run and unregister()s it
    when done. Used by BOTH the message path and the cron path.
    """
    token = Interrupt()
    with _LOCK:
        if (agent_name, thread_ts) in _RUNNING:
            return None
        _RUNNING[(agent_name, thread_ts)] = token
    return token


def mark_pinged(agent_name, thread_ts):
    """Flag the in-flight run for this thread: a message arrived and was declined
    by the busy guard while it was running. The worker reads token.pinged after
    the final reply lands and posts a short done note as a NEW message (the
    reply itself is an in-place edit of the placeholder, which Slack does not
    notify on and which now sits above the declined exchange). No-op when
    nothing is running.
    """
    with _LOCK:
        token = _RUNNING.get((agent_name, thread_ts))
        if token is not None:
            token.pinged = True


def unregister(agent_name, thread_ts, token):
    """Drop this run's token, but only if a newer run has not replaced it."""
    with _LOCK:
        if _RUNNING.get((agent_name, thread_ts)) is token:
            del _RUNNING[(agent_name, thread_ts)]


def is_running(agent_name, thread_ts):
    """Read-only peek: is a run in flight for this (agent, thread)?

    Never claims the slot (unlike try_register); used by `!new` to decline while
    a run is in flight.
    """
    with _LOCK:
        return (agent_name, thread_ts) in _RUNNING


def request(agent_name, thread_ts):
    """Signal the in-flight run for this thread. Return True if one was running."""
    with _LOCK:
        token = _RUNNING.get((agent_name, thread_ts))
    if token is None:
        return False
    token.request()
    return True
