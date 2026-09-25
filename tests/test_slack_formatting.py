import time

from src.slack.handlers import (
    _format_final_response,
    _md_to_mrkdwn,
    _truncate_for_slack,
)


def test_format_final_response_adds_trailing_blank_line():
    assert _format_final_response("done") == "done\n\n"


def test_format_final_response_does_not_accumulate_trailing_blank_lines():
    assert _format_final_response("done\n\n\n") == "done\n\n"


def test_md_to_mrkdwn_converts_bold_strike_links_headings():
    assert _md_to_mrkdwn("a **b** and __c__") == "a *b* and __c__"
    assert _md_to_mrkdwn("~~old~~") == "~old~"
    assert _md_to_mrkdwn("see [docs](https://x.io/a)") == "see <https://x.io/a|docs>"
    assert _md_to_mrkdwn("## **Plan**\nbody") == "*Plan*\nbody"


def test_md_to_mrkdwn_leaves_code_alone():
    text = "**x** `**y**`\n```\n**z** [a](https://b.c)\n```\n**w**"
    assert _md_to_mrkdwn(text) == "*x* `**y**`\n```\n**z** [a](https://b.c)\n```\n*w*"


def test_md_to_mrkdwn_unclosed_fence_stays_code():
    # A half-streamed code block must not be converted mid-stream.
    assert _md_to_mrkdwn("**a**\n```\n**b**") == "*a*\n```\n**b**"


def test_md_to_mrkdwn_leaves_plain_text_and_empty():
    assert _md_to_mrkdwn("a * b ** c # d") == "a * b ** c # d"
    assert _md_to_mrkdwn("") == ""
    assert _md_to_mrkdwn(None) is None


def test_truncate_for_slack_converts_markdown():
    assert _truncate_for_slack("**done**") == "*done*"
    assert _truncate_for_slack("**done**", limit=6) == "*done*"


def test_md_bold_needs_word_boundaries():
    assert _md_to_mrkdwn("a **b** and __c__") == "a *b* and __c__"
    for text in (
        "__main__",
        "the __init__ method",
        "foo.__init__",
        "x**2 + y**2",
        "path/**/*.py",
    ):
        assert _md_to_mrkdwn(text) == text


def test_md_heading_only_after_blank_line_or_at_start():
    assert _md_to_mrkdwn("ls\n# list files") == "ls\n# list files"
    assert _md_to_mrkdwn("intro\n\n## Plan\nbody") == "intro\n\n*Plan*\nbody"
    assert _md_to_mrkdwn("## **Plan**\nbody") == "*Plan*\nbody"


def test_md_heading_crlf_closing_hashes_and_linear_time():
    assert _md_to_mrkdwn("# A\r\n\r\n## B\r\ntext") == "*A*\r\n\r\n*B*\r\ntext"
    assert _md_to_mrkdwn("a\n\n## C#") == "a\n\n*C#*"
    assert _md_to_mrkdwn("## Plan ##") == "*Plan*"
    start = time.perf_counter()
    _md_to_mrkdwn("\n\n# a" + " \t" * 50000 + "x")
    assert time.perf_counter() - start < 1


def test_truncate_for_slack_does_not_split_link():
    from src.slack.handlers import _TRUNCATION_NOTICE

    text = "x" * 10 + " [docs](https://x.io/a) tail"
    out = _truncate_for_slack(text, limit=20)  # cut lands inside <https://x.io/a|docs>
    assert out.endswith(_TRUNCATION_NOTICE)
    head = out[: -len(_TRUNCATION_NOTICE)]
    assert "<" not in head
    assert head == "x" * 10 + " "


def test_md_to_mrkdwn_40k_input_is_fast_and_correct():
    sample = "## Plan\n**a** ~~b~~ [d](https://x.io) __init__\n"
    text = sample + "a **b ~~c [x](" * 3000 + "\n\n" + sample
    start = time.perf_counter()
    out = _md_to_mrkdwn(text)
    assert time.perf_counter() - start < 0.2
    converted = "*Plan*\n*a* ~b~ <https://x.io|d> __init__\n"
    assert out.startswith(converted)
    assert out.endswith("\n\n" + converted)


def test_md_link_repeated_opener_is_fast_and_unsafe_links_stay_raw():
    text = "[a](http://" * 3600
    start = time.perf_counter()
    assert _md_to_mrkdwn(text) == text
    assert time.perf_counter() - start < 0.2
    for raw in ("[a>b](https://x.io)", "[x](https://a.io/|evil)"):
        assert _md_to_mrkdwn(raw) == raw
