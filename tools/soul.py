"""Soul tools — the assistant's self-modifiable identity, stored as soul.md."""

from __future__ import annotations

import re
from pathlib import Path

import arcana

SOUL_PATH = Path(__file__).parent.parent / "soul.md"


def _read_soul() -> str:
    if SOUL_PATH.exists():
        return SOUL_PATH.read_text()
    return "# Ava\n\n## Identity\n\n- **Name**: Ava\n"


def _write_soul(content: str):
    SOUL_PATH.write_text(content)


def _extract_section(content: str, heading: str) -> str:
    """Extract content under a ## heading."""
    pattern = rf"## {re.escape(heading)}\s*\n(.*?)(?=\n## |\Z)"
    m = re.search(pattern, content, re.DOTALL)
    return m.group(1).strip() if m else ""


def _extract_field(content: str, field: str) -> str:
    """Extract a **Field**: value from the content."""
    pattern = rf"\*\*{re.escape(field)}\*\*:\s*(.+)"
    m = re.search(pattern, content)
    return m.group(1).strip() if m else ""


def _replace_section(content: str, heading: str, new_body: str) -> str:
    """Replace the body of a ## section."""
    pattern = rf"(## {re.escape(heading)}\s*\n).*?(?=\n## |\Z)"
    replacement = rf"\g<1>\n{new_body}\n"
    result = re.sub(pattern, replacement, content, flags=re.DOTALL)
    return result


def _replace_field(content: str, field: str, value: str) -> str:
    """Replace a **Field**: value line."""
    pattern = rf"(\*\*{re.escape(field)}\*\*:\s*).+"
    return re.sub(pattern, rf"\g<1>{value}", content)


def build_personality() -> str:
    """Build system prompt dynamically from soul.md."""
    soul = _read_soul()

    name = _extract_field(soul, "Name") or "Ava"
    personality = _extract_section(soul, "Personality")
    style = _extract_section(soul, "Speaking Style")
    about_user = _extract_section(soul, "About User")
    notes = _extract_section(soul, "Notes")

    parts = [f"你叫 {name}，是用户的私人 AI 助手，运行在用户的 Mac 上。"]

    if personality:
        parts.append(f"\n## 你的性格\n{personality}")

    if style:
        parts.append(f"\n## 说话方式\n{style}")

    parts.append("""
## 回复结构
你的回复分两层：
1. 口头部分（第一段）：简短口语化，有温度，1-3句。会被语音朗读。
2. 详细部分（后面）：代码、列表等，只在屏幕显示。
口头部分不要说"详情如下"之类废话。""")

    if about_user and about_user != "（通过对话自然积累）":
        parts.append(f"\n## 你对用户的了解\n{about_user}")

    if notes and notes != "（自己的想法和记录）":
        parts.append(f"\n## 你的笔记\n{notes}")

    parts.append("""
## 能力
- shell: 执行终端命令
- list_sessions / read_session / send_to_session / create_claude_session: 管理 iTerm2 会话
- look: 摄像头  |  screenshot: 截屏
- update_self: 改自己的名字、性格、语音、说话风格
- remember_user: 记住关于用户的事
- add_note: 给自己写笔记

## 原则
- 不朗读代码和长列表
- 不说废话，直接做
- 用户让你做事直接调工具
- 你可以也应该主动修改自己——用户让你改名就改，觉得自己哪里需要调整就调""")

    return "\n".join(parts)


def get_name() -> str:
    return _extract_field(_read_soul(), "Name") or "Ava"


def get_voice() -> str:
    return _extract_field(_read_soul(), "Voice") or "zh-CN-YunxiNeural"


# === Tools ===


@arcana.tool(
    when_to_use="当用户让你改名字、改性格、改语音、改说话风格时。也包括你自己觉得需要调整的时候。",
    what_to_expect="成功修改并返回确认",
    side_effect="write",
)
async def update_self(field: str, value: str) -> str:
    """修改自己。field 可以是: name, voice, personality, style。personality 会替换整个性格段落。"""
    soul = _read_soul()

    if field.lower() == "name":
        soul = _replace_field(soul, "Name", value)
        # Also update the title
        soul = re.sub(r"^# .+", f"# {value}", soul)
    elif field.lower() == "voice":
        soul = _replace_field(soul, "Voice", value)
    elif field.lower() == "personality":
        soul = _replace_section(soul, "Personality", value)
    elif field.lower() == "style":
        soul = _replace_section(soul, "Speaking Style", value)
    else:
        return f"不认识 {field}。可改: name, voice, personality, style"

    _write_soul(soul)
    return f"好的，已经把 {field} 改成了: {value}"


@arcana.tool(
    when_to_use="当你从对话中了解到用户的新信息时。比如名字、职业、偏好、习惯、项目信息等。主动使用。",
    what_to_expect="信息已记录到 soul.md",
)
async def remember_user(fact: str) -> str:
    """记住关于用户的一个事实。"""
    soul = _read_soul()
    section = _extract_section(soul, "About User")
    if fact in section:
        return "已经记住了"
    new_line = f"- {fact}"
    if section == "（通过对话自然积累）" or not section:
        new_section = new_line
    else:
        new_section = section + "\n" + new_line
    soul = _replace_section(soul, "About User", new_section)
    _write_soul(soul)
    return f"记住了: {fact}"


@arcana.tool(
    when_to_use="当你想给自己写笔记、记录发现、或者想在以后的对话中记住某件事时。",
    what_to_expect="笔记已记录",
)
async def add_note(note: str) -> str:
    """给自己写一条笔记。"""
    soul = _read_soul()
    section = _extract_section(soul, "Notes")
    new_line = f"- {note}"
    if section == "（自己的想法和记录）" or not section:
        new_section = new_line
    else:
        new_section = section + "\n" + new_line
    soul = _replace_section(soul, "Notes", new_section)
    _write_soul(soul)
    return f"记下了: {note}"
