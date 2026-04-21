"""Tests for self-review distillation into soul.md.

Covers:
- tools/soul.py::append_self_feedback — section creation, append, prune.
- memory.py::distill_self_feedback — prompt content, canned-reply write-
  through, NOTHING no-op.

All filesystem interactions are redirected to tmp paths so the real
soul.md is never touched.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import memory
from tools import soul as soul_mod


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def tmp_soul(tmp_path, monkeypatch):
    """Redirect soul.md + its snapshot dir into a tmp area.

    Yields the Path to the tmp soul.md. Tests can pre-seed it via
    `path.write_text(...)` before calling functions under test.
    """
    soul_path = tmp_path / "soul.md"
    history_dir = tmp_path / ".soul" / "history"
    monkeypatch.setattr(soul_mod, "SOUL_PATH", soul_path)
    monkeypatch.setattr(soul_mod, "SOUL_HISTORY_DIR", history_dir)
    return soul_path


# -----------------------------------------------------------------------------
# append_self_feedback
# -----------------------------------------------------------------------------


def test_append_self_feedback_creates_section_when_missing(tmp_soul):
    tmp_soul.write_text(
        "# Ava\n\n## Identity\n\n- **Name**: Ava\n\n## About User\n\n- fact\n"
    )

    soul_mod.append_self_feedback("用户纠正我说过话太啰嗦")

    content = tmp_soul.read_text()
    assert f"## {soul_mod.SELF_FEEDBACK_HEADING}" in content
    # Existing sections preserved (not renamed / not reordered above feedback).
    assert content.index("## Identity") < content.index(
        f"## {soul_mod.SELF_FEEDBACK_HEADING}"
    )
    assert content.index("## About User") < content.index(
        f"## {soul_mod.SELF_FEEDBACK_HEADING}"
    )
    today = time.strftime("%Y-%m-%d")
    assert f"- [{today}] 用户纠正我说过话太啰嗦" in content


def test_append_self_feedback_appends_when_section_present(tmp_soul):
    initial = (
        "# Ava\n\n## Identity\n\n- **Name**: Ava\n\n"
        f"## {soul_mod.SELF_FEEDBACK_HEADING}\n\n- [2026-01-01] 旧反馈\n"
    )
    tmp_soul.write_text(initial)

    soul_mod.append_self_feedback("新反馈条目")

    content = tmp_soul.read_text()
    # Both old and new lines present, old before new.
    assert "- [2026-01-01] 旧反馈" in content
    today = time.strftime("%Y-%m-%d")
    assert f"- [{today}] 新反馈条目" in content
    assert content.index("旧反馈") < content.index("新反馈条目")
    # Section heading not duplicated.
    assert content.count(f"## {soul_mod.SELF_FEEDBACK_HEADING}") == 1


def test_append_self_feedback_prunes_when_too_long(tmp_soul):
    # Pre-seed the section past the threshold.
    max_lines = soul_mod.SELF_FEEDBACK_MAX_LINES
    existing_bullets = "\n".join(
        f"- [2026-01-01] 旧条目 {i}" for i in range(max_lines + 5)
    )
    tmp_soul.write_text(
        "# Ava\n\n"
        f"## {soul_mod.SELF_FEEDBACK_HEADING}\n\n{existing_bullets}\n"
    )

    soul_mod.append_self_feedback("全新反馈")
    content = tmp_soul.read_text()

    # Count only bullet lines inside the self-feedback section.
    section_body = content.split(f"## {soul_mod.SELF_FEEDBACK_HEADING}", 1)[1]
    bullet_lines = [
        ln for ln in section_body.splitlines() if ln.strip().startswith("- ")
    ]
    # Pruned to roughly half + 1 new entry; strictly below the max.
    assert len(bullet_lines) < max_lines
    assert len(bullet_lines) >= 1
    # The new entry is preserved.
    assert any("全新反馈" in ln for ln in bullet_lines)
    # The oldest entries are gone (index 0..some) but the latest old entries
    # remain — confirms "keep most recent half" ordering.
    assert not any("旧条目 0 " in ln or "旧条目 0\n" in ln for ln in bullet_lines)
    assert any(f"旧条目 {max_lines + 4}" in ln for ln in bullet_lines)


def test_append_self_feedback_empty_line_is_noop(tmp_soul):
    tmp_soul.write_text("# Ava\n\n## Identity\n\n- **Name**: Ava\n")
    before = tmp_soul.read_text()
    soul_mod.append_self_feedback("   ")
    assert tmp_soul.read_text() == before


# -----------------------------------------------------------------------------
# distill_self_feedback prompt construction
# -----------------------------------------------------------------------------


def test_self_feedback_prompt_contains_transcript_context():
    transcript = "用户: 别再啰嗦了\n助手: 好的，我会更简洁。"
    user_text = memory._build_self_feedback_prompt_user_text(transcript)
    assert transcript in user_text
    # Sanity: prompt mentions "20 轮" so the sub-agent knows the window.
    assert "20" in user_text


def test_self_feedback_system_prompt_demands_specificity():
    """Guard: the system prompt must reject the two failure modes we care
    about (vagueness + sycophancy) and honor the NOTHING sentinel."""
    sp = memory._SELF_FEEDBACK_PROMPT
    # Explicitly rejects vague self-help and sycophantic no-ops.
    assert "具体" in sp
    assert "空话" in sp
    assert "自夸" in sp
    # Honors the shared NOTHING sentinel.
    assert "NOTHING" in sp


# -----------------------------------------------------------------------------
# distill_self_feedback end-to-end (with fake runner + fake chat_store)
# -----------------------------------------------------------------------------


async def test_distill_self_feedback_writes_reply_via_append(
    tmp_soul, monkeypatch
):
    """Non-trivial reply → lands in soul.md via append_self_feedback."""
    tmp_soul.write_text("# Ava\n\n## Identity\n\n- **Name**: Ava\n")

    async def fake_list(sid, limit=200):
        assert sid == "s-self"
        return [
            {"role": "user", "content": "你别再每次都打开摄像头"},
            {"role": "assistant", "content": "抱歉，我明白了。"},
        ]

    async def fake_runner(system_prompt, user_text):
        # Sub-agent's canned reply — must be >= DISTILL_MIN_LEN + real content.
        return "未经询问就开摄像头让用户不舒服，下次必须先问一句。"

    monkeypatch.setattr(memory.chat_store, "list_messages", fake_list)

    result = await memory.distill_self_feedback(
        "s-self",
        runtime=object(),
        runner=fake_runner,
    )
    assert result is not None
    assert "摄像头" in result

    content = tmp_soul.read_text()
    assert f"## {soul_mod.SELF_FEEDBACK_HEADING}" in content
    assert "摄像头" in content
    today = time.strftime("%Y-%m-%d")
    assert f"- [{today}]" in content


async def test_distill_self_feedback_nothing_reply_leaves_soul_untouched(
    tmp_soul, monkeypatch
):
    original = "# Ava\n\n## Identity\n\n- **Name**: Ava\n"
    tmp_soul.write_text(original)

    async def fake_list(sid, limit=200):
        return [{"role": "user", "content": "一切都好"}]

    async def fake_runner(sp, ut):
        return "NOTHING"

    monkeypatch.setattr(memory.chat_store, "list_messages", fake_list)

    result = await memory.distill_self_feedback(
        "s-self",
        runtime=object(),
        runner=fake_runner,
    )
    assert result is None
    # soul.md must be byte-identical — no section created, no snapshot write.
    assert tmp_soul.read_text() == original


async def test_distill_self_feedback_short_reply_is_noop(tmp_soul, monkeypatch):
    original = "# Ava\n\n## Identity\n\n- **Name**: Ava\n"
    tmp_soul.write_text(original)

    async def fake_list(sid, limit=200):
        return [{"role": "user", "content": "嗯"}]

    async def fake_runner(sp, ut):
        return "ok"  # well below DISTILL_MIN_LEN

    monkeypatch.setattr(memory.chat_store, "list_messages", fake_list)

    result = await memory.distill_self_feedback(
        "s-self",
        runtime=object(),
        runner=fake_runner,
    )
    assert result is None
    assert tmp_soul.read_text() == original


async def test_distill_self_feedback_uses_fake_runtime_chatsession(
    tmp_soul, monkeypatch
):
    """Simulate a fake runtime whose throwaway ChatSession returns a canned
    reply; assert that reply lands in soul.md via append_self_feedback."""
    tmp_soul.write_text("# Ava\n")

    canned = "回复用户时忽略了他明确说过的偏好，下次先复核上下文再答。"

    class FakeResp:
        content = canned

    class FakeSession:
        def __init__(self, system_prompt):
            self.system_prompt = system_prompt
            self.seen = []

        async def send(self, user_text):
            self.seen.append(user_text)
            return FakeResp()

    class FakeRuntime:
        def __init__(self):
            self.sessions: list[FakeSession] = []

        def create_chat_session(self, system_prompt):
            s = FakeSession(system_prompt)
            self.sessions.append(s)
            return s

    async def fake_list(sid, limit=200):
        return [
            {"role": "user", "content": "我说过别用英文回答"},
            {"role": "assistant", "content": "Sure, here you go."},
            {"role": "user", "content": "你又用英文"},
        ]

    monkeypatch.setattr(memory.chat_store, "list_messages", fake_list)

    runtime = FakeRuntime()
    result = await memory.distill_self_feedback("s-x", runtime=runtime)

    assert result == canned
    # The sub-agent was actually invoked with our self-feedback system prompt.
    assert len(runtime.sessions) == 1
    assert runtime.sessions[0].system_prompt == memory._SELF_FEEDBACK_PROMPT
    # And saw the transcript.
    assert any("我说过别用英文回答" in u for u in runtime.sessions[0].seen)

    content = tmp_soul.read_text()
    assert canned in content


# -----------------------------------------------------------------------------
# record_turn_and_maybe_distill schedules both distillations
# -----------------------------------------------------------------------------


async def test_record_turn_fires_both_distillations(tmp_soul, monkeypatch):
    """At the Kth turn, both user-knowledge and self-feedback distill run."""
    calls: dict[str, int] = {"user_knowledge": 0, "self_feedback": 0}

    async def fake_distill_user(sid, *, runtime, k=None):
        calls["user_knowledge"] += 1
        return None

    async def fake_distill_self(sid, *, runtime, k=None):
        calls["self_feedback"] += 1
        return None

    monkeypatch.setattr(memory, "distill_and_record", fake_distill_user)
    monkeypatch.setattr(memory, "distill_self_feedback", fake_distill_self)

    c = memory.TurnCounter(every_k=2)
    assert memory.record_turn_and_maybe_distill("s1", runtime=object(), counter=c) is None
    task = memory.record_turn_and_maybe_distill("s1", runtime=object(), counter=c)
    assert task is not None
    await task
    assert calls == {"user_knowledge": 1, "self_feedback": 1}


async def test_record_turn_one_distill_failing_does_not_block_other(
    tmp_soul, monkeypatch
):
    """If user-knowledge blows up, self-feedback must still run (and vice versa)."""
    calls: dict[str, int] = {"user_knowledge": 0, "self_feedback": 0}

    async def boom_user(sid, *, runtime, k=None):
        calls["user_knowledge"] += 1
        raise RuntimeError("user-knowledge distill failed")

    async def fake_self(sid, *, runtime, k=None):
        calls["self_feedback"] += 1
        return None

    monkeypatch.setattr(memory, "distill_and_record", boom_user)
    monkeypatch.setattr(memory, "distill_self_feedback", fake_self)

    c = memory.TurnCounter(every_k=1)
    task = memory.record_turn_and_maybe_distill("s1", runtime=object(), counter=c)
    assert task is not None
    # Must not raise even though one coroutine raised.
    await task
    assert calls == {"user_knowledge": 1, "self_feedback": 1}
