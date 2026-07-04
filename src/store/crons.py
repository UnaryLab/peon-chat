"""Cron store (JSON file: a LIST of cron entries, sibling of sessions.json).

Slack-native scheduled runs. Each entry is a dict:
  {"id": str, "schedule": "<5-field cron expr>", "agent": str, "channel": str,
   "thread_ts": str, "prompt": str, "enabled": bool}
Lives as a SIBLING of sessions.json (so SESSIONS_PATH redirects it with no new
env var), lock-guarded like the other stores. The top-level shape is a LIST (not
the dict the session/override stores use), so it loads/saves via the shared
list-store pair in store.base (_load_list_store/_save_list_store).
Note: Claude Code has its own /schedule (cloud routines); THIS is the
Slack-native equivalent the user asked for, self-contained in this process.
"""

from __future__ import annotations

import uuid

from .base import (
    _SESSIONS_LOCK,
    _load_list_store,
    _resolve_path,
    _save_list_store,
    _sibling_store_path,
)


def _crons_path():
    """Resolve the cron-store path: a sibling of the sessions path."""
    return _sibling_store_path("crons.json")


def list_crons(path=None):
    """Return the persisted list of cron entries (a fresh list, possibly empty)."""
    if path is None:
        path = _resolve_path("_crons_path", _crons_path)
    with _SESSIONS_LOCK:
        return _load_list_store(path)


def add_cron(schedule, agent, channel, thread_ts, prompt, cron_id=None, path=None):
    """Append a new ENABLED cron entry and return it (read-modify-write under the lock).

    `cron_id` defaults to a short uuid4 fragment so list/remove can address it. The
    entry stores the 5-field `schedule`, the target `agent`/`channel`/`thread_ts`,
    and the `prompt` to run, with enabled=True.
    """
    if path is None:
        path = _resolve_path("_crons_path", _crons_path)
    if cron_id is None:
        cron_id = uuid.uuid4().hex[:8]
    entry = {
        "id": cron_id,
        "schedule": schedule,
        "agent": agent,
        "channel": channel,
        "thread_ts": thread_ts,
        "prompt": prompt,
        "enabled": True,
    }
    with _SESSIONS_LOCK:
        crons = _load_list_store(path)
        crons.append(entry)
        _save_list_store(crons, path)
    return entry


def remove_cron(cron_id, path=None):
    """Delete the cron entry with `cron_id`. Returns True if one was removed."""
    if path is None:
        path = _resolve_path("_crons_path", _crons_path)
    with _SESSIONS_LOCK:
        crons = _load_list_store(path)
        kept = [c for c in crons if c.get("id") != cron_id]
        if len(kept) == len(crons):
            return False
        _save_list_store(kept, path)
        return True


def set_cron_enabled(cron_id, enabled, path=None):
    """Toggle the `enabled` flag of the cron with `cron_id`. Returns True if found."""
    if path is None:
        path = _resolve_path("_crons_path", _crons_path)
    with _SESSIONS_LOCK:
        crons = _load_list_store(path)
        found = False
        for c in crons:
            if c.get("id") == cron_id:
                c["enabled"] = bool(enabled)
                found = True
        if found:
            _save_list_store(crons, path)
        return found
