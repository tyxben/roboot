"""Generic file read / write / edit tools.

Lets the agent work with files directly instead of routing everything through
`shell` (where `cat` truncates at 4000 chars and `echo > file` trips the
danger gate). read_file paginates by line; write_file / edit_file are
declared side_effect="write" and gated in confirm mode (tool_guard lists them
in its always-confirm set).

Security — these tools bypass `shell`, so they enforce their OWN path policy:
  * Roboot's at-rest secrets are NEVER readable or writable here (config.yaml,
    .identity, .auth, the chat/reminder DBs, .faces, .voice_prefs, the
    allowlist). Reading config.yaml would leak API keys into the transcript;
    writing the identity key would brick pairing.
  * soul.md is readable but NOT writable here — self-modification must keep
    going through the soul_review gate (tools/soul.py), not this raw writer.
  * Common OS credential/system paths (~/.ssh, ~/.aws, ~/.gnupg, /etc, ...)
    are refused for both read and write.
The deny check runs on the *resolved* absolute path, so `../` games and
symlink-free relative paths can't dodge it. This is defense-in-depth that
holds even when ROBOOT_TOOL_APPROVAL=off.
"""

from __future__ import annotations

import os
from pathlib import Path

import arcana

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HOME = Path.home()

MAX_READ_BYTES = 200_000  # hard cap on bytes pulled off disk per read
MAX_RETURN_CHARS = 20_000  # cap on what we hand back to the model
DEFAULT_LINE_LIMIT = 400


# Repo-relative paths that are secret at rest — never read or write.
_SECRET_REPO_PATHS = {
    "config.yaml",
    ".identity",
    ".auth",
    ".faces",
    ".voice_prefs",
    ".chat_history.db",
    ".reminders.db",
    ".tool_audit",
    ".soul",
}
# Repo-relative paths writable=NO, readable=YES (self-mod must use soul tools).
_READONLY_REPO_PATHS = {"soul.md"}

# Absolute path fragments refused for both read and write (OS secrets/system).
_DENY_FRAGMENTS = (
    "/.ssh/",
    "/.aws/",
    "/.gnupg/",
    "/.kube/",
    "/.config/gcloud/",
)
_DENY_PREFIXES = ("/etc/", "/private/etc/", "/var/db/")
# ~/.roboot/tool_allowlist.json — editing the allowlist must not be agent-driven.
_DENY_ABS = {str((_HOME / ".roboot").resolve())}


def _resolved(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (_REPO_ROOT / p)
    # resolve() collapses .. and symlinks; strict=False so missing write
    # targets still resolve to an absolute path we can vet.
    return p.resolve()


def _repo_rel(p: Path) -> str | None:
    try:
        return str(p.relative_to(_REPO_ROOT))
    except ValueError:
        return None


def _deny_reason(path: str, *, for_write: bool) -> str | None:
    """Return a refusal reason if the path is off-limits, else None."""
    p = _resolved(path)
    s = str(p)

    for frag in _DENY_FRAGMENTS:
        if frag in s + "/":
            return f"拒绝访问凭据目录：{path}"
    for pre in _DENY_PREFIXES:
        if s.startswith(pre):
            return f"拒绝访问系统目录：{path}"
    for deny in _DENY_ABS:
        if s == deny or s.startswith(deny + os.sep):
            return f"拒绝访问受保护路径：{path}"

    rel = _repo_rel(p)
    if rel is not None:
        top = rel.split(os.sep, 1)[0]
        if top in _SECRET_REPO_PATHS:
            return f"拒绝访问 Roboot 机密文件：{rel}"
        if for_write and rel in _READONLY_REPO_PATHS:
            return f"soul.md 不能直接写入——请用 update_self / remember_user / add_note（会走审核门）"
    return None


@arcana.tool(
    when_to_use=(
        "当用户要你查看某个文件的内容时（比单纯用 shell 的 cat 更好：可分页、"
        "不会在 4000 字处被截断）。大文件用 offset/limit 翻页。"
    ),
    what_to_expect="文件指定行范围的文本，带行号和范围信息",
    failure_meaning="文件不存在、无权限、是机密文件、或不是文本文件",
    side_effect="read",
)
async def read_file(path: str, offset: int = 0, limit: int = DEFAULT_LINE_LIMIT) -> str:
    """读取一个文本文件（按行分页）。offset 从 0 起，limit 为最多读取的行数。"""
    deny = _deny_reason(path, for_write=False)
    if deny:
        return deny
    p = _resolved(path)
    if not p.exists():
        return f"文件不存在：{path}"
    if not p.is_file():
        return f"不是文件：{path}"
    try:
        raw = p.read_bytes()[:MAX_READ_BYTES]
    except Exception as e:
        return f"读取失败：{e}"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"不是 UTF-8 文本文件（{len(raw)} 字节），无法显示：{path}"
    lines = text.splitlines()
    total = len(lines)
    if offset < 0:
        offset = 0
    if limit <= 0:
        limit = DEFAULT_LINE_LIMIT
    window = lines[offset : offset + limit]
    numbered = "\n".join(f"{offset + i + 1}\t{ln}" for i, ln in enumerate(window))
    if len(numbered) > MAX_RETURN_CHARS:
        numbered = numbered[:MAX_RETURN_CHARS] + "\n…(本次输出已截断，请用 offset 继续)"
    end = offset + len(window)
    header = f"[{path}] 第 {offset + 1}-{end} 行 / 共 {total} 行"
    more = f"\n（还有 {total - end} 行，用 offset={end} 继续）" if end < total else ""
    return f"{header}\n{numbered}{more}"


@arcana.tool(
    when_to_use="当用户要你新建文件或完全覆盖一个文件的内容时",
    what_to_expect="写入成功的确认（路径和字节数）",
    failure_meaning="路径受保护、目录不存在或无权限",
    side_effect="write",
)
async def write_file(path: str, content: str) -> str:
    """写入（覆盖）一个文本文件。父目录必须已存在。"""
    deny = _deny_reason(path, for_write=True)
    if deny:
        return deny
    p = _resolved(path)
    if not p.parent.exists():
        return f"父目录不存在：{p.parent}"
    try:
        p.write_text(content, encoding="utf-8")
    except Exception as e:
        return f"写入失败：{e}"
    return f"已写入 {path}（{len(content.encode('utf-8'))} 字节）"


@arcana.tool(
    when_to_use=(
        "当用户要你修改文件里的某段文本时（把 old_string 替换成 new_string）。"
        "old_string 必须在文件中唯一出现，否则会拒绝以防误改。"
    ),
    what_to_expect="替换成功的确认",
    failure_meaning="路径受保护、old_string 未找到或不唯一",
    side_effect="write",
)
async def edit_file(path: str, old_string: str, new_string: str) -> str:
    """把文件中唯一出现的 old_string 替换为 new_string。"""
    deny = _deny_reason(path, for_write=True)
    if deny:
        return deny
    p = _resolved(path)
    if not p.exists() or not p.is_file():
        return f"文件不存在：{path}"
    if not old_string:
        return "old_string 不能为空"
    try:
        text = p.read_text(encoding="utf-8")
    except Exception as e:
        return f"读取失败：{e}"
    count = text.count(old_string)
    if count == 0:
        return "未找到 old_string，未修改"
    if count > 1:
        return f"old_string 出现了 {count} 次（必须唯一），未修改——请加上更多上下文"
    try:
        p.write_text(text.replace(old_string, new_string, 1), encoding="utf-8")
    except Exception as e:
        return f"写入失败：{e}"
    return f"已修改 {path}（替换 1 处）"
