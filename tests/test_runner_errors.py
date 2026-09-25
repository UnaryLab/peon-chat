import json
from unittest import mock

from src.runners import claude_runner
from src.runners.codex import _error_detail_from_stdout
from src.runners.common import format_process_failure

from tests.helpers import CICERO, PROMPT, SID, _fake_popen_factory


def test_format_process_failure_uses_stderr():
    message = format_process_failure("claude", 1, stderr="boom\nmore detail")

    assert message == "claude exited with code 1: boom more detail"


def test_format_process_failure_falls_back_to_stdout():
    message = format_process_failure("codex", 1, stdout="context length exceeded")

    assert (
        message
        == "codex exited with code 1: likely token/context limit: context length exceeded"
    )


def test_format_process_failure_handles_missing_output():
    message = format_process_failure("claude", 1)

    assert message == "claude exited with code 1: no stderr/stdout captured"


def test_error_detail_from_stdout_turn_failed():
    line = json.dumps({"type": "turn.failed", "error": {"message": "usage limit hit"}})

    assert _error_detail_from_stdout(line) == "usage limit hit"


def test_error_detail_from_stdout_error_event_top_level_message():
    line = json.dumps({"type": "stream.error", "message": "connection reset"})

    assert _error_detail_from_stdout(line) == "connection reset"


def test_error_detail_from_stdout_empty_input():
    assert _error_detail_from_stdout("") == ""
    assert _error_detail_from_stdout(None) == ""


def test_error_detail_from_stdout_skips_non_json_lines():
    stdout = "not json\n" + json.dumps({"type": "turn.failed", "error": "boom"})

    assert _error_detail_from_stdout(stdout) == "boom"


def test_error_detail_from_stdout_non_string_message():
    line = json.dumps({"type": "turn.failed", "error": {"message": {"code": 500}}})

    assert isinstance(_error_detail_from_stdout(line), str)


def test_run_claude_streaming_raises_with_result_detail_when_stderr_empty(monkeypatch):
    # The claude CLI can report the real failure (an is_error result event on
    # stdout) then exit 1 with empty stderr. The raised error must carry the
    # result event's detail, not a bare "no stderr/stdout captured".
    monkeypatch.setenv("STREAM_OUTPUT", "1")
    lines = [
        json.dumps(
            {"type": "result", "is_error": True, "result": "usage limit reached"}
        ),
    ]
    with mock.patch(
        "src.runners.claude_runner.subprocess.Popen",
        side_effect=_fake_popen_factory(lines, returncode=1, stderr=""),
    ):
        try:
            claude_runner.run_claude(CICERO, PROMPT, SID, True)
            assert False, "expected ClaudeRunError"
        except claude_runner.ClaudeRunError as exc:
            assert "usage limit reached" in str(exc)
