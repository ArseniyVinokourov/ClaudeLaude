# Terminal mirror — `/bot-mirror`

Bridge a terminal Claude session to a Telegram topic. Read the
conversation from your phone; type from the topic into the running
terminal as if you were at the keyboard.

## How it works

Two channels:

- **Output** (terminal → topic): the bot tails the session's JSONL
  transcript at `~/.claude/projects/<encoded-cwd>/<csid>.jsonl` and
  projects each new user/assistant/tool event into the mirror topic.
  Works for any terminal Claude session, no setup change required.
- **Input** (topic → terminal): the bot writes bytes into the running
  terminal Claude's stdin via `dtach -p <socket>`. dtach is a tiny
  (~30 KB) PTY relay — it does not render, composite, or interpret
  escape codes; Claude draws directly into your terminal as if dtach
  were not in the chain.

The input bridge requires Claude to be running under dtach. The
`/bot-mirror` slash command handles this automatically: if your
current Claude wasn't started under dtach, it reopens the same
session (`claude --resume`) under dtach for you, with the original
chat history continuing in place.

## One-time setup

`./setup.sh` silently installs a small shell function (`claude()`)
into your active shell config (`~/.bashrc`, `~/.zshrc`, or
`~/.config/fish/functions/claude.fish` — picked by `$SHELL`). The
function is a **transparent passthrough by default** — `claude`
behaves exactly like the bare binary until you ask `/bot-mirror`
inside a session to reopen it under dtach.

On first edit, the rc is backed up to `<rc>.before-claudelaude`. The
installer is idempotent (a second run replaces our block in place)
and refuses to overwrite an unrelated `claude` function you already
have — remove your version, then re-run.

## Using it

1. Start Claude however you usually do: `claude`.
2. Type `/bot-mirror` inside Claude.
3a. If you're already under dtach, the bot creates the mirror topic
    immediately and the input bridge is live.
3b. If you're in a plain shell, the slash command writes a sentinel
    file and asks Claude to exit. The shell wrapper sees the sentinel,
    tells the bot to open the mirror topic, then relaunches Claude
    under dtach with `--resume <same-session-id>`. The history you
    had appears in the new session, the topic shows up in Telegram,
    and the input bridge is live — you didn't type a second command.
4. Open the topic URL on your phone. Output streams in.
5. To type from the phone, send a message in that topic. The bot
   acks 👀 and pushes the text into Claude's stdin via the dtach
   socket — you see the characters appear in the input box and the
   line auto-submits.

A second `/bot-mirror` for the same session returns the same topic —
the linkage is idempotent. If you re-launched terminal Claude (new
socket path), it refreshes the input binding automatically.

## Bypass the wrapper for one shell

If you want to be sure `claude` runs without ever going through the
wrapper (e.g. inside a script), set `CLAUDELAUDE_NO_WRAP=1`:

```bash
CLAUDELAUDE_NO_WRAP=1 claude
```

Note that the wrapper is already a no-op until you call `/bot-mirror`
and ask for a swap — this variable is rarely needed in practice.

## When input is not bridged

If `dtach` is not installed, or you're inside a session where the
swap can't run (no shell wrapper at all, e.g. a `.bashrc` we couldn't
write to), the mirror falls back to **output-only**. The bot reports
this on registration. Output still works normally; you just can't
type from Telegram.

## Running dtach manually

You can skip the wrapper entirely and just launch Claude in dtach
yourself:

```bash
export CLAUDELAUDE_DTACH_SOCKET=/tmp/my-mirror.sock
dtach -A "$CLAUDELAUDE_DTACH_SOCKET" -E -z -r winch claude
```

The flags: `-A` (attach to socket or create it), `-E` (disable the
default Ctrl-\ detach key so it never pulls you out by accident),
`-z` (disable suspend), `-r winch` (send SIGWINCH on re-attach so
Claude redraws at the right size).

Export `CLAUDELAUDE_DTACH_SOCKET` BEFORE running `claude` — the
`/bot-mirror` slash command reads it from the environment to tell
the bot where to send input.

## Lifecycle

The bot's healthcheck (every 30 s) detects:

- The mirror topic was deleted in Telegram → unregister silently.
- The dtach socket file is gone (terminal exited; dtach auto-removes
  the socket on child exit) → flip the mirror to output-only with a
  notice.

A long idle pause between turns is *not* a kill signal — the mirror
stays alive as long as you want it to. Unregister it explicitly by
deleting the topic in Telegram.

Mirrors persist in `.mirrors.json` and are restored on bot restart.

## Disabling and uninstalling

To remove the wrapper, edit your shell rc and delete the block
between `# >>> claudelaude swap >>>` and `# <<< claudelaude swap <<<`
markers (`~/.bashrc`, `~/.zshrc`) — or delete the function file
(`~/.config/fish/functions/claude.fish`) outright for fish. Or
restore the backup: `cp ~/.bashrc.before-claudelaude ~/.bashrc`.
After that, `claude` is just the binary again, plain.

## What is NOT bridged

- Photos, files, and stickers sent in a mirror topic — terminal
  Claude has no convention for arriving binary attachments. The bot
  refuses these with an ephemeral.
- Permission prompts (Allow/Deny). Those continue to flow through
  the bot's existing per-session topic, which predates and is
  orthogonal to mirror.

## Known limitations

- **Claude TUI display garbles on terminal resize in IntelliJ IDEA's
  WSL terminal.** When the IDE window is resized, Claude's alt-screen
  re-render stacks on top of the previous frame instead of replacing
  it (history fragments appear repeated, status bar lands as a chat
  line). This reproduces with bare `claude` too — it is not caused by
  the mirror wrapper. Workaround: use Windows Terminal alongside the
  IDE, or avoid resizing mid-session.

## Troubleshooting

- **"I typed `/bot-mirror` and got 'connection refused'."** — the bot
  isn't running. Start it: `cd <repo> && .venv/bin/python bot.py`.
- **"Mirror is output-only and I want input."** — install dtach
  (`apt install dtach` / `brew install dtach`), re-run `./setup.sh`,
  open a new terminal so the wrapper function loads, and re-launch
  `claude`.
- **"I see two copies of the same message in the topic."** — when you
  type from TG the bot acks via reaction, then Claude processes the
  prompt, then the JSONL follower projects it as the canonical user
  message. The duplication is benign.
- **"I had my own `claude()` function in `~/.bashrc` and setup
  refused to install."** — remove or rename your version and re-run
  `./setup.sh`.
