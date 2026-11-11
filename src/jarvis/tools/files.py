"""File and folder tools, confined to the folders the user has approved.

Every tool resolves its paths through the sandbox twice: once when assessing
the call (so the approval dialog shows the real target) and again when
running (so nothing changes in between). Tools that change something report
what they verified afterwards instead of assuming success.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field
from send2trash import send2trash

from jarvis.safety import Access, Assessment, Risk
from jarvis.tools.base import Tool, ToolContext, ToolError, ToolInput, ToolResult

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache", ".tox"}
_MAX_SCAN = 60_000
_MAX_BACKUP_BYTES = 50 * 1024 * 1024
_ACCESS_HINT = " Use request_directory_access to ask the user for access to that folder."


# ----------------------------------------------------------------- shared bits


def _assess_path(ctx: ToolContext, raw: str, access: Access, base: Assessment) -> Assessment:
    """Fill in sandbox findings for one path on top of a tool's own assessment."""
    check = ctx.sandbox.check(raw, access)
    if not check.allowed:
        base.blocked = check.reason + _ACCESS_HINT
        return base
    if check.sensitive:
        base.risk = Risk.HIGH
        base.reasons.append(f"{check.path} looks like a credentials or other sensitive file.")
    return base


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _entry(p: Path) -> dict:
    try:
        st = p.lstat()
    except OSError:
        return {"path": str(p), "type": "unreadable"}
    kind = "link" if p.is_symlink() else "folder" if p.is_dir() else "file"
    out = {"path": str(p), "name": p.name, "type": kind, "modified": _fmt_time(st.st_mtime)}
    if kind == "file":
        out["size"] = st.st_size
    return out


def _backup(ctx: ToolContext, path: Path) -> str | None:
    """Copy a file aside before it is changed. Returns the backup's location, or None if skipped."""
    if not ctx.config.safety.backup_before_modify or not path.is_file():
        return None
    if path.stat().st_size > _MAX_BACKUP_BYTES:
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(path).lstrip("/"))[-150:]
    dest = ctx.services.paths.backups_dir / f"{stamp}-{safe}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)
    return str(dest)


def _is_binary(sample: bytes) -> bool:
    if b"\x00" in sample:
        return True
    try:
        sample.decode("utf-8")
        return False
    except UnicodeDecodeError as exc:
        # a multi-byte character cut off at the end of the sample is not evidence of binary data
        return exc.start < len(sample) - 4


def _walk(root: Path, include_hidden: bool):
    """Yield (directory, file names, folder names) without following links or entering bulky tool folders."""
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and (include_hidden or not d.startswith(".")))
        if not include_hidden:
            filenames = [f for f in filenames if not f.startswith(".")]
        scanned += len(filenames) + len(dirnames)
        yield Path(dirpath), filenames, dirnames
        if scanned > _MAX_SCAN:
            return


# ---------------------------------------------------------------- list / find


class ListDirectoryInput(ToolInput):
    path: str = Field(default=".", description="Folder to list. Relative paths start from the main approved folder.")
    show_hidden: bool = False
    limit: int = Field(default=200, ge=1, le=1000)


def _assess_list(a: ListDirectoryInput, ctx: ToolContext) -> Assessment:
    return _assess_path(ctx, a.path, Access.READ, Assessment(title=f"List {a.path}"))


async def _run_list(a: ListDirectoryInput, ctx: ToolContext) -> ToolResult:
    path = ctx.sandbox.resolve(a.path, Access.READ)
    if not path.exists():
        raise ToolError(f"{path} does not exist.")
    if not path.is_dir():
        raise ToolError(f"{path} is a file, not a folder.")
    items = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    if not a.show_hidden:
        items = [p for p in items if not p.name.startswith(".")]
    shown = [_entry(p) for p in items[: a.limit]]
    return ToolResult(
        {"folder": str(path), "count": len(items), "entries": shown, "truncated": len(items) > a.limit},
        f"{len(items)} item(s) in {path.name or path}",
    )


class FindFilesInput(ToolInput):
    name_contains: str = Field(default="", description="Text the file or folder name must contain (case-insensitive).")
    pattern: str = Field(default="", description="Glob for the name, e.g. '*.py' or 'report*.docx'.")
    root: str = Field(default="", description="Folder to search under. Empty searches every approved folder.")
    kind: Literal["any", "file", "folder"] = "any"
    modified_within_days: float = Field(default=0, ge=0, description="Only items changed this recently. 0 means any time.")
    include_hidden: bool = False
    limit: int = Field(default=40, ge=1, le=200)


def _assess_find(a: FindFilesInput, ctx: ToolContext) -> Assessment:
    title = f"Search for files matching '{a.name_contains or a.pattern or '*'}'"
    if a.root:
        return _assess_path(ctx, a.root, Access.READ, Assessment(title=title))
    return Assessment(title=title)


def _find(roots: list[Path], a: FindFilesInput) -> tuple[list[dict], bool]:
    needle = a.name_contains.lower()
    cutoff = time.time() - a.modified_within_days * 86400 if a.modified_within_days else 0.0
    hits: list[tuple[float, Path]] = []
    truncated = False
    for root in roots:
        if not root.is_dir():
            continue
        for directory, files, folders in _walk(root, a.include_hidden):
            names = (files if a.kind != "folder" else []) + (folders if a.kind != "file" else [])
            for name in names:
                if needle and needle not in name.lower():
                    continue
                if a.pattern and not fnmatch.fnmatch(name.lower(), a.pattern.lower()):
                    continue
                p = directory / name
                try:
                    mtime = p.lstat().st_mtime
                except OSError:
                    continue
                if cutoff and mtime < cutoff:
                    continue
                hits.append((mtime, p))
                if len(hits) >= 5000:
                    truncated = True
                    break
    hits.sort(key=lambda pair: pair[0], reverse=True)   # most recently changed first
    return [_entry(p) for _, p in hits[: a.limit]], truncated or len(hits) > a.limit


async def _run_find(a: FindFilesInput, ctx: ToolContext) -> ToolResult:
    if not (a.name_contains or a.pattern or a.modified_within_days):
        raise ToolError("Give name_contains, pattern or modified_within_days to search by.")
    roots = [ctx.sandbox.resolve(a.root, Access.READ)] if a.root else ctx.sandbox.approved + ctx.sandbox.read_only
    entries, truncated = await asyncio.to_thread(_find, roots, a)
    return ToolResult(
        {"matches": entries, "truncated": truncated, "searched": [str(r) for r in roots], "order": "most recently modified first"},
        f"{len(entries)} match(es)",
    )


class SearchContentsInput(ToolInput):
    query: str = Field(description="Text to look for inside files.")
    root: str = Field(default="", description="Folder to search under. Empty searches every approved folder.")
    glob: str = Field(default="", description="Only files whose name matches, e.g. '*.md'.")
    regex: bool = Field(default=False, description="Treat query as a regular expression.")
    case_sensitive: bool = False
    limit: int = Field(default=40, ge=1, le=200)


def _assess_search(a: SearchContentsInput, ctx: ToolContext) -> Assessment:
    base = Assessment(title=f"Search file contents for '{a.query[:60]}'", reads_private=True)
    return _assess_path(ctx, a.root, Access.READ, base) if a.root else base


def _search(roots: list[Path], a: SearchContentsInput, ctx: ToolContext) -> dict:
    flags = 0 if a.case_sensitive else re.IGNORECASE
    try:
        rx = re.compile(a.query if a.regex else re.escape(a.query), flags)
    except re.error as exc:
        raise ToolError(f"Invalid regular expression: {exc}") from exc
    matches: list[dict] = []
    files_scanned = skipped_sensitive = 0
    deadline = time.monotonic() + 20
    for root in roots:
        if not root.is_dir():
            continue
        for directory, files, _ in _walk(root, include_hidden=False):
            for name in files:
                if a.glob and not fnmatch.fnmatch(name.lower(), a.glob.lower()):
                    continue
                p = directory / name
                check = ctx.sandbox.check(p, Access.READ)     # also rejects links that lead outside
                if not check.allowed:
                    continue
                if check.sensitive:
                    skipped_sensitive += 1
                    continue
                try:
                    if p.stat().st_size > 2_000_000:
                        continue
                    with p.open("rb") as fh:
                        head = fh.read(2048)
                        if _is_binary(head):
                            continue
                        text = (head + fh.read()).decode("utf-8", errors="replace")
                except OSError:
                    continue
                files_scanned += 1
                for lineno, line in enumerate(text.splitlines(), 1):
                    if rx.search(line):
                        matches.append({"path": str(p), "line": lineno, "text": line.strip()[:240]})
                        if len(matches) >= a.limit:
                            return {"matches": matches, "truncated": True, "files_scanned": files_scanned}
                if time.monotonic() > deadline:
                    return {"matches": matches, "truncated": True, "files_scanned": files_scanned, "note": "stopped after 20 seconds"}
    out: dict = {"matches": matches, "truncated": False, "files_scanned": files_scanned}
    if skipped_sensitive:
        out["skipped_sensitive_files"] = skipped_sensitive
    return out


async def _run_search(a: SearchContentsInput, ctx: ToolContext) -> ToolResult:
    roots = [ctx.sandbox.resolve(a.root, Access.READ)] if a.root else ctx.sandbox.approved + ctx.sandbox.read_only
    result = await asyncio.to_thread(_search, roots, a, ctx)
    return ToolResult(result, f"{len(result['matches'])} matching line(s) in {result['files_scanned']} file(s)")


# ----------------------------------------------------------------------- read


class ReadFileInput(ToolInput):
    path: str
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=400, ge=1, le=2000)


def _assess_read(a: ReadFileInput, ctx: ToolContext) -> Assessment:
    return _assess_path(ctx, a.path, Access.READ, Assessment(title=f"Read {a.path}", reads_private=True))


async def _run_read(a: ReadFileInput, ctx: ToolContext) -> ToolResult:
    path = ctx.sandbox.resolve(a.path, Access.READ)
    if not path.exists():
        raise ToolError(f"{path} does not exist.")
    if path.is_dir():
        raise ToolError(f"{path} is a folder. Use list_directory.")
    size = path.stat().st_size
    limit = ctx.config.safety.max_file_read_bytes
    with path.open("rb") as fh:
        raw = fh.read(limit + 1)
    if _is_binary(raw[:4096]):
        raise ToolError(f"{path.name} is not a text file ({size} bytes); it cannot be read as text.")
    lines = raw[:limit].decode("utf-8", errors="replace").splitlines()
    chunk = lines[a.start_line - 1: a.start_line - 1 + a.max_lines]
    body = "\n".join(f"{a.start_line + i:>5}  {line}" for i, line in enumerate(chunk))
    more = size > limit or a.start_line - 1 + len(chunk) < len(lines)
    header = f"{path} ({size} bytes, showing lines {a.start_line}-{a.start_line + len(chunk) - 1} of {'at least ' if size > limit else ''}{len(lines)})"
    if more:
        header += " - more remains; call again with a higher start_line"
    return ToolResult(f"{header}\n{body}", f"read {len(chunk)} line(s) of {path.name}")


class FileInfoInput(ToolInput):
    path: str


def _assess_info(a: FileInfoInput, ctx: ToolContext) -> Assessment:
    return _assess_path(ctx, a.path, Access.READ, Assessment(title=f"Inspect {a.path}"))


async def _run_info(a: FileInfoInput, ctx: ToolContext) -> ToolResult:
    path = ctx.sandbox.resolve(a.path, Access.READ)
    if not path.exists():
        return ToolResult({"path": str(path), "exists": False}, f"{path.name} does not exist")
    info = _entry(path) | {"exists": True}
    if path.is_dir():
        try:
            info["items"] = sum(1 for _ in path.iterdir())
        except OSError:
            pass
    return ToolResult(info, f"{info['type']} {path.name}")


# ---------------------------------------------------------------------- write


class WriteFileInput(ToolInput):
    path: str
    content: str = Field(description="The complete text of the file.")
    overwrite: bool = Field(default=False, description="Replace the file if it already exists (a backup is kept).")


def _assess_write(a: WriteFileInput, ctx: ToolContext) -> Assessment:
    check = ctx.sandbox.check(a.path, Access.WRITE)
    exists = check.allowed and check.path.exists()
    base = Assessment(
        risk=Risk.MODIFY if exists else Risk.LOW,
        title=f"{'Overwrite' if exists else 'Create'} {check.path if check.allowed else a.path}",
        detail=f"{len(a.content)} characters\n\n{a.content[:600]}{'...' if len(a.content) > 600 else ''}",
        grant_key="files:write",
    )
    if exists and check.path.is_file() and check.path.stat().st_size > _MAX_BACKUP_BYTES:
        base.risk = Risk.HIGH
        base.reasons.append("The existing file is too large to back up first.")
    return _assess_path(ctx, a.path, Access.WRITE, base)


async def _run_write(a: WriteFileInput, ctx: ToolContext) -> ToolResult:
    path = ctx.sandbox.resolve(a.path, Access.WRITE)
    if path.is_dir():
        raise ToolError(f"{path} is a folder.")
    existed = path.exists()
    if existed and not a.overwrite:
        raise ToolError(f"{path} already exists. Pass overwrite=true to replace it, or use edit_file to change part of it.")
    backup = _backup(ctx, path) if existed else None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.jarvis-tmp")
    tmp.write_text(a.content, encoding="utf-8")
    os.replace(tmp, path)
    written = path.stat().st_size
    result = {"path": str(path), "action": "overwritten" if existed else "created", "bytes": written,
              "verified": path.read_text(encoding="utf-8") == a.content}
    if backup:
        result["backup"] = backup
    return ToolResult(result, f"{result['action']} {path.name} ({written} bytes)")


class EditFileInput(ToolInput):
    path: str
    old_text: str = Field(description="Exact text to replace, copied from the file.")
    new_text: str
    replace_all: bool = False


def _assess_edit(a: EditFileInput, ctx: ToolContext) -> Assessment:
    base = Assessment(
        risk=Risk.MODIFY, title=f"Edit {a.path}", grant_key="files:write",
        detail=f"Replace:\n{a.old_text[:400]}\n\nWith:\n{a.new_text[:400]}",
    )
    return _assess_path(ctx, a.path, Access.WRITE, base)


async def _run_edit(a: EditFileInput, ctx: ToolContext) -> ToolResult:
    path = ctx.sandbox.resolve(a.path, Access.WRITE)
    if not path.is_file():
        raise ToolError(f"{path} does not exist or is not a file.")
    if not a.old_text:
        raise ToolError("old_text must not be empty.")
    text = path.read_text(encoding="utf-8")
    count = text.count(a.old_text)
    if count == 0:
        raise ToolError("old_text was not found in the file. Read the file again and copy the text exactly.")
    if count > 1 and not a.replace_all:
        raise ToolError(f"old_text appears {count} times. Include more surrounding text to make it unique, or set replace_all=true.")
    backup = _backup(ctx, path)
    updated = text.replace(a.old_text, a.new_text) if a.replace_all else text.replace(a.old_text, a.new_text, 1)
    tmp = path.with_name(f".{path.name}.jarvis-tmp")
    tmp.write_text(updated, encoding="utf-8")
    shutil.copymode(path, tmp)
    os.replace(tmp, path)
    result = {"path": str(path), "replacements": count if a.replace_all else 1, "verified": path.read_text(encoding="utf-8") == updated}
    if backup:
        result["backup"] = backup
    return ToolResult(result, f"edited {path.name} ({result['replacements']} replacement(s))")


class CreateDirectoryInput(ToolInput):
    path: str


def _assess_mkdir(a: CreateDirectoryInput, ctx: ToolContext) -> Assessment:
    return _assess_path(ctx, a.path, Access.WRITE, Assessment(risk=Risk.LOW, title=f"Create folder {a.path}"))


async def _run_mkdir(a: CreateDirectoryInput, ctx: ToolContext) -> ToolResult:
    path = ctx.sandbox.resolve(a.path, Access.WRITE)
    if path.exists() and not path.is_dir():
        raise ToolError(f"{path} already exists and is a file.")
    existed = path.is_dir()
    path.mkdir(parents=True, exist_ok=True)
    return ToolResult(
        {"path": str(path), "action": "already existed" if existed else "created", "verified": path.is_dir()},
        f"folder {path.name} {'already existed' if existed else 'created'}",
    )


# --------------------------------------------------------------- move / copy


class MoveInput(ToolInput):
    source: str
    destination: str = Field(description="New path. To move into a folder, end with the new name inside that folder.")
    overwrite: bool = False


def _assess_move(a: MoveInput, ctx: ToolContext) -> Assessment:
    dst = ctx.sandbox.check(a.destination, Access.WRITE)
    replacing = dst.allowed and dst.path.exists() and a.overwrite
    base = Assessment(
        risk=Risk.HIGH if replacing else Risk.MODIFY,
        title=f"Move {a.source} to {a.destination}",
        grant_key="files:move",
    )
    if replacing:
        base.reasons.append(f"{dst.path} already exists and would be replaced.")
    base = _assess_path(ctx, a.source, Access.WRITE, base)
    return base if base.blocked else _assess_path(ctx, a.destination, Access.WRITE, base)


async def _run_move(a: MoveInput, ctx: ToolContext) -> ToolResult:
    src = ctx.sandbox.resolve(a.source, Access.WRITE)
    dst = ctx.sandbox.resolve(a.destination, Access.WRITE)
    if not src.exists():
        raise ToolError(f"{src} does not exist.")
    if dst.is_dir() and not src.is_dir():
        dst = dst / src.name      # "move the file into that folder"
        ctx.sandbox.resolve(dst, Access.WRITE)
    if dst.exists():
        if not a.overwrite:
            raise ToolError(f"{dst} already exists. Choose another name, or pass overwrite=true.")
        if dst.is_dir():
            raise ToolError(f"{dst} is an existing folder; it will not be replaced.")
        _backup(ctx, dst)
        dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return ToolResult(
        {"from": str(src), "to": str(dst), "verified": dst.exists() and not src.exists()},
        f"moved {src.name} to {dst}",
    )


class CopyInput(ToolInput):
    source: str
    destination: str


def _assess_copy(a: CopyInput, ctx: ToolContext) -> Assessment:
    base = Assessment(risk=Risk.LOW, title=f"Copy {a.source} to {a.destination}", reads_private=True)
    base = _assess_path(ctx, a.source, Access.READ, base)
    return base if base.blocked else _assess_path(ctx, a.destination, Access.WRITE, base)


async def _run_copy(a: CopyInput, ctx: ToolContext) -> ToolResult:
    src = ctx.sandbox.resolve(a.source, Access.READ)
    dst = ctx.sandbox.resolve(a.destination, Access.WRITE)
    if not src.exists():
        raise ToolError(f"{src} does not exist.")
    if dst.is_dir() and not src.is_dir():
        dst = dst / src.name
        ctx.sandbox.resolve(dst, Access.WRITE)
    if dst.exists():
        raise ToolError(f"{dst} already exists; a copy never replaces an existing item.")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        await asyncio.to_thread(shutil.copytree, src, dst, symlinks=True)
    else:
        await asyncio.to_thread(shutil.copy2, src, dst)
    return ToolResult({"from": str(src), "to": str(dst), "verified": dst.exists()}, f"copied {src.name} to {dst}")


# --------------------------------------------------------------------- delete


class DeleteInput(ToolInput):
    path: str
    permanent: bool = Field(default=False, description="Erase immediately instead of moving to the trash. Cannot be undone.")


def _assess_delete(a: DeleteInput, ctx: ToolContext) -> Assessment:
    if a.permanent:
        base = Assessment(
            risk=Risk.HIGH, title=f"PERMANENTLY delete {a.path}",
            reasons=["Permanent deletion cannot be undone."],
        )
    else:
        base = Assessment(risk=Risk.MODIFY, title=f"Move {a.path} to the trash", grant_key="files:trash")
    check = ctx.sandbox.check(a.path, Access.WRITE)
    if check.allowed and check.path in ctx.sandbox.approved:
        base.blocked = "That is one of the approved top-level folders itself; it will not be deleted."
        return base
    if check.allowed and check.path.is_dir():
        try:
            count = sum(len(files) + len(dirs) for _, dirs, files in os.walk(check.path))
        except OSError:
            count = 0
        base.detail = f"Folder containing {count} item(s)."
    return _assess_path(ctx, a.path, Access.WRITE, base)


async def _run_delete(a: DeleteInput, ctx: ToolContext) -> ToolResult:
    path = ctx.sandbox.resolve(a.path, Access.WRITE)
    if not path.exists() and not path.is_symlink():
        raise ToolError(f"{path} does not exist.")
    if a.permanent:
        if path.is_dir() and not path.is_symlink():
            await asyncio.to_thread(shutil.rmtree, path)
        else:
            path.unlink()
        how = "permanently deleted"
    else:
        try:
            await asyncio.to_thread(send2trash, str(path))
        except Exception as exc:  # noqa: BLE001 - no trash on this filesystem, headless session, etc.
            raise ToolError(
                f"Could not move {path.name} to the trash ({exc}). It was NOT deleted. "
                "Permanent deletion is possible with permanent=true, which needs the user's explicit approval."
            ) from exc
        how = "moved to the trash"
    return ToolResult({"path": str(path), "action": how, "verified": not path.exists()}, f"{path.name} {how}")


# ------------------------------------------------------------- access request


class RequestAccessInput(ToolInput):
    path: str = Field(description="Folder Jarvis needs access to.")
    mode: Literal["read", "write"] = Field(default="read", description="'read' to look only, 'write' to also change files.")
    reason: str = Field(description="One sentence telling the user why access is needed.")


def _assess_access(a: RequestAccessInput, ctx: ToolContext) -> Assessment:
    try:
        path = ctx.sandbox.normalize(a.path)
    except Exception as exc:  # noqa: BLE001
        return Assessment(blocked=str(exc))
    base = Assessment(
        risk=Risk.HIGH,
        title=f"Grant Jarvis {'read and write' if a.mode == 'write' else 'read-only'} access to {path}",
        detail=f"Reason given: {a.reason}\n\nThis is saved in your settings and stays in effect until you remove it.",
        reasons=["This widens what the assistant can reach on your computer."],
    )
    if path == Path("/") or len(path.parts) <= 2 and path != ctx.sandbox.home:
        base.blocked = "Access to the filesystem root or a top-level system folder is never granted."
    elif any(path == p or p in path.parents or path in p.parents for p in ctx.services.paths.protected()):
        base.blocked = "That would expose the assistant's own settings and data."
    elif not path.is_dir():
        base.blocked = f"{path} is not an existing folder."
    elif ctx.sandbox.is_sensitive(path):
        base.reasons.append("This folder holds credentials or other sensitive data.")
    if path == ctx.sandbox.home:
        base.reasons.append("This is your entire home folder, including application data.")
    return base


async def _run_access(a: RequestAccessInput, ctx: ToolContext) -> ToolResult:
    path = ctx.sandbox.normalize(a.path)
    safety = ctx.config.safety
    target = safety.approved_dirs if a.mode == "write" else safety.read_only_dirs
    entry = str(path)
    if entry not in target:
        target.append(entry)
    if a.mode == "write" and entry in safety.read_only_dirs:
        safety.read_only_dirs.remove(entry)
    ctx.services.save_config()
    ctx.services.refresh()
    return ToolResult({"path": entry, "access": a.mode, "granted": True}, f"access to {path.name} granted ({a.mode})")


# ------------------------------------------------------------------- registry

FILE_TOOLS: list[Tool] = [
    Tool("list_directory", "List the files and folders inside a folder.", ListDirectoryInput, _run_list, _assess_list, group="files"),
    Tool(
        "find_files",
        "Find files or folders by name across the approved folders, newest first. Use this to locate a project, "
        "a document, or 'the most recent version' of something before opening or reading it.",
        FindFilesInput, _run_find, _assess_find, group="files",
    ),
    Tool("search_file_contents", "Search inside text files for a phrase or regular expression.", SearchContentsInput, _run_search, _assess_search, group="files"),
    Tool("read_file", "Read a text file (code, notes, CSV, Markdown...). Returns numbered lines.", ReadFileInput, _run_read, _assess_read, group="files"),
    Tool("file_info", "Check whether a path exists and get its type, size and modification time.", FileInfoInput, _run_info, _assess_info, group="files"),
    Tool(
        "write_file",
        "Create a text file, or replace one entirely when overwrite=true. To change part of an existing file use edit_file.",
        WriteFileInput, _run_write, _assess_write, group="files",
    ),
    Tool("edit_file", "Replace an exact piece of text inside an existing file. A backup is kept.", EditFileInput, _run_edit, _assess_edit, group="files"),
    Tool("create_directory", "Create a folder (and any missing parent folders).", CreateDirectoryInput, _run_mkdir, _assess_mkdir, group="files"),
    Tool("move_path", "Move or rename a file or folder.", MoveInput, _run_move, _assess_move, group="files"),
    Tool("copy_path", "Copy a file or folder to a new location. Never replaces anything.", CopyInput, _run_copy, _assess_copy, timeout=300, group="files"),
    Tool(
        "delete_path",
        "Delete a file or folder. By default it goes to the trash and can be restored; permanent=true erases it for good.",
        DeleteInput, _run_delete, _assess_delete, timeout=300, group="files",
    ),
    Tool(
        "request_directory_access",
        "Ask the user to let Jarvis reach a folder outside the currently approved ones. Call this when a path is "
        "refused as outside the approved folders and the user's request genuinely needs it.",
        RequestAccessInput, _run_access, _assess_access, group="files",
    ),
]
