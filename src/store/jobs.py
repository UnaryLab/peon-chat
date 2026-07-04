"""Background-job store (JSON file: a LIST of job entries, sibling of sessions.json).

Detached background jobs spawned via the trailing `<<job: ...>>` reply marker.
Each entry is a dict:
  {"id": str, "agent": str, "channel": str, "thread_ts": str,
   "pid": int, "logfile": str, "cmd": str, "started_ts": float}
`thread_ts` is the conversation KEY (a thread ts, or the DM channel id for a
flat 1:1 DM). Lives as a SIBLING of sessions.json (so SESSIONS_PATH redirects it
with no new env var), lock-guarded and atomically written like the other stores.
The top-level shape is a LIST, so it loads/saves via the shared list-store pair
in store.base (_load_list_store/_save_list_store, like the cron store). Entries
are removed by the completion watcher (src/slack/jobs.py) AFTER delivery, so a
persisted entry means "still running, or result not yet delivered".
"""

from __future__ import annotations

import time

from .base import (
    _SESSIONS_LOCK,
    _load_list_store,
    _resolve_path,
    _save_list_store,
    _sibling_store_path,
)


def _jobs_path():
    """Resolve the job-store path: a sibling of the sessions path."""
    return _sibling_store_path("jobs.json")


def list_jobs(path=None):
    """Return the persisted list of job entries (a fresh list, possibly empty)."""
    if path is None:
        path = _resolve_path("_jobs_path", _jobs_path)
    with _SESSIONS_LOCK:
        return _load_list_store(path)


def add_job(
    agent, channel, thread_ts, pid, logfile, cmd, job_id, path=None, limit=None
):
    """Append a new job entry and return it (read-modify-write under the lock).

    `job_id` is required: the spawner mints it up front for the logfile name, so
    the two always agree. `started_ts` (epoch seconds) is stamped here, the one
    place an entry is born; the timeout watcher enforces its window from it.

    `limit` is the GLOBAL concurrency guard: when set, the count-check and the
    append happen in ONE critical section under the shared store lock, so a
    store already holding `limit` or more entries appends nothing and returns
    None. Two simultaneous spawns serialize on the lock, so they can never both
    squeeze past the limit. None (the default) means no limit.
    """
    if path is None:
        path = _resolve_path("_jobs_path", _jobs_path)
    entry = {
        "id": job_id,
        "agent": agent,
        "channel": channel,
        "thread_ts": thread_ts,
        "pid": pid,
        "logfile": logfile,
        "cmd": cmd,
        "started_ts": time.time(),
    }
    with _SESSIONS_LOCK:
        jobs = _load_list_store(path)
        if limit is not None and len(jobs) >= limit:
            return None
        jobs.append(entry)
        _save_list_store(jobs, path)
    return entry


def remove_job(job_id, path=None):
    """Delete the job entry with `job_id`. Returns True if one was removed."""
    if path is None:
        path = _resolve_path("_jobs_path", _jobs_path)
    with _SESSIONS_LOCK:
        jobs = _load_list_store(path)
        kept = [j for j in jobs if j.get("id") != job_id]
        if len(kept) == len(jobs):
            return False
        _save_list_store(kept, path)
        return True
