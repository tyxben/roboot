"""Soul tools — the assistant's self-modifiable identity, stored as soul.md.

Every write snapshots the prior file to `.soul/history/<iso>.md` so that a
bad replace (the agent clobbering a whole personality section when it only
meant to tweak one trait) can be reviewed or hand-restored. Appends to
About User + Notes are timestamped so the transcript shows when each fact
was learned.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import arcana

SOUL_PATH = Path(__file__).parent.parent / "soul.md"
SOUL_HISTORY_DIR = Path(__file__).parent.parent / ".soul" / "history"


def _read_soul() -> str:
    if SOUL_PATH.exists():
        return SOUL_PATH.read_text()
    return "# Ava\n\n## Identity\n\n- **Name**: Ava\n"


def _write_soul(content: str):
    # Snapshot the prior state so clobbered bullets aren't lost forever.
    # Skip if no prior file (first write) or if content is unchanged.
    if SOUL_PATH.exists():
        prior = SOUL_PATH.read_text()
        if prior != content:
            SOUL_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d-%H%M%S")
            (SOUL_HISTORY_DIR / f"{ts}.md").write_text(prior)
    SOUL_PATH.write_text(content)


def _today() -> str:
    return time.strftime("%Y-%m-%d")


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


_CHANNEL_LABELS = {
    "web": "web console",
    "telegram": "Telegram",
    "voice": "voice (local mic + TTS)",
    "cli": "CLI (keyboard)",
    "unknown": "unknown",
}


def build_personality(
    channel: str = "unknown",
    sessions_summary: str | None = None,
) -> str:
    """Build system prompt dynamically from soul.md.

    Args:
        channel: which entrypoint the user is on. One of
            "web", "telegram", "voice", "cli", or "unknown".
        sessions_summary: optional pre-formatted multi-line string
            describing currently-running Claude Code sessions.
            Rendered only if non-empty.
    """
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
## 回复格式（极其重要）

你的回复必须用 `> ` 开头来标记"说出口"的部分。只有 `> ` 开头的行会被语音朗读，其他内容只在屏幕上显示。

像人说话一样：简短、口语化、有温度。不超过两句话。剩下的细节用普通文字或列表展示。

示例 1（查文件）：
> 目录里有配置文件和几个模块，看着像个 Python 项目。

- adapters/
- tools/
- config.yaml
- run.py

示例 2（查会话状态）：
> 8 个会话在跑，hair 和 agent 这两个比较活跃。

| 项目 | 状态 |
|------|------|
| hair | 活跃 |
| agent | 活跃 |
| 其他 | 空闲 |

示例 3（简单问题）：
> 现在十点半。

示例 4（用户说谢谢）：
> 没事儿。

注意：
- 不要在 > 行里放代码或技术术语
- 不要说"以下是详细信息"，直接放
- 简单问题只需要 > 行就够，不需要额外内容
- 像跟朋友说话，不像在做报告""")

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

    # Current-context block: channel + (optional) sessions summary.
    channel_label = _CHANNEL_LABELS.get(channel, channel or "unknown")
    ctx_lines = ["\n## Current context", f"- Channel: {channel_label}"]
    if sessions_summary and sessions_summary.strip():
        ctx_lines.append("- Active Claude Code sessions:")
        for line in sessions_summary.strip().splitlines():
            line = line.rstrip()
            if not line:
                continue
            ctx_lines.append(f"  - {line.lstrip('- ').lstrip()}")
    parts.append("\n".join(ctx_lines))

    return "\n".join(parts)


# --- Session summary helper -------------------------------------------------

# Single regex that covers the most common Claude Code "waiting for approval"
# tails. Ported from CONFIRM_PATTERNS in relay/src/pair-page.ts — we only keep
# the cheapest catch-all so we don't duplicate the full browser-side set.
_WAITING_RE = re.compile(
    r"(Do you want to|\[Y/n\]|\[y/N\]|\(y/n\)|Allow|Deny|要继续吗|是否允许)",
    re.IGNORECASE,
)


async def summarize_sessions() -> str | None:
    """Return a short multi-line summary of running Claude Code sessions.

    One line per session: "<project> (<name>)" plus " — waiting for confirmation"
    if the tail of the session looks like a Claude Code approval prompt.

    Returns None silently if iTerm2 isn't reachable so chat init never crashes.
    """
    try:
        from iterm_bridge import bridge  # local import: avoids import-time cost
    except Exception:
        return None

    try:
        sessions = await bridge.list_sessions()
    except Exception:
        return None

    if not sessions:
        return None

    lines: list[str] = []
    for s in sessions:
        label = s.project or s.name or s.session_id[:8]
        suffix = ""
        try:
            tail = await bridge.read_session(s.session_id, num_lines=10)
            if tail and _WAITING_RE.search(tail):
                suffix = " — waiting for confirmation"
        except Exception:
            pass
        if s.name and s.name != label:
            lines.append(f"{label} ({s.name}){suffix}")
        else:
            lines.append(f"{label}{suffix}")

    return "\n".join(lines) if lines else None


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
    new_line = f"- ({_today()}) {fact}"
    if section == "（通过对话自然积累）" or not section:
        new_section = new_line
    else:
        new_section = section + "\n" + new_line
    soul = _replace_section(soul, "About User", new_section)
    _write_soul(soul)
    return f"记住了: {fact}"


# --- Self-feedback append (internal, not an @arcana.tool) -------------------

SELF_FEEDBACK_HEADING = "自我反馈"
# When the section grows past this many bullet lines, prune the oldest half.
# Kept deterministic (no LLM) so the distiller path stays cheap and testable.
SELF_FEEDBACK_MAX_LINES = 50


def _split_section(content: str, heading: str) -> tuple[str, str, str] | None:
    """Return (before, section_body, after) for `## heading`, or None if absent.

    `section_body` does not include the heading line itself. `after` starts at
    the next `## ` heading (or the end of file).
    """
    pattern = rf"(## {re.escape(heading)}\s*\n)(.*?)(?=\n## |\Z)"
    m = re.search(pattern, content, re.DOTALL)
    if not m:
        return None
    before = content[: m.start()]
    body = m.group(2)
    after = content[m.end():]
    return before, body, after


def append_self_feedback(line: str) -> None:
    """Append a dated self-feedback bullet to soul.md's `## 自我反馈` section.

    Behavior:
    - Creates the section at end of file if missing (after existing sections).
    - Prefixes the entry with `- [YYYY-MM-DD]`.
    - If the section already holds more than SELF_FEEDBACK_MAX_LINES bullets,
      keeps only the most recent half before appending the new line.
    - Atomic: reads full soul.md once, edits in memory, writes once.

    Called internally by the distiller; not exposed as an @arcana.tool.
    """
    line = (line or "").strip()
    if not line:
        return

    dated = f"- [{_today()}] {line}"

    soul = _read_soul()
    split = _split_section(soul, SELF_FEEDBACK_HEADING)

    if split is None:
        # Append section at the very end. Normalize trailing newline.
        tail = soul.rstrip() + "\n\n" + f"## {SELF_FEEDBACK_HEADING}\n\n{dated}\n"
        _write_soul(tail)
        return

    before, body, after = split

    # Existing bullet lines in this section (preserve order, skip blanks).
    existing = [ln for ln in body.splitlines() if ln.strip().startswith("- ")]

    if len(existing) >= SELF_FEEDBACK_MAX_LINES:
        # Keep only the most recent half (rounded down), then append.
        keep = len(existing) // 2
        existing = existing[-keep:] if keep > 0 else []

    existing.append(dated)
    new_body = "\n".join(existing) + "\n"

    # Rebuild the file, preserving content before/after.
    # `before` already ends right before the heading; the heading itself
    # is re-emitted here to stay consistent.
    new_content = (
        before
        + f"## {SELF_FEEDBACK_HEADING}\n\n"
        + new_body
        + (after if after.startswith("\n") else ("\n" + after if after else ""))
    )
    # Avoid trailing blank-line bloat on files that already end cleanly.
    new_content = new_content.rstrip() + "\n"
    _write_soul(new_content)


@arcana.tool(
    when_to_use="当你想给自己写笔记、记录发现、或者想在以后的对话中记住某件事时。",
    what_to_expect="笔记已记录",
)
async def add_note(note: str) -> str:
    """给自己写一条笔记。"""
    soul = _read_soul()
    section = _extract_section(soul, "Notes")
    new_line = f"- ({_today()}) {note}"
    if section == "（自己的想法和记录）" or not section:
        new_section = new_line
    else:
        new_section = section + "\n" + new_line
    soul = _replace_section(soul, "Notes", new_section)
    _write_soul(soul)
    return f"记下了: {note}"
