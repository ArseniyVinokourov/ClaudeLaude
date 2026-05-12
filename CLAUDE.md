# ClaudeLaude Bot — Project Rules

## What this project is
Telegram bot (ClaudeLaude) that provides a Forum Topics UI for Claude Code. Each user runs their own instance locally. Bot sessions, hooks, permissions — all through Telegram.

## Language
- Project documentation, user-facing text, commit messages — Russian or English.
- Code and technical artifacts (Python, shell scripts, configs) — English only.
- Documentation files (README, CONTRIBUTING, docs/*) — **English only** by default. Russian only when the user explicitly asks for a specific file.

## Architecture
- `bot.py` — main module, Telegram long-polling loop, command handlers, session lifecycle
- `config.py` — .env loading, .state.json persistence
- `telegram.py` — Telegram Bot API wrapper (sendMessage, editMessage, etc.)
- `sessions.py` — Claude Code session manager (subprocess, stream-json parsing)
- `hooks.py` — HTTP server for Claude Code hooks (notifications, permission requests)
- `install.sh` / `setup.sh` / `update.sh` — distribution scripts
- `docs/` — install/update prompts for end users

## Key technical constraints
1. **Single-user bot.** OWNER_ID is the only authorized user. No multi-tenant.
2. **Local-only.** Everything runs on the user's machine. No external servers.
3. **Claude Code subprocess.** Sessions use `claude --resume` with `--output-format stream-json --verbose`.
4. **Telegram API limits.** Messages truncate at 4096 chars. Inline buttons ~25 chars on mobile. No markdown tables, no `<pre>` blocks. Design for ~35-char mobile width.
5. **Python 3.10+**, no heavy frameworks. Only `requests` + `python-dotenv`.
6. **Hooks respond JSON.** Format: `{"decision": {"behavior": "allow"}}` or `deny` — NOT `permissionDecision`. Timeout = auto-deny. Verify any new hook format against Claude Code docs (use the claude-code-guide subagent).
7. **`_CLAUDE_BIN` constant.** All subprocess calls to claude must use `_CLAUDE_BIN`, not bare `'claude'` (systemd PATH issue).
8. **`stream-json --verbose`.** Required since Claude Code 2.1.x. Message content is nested dict (`data["message"]["content"]`).
9. **Ephemeral General.** All messages in General topic auto-delete (5-15s), only the pinned message stays. Exactly one pinned message — new pin needs `unpinAllChatMessages` first.
10. **Worker thread owns `TurnState`.** Only the worker thread manages status indicators and the turn timer. Main thread hands off, never touches.
11. **No hard wall-clock ceiling on sessions.** Only inactivity timeout kills sessions. Active work must never be interrupted by duration alone.
12. **Bot offline → hook fails silently.** Do NOT add an auto-allow fallback in the hook chain. When the bot is unreachable, Claude Code falls back to its default interactive behavior.
13. **TG message IDs are global within the chat,** not per-topic. Never sweep wide ID ranges with `deleteMessage` — you'll hit unrelated session topics. Target specific IDs only.
14. **`editMessageText` without `reply_markup` removes inline keyboard.** Re-pass `buttons=` on every edit that should keep its buttons.
15. **Security commands from Telegram are one-directional.** Lock from TG is fine; unlock from TG defeats the purpose. Threat model = attacker has full TG access. Recovery paths require a different channel.

## Git
- Commit messages in English, present tense.
- One logical change per commit.
- Branch names use a category prefix: `feat/`, `fix/`, `ui/`, `chore/`, `ops/`, `security/`, `docs/`, `refactor/`. Same prefix shows up as `[prefix]` tag on each item in the project TODO list.
- Trunk-based: branch from main → PR → squash-merge → auto-tag. Never push to main directly.
