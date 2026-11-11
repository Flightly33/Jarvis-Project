"""Where Jarvis keeps its own files (XDG base directories on Linux).

`JARVIS_HOME` relocates everything under one directory, which the tests and
portable installs use.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    config_dir: Path
    data_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def secrets_file(self) -> Path:
        return self.config_dir / "secrets.toml"

    @property
    def database(self) -> Path:
        return self.data_dir / "jarvis.sqlite3"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def screenshots_dir(self) -> Path:
        return self.data_dir / "screenshots"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def claude_workspace(self) -> Path:
        """Working directory handed to Claude Code, kept apart from the user's projects."""
        return self.data_dir / "claude-workspace"

    def ensure(self) -> None:
        for d in (
            self.config_dir,
            self.data_dir,
            self.models_dir,
            self.backups_dir,
            self.screenshots_dir,
            self.logs_dir,
            self.claude_workspace,
        ):
            d.mkdir(parents=True, exist_ok=True)
        # Config may hold account details later on; keep it private to the user.
        for d in (self.config_dir, self.data_dir):
            try:
                d.chmod(0o700)
            except OSError:
                pass

    def protected(self) -> list[Path]:
        """Directories the assistant's own file tools must never touch."""
        return [self.config_dir, self.data_dir]


def default_paths() -> AppPaths:
    home_override = os.environ.get("JARVIS_HOME")
    if home_override:
        base = Path(home_override).expanduser()
        return AppPaths(config_dir=base / "config", data_dir=base / "data")
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return AppPaths(config_dir=config_home / "jarvis", data_dir=data_home / "jarvis")
