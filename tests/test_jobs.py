"""Background jobs: the <<job:>> marker, the jobs.json store, the detached
spawn, the completion watcher, the restart re-attach, and the !job list/kill
control phrases.

All process/Slack I/O is mocked: the spawn is asserted through the Kind-A
app.subprocess.Popen patch target, pid liveness / the watcher / the completion
fire through the Kind-B facade names (_pid_alive, _watch_job, _finish_job,
_run_and_update), and the !job kill signal through a monkeypatched os.killpg.
No real subprocess, signal, sleep, or Slack call.
"""

import logging
import os
import signal
import subprocess
import threading

from src.runners import claude_runner, common

from tests.helpers import (
    _FakeJobClient,
    _FakeSay,
    _CONTROL_AGENT,
    _FILE_AGENT,
    _appmod,
    _HAVE_APP,
)


# ---------------------------------------------------------------------------
# Marker parsing (mirrors the <<files:>> trailing-only rules)
# ---------------------------------------------------------------------------


def test_parse_job_marker_extracts_and_strips():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    # No marker -> text unchanged, no command.
    assert _appmod._parse_job_marker("just a normal reply") == (
        "just a normal reply",
        None,
    )
    # Trailing marker -> command parsed (trimmed) and stripped off the reply.
    clean, cmd = _appmod._parse_job_marker(
        "Started the sweep.\n<<job: python run_sweep.py --all >>"
    )
    assert clean == "Started the sweep."
    assert cmd == "python run_sweep.py --all"
    # A mid-text marker mention is plain prose; only the TRAILING line-start
    # one triggers.
    clean, cmd = _appmod._parse_job_marker("a <<job: x>> b\n<<job: y>>")
    assert cmd == "y"
    assert clean == "a <<job: x>> b"
    mid_only = "the syntax is <<job: cmd>> which I will not use here."
    assert _appmod._parse_job_marker(mid_only) == (mid_only, None)
    # Falsy input is returned as-is.
    assert _appmod._parse_job_marker("") == ("", None)


def test_job_marker_midline_mention_with_gt_tail_is_prose():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    # A mid-line `<<job:` mention plus a reply that happens to END in ">>"
    # (a Slack link plus ">", an ASCII arrow "-->>") must neither parse nor
    # strip: pre-fix, the greedy body ran from the mention to the text-final
    # ">>" and the prose was executed as shell.
    link_tail = "use <<job: x>> like this. docs: <http://x|link> >>"
    arrow_tail = "you could use <<job: sleep 5>> here. onwards -->>"
    multiline = "plan:\nuse <<job: x>> as shown above\nand then -->>"
    for text in (link_tail, arrow_tail, multiline):
        assert _appmod._parse_job_marker(text) == (text, None)
        assert _appmod._strip_job_marker(text) == text


def test_job_marker_linestart_quoted_example_is_prose():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    # A LINE-START quoted example (how a model quotes the syntax) whose reply
    # happens to end in ">>": the opener line contains ">>" but the text does
    # not end there, so it is PROSE. Pre-fix, the greedy multi-line body ran
    # from the mention to the text-final ">>" and executed the prose as shell.
    exploit = (
        "Here is what I would run:\n<<job: make all>>\n"
        "But I did NOT run it. onwards -->>"
    )
    assert _appmod._parse_job_marker(exploit) == (exploit, None)
    assert _appmod._strip_job_marker(exploit) == exploit


def test_job_marker_multiline_first_line_with_append_is_prose():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    # The one documented limitation of the opener-line rule: a multi-line body
    # whose FIRST line contains ">>" is prose. Put ">>" appends on a
    # single-line marker or on later lines of a heredoc.
    text = "ok.\n<<job: echo a >> log\nmake>>"
    assert _appmod._parse_job_marker(text) == (text, None)
    assert _appmod._strip_job_marker(text) == text


def test_job_marker_heredoc_with_append_on_later_line_parses():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    # ">>" on a LATER line of a multi-line body is fine: only the opener line
    # must be ">>"-free for the body to span newlines.
    raw = "ok.\n<<job: sh -s <<EOF\necho a >> log\nEOF>>"
    clean, cmd = _appmod._parse_job_marker(raw)
    assert clean == "ok."
    assert cmd == "sh -s <<EOF\necho a >> log\nEOF"


def test_strip_job_marker_removes_complete_and_partial():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    assert _appmod._strip_job_marker("done.\n<<job: sleep 5>>") == "done."
    # A partial/unterminated trailing marker (mid-stream) is removed too; real
    # emission puts the marker at line start (the preamble's own-line rule).
    assert _appmod._strip_job_marker("almost\n<<job: sle") == "almost"
    assert _appmod._strip_job_marker("plain text") == "plain text"


def test_marker_order_job_first_then_files():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    # The worker's deterministic order: parse the JOB marker first, then the
    # FILES marker, so a reply ending `<<files: ...>>` then `<<job: ...>>`
    # (job line LAST) triggers both.
    raw = "All set.\n<<files: plot.png>>\n<<job: sleep 5>>"
    text, cmd = _appmod._parse_job_marker(raw)
    text, names = _appmod._parse_file_marker(text)
    assert cmd == "sleep 5"
    assert names == ["plot.png"]
    assert text == "All set."
    # The reverse text order: after the job parse (which matches nothing
    # trailing), the files marker is trailing and triggers; the job marker is
    # then mid-text prose (the trailing-only rule), so no job starts.
    raw = "All set.\n<<job: sleep 5>>\n<<files: plot.png>>"
    text, cmd = _appmod._parse_job_marker(raw)
    text, names = _appmod._parse_file_marker(text)
    assert cmd is None
    assert names == ["plot.png"]
    assert text == "All set.\n<<job: sleep 5>>"


def test_parse_job_marker_carries_append_redirect():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    # The greedy body carries a command containing ">>" (append redirect).
    clean, cmd = _appmod._parse_job_marker(
        "Logging in the background.\n<<job: echo x >> out.log>>"
    )
    assert clean == "Logging in the background."
    assert cmd == "echo x >> out.log"
    # A mid-text mention of a ">>"-carrying marker followed by prose is still
    # plain prose (the reply does not end with ">>", so nothing anchors).
    mid = "you could run <<job: echo x >> f>> but I will not."
    assert _appmod._parse_job_marker(mid) == (mid, None)


def test_parse_job_marker_command_ending_in_gt():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    # A reply ending ">>>": the final two ">" close the marker and the command
    # keeps its own trailing ">".
    clean, cmd = _appmod._parse_job_marker("run.\n<<job: sort a > b >>>")
    assert clean == "run."
    assert cmd == "sort a > b >"


def test_parse_job_marker_multiline_heredoc():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    # DOTALL: the command may span newlines, so heredoc-style commands work.
    raw = "ok.\n<<job: cat <<EOF > cfg.txt\nline1\nline2\nEOF>>"
    clean, cmd = _appmod._parse_job_marker(raw)
    assert clean == "ok."
    assert cmd == "cat <<EOF > cfg.txt\nline1\nline2\nEOF"


def test_strip_job_marker_scrubs_partial_with_append_redirect():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    # A partially streamed marker whose command contains ">>" (its closing ">>"
    # not yet arrived) is scrubbed; so is the complete ">>"-carrying marker.
    assert _appmod._strip_job_marker("done.\n<<job: echo x >> ou") == "done."
    assert _appmod._strip_job_marker("done.\n<<job: echo x >> out.log>>") == "done."


def test_marker_order_files_then_job_with_append_redirect():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    # Combined markers with a ">>"-carrying command: the job body may not cross
    # another marker opener, so the files marker before it survives the parse.
    raw = "All set.\n<<files: plot.png>>\n<<job: python sweep.py >> sweep.log>>"
    text, cmd = _appmod._parse_job_marker(raw)
    text, names = _appmod._parse_file_marker(text)
    assert cmd == "python sweep.py >> sweep.log"
    assert names == ["plot.png"]
    assert text == "All set."


def test_stream_updater_scrubs_partial_job_marker():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    updates = []

    class _Client:
        def chat_update(self, channel=None, ts=None, text=None):
            updates.append(text)
            return {"ok": True}

    upd = _appmod._make_stream_updater(_Client(), "C1", "ph-ts", now=lambda: 0.0)
    upd("working on it\n<<job: sle")
    assert updates == ["working on it"]


# ---------------------------------------------------------------------------
# Store (jobs.json: list-shaped sibling of sessions.json, via the facade)
# ---------------------------------------------------------------------------


def test_job_store_add_list_remove(tmp_path):
    jobs = str(tmp_path / "jobs.json")
    assert claude_runner.list_jobs(path=jobs) == []
    e = claude_runner.add_job(
        "aristotle",
        "C1",
        "T1",
        4242,
        "/wd/job-abc.log",
        "sleep 5",
        job_id="abc",
        path=jobs,
    )
    assert claude_runner.list_jobs(path=jobs) == [e]
    # started_ts is stamped at add time (epoch seconds, for the timeout window).
    assert isinstance(e.pop("started_ts"), float)
    assert e == {
        "id": "abc",
        "agent": "aristotle",
        "channel": "C1",
        "thread_ts": "T1",
        "pid": 4242,
        "logfile": "/wd/job-abc.log",
        "cmd": "sleep 5",
    }
    # Removing a non-existent id is a no-op (False); the real id removes (True).
    assert claude_runner.remove_job("nope", path=jobs) is False
    assert claude_runner.remove_job("abc", path=jobs) is True
    assert claude_runner.list_jobs(path=jobs) == []


def test_job_store_path_from_sessions_env(monkeypatch, tmp_path):
    # jobs.json is a sibling of the sessions path, so SESSIONS_PATH redirects it.
    monkeypatch.setenv("SESSIONS_PATH", str(tmp_path / "sessions.json"))
    assert claude_runner._jobs_path() == str(tmp_path / "jobs.json")


def test_job_store_corrupt_file_yields_empty(tmp_path):
    jobs = tmp_path / "jobs.json"
    jobs.write_text("{not json", encoding="utf-8")
    assert claude_runner.list_jobs(path=str(jobs)) == []


# ---------------------------------------------------------------------------
# Spawn (_start_job): detached Popen into the thread workdir + persisted entry
# ---------------------------------------------------------------------------


def test_start_job_spawns_detached_and_persists(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    monkeypatch.setenv("WORKDIR_BASE", str(tmp_path / "wd"))
    jobs_path = str(tmp_path / "jobs.json")
    monkeypatch.setattr(claude_runner, "_jobs_path", lambda: jobs_path)

    captured = {}

    class _FakeProc:
        pid = 4242

    def _fake_popen(
        argv, cwd=None, start_new_session=None, stdin=None, stdout=None, stderr=None
    ):
        captured["argv"] = argv
        captured["cwd"] = cwd
        captured["start_new_session"] = start_new_session
        captured["stdin"] = stdin
        captured["stderr"] = stderr
        captured["stdout_name"] = stdout.name
        return _FakeProc()

    monkeypatch.setattr(_appmod.subprocess, "Popen", _fake_popen)

    watched = {}
    evt = threading.Event()

    def _fake_watch(entry, client, proc=None, sleep=None):
        watched["entry"] = entry
        watched["proc"] = proc
        evt.set()

    monkeypatch.setattr(_appmod, "_watch_job", _fake_watch)

    client = object()
    entry = _appmod._start_job(
        client, _FILE_AGENT, "C1", "1700000000.000100", "sleep 5"
    )

    # Detached /bin/sh -c spawn, cwd'd to the thread workdir, log inside it.
    assert captured["argv"] == ["/bin/sh", "-c", "sleep 5"]
    assert captured["start_new_session"] is True
    assert captured["stdin"] is _appmod.subprocess.DEVNULL
    assert captured["stderr"] is _appmod.subprocess.STDOUT
    workdir = claude_runner.get_workdir("aristotle", "1700000000.000100")
    assert captured["cwd"] == workdir
    assert captured["stdout_name"] == entry["logfile"]
    assert entry["logfile"].startswith(workdir + os.sep)
    assert os.path.basename(entry["logfile"]) == f"job-{entry['id']}.log"
    # Entry persisted with the spawned pid + cmd, and the watcher armed on it.
    assert entry["pid"] == 4242
    assert entry["cmd"] == "sleep 5"
    assert claude_runner.list_jobs(path=jobs_path) == [entry]
    assert evt.wait(timeout=5)
    assert watched["entry"] == entry
    assert watched["proc"].pid == 4242


# ---------------------------------------------------------------------------
# Watcher (_watch_job): proc.wait this lifetime, pid poll after re-attach
# ---------------------------------------------------------------------------


def test_watch_job_waits_proc_then_runs_completion(monkeypatch):
    if not _HAVE_APP:
        return
    assert _appmod is not None

    class _Proc:
        returncode = 3

        def wait(self, timeout=None):
            self.waited = True
            return 3

    done = {}
    monkeypatch.setattr(
        _appmod,
        "_finish_job",
        lambda entry, client, code: done.update(entry=entry, code=code),
    )
    proc = _Proc()
    entry = {"id": "j1", "pid": 4242}
    _appmod._watch_job(entry, object(), proc=proc)
    assert proc.waited is True
    assert done["entry"] is entry
    assert done["code"] == 3


def test_watch_job_reattach_polls_pid_until_gone(monkeypatch):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    alive = iter([True, True, False])
    monkeypatch.setattr(_appmod, "_pid_alive", lambda pid: next(alive))
    done = {}
    monkeypatch.setattr(
        _appmod,
        "_finish_job",
        lambda entry, client, code: done.update(entry=entry, code=code),
    )
    sleeps = []
    entry = {"id": "j2", "pid": 4242}
    _appmod._watch_job(entry, object(), proc=None, sleep=sleeps.append)
    # Two "alive" polls -> two sleeps; the exit code is unknown after re-attach.
    assert sleeps == [_appmod._JOB_POLL_INTERVAL_S] * 2
    assert done["code"] is None


def test_watch_job_reattach_dead_pid_finishes_immediately(monkeypatch):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    monkeypatch.setattr(_appmod, "_pid_alive", lambda pid: False)
    done = {}
    monkeypatch.setattr(
        _appmod,
        "_finish_job",
        lambda entry, client, code: done.update(code=code, hit=True),
    )

    def _no_sleep(_s):
        raise AssertionError("a dead pid must complete with no sleep")

    _appmod._watch_job({"id": "j3", "pid": 4242}, object(), proc=None, sleep=_no_sleep)
    assert done == {"code": None, "hit": True}


# ---------------------------------------------------------------------------
# Completion (_finish_job): resume the thread via the run seam, or fall back
# to a plain note when the thread is busy / the agent is gone. jobs.json entry
# is dropped either way; posting translates the conversation key (flat DM).
# ---------------------------------------------------------------------------


def _job_entry(tmp_path, jobs_path, agent="aristotle", thread_ts="9000000000.000900"):
    log = tmp_path / "job-jx.log"
    log.write_text("line1\nline2\n", encoding="utf-8")
    return claude_runner.add_job(
        agent, "C9", thread_ts, 111, str(log), "sleep 1", job_id="jx", path=jobs_path
    )


def test_finish_job_synthesizes_agent_turn_via_run_seam(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    jobs_path = str(tmp_path / "jobs.json")
    monkeypatch.setattr(claude_runner, "_jobs_path", lambda: jobs_path)
    entry = _job_entry(tmp_path, jobs_path)

    captured = {}

    def _fake_run(
        client, channel, placeholder_ts, agent, prompt, thread_ts, token=None
    ):
        captured["run"] = {
            "channel": channel,
            "placeholder_ts": placeholder_ts,
            "agent_name": agent["name"],
            "prompt": prompt,
            "thread_ts": thread_ts,
            "token": token,
        }

    monkeypatch.setattr(_appmod, "_run_and_update", _fake_run)
    client = _FakeJobClient()
    _appmod._finish_job(entry, client, 0)

    # The entry is gone; the placeholder posted into the job's thread.
    assert claude_runner.list_jobs(path=jobs_path) == []
    assert client.posts[0]["channel"] == "C9"
    assert client.posts[0]["thread_ts"] == "9000000000.000900"
    run = captured["run"]
    assert run["channel"] == "C9"
    assert run["thread_ts"] == "9000000000.000900"
    assert run["placeholder_ts"] == "ph-ts"
    assert run["agent_name"] == "aristotle"
    # The completion prompt carries the exit code and the log tail; the token
    # holds the thread's busy slot (threaded into the run like a live message).
    assert run["prompt"].startswith(
        "[background job finished, exit code 0] output tail:"
    )
    assert "line2" in run["prompt"]
    assert run["token"] is not None


def test_finish_job_busy_thread_posts_plain_note(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    from src.slack import interrupt

    jobs_path = str(tmp_path / "jobs.json")
    monkeypatch.setattr(claude_runner, "_jobs_path", lambda: jobs_path)
    entry = _job_entry(tmp_path, jobs_path)

    def _no_run(*a, **k):
        raise AssertionError("a busy thread must not synthesize a run")

    monkeypatch.setattr(_appmod, "_run_and_update", _no_run)
    held = interrupt.try_register("aristotle", "9000000000.000900")
    assert held is not None
    try:
        client = _FakeJobClient()
        _appmod._finish_job(entry, client, 7)
        # NOT skipped silently: the raw note (exit code + tail) lands in-thread.
        assert len(client.posts) == 1
        post = client.posts[0]
        assert post["thread_ts"] == "9000000000.000900"
        assert "exit code 7" in post["text"]
        assert "line2" in post["text"]
        # The entry is dropped; the in-flight run's slot is untouched.
        assert claude_runner.list_jobs(path=jobs_path) == []
        assert interrupt.try_register("aristotle", "9000000000.000900") is None
    finally:
        interrupt.unregister("aristotle", "9000000000.000900", held)


def test_finish_job_unknown_agent_posts_plain_note(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    jobs_path = str(tmp_path / "jobs.json")
    monkeypatch.setattr(claude_runner, "_jobs_path", lambda: jobs_path)
    entry = _job_entry(tmp_path, jobs_path, agent="ghost")

    def _no_run(*a, **k):
        raise AssertionError("an unknown agent must not synthesize a run")

    monkeypatch.setattr(_appmod, "_run_and_update", _no_run)
    client = _FakeJobClient()
    _appmod._finish_job(entry, client, None)
    assert len(client.posts) == 1
    assert "exit code unknown" in client.posts[0]["text"]
    assert claude_runner.list_jobs(path=jobs_path) == []


def test_finish_job_flat_dm_key_posts_flat(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    jobs_path = str(tmp_path / "jobs.json")
    monkeypatch.setattr(claude_runner, "_jobs_path", lambda: jobs_path)
    # The conversation KEY is a DM channel id, not a ts: posts must go flat.
    entry = _job_entry(tmp_path, jobs_path, thread_ts="D0123ABCDEF")
    monkeypatch.setattr(_appmod, "_run_and_update", lambda *a, **k: None)
    client = _FakeJobClient()
    _appmod._finish_job(entry, client, 0)
    assert client.posts[0]["thread_ts"] is None


def test_finish_job_failed_delivery_keeps_entry(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    jobs_path = str(tmp_path / "jobs.json")
    monkeypatch.setattr(claude_runner, "_jobs_path", lambda: jobs_path)
    # Unknown agent -> the plain-note path; a raising client means NO delivery,
    # so the entry must survive for redelivery on the next restart.
    entry = _job_entry(tmp_path, jobs_path, agent="ghost")

    class _BoomClient:
        def chat_postMessage(self, **kwargs):
            raise RuntimeError("slack down")

    _appmod._finish_job(entry, _BoomClient(), 0)
    assert claude_runner.list_jobs(path=jobs_path) == [entry]


# ---------------------------------------------------------------------------
# Restart re-attach (_reattach_jobs)
# ---------------------------------------------------------------------------


def test_reattach_jobs_drops_unknown_watches_live_keeps_offline(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    jobs_path = str(tmp_path / "jobs.json")
    monkeypatch.setattr(claude_runner, "_jobs_path", lambda: jobs_path)
    ghost = claude_runner.add_job(
        "ghost", "C1", "T1", 1, "/l1", "c1", job_id="g1", path=jobs_path
    )
    live_job = claude_runner.add_job(
        "aristotle", "C2", "T2", 2, "/l2", "c2", job_id="a1", path=jobs_path
    )
    offline = claude_runner.add_job(
        "brunel", "C3", "T3", 3, "/l3", "c3", job_id="b1", path=jobs_path
    )

    class _Client:
        pass

    class _App:
        client = _Client()

    class _Handler:
        app = _App()

    live = {"aristotle": {"handler": _Handler()}}

    watched = []
    evt = threading.Event()

    def _fake_watch(entry, client, proc=None, sleep=None):
        watched.append({"entry": entry, "client": client, "proc": proc})
        evt.set()

    monkeypatch.setattr(_appmod, "_watch_job", _fake_watch)
    _appmod._reattach_jobs(live)

    # The unknown agent's entry is dropped; the live agent gets a watcher (no
    # proc handle after a restart); the offline-but-registered agent's entry is
    # left for a later restart, unwatched.
    assert evt.wait(timeout=5)
    assert len(watched) == 1
    assert watched[0]["entry"] == live_job
    assert watched[0]["client"] is _Handler.app.client
    assert watched[0]["proc"] is None
    remaining = {j["id"] for j in claude_runner.list_jobs(path=jobs_path)}
    assert remaining == {"a1", "b1"}
    assert ghost["id"] not in remaining
    assert offline["id"] in remaining


def test_reattach_watcher_reaches_finish_through_facade(monkeypatch, tmp_path):
    """The full Kind-B chain: _reattach_jobs -> facade._watch_job (REAL) ->
    facade._pid_alive (patched: dead) -> facade._finish_job (patched capture)."""
    if not _HAVE_APP:
        return
    assert _appmod is not None
    jobs_path = str(tmp_path / "jobs.json")
    monkeypatch.setattr(claude_runner, "_jobs_path", lambda: jobs_path)
    entry = claude_runner.add_job(
        "aristotle", "C2", "T2", 424242, "/l2", "c2", job_id="fa1", path=jobs_path
    )

    class _Client:
        pass

    class _App:
        client = _Client()

    class _Handler:
        app = _App()

    monkeypatch.setattr(_appmod, "_pid_alive", lambda pid: False)
    done = {}
    evt = threading.Event()

    def _fake_finish(e, client, code):
        done.update(entry=e, client=client, code=code)
        evt.set()

    monkeypatch.setattr(_appmod, "_finish_job", _fake_finish)
    _appmod._reattach_jobs({"aristotle": {"handler": _Handler()}})
    assert evt.wait(timeout=5)
    assert done["entry"] == entry
    assert done["client"] is _Handler.app.client
    assert done["code"] is None


# ---------------------------------------------------------------------------
# Worker integration: both trailing markers on one reply, and the no-marker
# default (nothing spawned). Mirrors the test_worker.py marker tests.
# ---------------------------------------------------------------------------


def test_run_and_update_job_marker_spawns_after_reply(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    sessions = str(tmp_path / "sessions.json")
    overrides = str(tmp_path / "overrides.json")
    monkeypatch.setattr(claude_runner, "_sessions_path", lambda: sessions)
    monkeypatch.setattr(claude_runner, "_overrides_path", lambda: overrides)
    monkeypatch.setenv("WORKDIR_BASE", str(tmp_path / "wd"))

    # Pre-create the named file in the thread's workdir so the files marker
    # resolves it alongside the job marker.
    workdir = claude_runner.get_workdir("aristotle", "T_job", create=True)
    with open(os.path.join(workdir, "plot.png"), "w", encoding="utf-8") as f:
        f.write("PNG")

    posted = {}
    uploads = []
    started = []
    captured = {}

    class _Runner:
        @staticmethod
        def answer(
            agent,
            prompt,
            prior,
            overrides=None,
            on_update=None,
            cancel=None,
            on_session=None,
        ):
            captured["prompt"] = prompt
            return (
                "Kicked off the sweep.\n<<files: plot.png>>\n<<job: sleep 5>>",
                "sid-1",
                {},
            )

    class _Client:
        def chat_update(self, channel=None, ts=None, text=None):
            posted["text"] = text
            return {"ok": True}

        def files_upload_v2(self, **kwargs):
            uploads.append(kwargs)
            return {"ok": True}

    monkeypatch.setattr(_appmod.runners, "get_runner", lambda backend: _Runner)
    monkeypatch.setattr(
        _appmod,
        "_start_job",
        lambda client, agent, channel, thread_ts, cmd: started.append(
            {
                "agent": agent["name"],
                "channel": channel,
                "thread_ts": thread_ts,
                "cmd": cmd,
            }
        ),
    )
    client = _Client()

    _appmod._run_and_update(client, "C1", "TS1", _FILE_AGENT, "run the sweep", "T_job")

    # The per-turn preamble documents the opt-in job marker.
    assert "<<job:" in captured["prompt"]
    # Both markers are gone from the shown reply; the prose remains.
    assert "<<job:" not in posted["text"]
    assert "<<files:" not in posted["text"]
    assert "Kicked off the sweep." in posted["text"]
    # The named file uploaded AND the job spawned with the thread's key.
    assert len(uploads) == 1
    assert started == [
        {"agent": "aristotle", "channel": "C1", "thread_ts": "T_job", "cmd": "sleep 5"}
    ]


def test_run_and_update_no_job_marker_starts_nothing(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    sessions = str(tmp_path / "sessions.json")
    overrides = str(tmp_path / "overrides.json")
    monkeypatch.setattr(claude_runner, "_sessions_path", lambda: sessions)
    monkeypatch.setattr(claude_runner, "_overrides_path", lambda: overrides)
    monkeypatch.setenv("WORKDIR_BASE", str(tmp_path / "wd"))

    class _Runner:
        @staticmethod
        def answer(
            agent,
            prompt,
            prior,
            overrides=None,
            on_update=None,
            cancel=None,
            on_session=None,
        ):
            return "just a normal answer", "sid-1", {}

    class _Client:
        def chat_update(self, channel=None, ts=None, text=None):
            return {"ok": True}

        def files_upload_v2(self, **kwargs):
            return {"ok": True}

    monkeypatch.setattr(_appmod.runners, "get_runner", lambda backend: _Runner)

    def _no_start(*a, **k):
        raise AssertionError("a plain reply must not start a job")

    monkeypatch.setattr(_appmod, "_start_job", _no_start)
    _appmod._run_and_update(_Client(), "C1", "TS1", _FILE_AGENT, "hi", "T_nojob")


def test_run_and_update_interrupted_run_spawns_no_job(monkeypatch, tmp_path):
    # A !stop'ed run that still returns a trailing job marker must NOT spawn the
    # job: the user asked to cancel the work.
    if not _HAVE_APP:
        return
    assert _appmod is not None
    sessions = str(tmp_path / "sessions.json")
    overrides = str(tmp_path / "overrides.json")
    monkeypatch.setattr(claude_runner, "_sessions_path", lambda: sessions)
    monkeypatch.setattr(claude_runner, "_overrides_path", lambda: overrides)
    monkeypatch.setenv("WORKDIR_BASE", str(tmp_path / "wd"))

    class _Runner:
        @staticmethod
        def answer(
            agent,
            prompt,
            prior,
            overrides=None,
            on_update=None,
            cancel=None,
            on_session=None,
        ):
            cancel.request()  # the user's !stop landed mid-run
            return "partial work\n<<job: sleep 999>>", "sid-1", {}

    class _Client:
        def chat_update(self, channel=None, ts=None, text=None):
            return {"ok": True}

        def files_upload_v2(self, **kwargs):
            return {"ok": True}

    monkeypatch.setattr(_appmod.runners, "get_runner", lambda backend: _Runner)

    def _no_start(*a, **k):
        raise AssertionError("an interrupted run must not start its job")

    monkeypatch.setattr(_appmod, "_start_job", _no_start)
    _appmod._run_and_update(_Client(), "C1", "TS1", _FILE_AGENT, "go", "T_stopjob")


# ---------------------------------------------------------------------------
# Control phrases: !job list / !job kill <id> (through _handle_control_phrase;
# agent-scoped, and kill never removes the entry: the watcher owns that)
# ---------------------------------------------------------------------------


def _redirect_jobs(monkeypatch, tmp_path):
    """Point the job store at a temp file; return its path."""
    jobs = str(tmp_path / "jobs.json")
    monkeypatch.setattr(claude_runner, "_jobs_path", lambda: jobs)
    return jobs


def _forbid_killpg(monkeypatch):
    monkeypatch.setattr(
        os, "killpg", lambda *a: (_ for _ in ()).throw(AssertionError("killpg called"))
    )


def test_control_phrase_job_list_scoped_and_truncated(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    jobs = _redirect_jobs(monkeypatch, tmp_path)
    long_cmd = "x" * (_appmod._JOB_LIST_CMD_CHARS + 20)
    claude_runner.add_job(
        "aristotle", "C1", "T1", 111, "/wd/job-aaa.log", long_cmd, "aaa", path=jobs
    )
    claude_runner.add_job(
        "aristotle", "C9", "T9", 119, "/wd/job-ccc.log", "make", "ccc", path=jobs
    )
    claude_runner.add_job(
        "brunel", "C2", "T2", 222, "/wd/job-bbb.log", "sleep 9", "bbb", path=jobs
    )
    say = _FakeSay()
    handled = _appmod._handle_control_phrase(_CONTROL_AGENT, "!job list", "T1", say)
    assert handled is True
    text = say.posts[0]["text"]
    # One line per job: id, conversation key, pid, command.
    assert "`aaa`" in text and "[T1]" in text and "pid 111" in text
    # The agent's jobs from OTHER conversations are listed too.
    assert "`ccc`" in text and "[T9]" in text and "pid 119" in text
    # A long command is ellipsized at _JOB_LIST_CMD_CHARS.
    assert "x" * _appmod._JOB_LIST_CMD_CHARS + "…" in text
    assert long_cmd not in text
    # Another agent's job is invisible.
    assert "bbb" not in text


def test_control_phrase_job_list_empty(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    _redirect_jobs(monkeypatch, tmp_path)
    say = _FakeSay()
    _appmod._handle_control_phrase(_CONTROL_AGENT, "!job list", "T1", say)
    assert "no background jobs" in say.posts[0]["text"]


def test_control_phrase_job_kill_signals_process_group(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    jobs = _redirect_jobs(monkeypatch, tmp_path)
    claude_runner.add_job(
        "aristotle", "C1", "T1", 4242, "/wd/job-aaa.log", "sleep 999", "aaa", path=jobs
    )
    calls = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    say = _FakeSay()
    _appmod._handle_control_phrase(_CONTROL_AGENT, "!job kill aaa", "T1", say)
    # The WHOLE process group is SIGTERMed (the job is a session/group leader).
    assert calls == [(4242, signal.SIGTERM)]
    assert "kill signaled" in say.posts[0]["text"]
    assert "aaa" in say.posts[0]["text"]
    # The entry is NOT removed: the watcher owns delivery + removal.
    assert len(claude_runner.list_jobs(path=jobs)) == 1


def test_control_phrase_job_kill_missing_id(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    _redirect_jobs(monkeypatch, tmp_path)
    _forbid_killpg(monkeypatch)
    say = _FakeSay()
    _appmod._handle_control_phrase(_CONTROL_AGENT, "!job kill zzz", "T1", say)
    assert "no such job" in say.posts[0]["text"]
    assert "zzz" in say.posts[0]["text"]


def test_control_phrase_job_kill_other_agents_job(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    jobs = _redirect_jobs(monkeypatch, tmp_path)
    claude_runner.add_job(
        "brunel", "C2", "T2", 222, "/wd/job-bbb.log", "sleep 9", "bbb", path=jobs
    )
    _forbid_killpg(monkeypatch)
    say = _FakeSay()
    # A matching id owned by ANOTHER agent reads as "no such job" (isolation).
    _appmod._handle_control_phrase(_CONTROL_AGENT, "!job kill bbb", "T1", say)
    assert "no such job" in say.posts[0]["text"]
    assert len(claude_runner.list_jobs(path=jobs)) == 1


def test_control_phrase_job_kill_already_dead_group(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    jobs = _redirect_jobs(monkeypatch, tmp_path)
    claude_runner.add_job(
        "aristotle", "C1", "T1", 4242, "/wd/job-aaa.log", "sleep 1", "aaa", path=jobs
    )

    def _gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", _gone)
    say = _FakeSay()
    _appmod._handle_control_phrase(_CONTROL_AGENT, "!job kill aaa", "T1", say)
    # Already-finished: say so; the watcher's normal completion flow delivers.
    assert "already finished" in say.posts[0]["text"]
    assert len(claude_runner.list_jobs(path=jobs)) == 1


def test_control_phrase_job_kill_permission_denied_group(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    jobs = _redirect_jobs(monkeypatch, tmp_path)
    claude_runner.add_job(
        "aristotle", "C1", "T1", 4242, "/wd/job-aaa.log", "sleep 1", "aaa", path=jobs
    )

    def _foreign(pid, sig):
        # A recycled pid now owned by another uid: killpg raises PermissionError.
        raise PermissionError

    monkeypatch.setattr(os, "killpg", _foreign)
    say = _FakeSay()
    handled = _appmod._handle_control_phrase(_CONTROL_AGENT, "!job kill aaa", "T1", say)
    # Acked (never escapes to the Bolt listener); same wording as the gone group.
    assert handled is True
    assert "already finished" in say.posts[0]["text"]
    assert len(claude_runner.list_jobs(path=jobs)) == 1


def test_control_phrase_job_bad_usage(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    _redirect_jobs(monkeypatch, tmp_path)
    _forbid_killpg(monkeypatch)
    for phrase in ("!job", "!job frobnicate", "!job kill"):
        say = _FakeSay()
        handled = _appmod._handle_control_phrase(_CONTROL_AGENT, phrase, "T1", say)
        assert handled is True
        assert "usage" in say.posts[0]["text"].lower()


# ---------------------------------------------------------------------------
# Timeout guardrail (AGENT_TIMEOUT_MIN, the one shared knob): the watcher
# enforces the window from the
# entry's started_ts on BOTH paths (proc.wait this lifetime, pid poll after a
# re-attach): SIGTERM the group, _JOB_KILL_GRACE_S grace, SIGKILL if still
# alive, then the NORMAL completion flow with the timed-out label.
# ---------------------------------------------------------------------------


class _TimeoutProc:
    """A fake Popen whose bounded wait(timeout=...) raises TimeoutExpired the
    first `hangs` times (still running); a bare wait() always returns."""

    def __init__(self, hangs, returncode=-15):
        self.hangs = hangs
        self.returncode = returncode
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if timeout is not None and self.hangs > 0:
            self.hangs -= 1
            raise subprocess.TimeoutExpired("sh", timeout)


def _capture_killpg(monkeypatch):
    calls = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    return calls


def _capture_finish(monkeypatch):
    done = {}

    def _fake_finish(entry, client, code, timed_out_min=None):
        done.update(entry=entry, code=code, timed_out_min=timed_out_min)

    monkeypatch.setattr(_appmod, "_finish_job", _fake_finish)
    return done


def test_watch_job_timeout_sigterm_then_sigkill(monkeypatch):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    monkeypatch.setenv("AGENT_TIMEOUT_MIN", "10")
    kills = _capture_killpg(monkeypatch)
    done = _capture_finish(monkeypatch)
    # Still hung after the grace wait too -> escalate to SIGKILL.
    proc = _TimeoutProc(hangs=2, returncode=-9)
    entry = {"id": "jt1", "pid": 4242, "started_ts": 1000.0}
    _appmod._watch_job(entry, object(), proc=proc, now=lambda: 1601.0)
    assert kills == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    # Bounded waits: the remaining window (already expired -> 0), then the
    # grace, then the unbounded reap after the SIGKILL.
    assert proc.waits == [0.0, _appmod._JOB_KILL_GRACE_S, None]
    assert done["code"] == -9
    assert done["timed_out_min"] == 10


def test_watch_job_timeout_sigterm_suffices_within_grace(monkeypatch):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    monkeypatch.setenv("AGENT_TIMEOUT_MIN", "10")
    kills = _capture_killpg(monkeypatch)
    done = _capture_finish(monkeypatch)
    # The grace wait returns: the job died on SIGTERM, no SIGKILL.
    proc = _TimeoutProc(hangs=1, returncode=-15)
    entry = {"id": "jt2", "pid": 4242, "started_ts": 1000.0}
    _appmod._watch_job(entry, object(), proc=proc, now=lambda: 1601.0)
    assert kills == [(4242, signal.SIGTERM)]
    assert proc.waits == [0.0, _appmod._JOB_KILL_GRACE_S]
    assert done["code"] == -15
    assert done["timed_out_min"] == 10


def test_watch_job_timeout_zero_disables(monkeypatch):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    monkeypatch.setenv("AGENT_TIMEOUT_MIN", "0")
    _forbid_killpg(monkeypatch)
    done = _capture_finish(monkeypatch)

    class _Proc:
        returncode = 0

        def wait(self, timeout=None):
            # Disabled timeout -> the unbounded legacy wait, however long ago
            # the job started.
            assert timeout is None

    entry = {"id": "jt3", "pid": 4242, "started_ts": 0.0}
    _appmod._watch_job(entry, object(), proc=_Proc())
    assert done["code"] == 0
    assert done["timed_out_min"] is None


def test_watch_job_reattach_enforces_remaining_window(monkeypatch):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    monkeypatch.setenv("AGENT_TIMEOUT_MIN", "10")
    kills = _capture_killpg(monkeypatch)
    done = _capture_finish(monkeypatch)
    # The pid stays alive through the SIGTERM+grace -> SIGKILL.
    monkeypatch.setattr(_appmod, "_pid_alive", lambda pid: True)
    # Window is 1000..1600: the first poll (1100) is inside the REMAINING
    # window (no fresh full window from the re-attach), the second (1601) is
    # past it and kills.
    times = iter([1100.0, 1601.0])
    sleeps = []
    entry = {"id": "jt5", "pid": 4242, "started_ts": 1000.0}
    _appmod._watch_job(
        entry, object(), proc=None, sleep=sleeps.append, now=lambda: next(times)
    )
    assert sleeps == [_appmod._JOB_POLL_INTERVAL_S, _appmod._JOB_KILL_GRACE_S]
    assert kills == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert done["code"] is None
    assert done["timed_out_min"] == 10


def test_watch_job_reattach_already_expired_killed_on_first_poll(monkeypatch):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    monkeypatch.setenv("AGENT_TIMEOUT_MIN", "10")
    kills = _capture_killpg(monkeypatch)
    done = _capture_finish(monkeypatch)
    # Alive on the first poll; gone after the SIGTERM+grace (no SIGKILL).
    alive = iter([True, False])
    monkeypatch.setattr(_appmod, "_pid_alive", lambda pid: next(alive))
    sleeps = []
    entry = {"id": "jt6", "pid": 4242, "started_ts": 100.0}
    _appmod._watch_job(
        entry, object(), proc=None, sleep=sleeps.append, now=lambda: 10_000.0
    )
    # Killed on the FIRST poll: only the grace sleep, no poll-interval sleep.
    assert sleeps == [_appmod._JOB_KILL_GRACE_S]
    assert kills == [(4242, signal.SIGTERM)]
    assert done["code"] is None
    assert done["timed_out_min"] == 10


def test_watch_job_missing_started_ts_treated_as_started_now(monkeypatch):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    monkeypatch.setenv("AGENT_TIMEOUT_MIN", "10")
    _forbid_killpg(monkeypatch)
    done = _capture_finish(monkeypatch)
    alive = iter([True, False])
    monkeypatch.setattr(_appmod, "_pid_alive", lambda pid: next(alive))
    # A pre-timeout entry (no started_ts) starts its window NOW (5000), so the
    # next poll (5001) is nowhere near the 5600 deadline: no retro-kill.
    times = iter([5000.0, 5001.0])
    sleeps = []
    entry = {"id": "jt7", "pid": 4242}
    _appmod._watch_job(
        entry, object(), proc=None, sleep=sleeps.append, now=lambda: next(times)
    )
    assert sleeps == [_appmod._JOB_POLL_INTERVAL_S]
    assert done["code"] is None
    assert done["timed_out_min"] is None


def test_watch_job_started_ts_zero_is_respected(monkeypatch):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    # A stored started_ts of 0 is a real epoch value, NOT "missing": the
    # window runs from 0 (long expired), so the job is killed on the first
    # poll instead of reading as started-now (the old falsy-or bug).
    monkeypatch.setenv("AGENT_TIMEOUT_MIN", "10")
    kills = _capture_killpg(monkeypatch)
    done = _capture_finish(monkeypatch)
    alive = iter([True, False])
    monkeypatch.setattr(_appmod, "_pid_alive", lambda pid: next(alive))
    entry = {"id": "jt9", "pid": 4242, "started_ts": 0}
    _appmod._watch_job(
        entry, object(), proc=None, sleep=lambda s: None, now=lambda: 10_000.0
    )
    assert kills == [(4242, signal.SIGTERM)]
    assert done["code"] is None
    assert done["timed_out_min"] == 10


def test_watch_job_malformed_timeout_falls_back_with_warning(monkeypatch, caplog):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    monkeypatch.setenv("AGENT_TIMEOUT_MIN", "48h")
    kills = _capture_killpg(monkeypatch)
    done = _capture_finish(monkeypatch)
    alive = iter([True, False])
    monkeypatch.setattr(_appmod, "_pid_alive", lambda pid: next(alive))
    # Started 10 min past the DEFAULT window the malformed value falls back to
    # (a naive parse-to-0 would have disabled the timeout instead of expiring).
    entry = {"id": "jt8", "pid": 4242, "started_ts": 60.0}
    with caplog.at_level(logging.WARNING):
        _appmod._watch_job(
            entry,
            object(),
            proc=None,
            sleep=lambda s: None,
            now=lambda: 60.0 + (common.AGENT_TIMEOUT_DEFAULT_MIN + 10) * 60.0,
        )
    assert "AGENT_TIMEOUT_MIN" in caplog.text
    assert kills == [(4242, signal.SIGTERM)]
    assert done["timed_out_min"] == common.AGENT_TIMEOUT_DEFAULT_MIN


def test_finish_job_timed_out_note_carries_label_and_tail(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    jobs_path = str(tmp_path / "jobs.json")
    monkeypatch.setattr(claude_runner, "_jobs_path", lambda: jobs_path)
    # Unknown agent -> the plain-note path shows the delivered text directly.
    entry = _job_entry(tmp_path, jobs_path, agent="ghost")
    client = _FakeJobClient()
    _appmod._finish_job(entry, client, -9, timed_out_min=10)
    text = client.posts[0]["text"]
    assert "timed out after 10 min" in text
    assert "exit code -9" in text
    assert "line2" in text  # the log tail is still included
    assert claude_runner.list_jobs(path=jobs_path) == []


# ---------------------------------------------------------------------------
# Concurrency guardrail (JOB_MAX_CONCURRENT): GLOBAL across agents; the
# count-check + append are ONE critical section in store.add_job, and an
# at-limit spawn is DECLINED (proc killed, no entry, no watcher, note posted).
# ---------------------------------------------------------------------------


def test_add_job_limit_counts_and_appends_atomically(tmp_path):
    jobs = str(tmp_path / "jobs.json")
    # Under the limit: appended. At the limit: declined in the SAME critical
    # section that counted (two back-to-back calls at limit-1 with limit 1:
    # the second must see the first's append and decline).
    e1 = claude_runner.add_job(
        "aristotle", "C1", "T1", 1, "/l1", "c1", job_id="j1", path=jobs, limit=1
    )
    assert e1 is not None
    e2 = claude_runner.add_job(
        "brunel", "C2", "T2", 2, "/l2", "c2", job_id="j2", path=jobs, limit=1
    )
    assert e2 is None
    assert [j["id"] for j in claude_runner.list_jobs(path=jobs)] == ["j1"]
    # No limit (disabled): appends regardless of the count.
    e3 = claude_runner.add_job(
        "cicero", "C3", "T3", 3, "/l3", "c3", job_id="j3", path=jobs, limit=None
    )
    assert e3 is not None
    assert [j["id"] for j in claude_runner.list_jobs(path=jobs)] == ["j1", "j3"]


def _spawn_fakes(monkeypatch, tmp_path, watch=None):
    """Common _start_job scaffolding: temp stores, a fake Popen (records wait),
    a killpg capture (a decline kills the whole GROUP, never proc.kill), and a
    fake watcher. Returns (jobs_path, killed, waits)."""
    monkeypatch.setenv("WORKDIR_BASE", str(tmp_path / "wd"))
    jobs_path = str(tmp_path / "jobs.json")
    monkeypatch.setattr(claude_runner, "_jobs_path", lambda: jobs_path)
    killed = []
    waits = []

    class _FakeProc:
        pid = 4242

        def kill(self):
            raise AssertionError("group kill required, not proc.kill")

        def wait(self, timeout=None):
            waits.append(timeout)

    monkeypatch.setattr(_appmod.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    if watch is None:
        watch = lambda entry, client, proc=None, sleep=None: None  # noqa: E731
    monkeypatch.setattr(_appmod, "_watch_job", watch)
    return jobs_path, killed, waits


def test_start_job_at_limit_declines_with_note(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None

    def _no_watch(*a, **k):
        raise AssertionError("a declined spawn must not arm a watcher")

    jobs_path, killed, waits = _spawn_fakes(monkeypatch, tmp_path, watch=_no_watch)
    monkeypatch.setenv("JOB_MAX_CONCURRENT", "1")
    # The limit is GLOBAL: ANOTHER agent's running job fills the one slot.
    claude_runner.add_job(
        "brunel", "C0", "T0", 999, "/l0", "c0", job_id="e1", path=jobs_path
    )
    posts = []

    class _Client:
        def chat_postMessage(self, channel=None, thread_ts=None, text=None):
            posts.append({"channel": channel, "thread_ts": thread_ts, "text": text})
            return {"ts": "x"}

    entry = _appmod._start_job(
        _Client(), _FILE_AGENT, "C1", "1700000000.000100", "sleep 5"
    )
    # Declined, not queued: no entry persisted, and the fresh process GROUP is
    # SIGKILLed (a forked child must not survive as an orphan) then reaped.
    assert entry is None
    assert killed == [(4242, signal.SIGKILL)]
    assert waits == [None]
    assert [j["id"] for j in claude_runner.list_jobs(path=jobs_path)] == ["e1"]
    # The note lands in the reply thread and points at the escape hatches.
    assert len(posts) == 1
    assert posts[0]["channel"] == "C1"
    assert posts[0]["thread_ts"] == "1700000000.000100"
    assert "job not started" in posts[0]["text"]
    assert "1 background job already running (limit 1)" in posts[0]["text"]
    assert "!job list" in posts[0]["text"]
    assert "!job kill" in posts[0]["text"]


def test_start_job_under_limit_proceeds(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    jobs_path, killed, _waits = _spawn_fakes(monkeypatch, tmp_path)
    monkeypatch.setenv("JOB_MAX_CONCURRENT", "2")
    claude_runner.add_job(
        "brunel", "C0", "T0", 999, "/l0", "c0", job_id="e1", path=jobs_path
    )
    entry = _appmod._start_job(
        object(), _FILE_AGENT, "C1", "1700000000.000100", "sleep 5"
    )
    assert entry is not None
    assert killed == []
    assert len(claude_runner.list_jobs(path=jobs_path)) == 2


def test_start_job_limit_zero_disables(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    jobs_path, killed, _waits = _spawn_fakes(monkeypatch, tmp_path)
    monkeypatch.setenv("JOB_MAX_CONCURRENT", "0")
    # More entries than the code DEFAULT (4): 0 must mean OFF, not the default.
    for i in range(_appmod._JOB_MAX_CONCURRENT_DEFAULT):
        claude_runner.add_job(
            "brunel", "C0", "T0", 900 + i, f"/l{i}", "c", job_id=f"e{i}", path=jobs_path
        )
    entry = _appmod._start_job(
        object(), _FILE_AGENT, "C1", "1700000000.000100", "sleep 5"
    )
    assert entry is not None
    assert killed == []
    assert (
        len(claude_runner.list_jobs(path=jobs_path))
        == _appmod._JOB_MAX_CONCURRENT_DEFAULT + 1
    )
