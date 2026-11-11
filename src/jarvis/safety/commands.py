"""Classifies a shell command before it is allowed to run.

Honest scope: pattern matching on shell text is *not* a security boundary. A
determined adversary can always phrase a harmful command so that no pattern
catches it. That is why the default for anything not recognised as harmless is
to ask the user, showing the exact command. The classifier's jobs are:

* refuse outright the handful of commands that destroy a system or home folder,
* flag known-destructive or privileged commands with a clear warning,
* let genuinely read-only commands (`ls`, `git status`, ...) run unattended,
  but only when every file they touch is inside an approved folder.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from jarvis.safety.policy import Risk
from jarvis.safety.sandbox import Access, PathSandbox

Category = Literal["blocked", "dangerous", "allowlisted", "read-only", "unclassified"]


@dataclass
class CommandVerdict:
    category: Category
    risk: Risk
    reasons: list[str] = field(default_factory=list)
    sends_external: bool = False

    @property
    def blocked(self) -> bool:
        return self.category == "blocked"

    @property
    def auto_ok(self) -> bool:
        return self.category in ("read-only", "allowlisted")


_OPERATORS = {";", "&", "&&", "||", "|", "|&", "(", ")", "<", ">", ">>", "<<", "<<<", ">&", "<&", ">|", "&>", "&>>"}
_WRAPPERS = {"sudo", "doas", "pkexec", "env", "nice", "ionice", "nohup", "time", "command", "exec", "stdbuf", "setsid"}
_PRIVILEGE = {"sudo", "doas", "pkexec", "su"}
_INTERPRETERS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "python", "python3", "perl", "ruby", "node", "php", "lua"}
_NETWORK = {
    "curl", "wget", "http", "https", "xh", "nc", "ncat", "netcat", "socat", "ssh", "scp", "sftp", "rsync", "ftp",
    "lftp", "telnet", "aria2c", "gh", "aws", "gcloud", "az", "rclone", "sendmail", "mail", "mailx", "mutt",
    "dig", "nslookup", "host", "ping", "traceroute", "nmap",
}
_GIT_NETWORK = {"clone", "fetch", "pull", "push", "ls-remote", "submodule"}
_DISK_TOOLS = {"dd", "shred", "wipe", "srm", "wipefs", "fdisk", "sfdisk", "cfdisk", "parted", "mkswap", "cryptsetup", "losetup", "blkdiscard"}
_SYSTEM_SETTINGS = {
    "ufw", "iptables", "ip6tables", "nft", "firewall-cmd", "setenforce", "aa-disable", "aa-complain", "apparmor_parser",
    "visudo", "passwd", "chpasswd", "useradd", "userdel", "usermod", "adduser", "deluser", "groupadd", "groupdel",
    "gpasswd", "chsh", "ssh-add", "mount", "umount", "swapoff", "swapon", "modprobe", "rmmod", "insmod",
    "update-alternatives", "update-grub", "grub-install", "efibootmgr", "crontab", "at", "chattr", "setfacl", "setcap",
}
_POWER = {"shutdown", "reboot", "poweroff", "halt"}
_SYSTEMCTL_CHANGES = {
    "start", "stop", "restart", "reload", "enable", "disable", "mask", "unmask", "kill", "isolate", "daemon-reload",
    "daemon-reexec", "poweroff", "reboot", "halt", "suspend", "hibernate", "set-default", "edit", "set-property",
}
_SYSTEM_PKG = {"apt", "apt-get", "aptitude", "dpkg", "dnf", "yum", "zypper", "pacman", "yay", "paru", "snap", "flatpak", "rpm", "brew", "nix-env", "apk", "emerge"}
_SYSTEM_PKG_READONLY = {"list", "search", "show", "info", "policy", "depends", "rdepends", "-l", "-L", "-s", "-S", "-Q", "-Qi", "-Ql", "-Qs", "-Ss", "-Si", "history", "remotes", "--version", "version"}
_LANG_PKG = {"pip", "pip3", "pipx", "npm", "yarn", "pnpm", "bun", "cargo", "gem", "go", "uv", "poetry", "conda", "mamba", "composer"}
_LANG_PKG_CHANGES = {"install", "i", "add", "uninstall", "remove", "rm", "update", "upgrade", "ci", "link", "sync", "create", "global", "get", "exec", "dlx"}
_SYSTEM_TOP_LEVEL = {"bin", "boot", "dev", "etc", "home", "lib", "lib32", "lib64", "libx32", "opt", "proc", "root", "run", "sbin", "srv", "sys", "usr", "var", "snap", "media", "mnt"}

_FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:")
_PIPE_TO_INTERPRETER = re.compile(r"\|\s*(?:sudo\s+)?(?:env\s+)?(?:ba|z|da|k)?sh\b|\|\s*(?:sudo\s+)?(?:python3?|perl|ruby|node|php)\b")
_SUBSHELL_DOWNLOAD = re.compile(r"(?:\$\(|<\(|`)\s*(?:curl|wget)\b")
_RAW_SOCKET = re.compile(r"/dev/(?:tcp|udp)/")
_BLOCK_DEVICE = re.compile(r"^/dev/(?:sd[a-z]|hd[a-z]|vd[a-z]|xvd[a-z]|nvme\d|mmcblk\d|md\d|dm-\d|mapper/|disk/|loop\d)")

# Characters that make a command more than a plain pipeline of plain words. A command
# containing any of them is never auto-approved as read-only.
_NOT_PLAIN = set("$`\n\r*?[]{}\\<>;&()#!")

_VERSION_PROGS = {
    "python", "python3", "pip", "pip3", "node", "npm", "npx", "yarn", "pnpm", "bun", "deno", "cargo", "rustc", "go",
    "java", "javac", "gcc", "g++", "clang", "make", "cmake", "git", "docker", "podman", "uv", "ruff", "pytest",
    "dotnet", "godot", "godot4", "ffmpeg", "code", "bash", "zsh", "ruby", "php", "perl", "sqlite3", "psql", "mysql",
}
_VERSION_ARGS = {"--version", "-V", "version", "-version"}

# Programs that only report. The value lists options that would make them write or run something.
_READ_ONLY_PROGS: dict[str, set[str]] = {
    "ls": set(), "cat": set(), "head": set(), "tail": set(), "tac": set(), "nl": set(), "wc": {"--files0-from"},
    "stat": set(), "file": {"-f", "--files-from", "-C", "--compile"}, "du": {"--files0-from"}, "tree": {"-o"},
    "md5sum": set(), "sha1sum": set(), "sha256sum": set(), "sha512sum": set(), "cksum": set(),
    "sort": {"-o", "--output", "--compress-program", "-T", "--temporary-directory", "--files0-from"},
    "uniq": set(), "cut": set(), "column": set(), "fold": set(), "diff": set(), "cmp": set(), "comm": set(),
    "realpath": set(), "readlink": set(), "basename": set(), "dirname": set(), "jq": {"-f", "--from-file", "-L"},
    "grep": {"-f", "--file"}, "egrep": {"-f", "--file"}, "fgrep": {"-f", "--file"},
    "rg": {"-f", "--file", "--pre", "--pre-glob", "--hostname-bin"},
    "find": {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"},
}
# Programs whose arguments are not file paths. Value: options that change state.
_INFO_PROGS: dict[str, set[str]] = {
    "pwd": set(), "whoami": set(), "id": set(), "groups": set(), "date": {"-s", "--set"}, "uptime": set(),
    "uname": set(), "arch": set(), "nproc": set(), "free": set(), "df": set(), "lscpu": set(), "lsblk": set(),
    "lsusb": set(), "lspci": set(), "lsmem": set(), "lsb_release": set(), "ps": set(), "pgrep": set(),
    "pidof": set(), "which": set(), "whereis": set(), "echo": set(), "true": set(), "cal": set(), "locale": set(),
    "tty": set(), "sensors": {"-s", "--set"},
}
_INFO_NO_POSITIONAL = {"hostname", "nvidia-smi", "hostnamectl", "timedatectl", "loginctl"}
_GIT_READ_ONLY = {"status", "log", "diff", "show", "ls-files", "rev-parse", "describe", "blame", "shortlog", "branch", "remote", "tag", "stash"}
_GIT_BRANCH_FLAGS = {"-a", "-r", "-v", "-vv", "--list", "--show-current", "--all", "--remotes"}
_GIT_WRITE_OPTS = {"--output", "-o", "--ext-diff", "--textconv"}


def classify_command(command: str, cwd: Path, sandbox: PathSandbox, allowlist: list[str] | None = None) -> CommandVerdict:
    text = command.strip()
    if not text:
        return CommandVerdict("blocked", Risk.HIGH, ["No command was given."])
    if "\x00" in text:
        return CommandVerdict("blocked", Risk.HIGH, ["The command contains a null byte."])

    tokens, parse_ok = _tokenize(text)
    block, dangers, external = _analyze(text, cwd, sandbox, depth=0)
    if block:
        return CommandVerdict("blocked", Risk.HIGH, [block], external)

    if _matches_allowlist(text, tokens, allowlist or []):
        return CommandVerdict("allowlisted", Risk.LOW, ["You have allowed this command to run without asking."], external)

    if dangers:
        return CommandVerdict("dangerous", Risk.HIGH, dangers, external)

    if parse_ok and _is_read_only(text, tokens, cwd, sandbox):
        return CommandVerdict("read-only", Risk.READ, [], external)

    reason = "This command is not on the list of known read-only commands, so it runs only with your approval."
    if not parse_ok:
        reason = "The command could not be parsed reliably (unbalanced quotes?), so it runs only with your approval."
    return CommandVerdict("unclassified", Risk.MODIFY, [reason], external)


def _analyze(text: str, cwd: Path, sandbox: PathSandbox, depth: int) -> tuple[str | None, list[str], bool]:
    """Return (refusal, danger reasons, talks-to-network) for a command, looking inside `sh -c '...'`."""
    # A newline separates commands just as `;` does.
    flat = text.replace("\r", "\n").replace("\n", " ; ")
    tokens, _ = _tokenize(flat)
    segments = _split_segments(tokens)
    external = _RAW_SOCKET.search(text) is not None or any(_segment_uses_network(seg) for seg in segments)
    block = _blocked_reason(text, segments, cwd, sandbox)
    dangers = _danger_reasons(text, segments, cwd, sandbox, external)
    if depth < 2:
        for seg in segments:
            prog, args, _ = _program(seg)
            if prog in {"sh", "bash", "zsh", "dash", "ksh", "eval"}:
                inner = args if prog == "eval" else [args[i + 1] for i, a in enumerate(args[:-1]) if a.startswith("-") and "c" in a and not a.startswith("--")]
                for script in inner:
                    b, d, e = _analyze(script, cwd, sandbox, depth + 1)
                    block = block or b
                    dangers.extend(x for x in d if x not in dangers)
                    external = external or e
    return block, dangers, external


# ----------------------------------------------------------------- tokenizing


def _tokenize(text: str) -> tuple[list[str], bool]:
    lex = shlex.shlex(text, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex), True
    except ValueError:
        return text.split(), False


def _split_segments(tokens: list[str]) -> list[list[str]]:
    """Split on shell control operators into the simple commands they connect."""
    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok in _OPERATORS and tok not in {"<", ">", ">>", "<<", "<<<", ">&", "<&", ">|", "&>", "&>>"}:
            segments.append([])
        else:
            segments[-1].append(tok)
    return [s for s in segments if s]


def _program(segment: list[str]) -> tuple[str, list[str], bool]:
    """Return (program name, its arguments, ran-with-privilege) after stripping wrappers."""
    privileged = False
    i = 0
    while i < len(segment):
        tok = segment[i]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tok):   # VAR=value prefix
            i += 1
            continue
        name = tok.rsplit("/", 1)[-1]
        if name in _PRIVILEGE:
            privileged = True
        if name in _WRAPPERS or name == "su":
            i += 1
            # skip the wrapper's own options (e.g. `sudo -u root`, `timeout 5`, `nice -n 5`)
            while i < len(segment) and (segment[i].startswith("-") or segment[i].isdigit()):
                takes_value = segment[i] in {"-u", "-g", "-n", "-c", "-C", "-h", "-p", "-U"}
                i += 2 if takes_value and name != "su" else 1
            continue
        if name in {"timeout", "watch", "xargs"}:
            i += 1
            while i < len(segment) and (segment[i].startswith("-") or re.fullmatch(r"[\d.]+[smhd]?", segment[i])):
                i += 1
            continue
        return name, segment[i + 1:], privileged
    return "", [], privileged


def _segment_uses_network(segment: list[str]) -> bool:
    prog, args, _ = _program(segment)
    if prog in _NETWORK:
        return True
    if prog == "git":
        sub = next((a for a in args if not a.startswith("-")), "")
        return sub in _GIT_NETWORK
    if prog in _LANG_PKG or prog in _SYSTEM_PKG:
        return True
    return False


# -------------------------------------------------------------------- blocked


def _critical_target(arg: str, cwd: Path, sandbox: PathSandbox) -> bool:
    """True for the filesystem root, a top-level system folder, or the home folder itself."""
    raw = arg.strip()
    if raw in {"$HOME", "${HOME}", "$HOME/", "${HOME}/", "$HOME/*", "${HOME}/*", "~", "~/", "~/*", "/*", "/.", "/.*"}:
        return True
    if raw.startswith(("$", "`")):
        return False
    stripped = raw.rstrip("*").rstrip("/") or "/"
    try:
        path = Path(stripped).expanduser()
        if not path.is_absolute():
            path = cwd / path       # `rm -rf ..` from inside the home folder is just as fatal
        path = path.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    home = sandbox.home
    if path == Path("/") or path == home or path == home.parent:
        return True
    parts = path.parts
    return len(parts) == 2 and parts[1] in _SYSTEM_TOP_LEVEL


def _blocked_reason(text: str, segments: list[list[str]], cwd: Path, sandbox: PathSandbox) -> str | None:
    if _FORK_BOMB.search(text):
        return "This is a fork bomb: it would freeze the computer."
    for seg in segments:
        prog, args, _ = _program(seg)
        positional = [a for a in args if not a.startswith("-")]
        flags = [a for a in args if a.startswith("-")]
        if prog == "rm":
            if "--no-preserve-root" in flags:
                return "Refused: this would delete the entire filesystem."
            if any(_critical_target(a, cwd, sandbox) for a in positional):
                return "Refused: this would delete the system or your whole home folder."
        if prog in {"chmod", "chown", "chgrp"} and any(f.startswith("-") and ("R" in f or f == "--recursive") for f in flags):
            if any(_critical_target(a, cwd, sandbox) for a in positional[1:] or positional):
                return "Refused: recursively changing permissions on the system or home folder would break it."
        if prog.startswith("mkfs") or prog in {"wipefs", "shred", "blkdiscard", "mkswap"}:
            if any(_BLOCK_DEVICE.match(a) for a in positional):
                return "Refused: this would erase a disk."
        if prog == "dd" and any(a.startswith("of=") and _BLOCK_DEVICE.match(a[3:]) for a in args):
            return "Refused: this would overwrite a disk."
        if prog == "find" and (set(args) & {"-delete", "-exec", "-execdir"}):
            starts = []
            for a in args:
                if a.startswith("-"):
                    break
                starts.append(a)
            if "-delete" in args and any(_critical_target(a, cwd, sandbox) for a in starts or ["."]):
                return "Refused: this would delete the system or your whole home folder."
        if prog in {"mv", "cp"} and positional and positional[-1] == "/dev/null" and len(positional) > 1:
            return "Refused: moving files into /dev/null destroys them."
        # output redirected onto a raw disk device
        for i, tok in enumerate(seg[:-1]):
            if tok in {">", ">>", ">|"} and _BLOCK_DEVICE.match(seg[i + 1]):
                return "Refused: this would write over a disk."
    return None


# ------------------------------------------------------------------ dangerous


def _danger_reasons(text: str, segments: list[list[str]], cwd: Path, sandbox: PathSandbox, external: bool) -> list[str]:
    reasons: list[str] = []

    def add(msg: str) -> None:
        if msg not in reasons:
            reasons.append(msg)

    if _PIPE_TO_INTERPRETER.search(text) or _SUBSHELL_DOWNLOAD.search(text):
        add("It feeds downloaded or generated text straight into an interpreter, which runs whatever arrives.")
    if _RAW_SOCKET.search(text):
        add("It opens a raw network connection from the shell.")

    for seg in segments:
        prog, args, privileged = _program(seg)
        sub = next((a for a in args if not a.startswith("-")), "")
        flags = {a.split("=", 1)[0] for a in args if a.startswith("-")}
        if privileged:
            add("It runs with administrator rights.")
        if external:
            for a in args:
                candidate = a.split("=", 1)[-1].lstrip("@")
                if candidate.startswith(("/", "~", ".")) and sandbox.is_sensitive(Path(candidate).expanduser()):
                    add("It uses a credentials or secrets file in a command that talks to the network.")
        if prog in {"rm", "rmdir", "unlink"}:
            add("It deletes files permanently, bypassing the trash.")
        elif prog == "find" and (set(args) & {"-delete", "-exec", "-execdir", "-ok"}):
            add("It deletes or runs a command on every file it finds.")
        elif prog in _DISK_TOOLS or prog.startswith("mkfs") or prog == "truncate":
            add("It overwrites or erases data at a low level.")
        elif prog in {"chmod", "chown", "chgrp"} and any(not f.startswith("--") and "R" in f or f == "--recursive" for f in flags):
            add("It changes permissions or ownership recursively.")
        elif prog in _SYSTEM_PKG and sub not in _SYSTEM_PKG_READONLY and sub:
            add("It installs, removes or changes system software.")
        elif prog in _LANG_PKG and (sub in _LANG_PKG_CHANGES or (prog == "uv" and sub in {"pip", "tool", "add", "sync"})):
            add("It installs or removes software packages, which can run their own install scripts.")
        elif prog in {"systemctl", "service"} and (set(args) & _SYSTEMCTL_CHANGES):
            add("It changes system services.")
        elif prog in _SYSTEM_SETTINGS and not (prog == "crontab" and flags <= {"-l"} and "-l" in flags):
            add("It changes security or system settings.")
        elif prog in _POWER or (prog == "init" and sub in {"0", "6"}):
            add("It shuts down or restarts the computer.")
        elif prog == "git":
            arg_set = set(args)
            if sub == "push" and (arg_set & {"--force", "-f", "--force-with-lease", "--mirror", "--delete"}):
                add("It force-pushes or deletes remote branches, which can destroy others' work.")
            elif sub == "reset" and "--hard" in arg_set:
                add("It discards uncommitted work (git reset --hard).")
            elif sub == "clean" and any(a.startswith("-") and "f" in a for a in args):
                add("It deletes untracked files (git clean).")
            elif sub in {"filter-branch", "filter-repo"} or (sub == "reflog" and "expire" in arg_set):
                add("It rewrites repository history.")
            elif sub == "branch" and (arg_set & {"-D", "--delete", "-d"}):
                add("It deletes a branch.")
            elif sub == "stash" and (arg_set & {"drop", "clear"}):
                add("It discards stashed work.")
            elif sub in {"checkout", "restore"} and (arg_set & {".", "--", ":/"}):
                add("It may discard uncommitted changes.")
        elif prog in {"kill", "pkill", "killall"} and (arg_set := set(args)) and (arg_set & {"-9", "-KILL", "-SIGKILL", "-1"}):
            add("It force-kills processes, which can lose unsaved work.")
        elif prog in {"docker", "podman"} and sub in {"system", "volume", "image", "container"} and "prune" in args:
            add("It deletes containers, images or volumes.")

        # Redirecting output onto a file outside the approved folders, or onto a sensitive one.
        for i, tok in enumerate(seg[:-1]):
            if tok in {">", ">>", ">|", "&>", "&>>"}:
                target = seg[i + 1]
                if target in {"/dev/null", "/dev/stdout", "/dev/stderr"} or target.startswith("&"):
                    continue
                check = sandbox.check(target, Access.WRITE, cwd)
                if not check.allowed:
                    add(f"It writes to {target}, which is outside the folders you approved for changes.")
                elif check.sensitive:
                    add(f"It writes to {target}, which looks like a credentials or secrets file.")
        if prog == "tee":
            for target in (a for a in args if not a.startswith("-")):
                check = sandbox.check(target, Access.WRITE, cwd)
                if not check.allowed or check.sensitive:
                    add(f"It writes to {target}, which is outside the approved folders or is a sensitive file.")
    return reasons


# ------------------------------------------------------------------ allowlist


def _matches_allowlist(text: str, tokens: list[str], allowlist: list[str]) -> bool:
    if not allowlist:
        return False
    # An allowlisted prefix must not be usable to smuggle in a second command.
    if any(tok in _OPERATORS for tok in tokens) or any(ch in text for ch in "$`\n\r"):
        return False
    normalized = " ".join(text.split())
    for entry in allowlist:
        entry = " ".join(entry.split())
        if entry and (normalized == entry or normalized.startswith(entry + " ")):
            return True
    return False


# ------------------------------------------------------------------ read-only


def _is_read_only(text: str, tokens: list[str], cwd: Path, sandbox: PathSandbox) -> bool:
    if any(ch in _NOT_PLAIN for ch in text):
        return False
    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok == "|":
            segments.append([])
        elif tok in _OPERATORS:
            return False
        else:
            segments[-1].append(tok)
    if any(not seg for seg in segments):
        return False
    if not sandbox.check(cwd, Access.READ).allowed:
        return False
    return all(_segment_read_only(seg, cwd, sandbox) for seg in segments)


def _has_option(args: list[str], forbidden: set[str]) -> bool:
    for a in args:
        if not a.startswith("-"):
            continue
        name = a.split("=", 1)[0]
        if name in forbidden:
            return True
        # attached short-option values such as `-ofile`
        if not a.startswith("--") and any(len(f) == 2 and f[1] in a[1:] for f in forbidden if not f.startswith("--")):
            return True
    return False


def _paths_ok(args: list[str], cwd: Path, sandbox: PathSandbox) -> bool:
    for a in args:
        if a.startswith("-"):
            value = a.split("=", 1)[1] if "=" in a else ""
            if value and ("/" in value or value.startswith("~")):
                check = sandbox.check(value, Access.READ, cwd)
                if not check.allowed or check.sensitive:
                    return False
            continue
        check = sandbox.check(a, Access.READ, cwd)
        if not check.allowed or check.sensitive:
            return False
    return True


def _segment_read_only(seg: list[str], cwd: Path, sandbox: PathSandbox) -> bool:
    prog, args = seg[0], seg[1:]
    if "/" in prog:
        return False   # only plain program names resolved through PATH
    if prog in _VERSION_PROGS and len(args) == 1 and args[0] in _VERSION_ARGS:
        return True
    if prog in _INFO_PROGS:
        return not _has_option(args, _INFO_PROGS[prog])
    if prog in _INFO_NO_POSITIONAL:
        # These can also change settings; only the bare status forms are treated as harmless.
        return not args or (prog != "nvidia-smi" and args == ["status"])
    if prog == "systemctl":
        sub = next((a for a in args if not a.startswith("-")), "")
        return sub in {"status", "is-active", "is-enabled", "is-failed", "list-units", "list-unit-files", "show", "cat"}
    if prog == "git":
        return _git_read_only(args, cwd, sandbox)
    if prog in _READ_ONLY_PROGS:
        if _has_option(args, _READ_ONLY_PROGS[prog]):
            return False
        if prog == "uniq" and len([a for a in args if not a.startswith("-")]) > 1:
            return False   # a second file name is where uniq writes its output
        if prog in {"grep", "egrep", "fgrep", "rg"}:
            positional = [a for a in args if not a.startswith("-")]
            # the first plain argument is the search pattern, not a path
            return _paths_ok([a for a in args if a.startswith("-")] + positional[1:], cwd, sandbox)
        if prog == "find":
            starts = []
            for a in args:
                if a.startswith("-"):
                    break
                starts.append(a)
            return _paths_ok(starts, cwd, sandbox)
        if prog == "jq":
            positional = [a for a in args if not a.startswith("-")]
            return _paths_ok(positional[1:], cwd, sandbox)
        return _paths_ok(args, cwd, sandbox)
    return False


def _git_read_only(args: list[str], cwd: Path, sandbox: PathSandbox) -> bool:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "--no-pager":
            i += 1
        elif args[i] == "-C" and i + 1 < len(args):
            if not sandbox.check(args[i + 1], Access.READ, cwd).allowed:
                return False
            i += 2
        else:
            return False   # `-c key=value` can make git run arbitrary programs
    if i >= len(args):
        return False
    sub, rest = args[i], args[i + 1:]
    if sub not in _GIT_READ_ONLY or _has_option(rest, _GIT_WRITE_OPTS) or "--no-index" in rest:
        return False
    for a in rest:
        value = a.split("=", 1)[-1]
        if value.startswith(("/", "~")) or ".." in Path(value).parts:
            check = sandbox.check(value, Access.READ, cwd)
            if not check.allowed or check.sensitive:
                return False
    if sub == "branch":
        return all(a in _GIT_BRANCH_FLAGS for a in rest)
    if sub == "remote":
        return all(a in {"-v", "--verbose", "show"} for a in rest[:1]) and len(rest) <= 2
    if sub == "tag":
        return all(a in {"-l", "--list"} or not a.startswith("-") for a in rest) and (not rest or rest[0] in {"-l", "--list"})
    if sub == "stash":
        return rest[:1] == ["list"] or rest[:1] == ["show"]
    return True
