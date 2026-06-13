"""Tests for tools/files.py — read/write/edit + the path deny policy."""

from __future__ import annotations

import pytest

from tools import files


# ---------------------------------------------------------------------------
# Path deny policy (the security-critical part)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "config.yaml",
        ".identity/daemon.ed25519.key",
        ".auth/token",
        ".chat_history.db",
        ".reminders.db",
        ".faces/faces.json",
        ".voice_prefs/prefs.json",
        ".tool_audit/x.json",
        "/etc/passwd",
        "~/.ssh/id_rsa",
        "~/.aws/credentials",
        "~/.gnupg/secring.gpg",
    ],
)
def test_secret_paths_denied_for_read(path):
    assert files._deny_reason(path, for_write=False) is not None


def test_dotdot_cannot_escape_to_secret():
    # A relative path that resolves into config.yaml is still caught because
    # the check runs on the resolved absolute path.
    assert files._deny_reason("foo/../config.yaml", for_write=False) is not None


@pytest.mark.parametrize(
    "path,for_write",
    [
        # macOS case-insensitive FS: case-variant paths reach the real
        # lower-case secret, so the policy must match casefolded.
        ("Config.yaml", False),
        ("CONFIG.YAML", False),
        (".Identity/daemon.ed25519.key", True),
        (".IDENTITY/daemon.ed25519.key", False),
        ("Soul.md", True),
        ("SOUL.MD", True),
        ("~/.SSH/id_rsa", False),
        ("~/.AWS/credentials", False),
    ],
)
def test_case_variant_paths_denied(path, for_write):
    assert files._deny_reason(path, for_write=for_write) is not None


def test_var_db_denied_both_symlink_forms():
    assert files._deny_reason("/var/db/dslocal/x", for_write=True) is not None
    assert files._deny_reason("/private/var/db/dslocal/x", for_write=False) is not None


@pytest.mark.parametrize(
    "path",
    [
        ".chat_history.db-wal",
        ".chat_history.db-shm",
        ".reminders.db-wal",
        ".todos.db-shm",
        ".todos.db-journal",
    ],
)
def test_db_sidecar_files_denied(path):
    # write_file to a -wal/-shm sidecar would corrupt the live DB.
    assert files._deny_reason(path, for_write=True) is not None
    assert files._deny_reason(path, for_write=False) is not None


def test_soul_md_read_ok_write_denied():
    assert files._deny_reason("soul.md", for_write=False) is None
    assert files._deny_reason("soul.md", for_write=True) is not None


def test_ordinary_path_allowed(tmp_path):
    assert files._deny_reason(str(tmp_path / "notes.txt"), for_write=True) is None


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


async def test_read_file_paginates(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 11)))  # 10 lines
    out = await files.read_file(str(f), offset=0, limit=3)
    assert "共 10 行" in out
    assert "1\tline1" in out and "3\tline3" in out
    assert "line4" not in out
    assert "offset=3" in out  # tells the model how to continue


async def test_read_file_offset(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 6)))
    out = await files.read_file(str(f), offset=3, limit=10)
    assert "4\tline4" in out and "5\tline5" in out
    assert "line1" not in out


async def test_read_missing_file(tmp_path):
    assert "不存在" in await files.read_file(str(tmp_path / "nope.txt"))


async def test_read_secret_refused():
    out = await files.read_file("config.yaml")
    assert "机密" in out


async def test_read_non_utf8(tmp_path):
    f = tmp_path / "bin.dat"
    f.write_bytes(b"\xff\xfe\x00\x01binary")
    assert "UTF-8" in await files.read_file(str(f))


# ---------------------------------------------------------------------------
# write_file / edit_file
# ---------------------------------------------------------------------------


async def test_write_file_round_trip(tmp_path):
    f = tmp_path / "out.txt"
    out = await files.write_file(str(f), "hello world")
    assert "已写入" in out
    assert f.read_text() == "hello world"


async def test_write_refuses_secret():
    assert "机密" in await files.write_file("config.yaml", "pwned")


async def test_write_refuses_soul():
    out = await files.write_file("soul.md", "I am evil now")
    assert "soul.md" in out and "审核" in out


async def test_write_missing_parent(tmp_path):
    out = await files.write_file(str(tmp_path / "no" / "such" / "f.txt"), "x")
    assert "父目录不存在" in out


async def test_edit_file_unique_replace(tmp_path):
    f = tmp_path / "e.txt"
    f.write_text("alpha beta gamma")
    out = await files.edit_file(str(f), "beta", "DELTA")
    assert "替换 1 处" in out
    assert f.read_text() == "alpha DELTA gamma"


async def test_edit_file_refuses_nonunique(tmp_path):
    f = tmp_path / "e.txt"
    f.write_text("x x x")
    out = await files.edit_file(str(f), "x", "y")
    assert "唯一" in out
    assert f.read_text() == "x x x"  # unchanged


async def test_edit_file_missing_old_string(tmp_path):
    f = tmp_path / "e.txt"
    f.write_text("hello")
    out = await files.edit_file(str(f), "absent", "y")
    assert "未找到" in out
