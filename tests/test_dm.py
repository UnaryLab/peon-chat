"""Regression tests for the 1:1 DM ("im") flow.

Slack never dispatches app_mention in an im channel, so message.im is the ONLY
delivery path for DMs: on_message must route every im message to _handle, a
top-level DM message is keyed by the DM CHANNEL id (one rolling conversation
per DM), and every post for a flat-DM key goes out FLAT (thread_ts=None; Slack
rejects a channel id as thread_ts). Threaded messages inside a DM, and all
channel/mpim behavior, are unchanged.
"""

import uuid

from src.runners import claude_runner

from tests.helpers import (
    _FILE_AGENT,
    _HANDLE_AGENT,
    _HAVE_APP,
    _FakeBoltApp,
    _FakeSay,
    _appmod,
)

# A realistic Slack DM channel id: the flat-DM conversation key.
_DM_CHANNEL = "D0123ABCDEF"


def _built_app(monkeypatch, calls):
    """Build a Bolt app on the fake App class with _handle capturing events."""
    from src.slack import app as slack_app

    agent = {
        "name": "brunel",
        "display_name": "Brunel",
        "backend": "claude",
        "claude_agent": None,
    }
    monkeypatch.setattr(slack_app, "App", _FakeBoltApp)
    monkeypatch.setattr(
        slack_app.handlers,
        "_handle",
        lambda agent, event, client, say: calls.append(event["text"]),
    )
    return slack_app.build_app_for(agent, "xoxb-brunel")


# --- 1. the message listener routes im messages to _handle --------------------


def test_on_message_routes_top_level_dm_to_handle(monkeypatch):
    # A top-level DM (channel_type im, NO thread_ts) must reach _handle: it is
    # the only delivery (no app_mention in im channels).
    if not _HAVE_APP:
        return
    calls = []
    bolt_app = _built_app(monkeypatch, calls)
    event = {
        "channel": _DM_CHANNEL,
        "channel_type": "im",
        "ts": "1.0001",
        "text": "hello there",
    }
    bolt_app.events["message"](event, None, None)
    assert calls == ["hello there"]


def test_on_message_dm_with_own_mention_still_handled(monkeypatch):
    # "<@bot> hi" typed in a DM: the ownership check assumes app_mention will
    # claim it, but app_mention never fires in im channels. The im branch runs
    # BEFORE that check, so the message is still handled.
    if not _HAVE_APP:
        return
    calls = []
    bolt_app = _built_app(monkeypatch, calls)
    event = {
        "channel": _DM_CHANNEL,
        "channel_type": "im",
        "ts": "1.0002",
        "text": "<@UBRUNEL> hi",
    }
    bolt_app.events["message"](event, None, None)
    assert calls == ["<@UBRUNEL> hi"]


def test_on_message_channel_top_level_still_ignored(monkeypatch):
    # Unchanged behavior outside DMs: a top-level channel message (no
    # thread_ts) is owned by app_mention, so on_message drops it.
    if not _HAVE_APP:
        return
    calls = []
    bolt_app = _built_app(monkeypatch, calls)
    event = {
        "channel": "C1",
        "channel_type": "channel",
        "ts": "1.0003",
        "text": "plain chatter",
    }
    bolt_app.events["message"](event, None, None)
    assert calls == []


# --- 2. the conversation key and the posting target are split -----------------


def test_reply_thread_ts_key_shapes():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    # A thread key (a Slack ts) posts into that thread.
    assert _appmod._reply_thread_ts("1234.5678") == "1234.5678"
    # A flat-DM key (a channel id) posts flat.
    assert _appmod._reply_thread_ts(_DM_CHANNEL) is None
    assert _appmod._reply_thread_ts(None) is None
    assert _appmod._reply_thread_ts("") is None


class _FakeThread:
    """Captures the worker Thread's args instead of running it (module-shared)."""

    captured = {}

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        _FakeThread.captured = {"args": args, "kwargs": kwargs or {}}

    def start(self):
        _FakeThread.captured["started"] = True


class _DMClient:
    """Fake Slack client that counts conversations_replies calls."""

    token = "xoxb-test"

    def __init__(self):
        self.replies_calls = 0

    def conversations_replies(self, **kwargs):
        self.replies_calls += 1
        return {"messages": []}


def _handle_event(monkeypatch, event):
    """Run handlers._handle with a captured worker thread; return (say, client)."""
    from src.slack import handlers

    monkeypatch.setattr(handlers.threading, "Thread", _FakeThread)
    _FakeThread.captured = {}
    say = _FakeSay()
    client = _DMClient()
    handlers._handle(_HANDLE_AGENT, event, client, say)
    return say, client


def test_handle_flat_dm_keys_by_channel_posts_flat_skips_history(monkeypatch):
    # The conversation key is the DM channel id (rolling conversation), the
    # placeholder posts FLAT, and the thread-history fetch is skipped (there is
    # no thread; the rolling session carries the DM context).
    if not _HAVE_APP:
        return
    from src.slack import interrupt

    event = {
        "channel": _DM_CHANNEL,
        "channel_type": "im",
        "ts": "2.0001",
        "text": "hi",
        "client_msg_id": f"MSG-{uuid.uuid4()}",
    }
    say, client = _handle_event(monkeypatch, event)
    assert _FakeThread.captured.get("started") is True
    # Worker args: (client, channel, placeholder_ts, agent, prompt, thread_ts).
    assert _FakeThread.captured["args"][5] == _DM_CHANNEL  # key = channel id
    assert say.posts[-1]["thread_ts"] is None  # placeholder posted flat
    assert client.replies_calls == 0  # no thread to fetch
    interrupt.unregister(
        _HANDLE_AGENT["name"], _DM_CHANNEL, _FakeThread.captured["kwargs"]["token"]
    )


def test_handle_threaded_dm_keeps_thread_key_and_posts_in_thread(monkeypatch):
    # A threaded message INSIDE a DM is a normal thread conversation: key =
    # thread_ts, placeholder posts into the thread, history is fetched.
    if not _HAVE_APP:
        return
    from src.slack import interrupt

    event = {
        "channel": _DM_CHANNEL,
        "channel_type": "im",
        "thread_ts": "3.0001",
        "ts": "3.0002",
        "text": "threaded follow-up",
        "client_msg_id": f"MSG-{uuid.uuid4()}",
    }
    say, client = _handle_event(monkeypatch, event)
    assert _FakeThread.captured.get("started") is True
    assert _FakeThread.captured["args"][5] == "3.0001"  # key = thread ts
    assert say.posts[-1]["thread_ts"] == "3.0001"  # posted into the thread
    assert client.replies_calls == 1  # history still fetched
    interrupt.unregister(
        _HANDLE_AGENT["name"], "3.0001", _FakeThread.captured["kwargs"]["token"]
    )


def test_control_ack_in_flat_dm_posts_flat(monkeypatch, tmp_path):
    # A control phrase in a flat DM: the ack goes through the same key ->
    # posting translation, so it posts flat instead of passing the channel id
    # as thread_ts (which Slack would reject).
    if not _HAVE_APP:
        return
    overrides = str(tmp_path / "overrides.json")
    monkeypatch.setattr(claude_runner, "_overrides_path", lambda: overrides)
    event = {
        "channel": _DM_CHANNEL,
        "channel_type": "im",
        "ts": "4.0001",
        "text": "!reset",
        "client_msg_id": f"MSG-{uuid.uuid4()}",
    }
    say, _client = _handle_event(monkeypatch, event)
    assert len(say.posts) == 1
    assert "overrides cleared" in say.posts[0]["text"]
    assert say.posts[0]["thread_ts"] is None


def test_new_in_flat_dm_clears_channel_key_and_posts_flat(monkeypatch, tmp_path):
    # !new in a flat DM: the conversation key is the DM channel id, so the
    # session stored under (agent, channel_id) is cleared (cutting the rolling
    # DM context) and the ack posts FLAT like every other control ack.
    if not _HAVE_APP:
        return
    sessions = str(tmp_path / "sessions.json")
    monkeypatch.setattr(claude_runner, "_sessions_path", lambda: sessions)
    claude_runner.set_session(
        _HANDLE_AGENT["name"], _DM_CHANNEL, "sid-dm", path=sessions
    )
    event = {
        "channel": _DM_CHANNEL,
        "channel_type": "im",
        "ts": "4.0002",
        "text": "!new",
        "client_msg_id": f"MSG-{uuid.uuid4()}",
    }
    say, _client = _handle_event(monkeypatch, event)
    assert (
        claude_runner.get_session(_HANDLE_AGENT["name"], _DM_CHANNEL, path=sessions)
        is None
    )
    assert len(say.posts) == 1
    assert "fresh context" in say.posts[0]["text"]
    assert say.posts[0]["thread_ts"] is None


# --- 3. the session is keyed by (agent, channel id) and resumes ---------------


def test_dm_session_keyed_by_channel_and_second_message_resumes(monkeypatch, tmp_path):
    # First flat-DM run: no prior session, persists one under the channel-id
    # key. Second run: get_session(agent, channel_id) returns it (resume).
    if not _HAVE_APP:
        return
    assert _appmod is not None
    from src.slack import handlers

    sessions = str(tmp_path / "sessions.json")
    overrides = str(tmp_path / "overrides.json")
    monkeypatch.setattr(claude_runner, "_sessions_path", lambda: sessions)
    monkeypatch.setattr(claude_runner, "_overrides_path", lambda: overrides)
    monkeypatch.setenv("WORKDIR_BASE", str(tmp_path / "wd"))

    priors = []

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
            priors.append(prior)
            return "ok", "sid-dm-1", {}

    class _Client:
        def chat_update(self, channel=None, ts=None, text=None):
            return {"ok": True}

    monkeypatch.setattr(_appmod.runners, "get_runner", lambda backend: _Runner)

    handlers._run_and_update(
        _Client(), _DM_CHANNEL, "PH1", _FILE_AGENT, "one", _DM_CHANNEL
    )
    handlers._run_and_update(
        _Client(), _DM_CHANNEL, "PH2", _FILE_AGENT, "two", _DM_CHANNEL
    )
    assert priors == [None, "sid-dm-1"]
    # The store key really is (agent, channel id).
    assert claude_runner.get_session(_FILE_AGENT["name"], _DM_CHANNEL) == "sid-dm-1"


# --- 4. flat-DM file upload posts flat, workdir still keyed by channel --------


def test_flat_dm_upload_has_no_thread_ts_but_workdir_uses_key(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    import os

    from src.slack import files as files_mod

    monkeypatch.setenv("WORKDIR_BASE", str(tmp_path / "wd"))
    # The workdir is namespaced by the CHANNEL-ID key; drop a file into it.
    workdir = claude_runner.get_workdir(_FILE_AGENT["name"], _DM_CHANNEL, create=True)
    with open(os.path.join(workdir, "plot.png"), "w", encoding="utf-8") as f:
        f.write("PNG")

    uploads = []

    class _Client:
        def files_upload_v2(self, **kwargs):
            uploads.append(kwargs)
            return {"ok": True}

    n = files_mod._maybe_upload_named(
        _Client(), _DM_CHANNEL, _DM_CHANNEL, _FILE_AGENT, ["plot.png"]
    )
    assert n == 1
    # Resolved inside the channel-id-keyed workdir, posted FLAT.
    assert len(uploads) == 1
    assert os.path.basename(uploads[0]["file"]) == "plot.png"
    assert uploads[0]["thread_ts"] is None


# --- 5. a cron whose thread_ts is a channel id posts its placeholder flat -----


def test_cron_fire_flat_dm_posts_placeholder_flat(monkeypatch):
    # A !cron add issued in a flat DM stores the channel id as the entry's
    # thread_ts; the fire must post the placeholder FLAT while the run still
    # uses the channel id as its conversation key.
    if not _HAVE_APP:
        return
    assert _appmod is not None
    from unittest import mock

    from src import agents

    posted = {}
    ran = {}

    client = mock.Mock()

    def _post(channel=None, thread_ts=None, text=None):
        posted.update({"channel": channel, "thread_ts": thread_ts})
        return {"ts": "PH-ts"}

    client.chat_postMessage.side_effect = _post
    handler = mock.Mock()
    handler.app.client = client
    live = {"aristotle": {"handler": handler}}

    def _fake_run(client, channel, placeholder_ts, agent, prompt, thread_ts):
        ran["thread_ts"] = thread_ts

    monkeypatch.setattr(_appmod, "_run_and_update", _fake_run)
    agent = next(a for a in agents.REGISTRY if a["name"] == "aristotle")
    assert agent is not None
    entry = {
        "id": "dmcron",
        "agent": "aristotle",
        "channel": _DM_CHANNEL,
        "thread_ts": _DM_CHANNEL,  # a flat-DM cron stores the channel id
        "prompt": "daily digest",
        "enabled": True,
    }
    _appmod._fire_cron(entry, live)
    assert posted["thread_ts"] is None  # placeholder posted flat
    assert ran["thread_ts"] == _DM_CHANNEL  # the run keeps the key
