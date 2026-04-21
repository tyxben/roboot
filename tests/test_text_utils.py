"""Tests for text_utils.extract_spoken_text — the `> ` blockquote TTS
convention that both the web server and the Telegram bot rely on to pick
which lines of a model reply should be spoken aloud."""

from __future__ import annotations

from text_utils import extract_spoken_text


def test_empty_input_returns_empty():
    assert extract_spoken_text("") == ""
    assert extract_spoken_text(None) == ""  # type: ignore[arg-type]


def test_single_blockquote_line_is_extracted():
    assert extract_spoken_text("> 你好，世界。") == "你好，世界。"


def test_multiple_blockquote_lines_are_joined():
    text = "> 第一句。\n普通内容\n> 第二句。\n```code```\n> 第三句。"
    assert extract_spoken_text(text) == "第一句。 第二句。 第三句。"


def test_blockquote_strips_bold_and_inline_code():
    text = "> **重点** 是 `代码` 工作正常。"
    assert extract_spoken_text(text) == "重点 是 代码 工作正常。"


def test_fallback_when_no_blockquote_skips_code_blocks_and_lists():
    text = "```\nignored\n```\n这是第一句。\n- 列表项\n# 标题\n这是第二句。"
    spoken = extract_spoken_text(text)
    assert "ignored" not in spoken
    assert "列表项" not in spoken
    assert "标题" not in spoken
    assert "第一句" in spoken
    assert "第二句" in spoken


def test_fallback_truncates_to_last_sentence_within_300_chars():
    # Fallback collects sentences line-by-line, so we need newline-separated
    # sentences (the real agent produces multi-line prose).
    long = "\n".join(["这是第一句。"] * 60 + ["最后一句。"])
    spoken = extract_spoken_text(long)
    assert len(spoken) <= 300
    assert spoken.endswith("。")


def test_blockquote_takes_precedence_over_fallback():
    """If even a single `> ` line exists, fallback logic must not activate —
    otherwise non-spoken paragraphs would leak into TTS."""
    text = "这是普通段落，不应该被朗读。\n> 只有这一句要朗读。\n这段也不要。"
    assert extract_spoken_text(text) == "只有这一句要朗读。"
