"""Confines file access to the folders the user has approved.

Every path a model supplies is untrusted. It is expanded, made absolute and
symlink-resolved *before* the containment check, so `..`, absolute paths and
symlinks that point outside an approved folder are all caught.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Access(str, Enum):
    READ = "read"
    WRITE = "write"


class SandboxError(Exception):
    """A path was outside what the user has approved."""


# Credential stores and private application data. Access is never automatic:
# it always goes to the user as a high-risk approval.
SENSITIVE_DIRS = (
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".claude", ".password-store",
    ".config/gcloud", ".config/gh", ".config/google-chrome", ".config/chromium", ".config/BraveSoftware",
    ".config/microsoft-edge", ".mozilla", ".thunderbird", ".local/share/keyrings", ".config/Bitwarden",
    ".config/1Password", ".config/op", ".local/share/Trash",
    # Windows
    "AppData/Local/Google/Chrome/User Data", "AppData/Local/Microsoft/Edge/User Data", "AppData/Roaming/Mozilla",
    "AppData/Local/BraveSoftware", "AppData/Local/Microsoft/Credentials", "AppData/Roaming/Microsoft/Credentials",
    "AppData/Roaming/Microsoft/Protect", "AppData/Local/Microsoft/Vault", "AppData/Roaming/gcloud",
)
SENSITIVE_FILE_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.kdbx", "*.keystore", "*.jks", "*.ovpn",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".netrc", ".pgpass", ".git-credentials", ".npmrc",
    ".pypirc", ".htpasswd", "credentials", "credentials.*", "secrets", "secrets.*", "secret.*",
    "wallet.dat", ".claude.json", "*.gpg", "shadow", "token.json", "client_secret*.json",
)


@dataclass(frozen=True)
class PathCheck:
    path: Path
    allowed: bool
    writable: bool
    sensitive: bool
    reason: str = ""


class PathSandbox:
    def __init__(
        self,
        approved: list[Path],
        read_only: list[Path] | None = None,
        protected: list[Path] | None = None,
        home: Path | None = None,
    ) -> None:
        self.home = (home or Path.home()).resolve()
        self._approved = _unique(_resolve_root(p) for p in approved)
        self._read_only = _unique(_resolve_root(p) for p in (read_only or []))
        self._protected = _unique(_resolve_root(p) for p in (protected or []))

    # ------------------------------------------------------------------ roots

    @property
    def approved(self) -> list[Path]:
        return list(self._approved)

    @property
    def read_only(self) -> list[Path]:
        return list(self._read_only)

    @property
    def default_dir(self) -> Path:
        """Where relative paths and commands start from."""
        for root in self._approved + self._read_only:
            if root.is_dir():
                return root
        return self._approved[0] if self._approved else self.home

    def display(self, path: Path | str) -> str:
        """A path as a person would want to read it: relative to the home folder, middle elided if long."""
        p = Path(path)
        try:
            text = "~/" + p.relative_to(self.home).as_posix()
        except ValueError:
            text = str(p)
        parts = text.replace("\\", "/").split("/")
        if len(text) > 60 and len(parts) > 4:
            text = "/".join([*parts[:2], "…", *parts[-2:]])
        return text

    def describe(self) -> str:
        lines = [f"read/write: {p}" for p in self._approved] + [f"read-only: {p}" for p in self._read_only]
        return "\n".join(lines) if lines else "(no folders approved yet)"

    # ----------------------------------------------------------------- checks

    def normalize(self, raw: str | os.PathLike[str], base: Path | None = None) -> Path:
        text = os.fspath(raw).strip()
        if not text:
            raise SandboxError("an empty path was given")
        if "\x00" in text:
            raise SandboxError("the path contains a null byte")
        p = Path(os.path.expanduser(text))
        if not p.is_absolute():
            p = (base or self.default_dir) / p
        # resolve() follows symlinks on every component that exists and tidies the rest.
        return p.resolve(strict=False)

    def check(self, raw: str | os.PathLike[str], access: Access, base: Path | None = None) -> PathCheck:
        try:
            path = self.normalize(raw, base)
        except SandboxError as exc:
            return PathCheck(Path(os.fspath(raw) or "."), False, False, False, str(exc))
        for root in self._protected:
            if _within(path, root):
                return PathCheck(path, False, False, True, "that location holds the assistant's own settings and data")
        sensitive = self.is_sensitive(path)
        if any(_within(path, root) for root in self._approved):
            return PathCheck(path, True, True, sensitive)
        if any(_within(path, root) for root in self._read_only):
            if access is Access.WRITE:
                return PathCheck(path, False, False, sensitive, f"{path} is in a folder approved for reading only")
            return PathCheck(path, True, False, sensitive)
        return PathCheck(path, False, False, sensitive, f"{path} is outside the approved folders")

    def resolve(self, raw: str | os.PathLike[str], access: Access, base: Path | None = None) -> Path:
        result = self.check(raw, access, base)
        if not result.allowed:
            raise SandboxError(result.reason)
        return result.path

    def is_sensitive(self, path: Path) -> bool:
        for rel in SENSITIVE_DIRS:
            if _within(path, self.home / rel):
                return True
        name = path.name
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in SENSITIVE_FILE_PATTERNS):
            return True
        # A sensitive folder nested anywhere (e.g. a project's own .ssh or .aws directory).
        return any(part in {".ssh", ".gnupg", ".aws"} for part in path.parts)

    def contains(self, path: Path) -> bool:
        return any(_within(path, root) for root in self._approved + self._read_only)


def _resolve_root(p: Path) -> Path:
    return Path(os.path.expanduser(os.fspath(p))).resolve(strict=False)


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _unique(paths) -> list[Path]:
    seen: list[Path] = []
    for p in paths:
        if p not in seen:
            seen.append(p)
    return seen
