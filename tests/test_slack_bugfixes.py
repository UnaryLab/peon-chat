"""Regression tests for the Slack-layer bug fixes.

One test (or pair) per fix: per-agent dedup, directed-mention ownership, the
cron busy-guard/threaded-fire/boundary-sleep trio, 4k truncation, the
file_share subtype, the download timeout, multi-line control phrases, the
transcript tail, Vixie N/S steps, the trailing-only <<files:>> marker, the
group-DM manifest entries, duplicate registry names, and the bot-id cache.
"""

import json
import threading
import uuid
from datetime import datetime

import pytest

from src import agents, manifest
from src.runners import claude_runner

from tests.helpers import (
    _CONTROL_AGENT,
    _FILE_AGENT,
    _HANDLE_AGENT,
    _HAVE_APP,
    _FakeSay,
    _appmod,
    _built_app,
)


# --- 1. dedup is keyed per agent, not globally per Slack message id ---------


def test_dedup_is_per_agent_not_global():
    # One message mentioning TWO agents is delivered (with the SAME event id) to
    # both agents' apps; each must answer. The same agent seeing the same event
    # twice (app_mention + message.*) still answers once.
    if not _HAVE_APP:
        return
    assert _appmod is not None
    from src.slack import handlers

    agent_a = dict(_HANDLE_AGENT)
    agent_b = {**_HANDLE_AGENT, "name": "brunel", "display_name": "Brunel"}
    # An empty prompt short-circuits into a say() before any store/runner work,
    # so the answer count is just the number of posts.
    event = {
        "channel": "C1",
        "ts": "T_dedup",
        "text": "",
        "client_msg_id": f"MSG-{uuid.uuid4()}",
    }
    say_a, say_b = _FakeSay(), _FakeSay()
    handlers._handle(agent_a, event, object(), say_a)
    handlers._handle(agent_b, event, object(), say_b)
    assert len(say_a.posts) == 1  # first agent answered
    assert len(say_b.posts) == 1  # second agent answered too (per-agent key)
    handlers._handle(agent_a, event, object(), say_a)
    assert len(say_a.posts) == 1  # same agent + same event id -> dedup'd


# --- 2. + 13. on_message mention ownership + bot-id cache ---------------------


def test_on_message_skips_follow_up_directed_at_another_bot(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    from src import store

    monkeypatch.setenv("SESSIONS_PATH", str(tmp_path / "sessions.json"))
    calls = []
    bolt_app = _built_app(monkeypatch, calls)
    store.set_session("brunel", "T1", "sid-brunel")  # we own this thread

    def _event(text):
        return {"channel": "C1", "thread_ts": "T1", "ts": "T2", "text": text}

    # Directed at ANOTHER bot (leading mention): their app_mention owns it.
    bolt_app.events["message"](_event("<@UOTHER> what do you think?"), None, None)
    assert calls == []
    # A mid-text mention is still an ordinary follow-up for us.
    bolt_app.events["message"](_event("ask <@UALICE> about X"), None, None)
    assert calls == ["ask <@UALICE> about X"]


def test_on_message_keeps_claiming_when_bot_id_unknown(monkeypatch, tmp_path):
    # bot_user_id lookup failing (None) keeps the OLD behavior: no ownership
    # check, the sessioned follow-up is still claimed.
    if not _HAVE_APP:
        return
    from src import store

    monkeypatch.setenv("SESSIONS_PATH", str(tmp_path / "sessions.json"))
    calls = []
    bolt_app = _built_app(monkeypatch, calls)
    bolt_app.client.auth_test.side_effect = RuntimeError("slack down")
    store.set_session("brunel", "T1", "sid-brunel")

    event = {
        "channel": "C1",
        "thread_ts": "T1",
        "ts": "T2",
        "text": "<@UOTHER> thoughts?",
    }
    bolt_app.events["message"](event, None, None)
    assert calls == ["<@UOTHER> thoughts?"]


def test_bot_user_id_failure_is_not_cached(monkeypatch, tmp_path):
    # A failed auth_test must NOT be cached as None: the next event retries and
    # the mention-ownership check comes back to life.
    if not _HAVE_APP:
        return
    from src import store

    monkeypatch.setenv("SESSIONS_PATH", str(tmp_path / "sessions.json"))
    calls = []
    bolt_app = _built_app(monkeypatch, calls)
    bolt_app.client.auth_test.side_effect = [
        RuntimeError("transient"),
        {"user_id": "UBRUNEL"},
    ]
    store.set_session("brunel", "T1", "sid-brunel")

    # Our own mention in-thread: owned by app_mention, so on_message must skip
    # it -- but only once the bot id resolves.
    event = {
        "channel": "C1",
        "thread_ts": "T1",
        "ts": "T2",
        "text": "<@UBRUNEL> hi again",
    }
    bolt_app.events["message"](event, None, None)  # lookup fails -> claimed
    assert calls == ["<@UBRUNEL> hi again"]
    bolt_app.events["message"](event, None, None)  # retried lookup -> skipped
    assert calls == ["<@UBRUNEL> hi again"]
    assert bolt_app.client.auth_test.call_count == 2


# --- 3a. the cron path respects the busy guard --------------------------------


def test_cron_path_declines_when_thread_busy(monkeypatch):
    # token=None (the cron path) must try_register, and when the slot is taken
    # NOT run: the placeholder becomes a short "skipped" note and the live run's
    # token is untouched (so !stop still signals the right run).
    if not _HAVE_APP:
        return
    assert _appmod is not None
    from src.slack import handlers, interrupt

    def _must_not_run(backend):
        raise AssertionError("must not resolve a runner while the thread is busy")

    monkeypatch.setattr(_appmod.runners, "get_runner", _must_not_run)

    updates = []

    class _Client:
        def chat_update(self, channel=None, ts=None, text=None):
            updates.append(text)
            return {"ok": True}

    held = interrupt.try_register(_FILE_AGENT["name"], "T_cronbusy")
    assert held is not None
    try:
        handlers._run_and_update(
            _Client(), "C1", "PH1", _FILE_AGENT, "scheduled prompt", "T_cronbusy"
        )
        assert len(updates) == 1
        assert "already in progress" in updates[0]
        # The live run's slot survived: signaling reaches the HELD token.
        assert interrupt.request(_FILE_AGENT["name"], "T_cronbusy") is True
        assert held.requested is True
    finally:
        interrupt.unregister(_FILE_AGENT["name"], "T_cronbusy", held)


def test_cron_path_claims_and_releases_slot(monkeypatch, tmp_path):
    # A free thread: the cron path claims the slot, threads the token into the
    # run (cancel is not None), and releases it afterwards.
    if not _HAVE_APP:
        return
    assert _appmod is not None
    from src.slack import handlers, interrupt

    sessions = str(tmp_path / "sessions.json")
    overrides = str(tmp_path / "overrides.json")
    monkeypatch.setattr(claude_runner, "_sessions_path", lambda: sessions)
    monkeypatch.setattr(claude_runner, "_overrides_path", lambda: overrides)
    monkeypatch.setenv("WORKDIR_BASE", str(tmp_path / "wd"))

    seen = {}

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
            seen["cancel"] = cancel
            return "ok", "sid-1", {}

    class _Client:
        def chat_update(self, channel=None, ts=None, text=None):
            return {"ok": True}

        def files_upload_v2(self, **kwargs):
            return {"ok": True}

    monkeypatch.setattr(_appmod.runners, "get_runner", lambda backend: _Runner)

    handlers._run_and_update(_Client(), "C1", "PH1", _FILE_AGENT, "p", "T_cronfree")
    assert seen["cancel"] is not None
    # Slot released: a new run can claim it.
    again = interrupt.try_register(_FILE_AGENT["name"], "T_cronfree")
    assert again is not None
    interrupt.unregister(_FILE_AGENT["name"], "T_cronfree", again)


# --- 3b. the tick returns while the fired cron still runs ---------------------


def test_scheduler_tick_returns_while_fire_still_running(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None

    crons = str(tmp_path / "crons.json")
    monkeypatch.setattr(claude_runner, "_crons_path", lambda: crons)
    claude_runner.add_cron(
        "30 9 * * *", "aristotle", "C9", "T9", "slow", cron_id="slow1", path=crons
    )

    started = threading.Event()
    release = threading.Event()
    done = []

    def _blocking_fire(entry, live):
        started.set()
        release.wait(timeout=10)
        done.append(entry["id"])

    monkeypatch.setattr(_appmod, "_fire_cron", _blocking_fire)

    n = _appmod._scheduler_tick({}, now=datetime(2026, 6, 24, 9, 30))
    # The tick returned while the fire is STILL blocked (a synchronous fire
    # would have completed -- done non-empty -- before the tick could return).
    assert n == 1
    assert done == []
    assert started.wait(timeout=5)
    release.set()


# --- 3c. the loop sleeps to the minute boundary, no double-fire ----------------


def test_scheduler_loop_boundary_sleep_and_no_double_fire(monkeypatch):
    if not _HAVE_APP:
        return
    assert _appmod is not None

    ticks = []
    monkeypatch.setattr(_appmod, "_scheduler_tick", lambda live, now: ticks.append(now))

    t1 = datetime(2026, 6, 24, 9, 30, 15, 500000)  # -> sleep 44.5s to :31
    t2 = datetime(2026, 6, 24, 9, 30, 59)  # same minute -> NO tick, sleep 1s
    t3 = datetime(2026, 6, 24, 9, 31, 0)  # new minute -> tick, sleep 60s
    clock_values = iter([t1, t2, t3])

    slept = []

    class _Stop(Exception):
        pass

    def fake_sleep(seconds):
        slept.append(seconds)
        if len(slept) == 3:
            raise _Stop

    with pytest.raises(_Stop):
        _appmod._scheduler_loop({}, now=lambda: next(clock_values), sleep=fake_sleep)

    assert ticks == [t1, t3]  # the same minute never fires twice
    assert slept[0] == pytest.approx(44.5)  # to the :31 boundary, not fixed 60
    assert slept[1] == pytest.approx(1.0)
    assert slept[2] == pytest.approx(60.0)  # exactly on the boundary -> full minute


# --- 4. long replies are truncated, not destroyed -----------------------------


def test_long_reply_truncated_keeps_footer_and_files(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    import os

    from src.slack import handlers

    sessions = str(tmp_path / "sessions.json")
    overrides = str(tmp_path / "overrides.json")
    monkeypatch.setattr(claude_runner, "_sessions_path", lambda: sessions)
    monkeypatch.setattr(claude_runner, "_overrides_path", lambda: overrides)
    monkeypatch.setenv("WORKDIR_BASE", str(tmp_path / "wd"))
    monkeypatch.setenv("SHOW_USAGE", "1")

    workdir = claude_runner.get_workdir("aristotle", "T_long", create=True)
    with open(os.path.join(workdir, "plot.png"), "w", encoding="utf-8") as f:
        f.write("PNG")

    long_body = "x" * 45_000
    reply = long_body + "\n<<files: plot.png>>"

    posted = {}
    uploads = []

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
            return reply, "sid-1", {"tokens": 500}

    class _Client:
        def chat_update(self, channel=None, ts=None, text=None):
            if len(text) > 4_000:
                raise RuntimeError("msg_too_long")
            posted["text"] = text
            return {"ok": True}

        def files_upload_v2(self, **kwargs):
            uploads.append(kwargs)
            return {"ok": True}

    monkeypatch.setattr(_appmod.runners, "get_runner", lambda backend: _Runner)

    handlers._run_and_update(_Client(), "C1", "TS1", _FILE_AGENT, "go", "T_long")

    final = posted["text"]
    assert len(final) <= 4_000  # under Slack chat.update's msg_too_long ceiling
    assert "truncated: full reply too long for Slack" in final
    assert "<<files:" not in final  # marker parsed/stripped BEFORE truncation
    assert final.rstrip().endswith("500 tok")  # the usage footer survived
    # The marker-named file still uploaded despite the truncated body.
    assert len(uploads) == 1
    assert os.path.basename(uploads[0]["file"]) == "plot.png"


# --- 5. file_share follow-ups reach the run path -------------------------------


def test_file_share_subtype_reaches_run_path(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    from src.slack import handlers, interrupt

    monkeypatch.setattr(_appmod, "_attachments_dir", lambda ts: str(tmp_path))
    monkeypatch.setattr(_appmod, "_http_get_bytes", lambda url, token: b"DATA")

    captured = {}

    class _FakeThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            captured["args"] = args
            captured["kwargs"] = kwargs or {}

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(handlers.threading, "Thread", _FakeThread)

    class _Client:
        token = "xoxb-test"

        def conversations_replies(self, **kwargs):
            return {"messages": []}

    event = {
        "channel": "C1",
        "ts": "T_fs",
        "text": "here is the data",
        "subtype": "file_share",
        "client_msg_id": f"MSG-{uuid.uuid4()}",
        "files": [{"name": "data.csv", "url_private": "https://x/data.csv"}],
    }
    say = _FakeSay()
    handlers._handle(_HANDLE_AGENT, event, _Client(), say)

    assert captured.get("started") is True
    prompt = captured["args"][4]
    assert "here is the data" in prompt
    assert "[Attached files:" in prompt and "data.csv" in prompt
    interrupt.unregister(_HANDLE_AGENT["name"], "T_fs", captured["kwargs"]["token"])

    # Other subtypes (edits etc.) are still rejected.
    say2 = _FakeSay()
    handlers._handle(
        _HANDLE_AGENT,
        {**event, "subtype": "message_changed", "client_msg_id": f"M-{uuid.uuid4()}"},
        _Client(),
        say2,
    )
    assert say2.posts == [] and "T_fs2" not in str(captured)


# --- 6. attachment download carries a timeout ----------------------------------


def test_http_get_bytes_passes_timeout(monkeypatch):
    if not _HAVE_APP:
        return
    from src.slack import files as files_mod

    seen = {}

    class _Resp:
        def read(self):
            return b"OK"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(files_mod.urllib.request, "urlopen", fake_urlopen)
    assert files_mod._http_get_bytes("https://slack/x", "tok") == b"OK"
    assert seen["timeout"] == files_mod._HTTP_TIMEOUT_S
    assert files_mod._HTTP_TIMEOUT_S > 0


# --- 7. multi-line control phrases match ---------------------------------------


def test_control_phrase_multiline_cron_add(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    crons = str(tmp_path / "crons.json")
    monkeypatch.setattr(claude_runner, "_crons_path", lambda: crons)
    say = _FakeSay()
    handled = _appmod._handle_control_phrase(
        _CONTROL_AGENT,
        '!cron add "0 9 * * *" run the standup\nand summarize the results',
        "T1",
        say,
        channel_id="C1",
    )
    assert handled is True
    stored = claude_runner.list_crons(path=crons)
    assert len(stored) == 1
    assert stored[0]["schedule"] == "0 9 * * *"
    assert "and summarize the results" in stored[0]["prompt"]


# --- 8. transcript keeps the TAIL of a long thread ------------------------------


def test_fetch_thread_history_follows_cursor_and_keeps_tail():
    if not _HAVE_APP:
        return
    from src.slack import handlers

    page1 = {
        "messages": [
            {"user": "U1", "ts": f"1.{i:04d}", "text": f"old-{i}"} for i in range(200)
        ],
        "response_metadata": {"next_cursor": "c2"},
    }
    page2 = {
        "messages": [
            {"user": "U1", "ts": f"2.{i:04d}", "text": f"new-{i}"} for i in range(60)
        ]
    }

    class _Client:
        def __init__(self):
            self.calls = []

        def conversations_replies(self, **kwargs):
            self.calls.append(kwargs)
            return page2 if kwargs.get("cursor") else page1

    client = _Client()
    history = handlers._fetch_thread_history(client, "C1", "1.0000", "9.9999")
    # The cursor was followed to the second page.
    assert len(client.calls) == 2
    assert client.calls[1]["cursor"] == "c2"
    # The transcript is the TAIL (the last 50 of 260): the newest exchange is
    # in, the stale head is out.
    assert "new-59" in history and "new-10" in history
    assert "old-0" not in history and "old-199" not in history
    assert "new-9" not in history  # message 210 of 260: just outside the tail


# --- 9. N/S cron steps use Vixie semantics ---------------------------------------


def test_cron_step_with_start_uses_vixie_semantics():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    # "5/15" = from 5 to field max, step 15 -> minutes 5, 20, 35, 50.
    for minute in (5, 20, 35, 50):
        assert _appmod.cron_matches("5/15 * * * *", datetime(2026, 6, 24, 9, minute))
    assert not _appmod.cron_matches("5/15 * * * *", datetime(2026, 6, 24, 9, 6))
    assert not _appmod.cron_matches("5/15 * * * *", datetime(2026, 6, 24, 9, 0))
    # The validity probe accepts exactly what the matcher implements.
    assert _appmod._cron_expr_valid("5/15 * * * *") is True


# --- 10. only a TRAILING <<files:>> marker triggers ------------------------------


def test_file_marker_mid_text_is_plain_prose():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    mid = "end your reply with <<files: name1, name2>> to deliver files, ok?"
    # A mid-text mention of the syntax: nothing stripped, nothing parsed.
    assert _appmod._parse_file_marker(mid) == (mid, [])
    assert _appmod._strip_file_marker(mid) == mid
    # A trailing marker still works.
    clean, names = _appmod._parse_file_marker("done\n<<files: a.png>>")
    assert (clean, names) == ("done", ["a.png"])


# --- 11. manifest covers group DMs -----------------------------------------------


def test_manifest_includes_group_dm_scope_and_event():
    m = manifest.build_manifest({"name": "x", "display_name": "X"})
    assert "mpim:history" in m["oauth_config"]["scopes"]["bot"]
    assert "message.mpim" in m["settings"]["event_subscriptions"]["bot_events"]


# --- 12. duplicate agent names are rejected --------------------------------------


def test_registry_rejects_duplicate_agent_names(tmp_path):
    path = tmp_path / "agents.json"
    path.write_text(
        json.dumps(
            [
                {"name": "a", "backend": "claude", "display_name": "A"},
                {"name": "a", "backend": "codex", "display_name": "A2"},
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicates agent name"):
        agents._load_registry(str(path))


# --- 13. a busy decline makes the run's completion post a done note --------------


def test_busy_decline_posts_done_note_on_completion(monkeypatch, tmp_path):
    # The final reply is a chat_update of the placeholder; a message declined by
    # the busy guard sits BELOW that placeholder, so the edit lands unseen. The
    # decline flags the in-flight token, and the worker then posts a NEW "done"
    # note after the final update. A run with no decline posts no extra message.
    if not _HAVE_APP:
        return
    assert _appmod is not None
    from src.slack import handlers, interrupt

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
            return "the result", "sid-1", {}

    calls = []

    class _Client:
        def chat_update(self, channel=None, ts=None, text=None):
            calls.append(("update", text))
            return {"ok": True}

        def chat_postMessage(self, channel=None, thread_ts=None, text=None):
            calls.append(("post", thread_ts, text))
            return {"ok": True}

    monkeypatch.setattr(_appmod.runners, "get_runner", lambda backend: _Runner)

    # A run is in flight (the slot is held, as _handle does before its worker).
    token = interrupt.try_register(_FILE_AGENT["name"], "1111.0001")
    assert token is not None

    # A second message arrives mid-run: _handle declines it and flags the token.
    event = {
        "channel": "C1",
        "ts": "1111.0002",
        "thread_ts": "1111.0001",
        "text": "done?",
        "client_msg_id": f"MSG-{uuid.uuid4()}",
    }
    say = _FakeSay()
    handlers._handle(_FILE_AGENT, event, object(), say)
    assert "still working" in say.posts[0]["text"]

    # The in-flight run completes: final update, THEN the done note as a new post.
    handlers._run_and_update(
        _Client(), "C1", "PH1", _FILE_AGENT, "p", "1111.0001", token=token
    )
    assert calls[-1][0] == "post"
    assert calls[-1][1] == "1111.0001"  # posted into the thread, not flat
    assert "see the updated reply above" in calls[-1][2].lower()
    assert calls[-2][0] == "update" and calls[-2][1].startswith("the result")

    # No decline during the run: no extra post.
    calls.clear()
    token2 = interrupt.try_register(_FILE_AGENT["name"], "T_note_free")
    handlers._run_and_update(
        _Client(), "C1", "PH2", _FILE_AGENT, "p", "T_note_free", token=token2
    )
    assert [c[0] for c in calls] == ["update"]
