"""Tests for the channel + session-list context injection in
tools.soul.build_personality(). The iTerm2 bridge path (summarize_sessions)
is intentionally not exercised here — it needs a live daemon.
"""

from __future__ import annotations

from tools.soul import build_personality


def test_build_personality_without_context_is_backward_compatible():
    """Called with no args, build_personality() still produces a usable
    system prompt and still includes the Current context block, but the
    sessions sub-block is absent (since sessions_summary is None).
    """
    prompt = build_personality()

    # Core structure preserved.
    assert "## 回复格式（极其重要）" in prompt
    assert "## 能力" in prompt

    # Current context section exists with the default channel.
    assert "## Current context" in prompt
    assert "- Channel: unknown" in prompt

    # No sessions block when no summary was provided.
    assert "Active Claude Code sessions" not in prompt


def test_build_personality_with_channel_and_sessions():
    """Passing a channel + sessions_summary renders both pieces verbatim
    (with friendly labels for known channels) and formats sessions as a
    nested bullet list.
    """
    summary = "proj-foo: last activity 3m ago\nproj-bar: waiting for confirmation"
    prompt = build_personality(channel="web", sessions_summary=summary)

    assert "## Current context" in prompt
    # Known channel gets a friendly label.
    assert "- Channel: web console" in prompt
    # Sessions block rendered as nested bullets.
    assert "- Active Claude Code sessions:" in prompt
    assert "  - proj-foo: last activity 3m ago" in prompt
    assert "  - proj-bar: waiting for confirmation" in prompt


def test_empty_sessions_summary_omits_block():
    """An empty/whitespace sessions_summary must NOT emit the bullet."""
    prompt = build_personality(channel="telegram", sessions_summary="   \n  ")
    assert "- Channel: Telegram" in prompt
    assert "Active Claude Code sessions" not in prompt
