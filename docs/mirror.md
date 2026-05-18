# Terminal mirror — `/bot mirror`

Bridge a terminal Claude session to a Telegram topic. Read the
conversation from your phone; type from the topic into the running
terminal.

## How it works

Two channels:

- **Output** (terminal → topic): the bot tails the session's JSONL
  transcript at `~/.claude/projects/<encoded-cwd>/<csid>.jsonl` and
  projects each new user/assistant/tool event into the mirror topic.
  Works for any terminal Claude session, no setup change required.
- **Input** (topic → terminal): the bot uses `dtach -p` to push bytes
  into the running terminal session's PTY. Requires the terminal
  Claude to be running inside a dtach session — the bot's PATH shim
  arranges this transparently.

## One-time setup

`setup.sh` handles everything when you say yes to the `/bot mirror`
and "transparent shim" prompts:

1. Installs `~/.claude/commands/bot.md` (the slash command).
2. Installs `~/.local/bin/claude` and `~/.local/bin/claude-bot` —
   tiny shims that route interactive `claude` runs through dtach.
3. Warns if `dtach` is missing (`apt install dtach` on
   Debian/Ubuntu, `brew install dtach` on macOS).
4. Warns if `~/.local/bin` is not ahead of the real claude in PATH.

After setup, running `claude` continues to work exactly as before —
the shim only intercepts interactive REPL invocations. `claude -p
...`, `claude --version`, and any piped-stdin call go straight to the
real binary.

If you'd rather not shadow `claude`, decline the shim install and
use `claude-bot` explicitly when you want a mirrorable session.

## Using it

1. Start Claude in any terminal: `claude` (or `claude-bot` if you
   declined the shim).
2. Type `/bot mirror` inside Claude.
3. Claude calls the local bot's `/hook/open_in_bot` endpoint, the
   bot creates a forum topic in your group, and prints the URL.
4. Open the URL on your phone. Output streams in.
5. To type from the phone, send a message in that topic. The bot
   reacts 👀 on receipt and pushes the text into the terminal's
   stdin.

A second `/bot mirror` for the same session returns the same topic —
the linkage is idempotent.

## When input is not bridged

If the terminal Claude wasn't started under the shim or `dtach` is
not installed, the mirror runs in **output-only** mode. The bot
reports this when registering and surfaces an ephemeral
"Output-only mirror" notice if you try to type. Output still works
normally.

## Lifecycle

The bot's healthcheck (every 30 s) detects:

- The mirror topic was deleted in Telegram → unregister silently.
- The dtach socket disappeared (terminal exited) → flip the mirror
  to output-only with a notice.
- The JSONL hasn't been touched for 30 minutes → assume the terminal
  session is idle/ended, post a notice, unregister.

Mirrors persist in `.mirrors.json` and are restored on bot restart.

## What is NOT bridged in v1

- Photos, files, and stickers sent in a mirror topic — terminal
  Claude has no convention for arriving binary attachments. The bot
  refuses these with an ephemeral.
- Permission prompts (Allow/Deny). Those continue to flow through
  the bot's existing per-session topic, which predates and is
  orthogonal to mirror.

## Troubleshooting

- "I typed `/bot mirror` and got 'connection refused'." — the bot
  isn't running. Start it: `cd <repo> && .venv/bin/python bot.py`.
- "Mirror is output-only and I want input." — install dtach
  (`apt install dtach` / `brew install dtach`) and re-run
  `setup.sh`, or just restart your terminal Claude under the shim.
- "I see two copies of the same message in the topic." — when you
  type from TG the bot acks via reaction, then Claude processes the
  prompt, then the JSONL follower projects it as the canonical user
  message. The duplication is benign and can be deduped later.
