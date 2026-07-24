"""File attachments: inbound download + outbound upload.

Slack messages can carry files[] (uploads, images, diagrams). Inbound: each
file's url_private is downloaded with the bot token (a Bearer header) and its
local path appended to the prompt so the CLI can read it. Outbound: the run
uploads files ONLY when it ends its reply with a trailing `<<files: a, b>>`
marker NAMING the files (resolved inside the thread's workdir); no marker (the
default) uploads nothing, so an ordinary reply never dumps the workdir back.
Diagrams (image/svg) need no special case -- they are ordinary files. All
HTTP/Slack I/O goes through small seams so tests mock it; nothing here performs
real network I/O at import time.

The Kind-B seams (_attachments_dir, _http_get_bytes) are resolved THROUGH the app
facade inside _download_attachments so a test's monkeypatch on the facade is seen
by this module's call sites.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import urllib.request

from src import store
from src.runners import claude_runner

logger = logging.getLogger("peon-chat")

# Dirs the outbound resolver must never descend into (tool caches, VCS, venvs).
# Pruned in-place during os.walk so their contents are never even stat'd.
_SKIP_DIRS = {
    ".ruff_cache",
    ".git",
    "__pycache__",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ipynb_checkpoints",
    ".venv",
}


def _trailing_marker_res(keyword):
    """Compile the (parse, strip) regex pair for a trailing `<<keyword: ...>>` marker.

    Owner of the tempered-dot pattern used by the FILES marker (the job marker
    in jobs.py owns its own greedy pair: a shell command must be able to carry
    ">>"). BOTH regexes act ONLY on a TRAILING marker (anchored at end of
    text): a reply that merely MENTIONS the marker syntax mid-text is plain
    prose and triggers nothing (and loses nothing after it). The marker body
    excludes ">>" (tempered dot) so a mid-text complete marker followed by
    prose can never anchor to the end. The parse regex captures the trailing
    complete marker's body (group 1); the strip regex also scrubs a
    partial/unterminated trailing marker still mid-stream (e.g. "<<files: pl")
    so it never flashes in the streamed reply.
    """
    return (
        re.compile(rf"<<\s*{keyword}\s*:\s*((?:(?!>>).)*)>>\s*$", re.IGNORECASE),
        re.compile(rf"\s*<<\s*{keyword}\s*:(?:(?!>>).)*(?:>>)?\s*$", re.IGNORECASE),
    )


def _split_trailing_marker(text, marker_re, strip_re):
    """Split text into (clean_text, body): body from the TRAILING complete marker.

    Core of _parse_file_marker (jobs._parse_job_marker rolls its own: its
    greedy strip regex must only apply on a complete-marker match): no trailing
    complete marker -> (text, "") with any partial trailing marker still
    stripped from clean_text (the marker is emitted last, so nothing real
    follows it).
    """
    if not text:
        return text, ""
    match = marker_re.search(text)
    body = match.group(1) if match else ""
    return strip_re.sub("", text).rstrip(), body


# Outbound delivery is opt-in: a run requests it by ENDING its reply with a
# `<<files: a, b>>` marker naming the files. Default (no marker) uploads nothing.
_FILES_MARKER_RE, _FILES_MARKER_STRIP_RE = _trailing_marker_res("files")


def _strip_file_marker(text):
    """Remove a TRAILING `<<files: ...>>` marker (complete or partial) from text."""
    if not text:
        return text
    return _FILES_MARKER_STRIP_RE.sub("", text)


def _parse_file_marker(text):
    """Split text into (clean_text, names): names from the TRAILING complete marker.

    No trailing marker -> (text, []); a mid-text marker mention is plain text and
    triggers nothing.
    """
    clean, body = _split_trailing_marker(text, _FILES_MARKER_RE, _FILES_MARKER_STRIP_RE)
    names = [n.strip() for n in body.split(",") if n.strip()]
    return clean, names


def _resolve_named_files(workdir, names):
    """Abs paths of the named files that exist INSIDE workdir (sorted, unique).

    Security boundary: a resolved path that escapes workdir (via `..`, an
    absolute name, or a symlink) is rejected. Each name is tried as a relative
    path first, then by basename via an os.walk that prunes _SKIP_DIRS/dot-dirs.
    A name that resolves nowhere under workdir is silently omitted.
    """
    if not workdir or not names or not os.path.isdir(workdir):
        return []
    base = os.path.realpath(workdir)
    found = []
    for name in names:
        cand = os.path.realpath(os.path.join(workdir, name))
        if cand.startswith(base + os.sep) and os.path.isfile(cand):
            found.append(cand)
            continue
        target = os.path.basename(name)
        for root, dirs, fnames in os.walk(workdir):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
            if target in fnames:
                hit = os.path.realpath(os.path.join(root, target))
                if hit.startswith(base + os.sep) and os.path.isfile(hit):
                    found.append(hit)
                break
    return sorted(set(found))


# Timeout (seconds) for one inbound attachment download. The download runs on
# the listener thread AFTER the thread's busy slot is claimed; without a timeout
# a stalled CDN read would wedge that slot forever, with no subprocess for !stop
# to signal.
_HTTP_TIMEOUT_S = 60


def _http_get_bytes(url, token):
    """Download `url` with the bot token and return the raw bytes.

    Uses stdlib urllib (no new dependency, ponytail): Slack's url_private requires
    an `Authorization: Bearer <bot token>` header. Factored out as the single HTTP
    seam so tests patch it (or urllib.request.urlopen) and never hit the network.
    """
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(  # noqa: S310 - Slack https url, header-auth
        req, timeout=_HTTP_TIMEOUT_S
    ) as resp:
        return resp.read()


def _attachments_dir(thread_ts):
    """A per-thread temp directory for downloaded attachments (created if absent).

    Scoped by thread_ts so files from different threads never collide. Lives under
    the system temp dir; left in place (the OS reaps temp), not cleaned per-run, so
    a later message in the same thread can still reference an earlier file path.

    The thread_ts is sanitized via the SAME canonical helper the workdir path uses
    (store._safe_token), so the path-component sanitizing rule lives in ONE
    place. Byte-identical for any real Slack thread_ts (digits + dot).
    """
    safe = store._safe_token(thread_ts)
    path = os.path.join(tempfile.gettempdir(), "peon-chat-files", safe)
    os.makedirs(path, exist_ok=True)
    return path


def _download_attachments(client, files, thread_ts):
    """Download each Slack file (by url_private) into the per-thread dir.

    `files` is the event's files[] list (Slack file dicts). For each file with a
    private URL we GET it with the bot token (client.token) and write it under the
    per-thread attachments dir, returning the list of local absolute paths in order.
    A per-file failure (missing URL, network/IO error) is logged and skipped so one
    bad file never blocks the message; an empty/None files list yields []. No real
    network call in tests: _http_get_bytes is the mocked seam.
    """
    from src import app as _appfacade

    if not files:
        return []
    token = getattr(client, "token", None)
    dest_dir = _appfacade._attachments_dir(thread_ts)
    paths = []
    for idx, f in enumerate(files):
        if not isinstance(f, dict):
            continue
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue
        # Prefer Slack's own name; fall back to id/index so we always have one.
        name = f.get("name") or f.get("id") or f"file-{idx}"
        name = os.path.basename(str(name)) or f"file-{idx}"
        local = os.path.join(dest_dir, name)
        try:
            data = _appfacade._http_get_bytes(url, token)
            with open(local, "wb") as out:
                out.write(data)
        except Exception:  # noqa: BLE001 - one bad file must not drop the message
            logger.warning("failed to download attachment %s", url)
            continue
        paths.append(local)
    return paths


def _append_attachments(prompt, paths):
    """Append the downloaded local paths to the prompt text, or return it unchanged.

    Adds one line "[Attached files: /abs/a.png, /abs/b.pdf]" so the CLI agent can
    open the files. No paths -> the prompt is returned byte-identical.
    """
    if not paths:
        return prompt
    return prompt + "\n\n[Attached files: " + ", ".join(paths) + "]"


def _thread_workdir(agent, thread_ts):
    """The per-thread workdir for this (agent, thread), or None.

    Uses the shared helper `claude_runner.get_workdir(agent_name, thread_ts)` (a
    PURE path lookup here: create defaults False, so this never spawns an empty
    dir; the outbound scan guards on os.path.isdir). Guarded so a helper that is
    absent or raises still yields None (never aborts the run).
    """
    helper = getattr(claude_runner, "get_workdir", None)
    if helper is None:
        return None
    try:
        return helper(agent["name"], thread_ts)
    except Exception:  # noqa: BLE001 - a misbehaving helper must not abort the run
        logger.warning("get_workdir failed for %s", agent["name"])
        return None


def _upload_workdir_files(client, channel, thread_ts, paths):
    """Upload each produced file back into the thread via files_upload_v2.

    One call per file (filename = basename). A per-file upload failure is logged
    and skipped so one bad upload never aborts the rest. Returns the count uploaded.
    """
    uploaded = 0
    for path in paths:
        try:
            client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                file=path,
                filename=os.path.basename(path),
            )
            uploaded += 1
        except Exception:  # noqa: BLE001 - one bad upload must not abort the rest
            logger.warning("failed to upload produced file %s", path)
    return uploaded


def _maybe_upload_named(client, channel, thread_ts, agent, names):
    """Upload the run-named files (from its `<<files: ...>>` marker) into the thread.

    No names (the default, no marker) -> a no-op returning 0 (files_upload_v2 is
    never called). Otherwise each name is resolved inside the thread's workdir
    (see _resolve_named_files; paths escaping the workdir are rejected) and the
    resolved files are uploaded. Guarded so an upload error never crashes the
    worker.

    `thread_ts` is the conversation KEY (a thread ts, or the DM channel id for
    a flat 1:1 DM): the workdir is resolved from the KEY, but the upload's
    POSTING target goes through _reply_thread_ts (a Kind-B name, resolved
    through the app facade like the other seams here) so a flat-DM upload
    posts flat (Slack rejects a channel id as thread_ts).
    """
    from src import app as _appfacade

    if not names:
        return 0
    workdir = _thread_workdir(agent, thread_ts)
    if not workdir:
        return 0
    produced = _resolve_named_files(workdir, names)
    if not produced:
        return 0
    return _upload_workdir_files(
        client, channel, _appfacade._reply_thread_ts(thread_ts), produced
    )
