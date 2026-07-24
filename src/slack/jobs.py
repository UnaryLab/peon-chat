"""Background jobs and subagent spawns: the trailing `<<job: ...>>` and
`<<spawn: ...>>` markers, the detached spawn, the completion watcher, the
restart re-attach, and the `!job` control phrases (`_handle_job_command`: list
this agent's jobs / SIGTERM a job's process group).

A run opts in to long/background work by ENDING its reply with a
`<<job: <shell command>>>` marker on its own line (the trailing-only rule of
`<<files:>>` plus a line-start rule: a mid-text or mid-line mention of the
syntax is plain prose and triggers nothing; plus an opener-line rule: a body
may span newlines only when the marker's opening line carries no ">>", so a
line-start quoted example followed by prose stays prose). After the
final reply posts, the worker spawns the command DETACHED (/bin/sh -c, its own
session via start_new_session, stdout+stderr written to a job-<id>.log in the
thread workdir) so it survives the turn AND this process, and persists a
jobs.json entry (src/store/jobs.py). A daemon watcher thread waits for the
process; on completion it reads the log tail and delivers the result back into
the conversation by synthesizing a follow-up agent turn through the SAME
_run_and_update seam a cron fire uses (the thread's session resumes, so the
agent summarizes in context). A thread that is BUSY at completion time gets the
raw completion note (exit code + log tail) as a plain message instead: the
information is delivered either way, never silently dropped. The jobs.json
entry is removed only AFTER a successful delivery, so a failed delivery is
retried on the next restart. On startup, _reattach_jobs re-arms a watcher for
every persisted entry from a previous process lifetime (the exit code is then
unknown).

The sibling `<<spawn: <task prompt>>>` marker spawns a detached one-shot CLI
SUBAGENT run riding the same machinery; _start_spawn tells the full spawn story
(and _finish_job its result delivery).

The command runs fully unsandboxed, like every run in peon-chat (the documented
security posture); the workdir is its cwd, not a confinement boundary.

Two guardrails: a per-job TIMEOUT (AGENT_TIMEOUT_MIN, the same knob that bounds
a runner's subprocess, enforced by the watcher from the entry's started_ts;
SIGTERM the group, grace, SIGKILL; delivered with a timed-out label) and a
GLOBAL concurrency LIMIT (JOB_MAX_CONCURRENT, checked atomically inside
store.add_job; an at-limit spawn is declined with a note, never queued). Both
are optional env vars; 0 disables either.

Kind-B seams: _watch_job, _finish_job, _pid_alive, _run_and_update, and
_reply_thread_ts are resolved THROUGH the app facade (lazy in-body
`from src import app as _appfacade`) so a test's monkeypatch on the facade is
seen by this module's call sites. The spawn uses the bare `import subprocess`
singleton, so tests patch app.subprocess.Popen (Kind A).
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
import uuid

from src import agents, runners, store
from src.runners.common import _int_env, agent_timeout_min

from . import interrupt

logger = logging.getLogger("peon-chat")

# Trailing-only marker pair, OWNED here (the `<<files:>>` pair keeps
# files._trailing_marker_res). The files pattern tempers its body on ">>",
# which cannot carry a shell command containing ">>" (append redirect,
# heredoc). The job body CAN carry ">>", guarded by THREE rules: the opener
# must START A LINE (`(?:^|\n)[ \t]*`, so a mid-line prose mention never
# anchors); the parse matches the LAST such `<<job:` occurrence (the body may
# not contain another marker opener, job or files, so a trailing `<<files:>>`
# marker after a mid-text job mention is never swallowed); and the OPENER
# LINE discriminates single-line from multi-line bodies:
#   - opener line contains ">>": the marker must be SINGLE-LINE (`[^\n]*`
#     body) and its closing ">>" must end the text. A line-start quoted
#     example ("<<job: make all>>" followed by more prose in a reply that
#     happens to end in ">>") is therefore PROSE, never run as shell.
#   - opener line contains NO ">>": the body may span newlines (heredocs),
#     greedy with DOTALL to the text-final ">>". The one limitation this
#     buys: a multi-line body whose FIRST line contains ">>" is prose; put
#     ">>" appends on a single-line marker or on later lines of a heredoc.
# A command ending in ">" works too (a reply ending "...>>>": the last two
# ">" close the marker, the rest stays in the command).
# The strip regex scrubs a partially streamed marker (its closing ">>" not
# yet arrived, possibly with ">>" inside the command) without eating prose
# the parse rejects: a line-start opener whose line runs to the end of the
# text (complete or partial) is scrubbed, and a multi-line body is scrubbed
# under the same no-">>"-on-the-opener-line rule as the parse.
_MARKER_OPENERS = r"<<\s*(?:job|files|spawn)\s*:"


def _greedy_marker_res(keyword):
    """Compile the greedy (parse, strip) regex pair for `<<keyword: ...>>`.

    Shared by the JOB and SPAWN markers (both bodies may carry ">>", under the
    opener-line rule above); the FILES pair keeps its tempered helper in
    files.py. The body may not contain another marker opener (job, files, or
    spawn), so a trailing sibling marker after a mid-text mention is never
    swallowed.
    """
    single_line_body = rf"(?:(?!{_MARKER_OPENERS})[^\n])*"
    multi_line_body = (
        rf"(?:(?!{_MARKER_OPENERS})(?!>>)[^\n])*\n(?:(?!{_MARKER_OPENERS}).)*"
    )
    parse = re.compile(
        rf"(?:^|\n)[ \t]*<<\s*{keyword}\s*:\s*"
        rf"({single_line_body}|{multi_line_body})>>\s*$",
        re.IGNORECASE | re.DOTALL,
    )
    strip = re.compile(
        rf"\s*(?:^|\n)[ \t]*<<\s*{keyword}\s*:"
        rf"(?:{single_line_body}\s*\Z|{multi_line_body}$)",
        re.IGNORECASE | re.DOTALL,
    )
    return parse, strip


_JOB_MARKER_RE, _JOB_MARKER_STRIP_RE = _greedy_marker_res("job")
# The sibling `<<spawn: <task prompt>>>` marker: same anchoring discipline, but
# the body is a natural-language task for a detached one-shot CLI subagent run
# (see _start_spawn), not shell.
_SPAWN_MARKER_RE, _SPAWN_MARKER_STRIP_RE = _greedy_marker_res("spawn")

# How much of the END of the job log is delivered back into the thread.
_JOB_LOG_TAIL_CHARS = 4000

# Cap on a SPAWN's extracted final message where it feeds the completion note
# (head+tail so both ends of a long report survive): the plain-note path must
# stay under Slack's ~40k post limit, and the delivery prompt rides the CLI
# argv, so an unbounded result risks ARG_MAX.
_SPAWN_RESULT_MAX_CHARS = 8000

# Cap on the Slack transcript prepended to a SPAWN's task prompt by
# _compose_spawn_prompt (the prompt rides the CLI argv, so it must stay
# bounded); over the cap the NEWEST end (tail) is kept behind a truncation note.
_SPAWN_TRANSCRIPT_MAX_CHARS = 16000

# Seconds between pid polls for a RE-ATTACHED watcher (no Popen handle after a
# restart, so liveness is probed instead of waited on).
_JOB_POLL_INTERVAL_S = 5

# Max chars of a job's command shown by `!job list` (longer is ellipsized).
_JOB_LIST_CMD_CHARS = 80

# The max lifetime of a job comes from AGENT_TIMEOUT_MIN (common.agent_timeout_min,
# read LIVE when a watcher (re)starts; 0 disables the timeout entirely).

# Grace between the timeout SIGTERM and the SIGKILL escalation, in seconds.
_JOB_KILL_GRACE_S = 5

# Default max jobs running at once, GLOBAL across all agents (JOB_MAX_CONCURRENT
# overrides, read LIVE at spawn; 0 disables the limit).
_JOB_MAX_CONCURRENT_DEFAULT = 16


def _parse_trailing(regex, text):
    """Split text into (clean_text, body): body from the TRAILING complete marker.

    No trailing COMPLETE marker -> (text, None) unchanged; the opener must
    start a line (see _greedy_marker_res), so a mid-text or mid-line marker
    mention is plain prose and triggers nothing. Unlike the files marker the
    body may contain ">>", under the opener-line rule: an opener line that
    contains ">>" must be a single-line marker closing at the very end of the
    text (a line-start quoted example followed by prose is prose), and only
    an opener line with no ">>" may span newlines (heredocs). Does not share
    files._split_trailing_marker: the strip regex must only apply where the
    parse could match (a blind strip would eat a mid-text mention's tail).
    """
    if not text:
        return text, None
    match = regex.search(text)
    if match is None:
        return text, None
    body = match.group(1).strip()
    return text[: match.start()].rstrip(), (body or None)


def _strip_trailing(regex, text):
    """Remove a TRAILING marker (complete or partial) from text."""
    if not text:
        return text
    return regex.sub("", text)


def _strip_job_marker(text):
    """Remove a TRAILING `<<job: ...>>` marker (complete or partial) from text."""
    return _strip_trailing(_JOB_MARKER_STRIP_RE, text)


def _parse_job_marker(text):
    """Split text into (clean_text, cmd): cmd from the TRAILING complete marker."""
    return _parse_trailing(_JOB_MARKER_RE, text)


def _strip_spawn_marker(text):
    """Remove a TRAILING `<<spawn: ...>>` marker (complete or partial) from text."""
    return _strip_trailing(_SPAWN_MARKER_STRIP_RE, text)


def _parse_spawn_marker(text):
    """Split text into (clean_text, task): the spawn analog of _parse_job_marker
    (the body is the subagent's task prompt, not shell)."""
    return _parse_trailing(_SPAWN_MARKER_RE, text)


def _compose_spawn_prompt(task, transcript):
    """Prepend the dispatching turn's Slack transcript to a spawn task prompt.

    The subagent runs with a FRESH session and no thread access, so peon-chat
    deterministically hands it the transcript the turn already fetched (never
    re-fetched): even a lazy task prompt ("run the comparison discussed
    above") has the conversation to refer to. Plain prompt text, identical
    for both backends, and the transcript sits MID-PROMPT, so marker-like
    text inside it is mechanically inert (prompts are never marker-parsed).
    No transcript (flat DM, cron fire, completion delivery) -> the task body
    alone. Over _SPAWN_TRANSCRIPT_MAX_CHARS the NEWEST tail is kept behind a
    truncation note. The single source of truth for the composition template.
    """
    if not transcript:
        return task
    if len(transcript) > _SPAWN_TRANSCRIPT_MAX_CHARS:
        transcript = (
            "[transcript truncated: newest part kept]\n"
            + transcript[-_SPAWN_TRANSCRIPT_MAX_CHARS:]
        )
    return (
        "Recent conversation for context (Slack thread transcript):\n"
        f"{transcript}\n\nYour task:\n{task}"
    )


def _spawn_fork_session(agent, thread_ts):
    """The stored thread session id a claude spawn FORKS, or None (fresh spawn).

    The ONE place the fork-vs-fresh decision lives: _run_and_update consults it
    to SKIP the transcript prepend (a fork already carries the conversation),
    and _start_spawn consults it to build the fork argv. Claude-only:
    `--fork-session` (verified against claude CLI 2.1.201) resumes the thread's
    session but writes to a NEW id the CLI mints, so the subagent inherits the
    full hidden conversation state while the thread's stored id is never
    mutated (and the forked id is never persisted). Safe by construction: the
    spawn launches only AFTER the dispatching run finished and its reply
    posted, so the fork resumes a quiescent session (one accepted window: the
    busy slot frees right after the detached Popen, so a fast next message can
    --resume the same id while the fork's startup read is in flight; the fork
    never mutates the original, it just may not see that concurrent turn).
    Codex has no fork analog -> None (fresh run + transcript prepend,
    unchanged).
    """
    if agent.get("backend", "claude") != "claude":
        return None
    return store.get_session(agent["name"], thread_ts) or None


def _read_log_tail(logfile):
    """The last _JOB_LOG_TAIL_CHARS chars of the job log ("" if unreadable).

    Seeks to the tail rather than reading the whole file (a long job's log can be
    huge); decoded with errors="replace" so binary noise degrades instead of
    raising. UTF-8 never packs more chars than bytes, so the byte-bounded read
    already bounds the chars.
    """
    if not logfile:
        return ""
    try:
        with open(logfile, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _JOB_LOG_TAIL_CHARS))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _pid_alive(pid):
    """Whether a process with `pid` exists (the os.kill(pid, 0) probe).

    A malformed pid (None or a non-int from a hand-edited store) reads as dead
    (TypeError caught), so the completion flow still delivers and clears the
    entry instead of wedging it forever.

    ponytail: accepts the pid-reuse race -- a recycled pid reads as "still
    running" and the re-attach watcher just keeps polling until the impostor
    exits; if the timeout window expires first, the expiry SIGTERM/SIGKILL
    can hit an innocent same-uid group that recycled the pid. No
    process-start-time verification (that remains the upgrade path); the
    poll self-corrects.
    """
    try:
        os.kill(pid, 0)
    except (OSError, TypeError):
        return False
    return True


def _signal_group(pid, sig):
    """Best-effort signal to a job's process group; False if unsignalable.

    Same guard set as the `!job kill` path: gone group (ProcessLookupError),
    pid recycled to another uid (PermissionError), malformed pid (TypeError).
    """
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError, TypeError):
        return False
    return True


def _start_job(client, agent, channel, thread_ts, cmd):
    """Spawn `cmd` detached in the thread workdir, persist it, and arm a watcher.

    Called by the worker AFTER the final reply posts. See _launch_detached for
    the shared spawn/persist/watch/decline mechanics.
    """
    workdir = store.get_workdir(agent["name"], thread_ts, create=True)
    job_id = uuid.uuid4().hex[:8]
    return _launch_detached(
        client, agent, channel, thread_ts, workdir, job_id, ["/bin/sh", "-c", cmd], cmd
    )


def _start_spawn(client, agent, channel, thread_ts, task):
    """Spawn `task` as a detached one-shot CLI SUBAGENT run; ride the job machinery.

    Builds a one-shot argv through the dispatching agent's own backend runner
    (claude with a stored thread session: a --resume <thread id> --fork-session
    FORK, full hidden context inherited, writing to a NEW CLI-minted id that is
    NEVER stored in sessions.json, the thread's id untouched -- see
    _spawn_fork_session; claude without one: fresh with a minted --session-id
    uuid, equally ephemeral; codex: the verified fresh `codex exec` shape, with
    the -o lastmsg file placed in the workdir so completion can recover the
    final message after a restart). Per-thread model/effort overrides apply
    like a normal run (threaded into build_command). The jobs.json entry carries the
    `kind: "spawn"` discriminator (and `lastmsg` for codex); everything else --
    detach, watcher, timeout, concurrency cap, !job list/kill, re-attach --
    is the SAME machinery as _start_job (see _launch_detached).
    """
    workdir = store.get_workdir(agent["name"], thread_ts, create=True)
    job_id = uuid.uuid4().hex[:8]
    overrides = store.get_override(agent["name"], thread_ts)
    backend = agent.get("backend", "claude")
    runner = runners.get_runner(backend)
    extra = {"kind": "spawn"}
    if backend == "codex":
        lastmsg = os.path.join(workdir, f"spawn-{job_id}.last")
        argv = runner.build_command(
            agent, task, None, True, lastmsg, overrides=overrides
        )
        extra["lastmsg"] = lastmsg
    else:
        fork_sid = _spawn_fork_session(agent, thread_ts)
        if fork_sid:
            argv = runner.build_command(
                agent, task, fork_sid, False, overrides=overrides, fork_session=True
            )
        else:
            argv = runner.build_command(
                agent, task, str(uuid.uuid4()), True, overrides=overrides
            )
    return _launch_detached(
        client, agent, channel, thread_ts, workdir, job_id, argv, task, extra=extra
    )


def _launch_detached(
    client, agent, channel, thread_ts, workdir, job_id, argv, cmd, extra=None
):
    """Spawn `argv` detached in `workdir`, persist a jobs.json entry, arm a watcher.

    Shared guts of _start_job (argv = /bin/sh -c <cmd>) and _start_spawn (see
    its docstring for the spawn story; `extra` carries the spawn's
    kind/lastmsg fields and `cmd` is then the task prompt). The process
    gets its own session (start_new_session=True) so it is not killed when the
    turn's CLI exits; stdin is /dev/null and stdout+stderr are written to
    job-<id>.log inside the workdir. Returns the persisted store entry.
    Failures raise; the caller guards (the reply already posted, so a spawn
    failure must not destroy it). If persisting/arming fails AFTER the spawn,
    the process is killed (best-effort) so no untracked orphan runs while the
    user is told it failed.

    GLOBAL concurrency guard (JOB_MAX_CONCURRENT, read LIVE per spawn; default
    _JOB_MAX_CONCURRENT_DEFAULT, 0 disables), shared by jobs AND spawns: at the
    limit the spawn is DECLINED, never queued (returns None after posting a
    note into the thread). The count-check + append are ONE critical section
    inside store.add_job (the shared store lock), so two simultaneous spawns
    can never both squeeze past the limit. The pid only exists after Popen, so
    we spawn FIRST and kill the fresh process on a decline: simpler than
    reserving a placeholder-pid entry, and race-free all the same (the
    persisted entry count never exceeds the limit, and no pid-less entry ever
    reaches !job list or _reattach_jobs).
    """
    from src import app as _appfacade

    limit = _int_env("JOB_MAX_CONCURRENT", _JOB_MAX_CONCURRENT_DEFAULT)
    logfile = os.path.join(workdir, f"job-{job_id}.log")
    with open(logfile, "wb") as log:
        proc = subprocess.Popen(
            argv,
            cwd=workdir,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        entry = store.add_job(
            agent["name"],
            channel,
            thread_ts,
            proc.pid,
            logfile,
            cmd,
            job_id,
            limit=limit if limit > 0 else None,
            extra=extra,
        )
        if entry is not None:
            logger.info("job %s: started pid %s: %s", job_id, proc.pid, cmd)
            threading.Thread(
                target=_appfacade._watch_job,
                args=(entry, client),
                kwargs={"proc": proc},
                daemon=True,
                name=f"job-watch-{job_id}",
            ).start()
    except Exception:
        # The user will be told the job failed to start; do not leave a live
        # detached orphan behind that claim. The job is a session/group
        # leader, so SIGKILL the WHOLE group (proc.kill() alone would orphan
        # a forked child, e.g. a make the shell already spawned), then reap.
        _signal_group(proc.pid, signal.SIGKILL)
        proc.wait()
        raise
    if entry is None:
        # Declined at the limit: SIGKILL the just-spawned process GROUP (it
        # is a session/group leader; proc.kill() alone would orphan a forked
        # child), reap it, and tell the thread why nothing is running.
        _signal_group(proc.pid, signal.SIGKILL)
        proc.wait()
        running = len(store.list_jobs())  # display only; the check was in add_job
        logger.info(
            "job %s: declined, %s jobs already running (limit %s)",
            job_id,
            running,
            limit,
        )
        noun = "job" if running == 1 else "jobs"
        client.chat_postMessage(
            channel=channel,
            thread_ts=_appfacade._reply_thread_ts(thread_ts),
            text=(
                f"job not started: {running} background {noun} already running "
                f"(limit {limit}); use `!job list` / `!job kill <id>` to make room."
            ),
        )
        return None
    return entry


def _watch_job(entry, client, proc=None, sleep=None, now=None):
    """Daemon-thread body: wait for the job to finish, then run the completion flow.

    With `proc` (a job spawned this process lifetime) it proc.wait()s and reports
    the real exit code. Without it (re-attached after a restart) it polls the
    persisted pid every _JOB_POLL_INTERVAL_S until gone -- an already-dead pid
    finishes on the FIRST poll, with no sleep -- and the exit code is unknown
    (None). `sleep`/`now` are injectable for hermetic tests. Never raises (a
    watcher error must not kill its daemon thread mid-flow silently).

    TIMEOUT: the LIVE AGENT_TIMEOUT_MIN captured here, at watcher (re)start, is
    the enforced window for this watcher's whole lifetime (a SIGHUP .env reload
    applies to watchers spawned/re-attached after it); 0 disables. The window
    runs from the entry's started_ts, so a RE-ATTACHED watcher enforces only the
    REMAINING time, and an already-expired re-attached job is killed on the
    first poll. On expiry: SIGTERM the process group, _JOB_KILL_GRACE_S grace,
    SIGKILL the group if still alive, then deliver through the NORMAL completion
    flow with the timed-out label.
    """
    from src import app as _appfacade

    if sleep is None:
        sleep = time.sleep
    if now is None:
        now = time.time
    try:
        timeout_min = agent_timeout_min()
        # An entry persisted before started_ts existed is treated as started
        # NOW: never retro-kill a pre-upgrade job on sight. `is None`, not
        # falsy: a stored 0 is a real epoch and must be respected.
        started = entry.get("started_ts")
        if started is None:
            started = now()
        deadline = (started + timeout_min * 60) if timeout_min > 0 else None
        pid = entry.get("pid")
        timed_out = False
        if proc is not None:
            if deadline is None:
                proc.wait()
            else:
                try:
                    proc.wait(timeout=max(0.0, deadline - now()))
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _signal_group(pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=_JOB_KILL_GRACE_S)
                    except subprocess.TimeoutExpired:
                        _signal_group(pid, signal.SIGKILL)
                        proc.wait()
            exit_code = proc.returncode
        else:
            while _appfacade._pid_alive(pid):
                if deadline is not None and now() >= deadline:
                    timed_out = True
                    if _signal_group(pid, signal.SIGTERM):
                        sleep(_JOB_KILL_GRACE_S)
                        if _appfacade._pid_alive(pid):
                            _signal_group(pid, signal.SIGKILL)
                    break
                sleep(_JOB_POLL_INTERVAL_S)
            exit_code = None
        if timed_out:
            logger.warning(
                "job %s: timed out after %s min; killed", entry.get("id"), timeout_min
            )
            _appfacade._finish_job(entry, client, exit_code, timed_out_min=timeout_min)
        else:
            _appfacade._finish_job(entry, client, exit_code)
    except Exception:  # noqa: BLE001 - the watcher thread must fail loudly, not die bare
        logger.exception("job %s: watcher failed", entry.get("id"))


def _spawn_result(entry):
    """Extract a finished SPAWN's final message, or None (caller falls back to tail).

    codex (entry carries `lastmsg`): the -o last-message file holds the reply.
    claude: the log holds the one-shot `--output-format json` blob on stdout
    (stderr merged in), so scan the log's lines from the END for the JSON
    object carrying a string "result". Any read/parse failure -> None, and the
    completion note degrades to the raw log tail, never a crash.

    ponytail: reads the whole claude log into memory; a one-shot -p run's
    stdout is a single JSON line plus stderr noise, so this stays small. A
    bounded reverse scan is the upgrade path if logs ever grow.
    """
    lastmsg = entry.get("lastmsg")
    if lastmsg:
        try:
            with open(lastmsg, "r", encoding="utf-8", errors="replace") as f:
                text = f.read().strip()
        except OSError:
            return None
        return text or None
    try:
        with open(entry.get("logfile"), "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except (OSError, TypeError):
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        result = payload.get("result") if isinstance(payload, dict) else None
        if isinstance(result, str):
            return result
    return None


def _finish_job(entry, client, exit_code, timed_out_min=None):
    """Completion flow: read the log tail, deliver the result, THEN drop the entry.

    `timed_out_min` (the watcher's enforced window, in minutes) marks a job the
    timeout killed: the completion prompt/note carries a clear "timed out after
    N min" label, log tail still included.

    Preferred delivery synthesizes a follow-up agent turn through the SAME
    _run_and_update seam a cron fire uses (placeholder post + the try_register
    busy guard; the thread's existing session resumes so the agent summarizes in
    context). A BUSY thread, or an agent gone from the registry, gets the raw
    completion note as a plain message instead -- never a silent skip. All
    posting translates the conversation KEY via _reply_thread_ts (a flat-DM key
    is the DM channel id and must post flat).

    The jobs.json entry is removed only once delivery has landed (the plain note
    posted, or the agent-turn placeholder posted): a delivery failure keeps the
    entry so the next restart re-attaches and re-delivers. A crash BETWEEN the
    post and the remove re-delivers too; a rare double-delivery beats a silently
    lost result.

    A SPAWN entry (kind == "spawn"; an absent kind is a legacy job) delivers
    the subagent's extracted final message (_spawn_result) instead of the log
    tail, falling back to the tail on any extraction failure; the result is
    capped head+tail at _SPAWN_RESULT_MAX_CHARS where it feeds the note (the
    plain note must fit Slack's post limit, the prompt rides argv). It rides
    the delivery PROMPT (or the plain note) as DATA: prompts are never
    marker-parsed, so a `<<files:>>`/`<<job:>>`/`<<spawn:>>` inside the
    subagent's output can never itself trigger an upload/job/spawn (only the
    delivering agent's own reply goes through normal marker parsing).
    """
    from src import app as _appfacade

    name = entry.get("agent")
    channel = entry.get("channel")
    thread_ts = entry.get("thread_ts")
    code = "exit code unknown" if exit_code is None else f"exit code {exit_code}"
    is_spawn = entry.get("kind") == "spawn"
    noun = "background subagent" if is_spawn else "background job"
    if timed_out_min:
        head = f"[{noun} timed out after {timed_out_min} min and was killed, {code}]"
    else:
        head = f"[{noun} finished, {code}]"
    result = _spawn_result(entry) if is_spawn else None
    if result is not None:
        # Cap the result HERE, the one point where it feeds `note`, so both the
        # delivery-prompt path (rides the CLI argv) and the plain-note path
        # (Slack's ~40k post limit) stay bounded.
        if len(result) > _SPAWN_RESULT_MAX_CHARS:
            half = _SPAWN_RESULT_MAX_CHARS // 2
            result = (
                result[:half]
                + "\n… _(truncated: full subagent result too long)_ …\n"
                + result[-half:]
            )
        note = f"{head} result:\n{result}"
    else:
        note = f"{head} output tail:\n{_read_log_tail(entry.get('logfile'))}"

    def _post_plain():
        """Post the raw completion note; True (and entry removed) on success."""
        try:
            client.chat_postMessage(
                channel=channel,
                thread_ts=_appfacade._reply_thread_ts(thread_ts),
                text=note,
            )
        except Exception:  # noqa: BLE001 - a Slack hiccup must not kill the watcher
            logger.exception(
                "job %s: failed to post completion note; keeping the entry "
                "for redelivery",
                entry.get("id"),
            )
            return False
        store.remove_job(entry.get("id"))
        return True

    agent = agents.get(name)
    if agent is None:
        logger.warning(
            "job %s: agent %r not in registry; posting raw note", entry.get("id"), name
        )
        _post_plain()
        return
    token = interrupt.try_register(name, thread_ts)
    if token is None:
        logger.info("job %s: thread busy; posting raw completion note", entry.get("id"))
        _post_plain()
        return
    # We hold the thread's busy slot; until _run_and_update's finally takes over
    # cleanup, a failed placeholder post must release it (mirrors _handle).
    try:
        placeholder = client.chat_postMessage(
            channel=channel,
            thread_ts=_appfacade._reply_thread_ts(thread_ts),
            text=f"{agent['display_name']} ({noun}) is thinking...",
        )
        placeholder_ts = placeholder["ts"]
    except Exception:  # noqa: BLE001 - a Slack hiccup must not wedge the busy slot
        interrupt.unregister(name, thread_ts, token)
        logger.exception(
            "job %s: failed to post placeholder; keeping the entry for redelivery",
            entry.get("id"),
        )
        return
    # Delivery is underway (the placeholder is visible in the thread): drop the
    # entry now, before the long agent turn.
    store.remove_job(entry.get("id"))
    _appfacade._run_and_update(
        client, channel, placeholder_ts, agent, note, thread_ts, token=token
    )


def _handle_job_command(agent, arg, thread_ts, say):
    """Handle `!job <sub>` (list | kill <id>). Posts an ack; never runs the agent.

    Scoped to the DISPATCHING agent: only ITS jobs (across ALL conversations)
    are listed and killable; another agent's job id reads as "no such job", so
    one bot cannot kill another bot's work. `kill` SIGTERMs the whole process
    group (the spawn's start_new_session makes the pid a group leader) and does
    NOT touch the jobs.json entry: the watcher owns delivery + removal, so a
    kill settles through the normal completion flow. An unsignalable group
    (ProcessLookupError; PermissionError from a pid recycled to another uid;
    a malformed pid via TypeError, like _pid_alive) is acked as already
    finished, and the watcher delivers as usual.
    """
    sub, _, rest = arg.partition(" ")
    sub = sub.lower()
    rest = rest.strip()
    mine = [j for j in store.list_jobs() if j.get("agent") == agent["name"]]

    if sub == "list":
        if not mine:
            say(
                text=f"{agent['display_name']}: no background jobs running.",
                thread_ts=thread_ts,
            )
            return
        lines = [f"{agent['display_name']}: background jobs:"]
        for j in mine:
            cmd = j.get("cmd") or ""
            if len(cmd) > _JOB_LIST_CMD_CHARS:
                cmd = cmd[:_JOB_LIST_CMD_CHARS] + "…"
            # A spawn entry's cmd is its TASK prompt; mark it so the two kinds
            # read apart in the list.
            if j.get("kind") == "spawn":
                cmd = f"spawn: {cmd}"
            lines.append(
                f"- `{j.get('id')}` [{j.get('thread_ts')}] pid {j.get('pid')}: {cmd}"
            )
        say(text="\n".join(lines), thread_ts=thread_ts)
        return

    if sub == "kill" and rest:
        job_id = rest
        entry = next((j for j in mine if j.get("id") == job_id), None)
        if entry is None:
            say(
                text=f"{agent['display_name']}: no such job `{job_id}`.",
                thread_ts=thread_ts,
            )
            return
        try:
            # ponytail: same pid-reuse ceiling as _pid_alive -- a recycled pid
            # now owned by ANOTHER uid raises PermissionError (acked below as
            # already finished, which for pid reuse the job is); a recycled
            # SAME-uid group can still be signaled. Process-start-time
            # verification is the upgrade path.
            os.killpg(entry.get("pid"), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, TypeError):
            say(
                text=(
                    f"{agent['display_name']}: job `{job_id}` already finished; "
                    "its result arrives via the watcher."
                ),
                thread_ts=thread_ts,
            )
            return
        say(
            text=f"{agent['display_name']}: kill signaled for job `{job_id}`.",
            thread_ts=thread_ts,
        )
        return

    say(text="Usage: !job list | !job kill <id>", thread_ts=thread_ts)


def _reattach_jobs(live):
    """Re-arm a watcher for every job persisted by a previous process lifetime.

    Called once at startup, after the live handler set is built. Per entry: an
    agent gone from the registry drops the entry (warning); an agent in the
    registry but not LIVE (no Slack connection to deliver through) is left in
    the store for a later restart; otherwise a re-attach watcher thread is
    spawned (_watch_job with no proc: it polls the pid, so an already-dead job
    runs the completion flow immediately, exit code unknown). Crash-safe: one
    bad entry never blocks the rest, and a broken store never kills startup.
    """
    from src import app as _appfacade

    try:
        entries = store.list_jobs()
    except Exception:  # noqa: BLE001 - a broken store must not kill startup
        logger.exception("failed to load jobs.json; skipping job re-attach")
        return
    for entry in entries:
        try:
            name = entry.get("agent")
            if agents.get(name) is None:
                logger.warning(
                    "job %s: agent %r no longer exists; dropping",
                    entry.get("id"),
                    name,
                )
                store.remove_job(entry.get("id"))
                continue
            handler_entry = live.get(name)
            if handler_entry is None:
                logger.warning(
                    "job %s: agent %r is not live; leaving for a later restart",
                    entry.get("id"),
                    name,
                )
                continue
            client = handler_entry["handler"].app.client
            threading.Thread(
                target=_appfacade._watch_job,
                args=(entry, client),
                daemon=True,
                name=f"job-watch-{entry.get('id')}",
            ).start()
            logger.info(
                "job %s: re-attached watcher (pid %s)",
                entry.get("id"),
                entry.get("pid"),
            )
        except Exception:  # noqa: BLE001 - one bad entry must not block the rest
            logger.exception("job %s: re-attach failed", entry.get("id"))
