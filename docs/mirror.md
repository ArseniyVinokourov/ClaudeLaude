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
- **Input** (topic → terminal): the bot uses `tmux send-keys` to type
  bytes into the running terminal Claude's pane, exactly as if you had
  typed them at the keyboard. Requires the terminal Claude to be
  running inside a tmux pane.

The input bridge is the only piece that needs setup. The output
channel works out of the box.

## One-time setup

Run `./setup.sh` and answer **Y** at the "Enable terminal mirror"
prompt. The installer:

1. Verifies `tmux` is installed (and points you at `apt install tmux`
   or `brew install tmux` if not).
2. Backs up `~/.bashrc` to `~/.bashrc.before-claudelaude` (only on
   first edit).
3. Appends a marker-delimited block to `~/.bashrc` containing a
   `claude()` shell function. The function transparently runs
   `claude` inside a dedicated tmux session so the bot can address
   its pane via `send-keys`.
4. The tmux session has its status bar disabled, so visually the
   wrapped claude looks identical to a plain one.

Your everyday workflow stays identical: you keep typing `claude` and
see what you've always seen. To use the new shell, open a new
terminal or `source ~/.bashrc`.

If you decline, the mirror still works in output-only mode. You can
opt in later by re-running `./setup.sh`.

## Bypass for one shell

Set `CLAUDELAUDE_NO_TMUX=1` before running `claude`:

```bash
CLAUDELAUDE_NO_TMUX=1 claude
```

The wrapper function checks the variable and forwards directly to the
real `claude` binary with no tmux involved. Useful for one-off
non-mirrored sessions without removing the wrapper.

The wrapper also detects that you're already inside a tmux pane
(`$TMUX` non-empty) and skips wrapping in that case — no nested tmux.

## Using it

1. Start Claude in any terminal: `claude` (the wrapper does the rest).
2. Type `/bot-mirror` inside Claude.
3. Claude calls the local bot's `/hook/open_in_bot` endpoint, the bot
   creates a forum topic in your group, and prints the URL.
4. Open the URL on your phone. Output streams in.
5. To type from the phone, send a message in that topic. The bot
   reacts 👀 on receipt and pushes the text into the terminal Claude
   as keystrokes — you see the characters appear in the input box and
   the line auto-submits.

A second `/bot-mirror` for the same session returns the same topic —
the linkage is idempotent. If you re-launched terminal Claude (new
tmux pane), it refreshes the input binding automatically.

## When input is not bridged

If terminal Claude was started without the wrapper (e.g.
`CLAUDELAUDE_NO_TMUX=1`, declined the install, or running outside our
wrapped shell), the mirror falls back to **output-only**. The bot
reports this on registration and surfaces an ephemeral "Output-only
mirror" notice if you try to type from Telegram. Output still works
normally.

## Running tmux manually (without the wrapper)

You can skip the wrapper entirely and just launch Claude in a tmux
session yourself: `tmux new -s mirror claude`. The mirror works the
same way — the bot reads `$TMUX`/`$TMUX_PANE` from the slash-command
context.

If you go this route, you'll probably want to hide tmux's default
green status bar (which the wrapper disables automatically). Add this
to `~/.tmux.conf`:

```
set -g status off
```

The bot deliberately doesn't toggle this for you — calling
`set-option status off` on a live session sends a SIGWINCH that makes
Claude's TUI redraw and shuffle its visible scrollback, which is
disorienting mid-conversation. Setting it once in your config
prevents the bar from ever appearing, so no resize-and-redraw cycle.

## Lifecycle

The bot's healthcheck (every 30 s) detects:

- The mirror topic was deleted in Telegram → unregister silently.
- The tmux pane disappeared (terminal exited or detached) → flip the
  mirror to output-only with a notice.

A long idle pause between turns is *not* a kill signal — the mirror
stays alive as long as you want it to. Unregister it explicitly by
deleting the topic in Telegram.

Mirrors persist in `.mirrors.json` and are restored on bot restart.

## Disabling and uninstalling

To remove the wrapper, edit `~/.bashrc` and delete the block between
`# >>> claudelaude mirror >>>` and `# <<< claudelaude mirror <<<`
markers. Or restore the backup: `cp ~/.bashrc.before-claudelaude
~/.bashrc`. After that, `claude` is just the binary again with no
tmux involved.

## What is NOT bridged in v1

- Photos, files, and stickers sent in a mirror topic — terminal
  Claude has no convention for arriving binary attachments. The bot
  refuses these with an ephemeral.
- Permission prompts (Allow/Deny). Those continue to flow through
  the bot's existing per-session topic, which predates and is
  orthogonal to mirror.

## Troubleshooting

- "I typed `/bot-mirror` and got 'connection refused'." — the bot
  isn't running. Start it: `cd <repo> && .venv/bin/python bot.py`.
- "Mirror is output-only and I want input." — install tmux
  (`apt install tmux` / `brew install tmux`), re-run `./setup.sh`,
  open a new terminal so the wrapper function loads, and re-launch
  `claude`.
- "I see two copies of the same message in the topic." — when you
  type from TG the bot acks via reaction, then Claude processes the
  prompt, then the JSONL follower projects it as the canonical user
  message. The duplication is benign and can be deduped later.
- "I had my own `claude()` function in `~/.bashrc` and setup aborted." —
  remove or rename it and re-run `./setup.sh`, or skip the wrapper
  install and rely on output-only mirror.
