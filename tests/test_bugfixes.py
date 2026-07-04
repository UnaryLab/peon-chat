"""Regression tests for the runner/store robustness fixes.

Covers: the streaming stderr-pipe deadlock (concurrent drain), the pre-spawn
!stop race (Interrupt.arm), the orphaned-stdout wedge (close + graceful settle),
atomic store writes, the dead-session heal vs stderr truncation, non-UTF8 CLI
output, and malformed timeout env vars. Fakes used only here live here (shared
fakes stay in tests/helpers.py, which this file only imports).
"""

import io
import json
import os
import signal
import sys
import threading
from unittest import mock

import pytest

from src.runners import claude_runner, codex_runner, common
from src.store import base as store_base
from src.store import crons as store_crons

from tests.helpers import (
    SID,
    PROMPT,
    BRUNEL,
    CICERO,
    DIJKSTRA,
    THREAD_ID,
    _fake_proc,
    _fake_popen_factory,
)


# ---------------------------------------------------------------------------
# Local fakes (single-file: not shared, so they live here per the test layout)
# ---------------------------------------------------------------------------


class _SignalProc:
    """Duck-typed Popen recording delivered signals (mirrors test_interrupt's)."""

    def __init__(self, alive=True):
        self.alive = alive
        self.signals = []
        self.stdout = None

    def poll(self):
        return None if self.alive else 0

    def send_signal(self, sig):
        self.signals.append(sig)


class _ClosableStdout:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _WedgedStdout:
    """Yields the given lines, then raises like a file closed under the reader."""

    def __init__(self, lines):
        self._lines = list(lines)

    def __iter__(self):
        return self

    def __next__(self):
        if self._lines:
            return self._lines.pop(0) + "\n"
        raise ValueError("I/O operation on closed file")


class _WedgedPopen:
    """Fake Popen whose CLI has exited but whose stdout read blows up mid-loop
    (the orphaned-write-fd wedge, surfaced as ValueError after a !stop close)."""

    def __init__(self, lines, returncode=0):
        self.stdout = _WedgedStdout(lines)
        self.stderr = io.StringIO("")
        self.returncode = returncode

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode  # already exited: the wedge scenario

    def kill(self):
        pass


def _delta_line(text):
    return json.dumps(
        {
            "type": "stream_event",
            "session_id": SID,
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": text},
            },
        }
    )


def _watchdog(cancel, timeout=30):
    """Kill the armed proc after `timeout`s so a regression FAILS instead of
    hanging pytest forever (the killed child makes the runner raise)."""
    timer = threading.Timer(
        timeout, lambda: cancel.proc.kill() if cancel.proc is not None else None
    )
    timer.daemon = True
    timer.start()
    return timer


# 256KB: comfortably above any OS pipe buffer (~64KB), so a CLI writing this to
# a PIPE'd stderr blocks unless the parent drains it concurrently.
_BIG_STDERR = 262144


# ---------------------------------------------------------------------------
# Bug 1: stderr pipe deadlock (streaming paths must drain stderr concurrently)
# ---------------------------------------------------------------------------


def test_claude_streaming_drains_large_stderr_no_deadlock():
    # REAL child process: writes 256KB to stderr BEFORE emitting any stdout. With
    # only a post-exit stderr read the child blocks on the stderr write, never
    # prints the result, and the readline loop deadlocks; the concurrent drain
    # keeps it moving. (Precedent for spawning python: tests/test_env.py.)
    result_line = json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "result": "drained"}
    )
    script = (
        "import sys\n"
        f"sys.stderr.write('e' * {_BIG_STDERR})\n"
        "sys.stderr.flush()\n"
        f"print({result_line!r})\n"
    )
    cancel = common.Interrupt()
    timer = _watchdog(cancel)
    try:
        reply, _meta = claude_runner._run_claude_streaming(
            BRUNEL, [sys.executable, "-c", script], 60, None, None, cancel=cancel
        )
    finally:
        timer.cancel()
    assert reply == "drained"


def test_codex_streaming_drains_large_stderr_no_deadlock():
    started_line = json.dumps({"type": "thread.started", "thread_id": THREAD_ID})
    script = (
        "import sys\n"
        f"sys.stderr.write('e' * {_BIG_STDERR})\n"
        "sys.stderr.flush()\n"
        f"print({started_line!r})\n"
    )
    cancel = common.Interrupt()
    timer = _watchdog(cancel)
    try:
        stdout = codex_runner._run_codex_streaming(
            [sys.executable, "-c", script], 60, None, cancel=cancel
        )
    finally:
        timer.cancel()
    assert THREAD_ID in stdout


def test_claude_streaming_drained_stderr_reaches_error(monkeypatch):
    # Nonzero exit: the DRAINED stderr text (not a post-exit read) feeds the
    # error message, and the raw text rides on the error for marker checks.
    monkeypatch.setenv("STREAM_OUTPUT", "1")
    with mock.patch(
        "src.runners.claude_runner.subprocess.Popen",
        side_effect=_fake_popen_factory([], returncode=1, stderr="kaboom-stderr"),
    ):
        with pytest.raises(claude_runner.ClaudeRunError) as ei:
            claude_runner.run_claude(CICERO, PROMPT, SID, True)
    assert "kaboom-stderr" in str(ei.value)
    assert ei.value.stderr == "kaboom-stderr"


def test_codex_streaming_drained_stderr_reaches_error(monkeypatch):
    monkeypatch.setenv("STREAM_OUTPUT", "1")
    with mock.patch(
        "src.runners.codex_runner.subprocess.Popen",
        side_effect=_fake_popen_factory([], returncode=2, stderr="codex-boom"),
    ):
        with pytest.raises(codex_runner.CodexRunError) as ei:
            codex_runner.run_codex(DIJKSTRA, PROMPT, None, True)
    assert "codex-boom" in str(ei.value)


# ---------------------------------------------------------------------------
# Bug 2: a !stop landing BEFORE the spawn must be delivered at arm() time
# ---------------------------------------------------------------------------


def test_interrupt_request_before_arm_delivers_sigint_at_arm():
    tok = common.Interrupt()
    tok.request()  # the !stop lands while the runner is still spawning
    assert tok.requested is True
    proc = _SignalProc(alive=True)
    tok.arm(proc)
    assert tok.proc is proc  # attribute stays readable (handlers read token.proc)
    assert proc.signals == [signal.SIGINT]  # delivered the moment the proc arrived


def test_interrupt_arm_then_request_normal_order_unchanged():
    tok = common.Interrupt()
    proc = _SignalProc(alive=True)
    tok.arm(proc)
    assert proc.signals == []  # arming alone never signals
    tok.request()
    assert proc.signals == [signal.SIGINT]


# ---------------------------------------------------------------------------
# Bug 3: orphaned-stdout wedge (exited proc, reader stuck) -> close + settle
# ---------------------------------------------------------------------------


def test_interrupt_request_closes_stdout_of_exited_proc():
    tok = common.Interrupt()
    proc = _SignalProc(alive=False)
    proc.stdout = _ClosableStdout()
    tok.arm(proc)
    tok.request()
    assert proc.signals == []  # already exited: no SIGINT
    assert proc.stdout.closed is True  # the wedged reader is unblocked instead


def test_claude_streaming_settles_partial_on_closed_stdout_when_cancelled(monkeypatch):
    monkeypatch.setenv("STREAM_OUTPUT", "1")
    cancel = common.Interrupt()
    cancel.request()
    with mock.patch(
        "src.runners.claude_runner.subprocess.Popen",
        side_effect=lambda *a, **k: _WedgedPopen([_delta_line("half a rep")]),
    ):
        reply, _meta = claude_runner.run_claude(
            BRUNEL, PROMPT, SID, True, cancel=cancel
        )
    assert reply == "half a rep"  # the partial reply, exactly like the SIGINT settle


def test_claude_streaming_closed_stdout_without_cancel_propagates(monkeypatch):
    monkeypatch.setenv("STREAM_OUTPUT", "1")
    with mock.patch(
        "src.runners.claude_runner.subprocess.Popen",
        side_effect=lambda *a, **k: _WedgedPopen([]),
    ):
        with pytest.raises(ValueError):
            claude_runner.run_claude(BRUNEL, PROMPT, SID, True)


def test_codex_streaming_settles_partial_on_closed_stdout_when_cancelled(monkeypatch):
    monkeypatch.setenv("STREAM_OUTPUT", "1")
    started_line = json.dumps({"type": "thread.started", "thread_id": THREAD_ID})
    cancel = common.Interrupt()
    cancel.request()
    with mock.patch(
        "src.runners.codex_runner.subprocess.Popen",
        side_effect=lambda *a, **k: _WedgedPopen([started_line]),
    ):
        stdout = codex_runner._run_codex_streaming(["codex"], 5, None, cancel=cancel)
    assert THREAD_ID in stdout  # partial stdout returned so the thread_id survives


# ---------------------------------------------------------------------------
# Bug 4: store writes are atomic (temp file + os.replace, no truncation window)
# ---------------------------------------------------------------------------


def test_dict_store_save_is_atomic_roundtrip(tmp_path):
    path = str(tmp_path / "sessions.json")
    store_base._save_dict_store({"brunel:T1": SID}, path)
    assert store_base._load_dict_store(path) == {"brunel:T1": SID}
    store_base._save_dict_store({"brunel:T1": SID, "cicero:T2": "x"}, path)  # overwrite
    assert store_base._load_dict_store(path) == {"brunel:T1": SID, "cicero:T2": "x"}
    assert os.listdir(tmp_path) == ["sessions.json"]  # no leftover temp file


def test_cron_store_save_is_atomic_roundtrip(tmp_path):
    path = str(tmp_path / "crons.json")
    entry = store_crons.add_cron("0 9 * * *", "brunel", "C1", "T1", "hi", path=path)
    store_crons.add_cron("0 10 * * *", "cicero", "C2", "T2", "yo", path=path)
    crons = store_crons.list_crons(path=path)
    assert [c["id"] for c in crons][0] == entry["id"]
    assert len(crons) == 2
    assert os.listdir(tmp_path) == ["crons.json"]  # no leftover temp file


# ---------------------------------------------------------------------------
# Bug 5: dead-session heal must see the FULL stderr, not the 1000-char cut
# ---------------------------------------------------------------------------


def test_dead_session_heal_survives_stderr_truncation():
    # >1000 chars of noise BEFORE the marker: format_process_failure truncates the
    # message detail, so str(exc) lacks the marker; the heal must still fire off
    # the raw stderr carried on the error and retry ONCE as a fresh session.
    dead = "dead-session-id"
    noisy = "E" * 1500 + f" No conversation found with session ID: {dead}"
    good = json.dumps({"result": "recovered", "is_error": False, "subtype": "success"})
    fail = _fake_proc(1, "", noisy)
    ok = _fake_proc(0, good)
    with mock.patch(
        "src.runners.claude_runner.subprocess.run", side_effect=[fail, ok]
    ) as m:
        reply, sid, _meta = claude_runner.answer(BRUNEL, PROMPT, dead)
    assert reply == "recovered"
    assert sid != dead
    second_argv = m.call_args_list[1][0][0]
    assert "--session-id" in second_argv and sid in second_argv  # fresh retry


# ---------------------------------------------------------------------------
# Bug 6: non-UTF8 CLI output must not escape as UnicodeDecodeError
# ---------------------------------------------------------------------------


def test_claude_streaming_tolerates_non_utf8_stdout():
    # REAL child emitting an invalid-UTF8 line before the result event: the text
    # decode replaces the bad bytes (the garbage line then fails JSON parse and is
    # skipped) instead of raising UnicodeDecodeError through the readline loop.
    result_line = json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "result": "decoded"}
    )
    script = (
        "import sys\n"
        "sys.stdout.buffer.write(b'\\xff\\xfeGARBAGE\\n')\n"
        f"sys.stdout.buffer.write({result_line!r}.encode() + b'\\n')\n"
        "sys.stdout.buffer.flush()\n"
    )
    reply, _meta = claude_runner._run_claude_streaming(
        BRUNEL, [sys.executable, "-c", script], 60, None, None
    )
    assert reply == "decoded"


def test_codex_streaming_tolerates_non_utf8_stdout():
    started_line = json.dumps({"type": "thread.started", "thread_id": THREAD_ID})
    script = (
        "import sys\n"
        "sys.stdout.buffer.write(b'\\xff\\xfeGARBAGE\\n')\n"
        f"sys.stdout.buffer.write({started_line!r}.encode() + b'\\n')\n"
        "sys.stdout.buffer.flush()\n"
    )
    stdout = codex_runner._run_codex_streaming([sys.executable, "-c", script], 60, None)
    assert THREAD_ID in stdout


def test_codex_reply_file_tolerates_non_utf8():
    # The -o last-message file holds a stray invalid byte: the read degrades to a
    # replacement char instead of raising through to the generic handler. Runs on
    # the legacy path (conftest pins STREAM_OUTPUT=0).
    stdout = json.dumps({"type": "thread.started", "thread_id": THREAD_ID})

    def _run(argv, **kwargs):
        out_path = argv[argv.index("-o") + 1]
        with open(out_path, "wb") as f:
            f.write(b"reply \xff end")
        proc = mock.Mock()
        proc.returncode = 0
        proc.stdout = stdout
        proc.stderr = ""
        return proc

    with mock.patch("src.runners.codex_runner.subprocess.run", side_effect=_run):
        reply, sid, _meta = codex_runner.run_codex(DIJKSTRA, PROMPT, None, True)
    assert sid == THREAD_ID
    assert reply == "reply \ufffd end"


# ---------------------------------------------------------------------------
# Bug 7: malformed *_TIMEOUT_MIN env values must not kill the process at import
# ---------------------------------------------------------------------------


def test_int_env_tolerates_malformed_values(monkeypatch):
    monkeypatch.setenv("PEON_TEST_TIMEOUT", "90m")  # malformed -> default + warning
    assert common._int_env("PEON_TEST_TIMEOUT", 2880) == 2880
    monkeypatch.setenv("PEON_TEST_TIMEOUT", "120")
    assert common._int_env("PEON_TEST_TIMEOUT", 2880) == 120
    monkeypatch.setenv("PEON_TEST_TIMEOUT", "")
    assert common._int_env("PEON_TEST_TIMEOUT", 2880) == 2880
    monkeypatch.delenv("PEON_TEST_TIMEOUT", raising=False)
    assert common._int_env("PEON_TEST_TIMEOUT", 2880) == 2880
