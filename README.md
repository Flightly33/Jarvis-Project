# J.A.R.V.I.S.

A personal assistant for your Linux computer: it takes spoken or typed requests, works out the steps,
carries them out with tools (files, shell commands, applications, web research, reminders, memory),
checks its own work and reports back — under safety controls that you set.

> **Status: early build.** The assistant core, the safety layer and the terminal chat are working and
> tested. Voice and the web interface are next. See [What works today](#what-works-today).

## What works today

| Area | State |
|---|---|
| Terminal chat (`jarvis chat`) with streaming replies | Working, tested live |
| Reasoning on your **Claude plan** (Haiku 5.5 by default, no API key, no extra charges) | Working, tested live |
| Files: list, find, search, read, write, edit, move, copy, delete (to trash) | Working, tested |
| Shell commands, including background jobs you can ask about later | Working, tested |
| Applications: list, open, close; open files and web pages; screenshots | Written and tested with stand-in programs; not yet tried on a real desktop |
| Web research with sources (Claude's built-in search) | Working, tested live |
| Reminders and long-term memory | Tools working; spoken reminder announcements arrive with the voice build |
| Approvals, folder sandbox, command screening, emergency stop, audit log | Working, tested |
| Wake word, speech recognition, speech output | Engines chosen and verified; not wired in yet |
| Web interface (status orb, mic button, approval cards) | Not built yet |
| Free online AI APIs / local models as an alternative brain | Not built yet |
| Gmail, Calendar, Drive, Outlook, school systems, GitHub | Not built yet |

## Install

Needs Linux, Python 3.11 or newer, and a Claude subscription (Pro is enough).

```bash
git clone https://github.com/Flightly33/Jarvis-Project.git
```

```bash
cd Jarvis-Project
```

```bash
git checkout claude/gifted-bohr-hvwy4i
```

```bash
python3 -m venv .venv
```

```bash
. .venv/bin/activate
```

```bash
pip install -e ".[claude]"
```

Sign in once with your Claude account (choose the account that has your plan, not an API key):

```bash
jarvis login
```

Check the setup:

```bash
jarvis doctor
```

Start talking:

```bash
jarvis chat
```

Things to try:

- `Create a folder called school in my Jarvis folder and put a file todo.md in it with three example tasks.`
- `What's using the most memory on this computer right now?`
- `Find the newest file in my Jarvis folder.`
- `Remind me in two minutes to stretch.` then `What reminders do I have?`
- `Remember that my game project Beat Wayfarer lives in ~/Projects/BeatWayfarer.` (it will ask for access to that folder when it first needs it)
- `Research the best ways to speed up an old Linux laptop and give me the three with the least risk, with sources.`
- `Run git status in my Jarvis folder.` (runs straight away) versus `Run touch test.txt there.` (asks first)
- Press **Ctrl+C** while it is working: everything it is doing stops.

## Billing: what this can and cannot cost you

Jarvis talks to Claude through Claude Code's programmatic mode, signed in with your own Claude account.
According to Anthropic's help centre, that usage draws from your plan's included limits. Jarvis adds
three safeguards:

1. It removes any `ANTHROPIC_API_KEY` from its environment, so Claude Code cannot silently switch to
   pay-per-use billing.
2. It checks how Claude Code authenticated at the start of every conversation and **refuses to run** if
   an API key is in use (`claude.require_subscription`, on by default).
3. It warns you if "usage credits" (paid overflow) are switched on for your account. With them off —
   the default — requests simply stop when you reach your plan's limit and resume when it resets.

Two honest caveats: Jarvis shares your plan's limits with everything else you do with Claude, and
Anthropic has changed its mind about this policy before (a different scheme was announced and then
paused in June 2026). If the rules change, the reasoning backend is replaceable by design.

## Safety model

Every action the model proposes passes through one gate before anything happens:

- **Folder sandbox.** File tools only work inside folders you approve (`~/Jarvis` to start with).
  Paths are resolved before checking, so `..` tricks and symlinks pointing elsewhere are caught. When
  a request needs another folder, Jarvis asks you, and you decide.
- **Risk levels.** Reading is free. Additive, easily undone actions (new file, open an app, set a
  reminder) run on their own. Changes to existing things ask first. High-risk actions — permanent
  deletion, installs, administrator commands, credential files, widening access — always ask.
- **Autonomy setting** (`safety.autonomy`): `cautious` asks for nearly everything, `balanced` is the
  default, `trusting` only asks for high-risk actions.
- **Shell commands.** A short list of read-only commands runs unattended, and only when every file
  they touch is inside an approved folder. A few catastrophic commands (`rm -rf ~`, disk wipes, fork
  bombs) are refused outright. Everything else is shown to you first; you can allow a command
  permanently from the prompt. Pattern-matching on shell text is not a security boundary, which is
  why "ask" is the default for anything unrecognised.
- **Reversibility.** Edits and overwrites keep a backup; deletion goes to the trash unless you approve
  a permanent delete.
- **Protection against hijacked instructions.** Web pages can contain text aimed at an assistant.
  Once web content is in a conversation, changes need your confirmation; and if private files have
  also been read, anything that contacts the internet needs your confirmation too.
- **Emergency stop.** Ctrl+C in the terminal stops the request, kills the commands it started and
  declines pending approvals.
- **Audit log.** Every tool call, the decision and who made it are recorded locally.
- **Jarvis cannot reach its own settings or data**, so it cannot loosen its own rules.

## Settings

`jarvis config path` prints where the settings file lives (normally `~/.config/jarvis/config.toml`).
The most useful entries:

```toml
[user]
name = ""              # what Jarvis should call you
honorific = ""         # e.g. "sir"

[claude]
model = "haiku"        # or "sonnet", "opus"
effort = "low"         # low is quickest; medium thinks harder

[safety]
autonomy = "balanced"
approved_dirs = ["~/Jarvis"]
read_only_dirs = []
command_allowlist = []  # e.g. ["npm test", "make"]
```

## Development

```bash
pip install -e ".[claude,dev]"
```

```bash
pytest
```

The test suite exercises the sandbox, the command classifier, the policy and every tool against a
real temporary filesystem and real child processes. Nothing in it uses your Claude plan.

## Layout

```
src/jarvis/
  safety/     sandbox, command classifier, policy, approvals, emergency stop
  tools/      files, shell, apps, system, memory, reminders, web + the executor they all pass through
  brain/      reasoning backends (Claude Code today)
  agent.py    conversation manager      persona.py  standing instructions for the model
  core.py     wiring                    cli.py      terminal interface
```

Speech synthesis, when added, will use `piper-tts`, which is GPL-3.0 licensed; it stays an optional
extra and is only loaded when you turn voice on.
