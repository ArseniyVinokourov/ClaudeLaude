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
  were not in the chain. The input bridge requires Claude to be
  running under dtach (the `claude()` wrapper installed by
  `./setup.sh` handles this transparently).

The input bridge is the only piece that needs setup. The output
channel works out of the box.

## One-time setup

Run `./setup.sh` and answer **Y** at the "Enable terminal mirror"
prompt. The installer:

1. Verifies `dtach` is installed (and points you at `apt install
   dtach` or `brew install dtach` if not).
2. Backs up `~/.bashrc` to `~/.bashrc.before-claudelaude` (only on
   first edit).
3. Appends a marker-delimited block to `~/.bashrc` containing a
   `claude()` shell function. The function runs `claude` under
   `dtach -A /tmp/clmirror-<shell-pid>.sock` so the bot can write to
   the same socket. It also exports `CLAUDELAUDE_DTACH_SOCKET` so the
   `/bot-mirror` slash command knows the socket path.

Your everyday workflow stays identical: you keep typing `claude` and
see what you've always seen. dtach is transparent — no extra status
bar, no key bindings to learn, no compositor between Claude and your
terminal. To use the new shell, open a new terminal or
`source ~/.bashrc`.

If you decline, the mirror still works in output-only mode. You can
opt in later by re-running `./setup.sh`.

## Bypass for one shell

Set `CLAUDELAUDE_NO_WRAP=1` before running `claude`:

```bash
CLAUDELAUDE_NO_WRAP=1 claude
```

The wrapper checks the variable and forwards directly to the real
`claude` binary with no dtach involved. Useful for one-off
non-mirrored sessions without removing the wrapper.

The wrapper also detects that you're already running under a
ClaudeLaude dtach socket (`$CLAUDELAUDE_DTACH_SOCKET` is set) and
skips wrapping again — no nested dtach.

## Using it

1. Start Claude in any terminal: `claude` (the wrapper does the rest).
2. Type `/bot-mirror` inside Claude.
3. Claude calls the local bot's `/hook/open_in_bot` endpoint, the bot
   creates a forum topic in your group, and prints the URL.
4. Open the URL on your phone. Output streams in.
5. To type from the phone, send a message in that topic. The bot
   reacts 👀 on receipt and pushes the text into Claude's stdin via
   the dtach socket — you see the characters appear in the input box
   and the line auto-submits.

A second `/bot-mirror` for the same session returns the same topic —
the linkage is idempotent. If you re-launched terminal Claude (new
socket path), it refreshes the input binding automatically.

## When input is not bridged

If terminal Claude was started without the wrapper (e.g.
`CLAUDELAUDE_NO_WRAP=1`, declined the install, or running outside our
wrapped shell), the mirror falls back to **output-only**. The bot
reports this on registration and surfaces an ephemeral "Output-only
mirror" notice if you try to type from Telegram. Output still works
normally.

## Running dtach manually (without the wrapper)

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

To remove the wrapper, edit `~/.bashrc` and delete the block between
`# >>> claudelaude mirror >>>` and `# <<< claudelaude mirror <<<`
markers. Or restore the backup: `cp ~/.bashrc.before-claudelaude
~/.bashrc`. After that, `claude` is just the binary again with no
dtach involved.

## What is NOT bridged in v1

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
  IDE, or avoid resizing mid-session. Reported to Anthropic /
  JetBrains as appropriate.

## Troubleshooting

- "I typed `/bot-mirror` and got 'connection refused'." — the bot
  isn't running. Start it: `cd <repo> && .venv/bin/python bot.py`.
- "Mirror is output-only and I want input." — install dtach
  (`apt install dtach` / `brew install dtach`), re-run `./setup.sh`,
  open a new terminal so the wrapper function loads, and re-launch
  `claude`.
- "I see two copies of the same message in the topic." — when you
  type from TG the bot acks via reaction, then Claude processes the
  prompt, then the JSONL follower projects it as the canonical user
  message. The duplication is benign and can be deduped later.
- "I had my own `claude()` function in `~/.bashrc` and setup aborted." —
  remove or rename it and re-run `./setup.sh`, or skip the wrapper
  install and rely on output-only mirror.
