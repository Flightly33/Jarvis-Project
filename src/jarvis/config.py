"""Typed configuration, stored as TOML in the user's config directory.

Secrets (API keys) never live in this file: they come from environment
variables or from a separate `secrets.toml` that is created with 0600
permissions and is never shown back through the UI.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

import tomli_w
from pydantic import BaseModel, ConfigDict, Field, field_validator

from jarvis.paths import AppPaths

Autonomy = Literal["cautious", "balanced", "trusting"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class _Section(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)


class UserConfig(_Section):
    name: str = ""
    honorific: str = Field(default="", description='How to address you, e.g. "sir". Empty for none.')


class BrainConfig(_Section):
    backend: Literal["claude", "openai", "demo"] = "claude"
    session_idle_minutes: int = Field(default=20, ge=1, description="Start a fresh conversation after this much silence.")
    max_steps: int = Field(default=25, ge=1, le=100, description="Tool round-trips allowed in one request.")


class ClaudeConfig(_Section):
    """Reasoning through Claude Code's programmatic mode (your Claude subscription)."""

    model: str = "haiku"
    effort: Effort | None = "low"
    auth: Literal["subscription", "api_key"] = "subscription"
    require_subscription: bool = Field(
        default=True,
        description="Refuse to run if Claude Code reports pay-per-use API-key billing instead of the subscription login.",
    )
    web: bool = Field(default=True, description="Allow Claude Code's built-in web search and fetch tools.")
    max_turns: int = Field(default=30, ge=1, le=200)
    cli_path: str = ""


class OpenAIConfig(_Section):
    """Any OpenAI-compatible chat-completions endpoint (free tiers, local models)."""

    base_url: str = ""
    model: str = ""
    api_key_env: str = Field(default="", description="Name of the environment variable or stored secret holding the key.")
    supports_vision: bool = False
    max_output_tokens: int = Field(default=2048, ge=64)
    request_timeout_seconds: float = Field(default=120, gt=0)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    context_char_budget: int = Field(default=60_000, ge=4_000, description="Rough history size before old turns are dropped.")
    include_usage: bool = False

    @field_validator("base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")


class SafetyConfig(_Section):
    autonomy: Autonomy = "balanced"
    approved_dirs: list[str] = Field(default_factory=lambda: ["~/Jarvis"], description="Folders Jarvis may read and change.")
    read_only_dirs: list[str] = Field(default_factory=list, description="Folders Jarvis may read but not change.")
    command_allowlist: list[str] = Field(
        default_factory=list, description="Shell command prefixes that run without asking, e.g. 'npm test'."
    )
    approval_timeout_seconds: int = Field(default=120, ge=5)
    exfiltration_guard: bool = True
    voice_approval: bool = True
    backup_before_modify: bool = True
    max_file_read_bytes: int = Field(default=200_000, ge=1_000)
    command_timeout_seconds: int = Field(default=120, ge=1)


class VoiceConfig(_Section):
    enabled: bool = True
    wake_enabled: bool = True
    wake_word: str = "jarvis"
    wake_sensitivity: Literal["low", "medium", "high"] = "medium"
    followup_seconds: float = Field(default=6.0, ge=0, description="After a reply, keep listening this long without the wake word. 0 disables.")
    start_timeout_seconds: float = Field(default=6.0, gt=0)
    end_silence_seconds: float = Field(default=0.8, ge=0.2, le=5)
    max_utterance_seconds: float = Field(default=30.0, ge=2)
    stt_model: str = "moonshine-base-en"
    tts_voice: str = "en_GB-alan-medium"
    tts_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    tts_volume: float = Field(default=1.0, ge=0.0, le=2.0)
    input_backend: Literal["auto", "sounddevice", "command"] = "auto"
    output_backend: Literal["auto", "sounddevice", "command"] = "auto"
    input_command: str = Field(default="", description="Command that writes 16 kHz mono signed 16-bit PCM to stdout.")
    output_command: str = Field(default="", description="Command that plays mono signed 16-bit PCM from stdin; {rate} is substituted.")
    input_device: str = ""
    output_device: str = ""

    @field_validator("wake_word")
    @classmethod
    def _wake_word(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v or not all(ch.isalpha() or ch in " '" for ch in v):
            raise ValueError("wake word must be one or more words made of letters")
        return v.lower()


class ServerConfig(_Section):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    open_browser: bool = True

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        if v not in LOOPBACK_HOSTS:
            raise ValueError(
                "the control interface can run commands on this computer, so it only listens on this machine "
                f"({', '.join(sorted(LOOPBACK_HOSTS))})"
            )
        return v


class Config(_Section):
    user: UserConfig = Field(default_factory=UserConfig)
    brain: BrainConfig = Field(default_factory=BrainConfig)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)


class ConfigError(Exception):
    pass


_HEADER = (
    "# J.A.R.V.I.S. configuration. Edit here or from the Settings panel in the interface.\n"
    "# API keys do not belong in this file - see secrets.toml / environment variables.\n\n"
)


def load_config(paths: AppPaths) -> Config:
    """Read the config file, creating it with defaults on first run."""
    if not paths.config_file.exists():
        cfg = Config()
        save_config(paths, cfg)
        return cfg
    try:
        raw = tomllib.loads(paths.config_file.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Could not read {paths.config_file}: {exc}") from exc
    try:
        return Config.model_validate(raw)
    except ValueError as exc:
        raise ConfigError(f"Invalid settings in {paths.config_file}:\n{exc}") from exc


def save_config(paths: AppPaths, cfg: Config) -> None:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    body = tomli_w.dumps(_drop_none(cfg.model_dump(mode="json")))
    tmp = paths.config_file.with_suffix(".toml.tmp")
    tmp.write_text(_HEADER + body, encoding="utf-8")
    os.replace(tmp, paths.config_file)


def _drop_none(value):
    """TOML has no null; omitted keys fall back to their defaults on load."""
    if isinstance(value, dict):
        return {k: _drop_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_none(v) for v in value]
    return value


# --------------------------------------------------------------------- secrets


def get_secret(paths: AppPaths, name: str) -> str | None:
    """Look a secret up by name: environment first, then the private secrets file."""
    if not name:
        return None
    value = os.environ.get(name)
    if value:
        return value
    return _read_secrets(paths).get(name) or None


def set_secret(paths: AppPaths, name: str, value: str) -> None:
    if not name or not name.replace("_", "").isalnum():
        raise ConfigError("secret names may only contain letters, digits and underscores")
    secrets = _read_secrets(paths)
    if value:
        secrets[name] = value
    else:
        secrets.pop(name, None)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    tmp = paths.secrets_file.with_suffix(".toml.tmp")
    # Create with restrictive permissions before any secret is written to it.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(tomli_w.dumps({"secrets": secrets}))
    os.replace(tmp, paths.secrets_file)


def _read_secrets(paths: AppPaths) -> dict[str, str]:
    try:
        raw = tomllib.loads(paths.secrets_file.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    section = raw.get("secrets", {})
    return {str(k): str(v) for k, v in section.items()} if isinstance(section, dict) else {}


def expand_dirs(entries: list[str]) -> list[Path]:
    """Expand ~ and environment variables; relative entries are taken from the home directory."""
    out: list[Path] = []
    for entry in entries:
        p = Path(os.path.expandvars(entry)).expanduser()
        if not p.is_absolute():
            p = Path.home() / p
        out.append(p)
    return out
