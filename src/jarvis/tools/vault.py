"""Jarvis's long-term brain: an Obsidian vault (a folder of Markdown notes) on this computer.

Jarvis searches and reads the notes to recall things, and writes what is worth keeping as
notes that link to each other, so the user can browse and edit the same brain in Obsidian.
Everything stays inside the vault folder; Obsidian's own settings folder is left alone.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path

from pydantic import Field

from jarvis.safety import Assessment, Risk
from jarvis.tools.base import Tool, ToolContext, ToolError, ToolInput, ToolResult, Trimmed

HOME_NOTE = "Jarvis/Home.md"
MEMORY_NOTE = "Jarvis/Memory.md"
_MAX_NOTE = 200_000
_STARTER = """# Jarvis

This vault is Jarvis's long-term brain. Jarvis reads these notes to recall things and adds notes as it learns.
You can read, edit, link and reorganise everything here in Obsidian - Jarvis will see your changes.

- [[Memory]] - short facts Jarvis was asked to remember
"""


def vault_root(ctx: ToolContext) -> Path:
    raw = ctx.config.notes.vault.strip()
    if not raw:
        raise ToolError("No Obsidian vault is connected. The user can choose one under Settings > Obsidian brain.")
    root = Path(os.path.expandvars(raw)).expanduser()
    if not root.is_dir():
        raise ToolError(f"The vault folder {root} does not exist any more. The user can choose it again under Settings > Obsidian brain.")
    return root.resolve()


def prepare_vault(folder: str) -> Path:
    """Check a folder chosen as the vault, create it if needed, and give it a starting note."""
    root = Path(os.path.expandvars(folder.strip())).expanduser()
    if not root.is_absolute():
        raise ValueError("Give the vault's full folder path, e.g. C:\\Users\\you\\Documents\\Jarvis Brain.")
    if root.resolve() in {Path(root.anchor).resolve(), Path.home().resolve()}:
        raise ValueError("Choose a folder just for notes, not a whole drive or your whole user folder.")
    root.mkdir(parents=True, exist_ok=True)
    home = root / HOME_NOTE
    if not home.exists():
        home.parent.mkdir(parents=True, exist_ok=True)
        home.write_text(_STARTER, encoding="utf-8", newline="")
    return root


def _note_path(root: Path, note: str, must_exist: bool) -> Path:
    name = note.strip().strip("[]").replace("\\", "/").strip("/")
    if not name:
        raise ToolError("Give the note's name.")
    if not name.lower().endswith(".md"):
        name += ".md"
    path = (root / name).resolve()
    if root != path and root not in path.parents:
        raise ToolError("That is outside the vault.")
    if any(part.startswith(".") for part in path.relative_to(root).parts):
        raise ToolError("Hidden folders such as .obsidian hold Obsidian's own settings and are left alone.")
    if must_exist and not path.is_file():
        # Obsidian links name a note without its folder, so look for it anywhere in the vault
        wanted = Path(name).name.lower()
        matches = [p for p in _notes(root) if p.name.lower() == wanted]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise ToolError("Several notes have that name: " + ", ".join(str(m.relative_to(root)).replace("\\", "/") for m in matches[:8]))
        raise ToolError(f"There is no note called '{note}'. Use vault_search to find it.")
    return path


def _notes(root: Path) -> list[Path]:
    found = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        found += [Path(folder) / f for f in files if f.lower().endswith(".md")]
    return found


def _rel(root: Path, path: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


def _search(root: Path, query: str, limit: int) -> list[dict]:
    words = [w for w in re.findall(r"[\w'-]+", query.lower()) if len(w) > 1]
    if not words:
        recent = sorted(_notes(root), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
        return [{"note": _rel(root, p), "changed": time.strftime("%Y-%m-%d", time.localtime(p.stat().st_mtime))} for p in recent]
    scored = []
    for path in _notes(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:_MAX_NOTE]
        except OSError:
            continue
        low, name = text.lower(), _rel(root, path).lower()
        score = sum(low.count(w) + 6 * name.count(w) for w in words)
        if not all(w in low or w in name for w in words):
            score //= 4                      # notes that have every word come first
        if score:
            at = min((low.find(w) for w in words if w in low), default=0)
            snippet = " ".join(text[max(0, at - 80):at + 220].split())
            scored.append((score, path.stat().st_mtime, {"note": _rel(root, path), "excerpt": snippet}))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [row[2] for row in scored[:limit]]


def append_memory(ctx: ToolContext, content: str, category: str) -> None:
    """Mirror a remembered fact into the vault, so the brain in Obsidian shows it too."""
    if not ctx.config.notes.vault.strip():
        return
    try:
        path = vault_root(ctx) / MEMORY_NOTE
        path.parent.mkdir(parents=True, exist_ok=True)
        start = "" if path.exists() else "# Memory\n\nShort facts Jarvis was asked to remember.\n\n"
        with path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(f"{start}- {time.strftime('%Y-%m-%d')} #{re.sub(r'[^a-z0-9]+', '-', category.lower()).strip('-') or 'general'} {' '.join(content.split())}\n")
    except (ToolError, OSError):
        pass                                  # the fact is still kept in Jarvis's own store


class VaultSearchInput(ToolInput):
    query: str = Field(default="", max_length=200, description="Words to look for in note names and text. Empty lists the most recently changed notes.")
    limit: int = Field(default=10, ge=1, le=40)


def _assess_search(a: VaultSearchInput, ctx: ToolContext) -> Assessment:
    return Assessment(title="Search your Obsidian notes", reads_private=True)


async def _run_search(a: VaultSearchInput, ctx: ToolContext) -> ToolResult:
    root = vault_root(ctx)
    found = await asyncio.to_thread(_search, root, a.query, a.limit)
    return ToolResult({"notes": found}, f"{len(found)} note(s) found")


class VaultReadInput(ToolInput):
    note: Trimmed = Field(description="The note's name or path inside the vault, e.g. 'Projects/Robotics' or just 'Robotics'.")


def _assess_read(a: VaultReadInput, ctx: ToolContext) -> Assessment:
    return Assessment(title=f"Read the note {a.note[:80]}", reads_private=True)


async def _run_read(a: VaultReadInput, ctx: ToolContext) -> ToolResult:
    root = vault_root(ctx)
    path = _note_path(root, a.note, must_exist=True)
    text = path.read_text(encoding="utf-8", errors="replace")
    cut = len(text) > ctx.config.safety.max_file_read_bytes
    return ToolResult(f"Note: {_rel(root, path)}\n\n{text[:ctx.config.safety.max_file_read_bytes]}" + ("\n\n(The note is longer; this is its beginning.)" if cut else ""),
                      f"read {path.stem}")


class VaultWriteInput(ToolInput):
    note: Trimmed = Field(description="The note's name or path inside the vault, e.g. 'Projects/Robotics Tournament'. Folders are created as needed.")
    content: str = Field(min_length=1, max_length=_MAX_NOTE, description="Markdown. Link related notes with [[Note name]] and label with #tags.")
    mode: Trimmed = Field(default="append", pattern="^(append|replace)$", description="'append' adds to the end (or creates the note); 'replace' rewrites the whole note.")


def _assess_write(a: VaultWriteInput, ctx: ToolContext) -> Assessment:
    try:
        exists = _note_path(vault_root(ctx), a.note, must_exist=False).exists()
    except ToolError:
        exists = False
    if a.mode == "replace" and exists:
        return Assessment(risk=Risk.MODIFY, title=f"Rewrite the note {a.note[:80]}", detail=a.content[:600], grant_key="vault-rewrite")
    return Assessment(risk=Risk.LOW, title=f"{'Add to' if exists else 'Create'} the note {a.note[:80]}")


async def _run_write(a: VaultWriteInput, ctx: ToolContext) -> ToolResult:
    root = vault_root(ctx)
    path = _note_path(root, a.note, must_exist=False)
    existed = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if a.mode == "replace" or not existed:
        if existed and ctx.config.safety.backup_before_modify:
            backups = ctx.services.paths.data_dir / "vault-backups"
            backups.mkdir(parents=True, exist_ok=True)
            (backups / f"{time.strftime('%Y%m%d-%H%M%S')}-{path.name}").write_bytes(path.read_bytes())
        tmp = path.with_name(path.name + ".jarvis-tmp")
        tmp.write_text(a.content if a.content.endswith("\n") else a.content + "\n", encoding="utf-8", newline="")
        os.replace(tmp, path)
    else:
        current = path.read_text(encoding="utf-8", errors="replace")
        with path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(("" if current.endswith("\n") or not current else "\n") + a.content + ("" if a.content.endswith("\n") else "\n"))
    done = "rewrote" if existed and a.mode == "replace" else "added to" if existed else "created"
    return ToolResult({"note": _rel(root, path), "result": done}, f"{done} {path.stem}")


VAULT_TOOLS: list[Tool] = [
    Tool(
        "vault_search",
        "Search Jarvis's long-term brain - the user's Obsidian vault - by words in note names and text. Use it to recall what is known "
        "about a project, person, class or plan before saying you do not know.",
        VaultSearchInput, _run_search, _assess_search, group="brain",
    ),
    Tool("vault_read", "Read a note from the Obsidian vault.", VaultReadInput, _run_read, _assess_read, group="brain"),
    Tool(
        "vault_write",
        "Save knowledge worth keeping as a note in the Obsidian vault: create a note, add to one, or rewrite one. Write clear Markdown, "
        "give notes plain descriptive names, link related notes with [[Note name]] and add #tags.",
        VaultWriteInput, _run_write, _assess_write, group="brain",
    ),
]
