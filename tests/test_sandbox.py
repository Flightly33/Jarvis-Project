from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.safety.sandbox import Access, PathSandbox, SandboxError


def test_allows_paths_inside_approved_folder(sandbox: PathSandbox, workspace: Path):
    check = sandbox.check(workspace / "notes" / "todo.txt", Access.WRITE)
    assert check.allowed and check.writable and not check.sensitive


def test_relative_paths_resolve_against_the_default_folder(sandbox: PathSandbox, workspace: Path):
    assert sandbox.resolve("report.md", Access.WRITE) == workspace / "report.md"


@pytest.mark.parametrize(
    "attempt",
    ["../secret.txt", "notes/../../secret.txt", "/etc/passwd", "~/.bashrc", "/", "~", "../../../../etc/shadow"],
)
def test_rejects_escapes(sandbox: PathSandbox, attempt: str):
    check = sandbox.check(attempt, Access.READ)
    assert not check.allowed
    with pytest.raises(SandboxError):
        sandbox.resolve(attempt, Access.READ)


def test_symlink_pointing_outside_is_rejected(sandbox: PathSandbox, workspace: Path, home: Path):
    outside = home / "private.txt"
    outside.write_text("secret")
    link = workspace / "innocent.txt"
    link.symlink_to(outside)
    assert not sandbox.check(link, Access.READ).allowed


def test_symlinked_directory_escape_is_rejected(sandbox: PathSandbox, workspace: Path, home: Path):
    (home / "elsewhere").mkdir()
    (workspace / "shortcut").symlink_to(home / "elsewhere", target_is_directory=True)
    assert not sandbox.check(workspace / "shortcut" / "new.txt", Access.WRITE).allowed


def test_read_only_folder_cannot_be_written(sandbox: PathSandbox, home: Path):
    target = home / "Documents" / "essay.docx"
    assert sandbox.check(target, Access.READ).allowed
    write = sandbox.check(target, Access.WRITE)
    assert not write.allowed and "reading only" in write.reason


def test_assistants_own_folders_are_always_off_limits(home: Path, paths):
    # Even if the user approves a parent folder, Jarvis's settings and data stay protected.
    box = PathSandbox(approved=[paths.config_dir.parent], protected=paths.protected(), home=home)
    assert not box.check(paths.config_file, Access.READ).allowed
    assert not box.check(paths.database, Access.WRITE).allowed


@pytest.mark.parametrize(
    "name", [".env", ".env.production", "id_rsa", "id_ed25519.pub", "server.pem", "vault.kdbx", "credentials.json", ".netrc"]
)
def test_secret_looking_files_are_flagged_sensitive(sandbox: PathSandbox, workspace: Path, name: str):
    check = sandbox.check(workspace / "project" / name, Access.READ)
    assert check.allowed and check.sensitive


def test_credential_folders_are_sensitive_even_when_approved(home: Path):
    box = PathSandbox(approved=[home], home=home)
    assert box.check(home / ".ssh" / "config", Access.READ).sensitive
    assert box.check(home / ".claude" / ".credentials.json", Access.READ).sensitive
    assert not box.check(home / "Music" / "song.mp3", Access.READ).sensitive


def test_empty_and_null_paths_are_rejected(sandbox: PathSandbox):
    assert not sandbox.check("", Access.READ).allowed
    assert not sandbox.check("a\x00b", Access.READ).allowed


def test_sibling_folder_with_shared_prefix_is_not_inside(home: Path, workspace: Path):
    # ~/Jarvis-private must not count as being inside ~/Jarvis
    (home / "Jarvis-private").mkdir()
    box = PathSandbox(approved=[workspace], home=home)
    assert not box.check(home / "Jarvis-private" / "x.txt", Access.READ).allowed
