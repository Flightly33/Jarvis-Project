# J.A.R.V.I.S.

A personal assistant for your own computer. You speak or type a request; it works out the steps,
carries them out with tools (files, commands, applications, web research, reminders, memory), checks
its own work and tells you what it did — under safety controls that you set.

> **Status: preview.** It runs on Windows 11 and Linux. See [What works today](#what-works-today)
> for exactly what has and has not been tested.

## Install on Windows

1. Download **JarvisSetup.exe** from the
   [preview release](https://github.com/Flightly33/Jarvis-Project/releases/tag/windows-preview) and run it.
   Windows may warn that the app is unrecognised (it is not code-signed): choose *More info*, then *Run anyway*.
2. Start **Jarvis** from the desktop or Start menu. A console window opens (keep it open; closing it quits
   Jarvis) and then the Jarvis window.
3. The first time, a Claude sign-in page appears. Use the account that has your Claude plan, not an API key.
4. The first time voice starts, Jarvis downloads its speech models (about 340 MB, once). The status line
   under the orb shows progress. You can type in the meantime.

To use Jarvis: say **"Jarvis"** followed by your request, press **Talk**, or type. **Stop** (or Esc) halts
whatever it is doing.

## Install from source (Linux or Windows)

Needs Python 3.11 or newer and a Claude subscription.

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
pip install -e ".[claude,voice]"
```

```bash
jarvis
```

Other commands: `jarvis chat` (terminal only), `jarvis doctor` (check the setup), `jarvis login`,
`jarvis selftest`, `jarvis config path`. On Linux, voice works best with the system package
`libportaudio2`; without it Jarvis records and plays through the desktop's own audio commands.

To look around without signing in to anything, set `backend = "demo"` under `[brain]` in the settings
file: the interface, approvals and voice all work, with canned replies instead of an AI model.

## What works today

| Area | State |
|---|---|
| Reasoning on your **Claude plan** (Haiku by default; no API key, no extra charges) | Tested live on Linux; not yet run on Windows |
| Interface: status orb, conversation, activity log, approval cards, settings, Stop | Tested in a real browser on Linux; the packaged Windows app passes an automated self-test |
| Files, shell commands (PowerShell on Windows), background jobs | Tested on Linux and on a real Windows machine |
| Wake word, speech recognition, spoken replies, interruption, follow-up listening | Tested end to end on the real speech models with synthetic speech; **not yet tried with a real microphone or speakers** |
| Applications: list, open, close; open files and web pages; screenshots | Tested with stand-in programs only; not yet tried on a real desktop |
| Reminders (spoken and shown when due) and long-term memory | Tested |
| Web research with sources | Tested live |
| A second brain: free online AI services (Groq, Gemini, OpenRouter) or a local model (Ollama), chosen in Settings | Tested against a simulated service; **not yet run against a real provider** |
| Calendar in the interface; Jarvis can add, list and remove events and announces them beforehand | Tested |
| Talk shortcut that works from any window (Ctrl+Alt+J on Windows) | Tested in code only; **not yet confirmed on a real PC** |
| Reading email (read-only, with an app password) | Tested against a simulated mail server; **not yet run against real Gmail** |
| A browser Jarvis drives itself (open, read, click, type; never password fields) | Tested with a real browser on Linux and in the Windows build |
| Jarvis Bridge extension: list, open and read tabs in your own Chrome or Edge | Tested against a simulated extension; **not yet loaded in a real browser** |
| Phone: chat, approvals, reminders and calendar alerts through a private Telegram bot | Tested against a simulated Telegram; **not yet run against the real service** |
| Phone calls and text messages to your number | Not built: they need a paid telephone service |
| Sending email; Google Calendar, Drive, Outlook, school systems, GitHub | Not built yet |
| Clicking and typing inside other applications' windows or your own browser's tabs | Not built yet |

## Phone

Jarvis can be reached from your phone through a Telegram bot that only you can use. It is free.

1. Install Telegram on your phone, open a chat with **@BotFather**, send `/newbot` and follow the steps.
   It gives you a token.
2. In Jarvis open *Settings > Phone*, paste the token and press **Save phone**.
3. Open your new bot in Telegram and send `/pair` followed by the code shown in Settings.

After that, anything you type to the bot is answered by Jarvis, reminders and upcoming calendar events
are sent to your phone, and approvals can be answered with "yes" or "no". High-risk actions can only be
approved at the computer. Send `/stop` to halt whatever Jarvis is doing.

Messages pass through Telegram's servers, and Jarvis must be running on your computer to answer. Keep
the token private: anyone holding it can read what the bot receives.

## Voice

Everything about voice runs on your computer; no audio is sent anywhere.

- **Wake word.** "Jarvis" by default; any word or short phrase can be set in Settings. In tests with
  synthetic voices it was caught 94-100% of the time with no false alarms, but a short name is easier to
  trigger by accident than a longer phrase. If it wakes too easily, try "Hey Jarvis".
- **Follow-ups.** After a reply the microphone stays open for a few seconds so you can continue without
  the wake word. If what it hears is clearly not meant for it, it stays quiet.
- **Interrupting.** Say the wake word, press Talk or press Stop while it is speaking.
- **Spoken commands** handled instantly: "stop", "never mind", "stop listening", and "yes" / "no" when an
  approval is waiting. High-risk approvals need the word "confirm" (or a click) — a casual "yes" is not enough.
- **Pause microphone** really closes the microphone until you resume it.
- With open speakers the microphone can hear Jarvis's own voice. That is harmless unless it says its own
  name; headphones avoid it entirely.

### If voice does not respond

- Look at the line under the orb: it says what voice is doing ("Preparing voice", "Microphone paused")
  or why it is unavailable.
- On Windows, check *Settings > Privacy & security > Microphone* and allow desktop apps to use the
  microphone; also check that the right microphone is the default input device.
- Press **Talk** and speak: if that works but the wake word does not, raise the wake word sensitivity in
  Settings or try a longer phrase such as "Hey Jarvis".
- If it mishears you often, the microphone level or background noise is the usual cause.

## Using a free AI service instead of (or alongside) your Claude plan

Jarvis shares your Claude plan's limits with everything else you do with Claude. When the plan runs out,
you can switch Jarvis to another service in **Settings > Thinking**:

1. Choose a service (Groq, Google Gemini, OpenRouter, or Ollama for a model running on your own computer).
2. Get a free key from that service (the link is shown) and paste it in. It is stored in a private file on
   this computer, never in the settings file, and never shown again.
3. Press **Show available models**, pick one that supports "tool use" / "function calling", and save.

Be realistic about this option: free tiers allow only a few requests a minute (a multi-step task can hit
the limit part-way), smaller models make more mistakes with tools than Claude does, and what you ask -
including file contents Jarvis reads for you - goes to that company, some of which use it to improve
their products. Jarvis's safety checks apply identically whichever service is thinking.

## Billing: what this can and cannot cost you

Jarvis talks to Claude through Claude Code's programmatic mode, signed in with your own Claude account.
According to Anthropic's help centre, that usage draws from your plan's included limits. Jarvis adds
three safeguards:

1. It removes any `ANTHROPIC_API_KEY` from its environment, so Claude Code cannot silently switch to
   pay-per-use billing.
2. It checks how Claude Code authenticated at the start of every conversation and **refuses to run** if
   an API key is in use.
3. It warns you if "usage credits" (paid overflow) are switched on for your account. With them off,
   requests simply stop when you reach your plan's limit and resume when it resets.

Two honest caveats: Jarvis shares your plan's limits with everything else you do with Claude, and
Anthropic has changed its mind about this policy before (a different scheme was announced and then
paused in June 2026). If the rules change, the reasoning backend is replaceable by design.

## Safety model

Every action the model proposes passes through one gate before anything happens:

- **Folder sandbox.** File tools only work inside folders you approve (a `Jarvis` folder in your home
  folder to start with; add more in Settings, or approve them when Jarvis asks). Paths are resolved
  before checking, so `..` tricks and links pointing elsewhere are caught.
- **Risk levels.** Reading is free. Additive, easily undone actions (new file, open an app, set a
  reminder) run on their own. Changes to existing things ask first. High-risk actions — permanent
  deletion, installs, administrator commands, credential files, widening access — always ask.
- **Autonomy setting**: *Cautious* asks for nearly everything, *Balanced* is the default, *Trusting*
  only asks for high-risk actions.
- **Commands.** A short list of read-only commands runs unattended, and only when every file they touch
  is inside an approved folder. A few catastrophic commands (wiping a disk, deleting Windows or your home
  folder) are refused outright. Everything else is shown to you first. Pattern-matching on command text
  is not a security boundary, which is why "ask" is the default for anything unrecognised.
- **Reversibility.** Edits and overwrites keep a backup; deletion goes to the trash / Recycle Bin unless
  you approve a permanent delete.
- **Protection against hijacked instructions.** Web pages can contain text aimed at an assistant. Once
  web content is in a conversation, changes need your confirmation; and if private files have also been
  read, anything that contacts the internet needs your confirmation too.
- **Emergency stop.** The Stop button, Esc, saying "Jarvis, stop" or Ctrl+C in the console interrupts the
  request, kills the commands it started, silences speech and declines pending approvals.
- **A locked-down interface.** The interface listens only on this computer, requires a secret created at
  start-up, and rejects requests from other websites.
- **Audit log.** Every tool call, the decision and who made it are recorded locally.
- **Jarvis cannot reach its own settings or data**, so it cannot loosen its own rules.

## Settings

Most options are in the Settings panel. The rest are in the settings file (`jarvis config path` prints
its location: `%APPDATA%\Jarvis\config.toml` on Windows, `~/.config/jarvis/config.toml` on Linux).

```toml
[user]
name = ""               # what Jarvis should call you
honorific = ""          # e.g. "sir"

[claude]
model = "haiku"         # or "sonnet", "opus"
effort = "low"

[safety]
autonomy = "balanced"
approved_dirs = ["~/Jarvis"]
command_allowlist = []  # e.g. ["npm test", "make"]

[voice]
wake_word = "jarvis"
stt_model = "moonshine-base-en"    # "moonshine-tiny-en" is a smaller, slightly less accurate download
tts_voice = "en_GB-alan-medium"    # or the path to any Piper voice (.onnx)
```

## Development

```bash
pip install -e ".[claude,voice,dev]"
```

```bash
pytest
```

The tests run the sandbox, command screening, policy, every tool, the interface server and the voice
loop against a real temporary filesystem and real child processes; nothing in them uses your Claude
plan. Set `JARVIS_TEST_MODELS` to a folder to also run the round trip on the real speech models. Every
push builds and tests the Windows app on GitHub and republishes the preview installer.

```
src/jarvis/
  safety/    sandbox, command screening, policy, approvals, emergency stop
  tools/     files, shell, apps, system, memory, reminders, web + the executor they all pass through
  brain/     reasoning backends (Claude Code; any OpenAI-compatible service; a no-model demo)
  voice/     audio in/out, speech engines, model download, the listening loop
  server/    local interface server and the web interface
  agent.py   conversation manager     persona.py   standing instructions for the model
  core.py    wiring                   cli.py       commands
```

Speech synthesis uses `piper-tts`, which is GPL-3.0 licensed. It is an optional component, loaded only
when voice is on; if you redistribute a build that includes it, the GPL's terms apply to that build.
