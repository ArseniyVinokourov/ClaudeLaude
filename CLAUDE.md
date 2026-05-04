# ClaudeLaude Bot — Project Rules

## What this project is
Telegram bot (ClaudeLaude) that provides a Forum Topics UI for Claude Code. Each user runs their own instance locally. Bot sessions, hooks, permissions — all through Telegram.

## Language
- Project documentation, user-facing text, commit messages — Russian or English.
- Code and technical artifacts (Python, shell scripts, configs) — English only.

## Architecture
- `bot.py` — main module, Telegram long-polling loop, command handlers, session lifecycle
- `config.py` — .env loading, .state.json persistence
- `telegram.py` — Telegram Bot API wrapper (sendMessage, editMessage, etc.)
- `sessions.py` — Claude Code session manager (subprocess, stream-json parsing)
- `hooks.py` — HTTP server for Claude Code hooks (notifications, permission requests)
- `install.sh` / `setup.sh` / `update.sh` — distribution scripts
- `docs/` — install/update prompts for end users

## Key constraints
1. **Single-user bot.** OWNER_ID is the only authorized user. No multi-tenant.
2. **Local-only.** Everything runs on the user's machine. No external servers.
3. **Claude Code subprocess.** Sessions use `claude --resume` with `--output-format stream-json --verbose`.
4. **Telegram API limits.** Messages truncate at 4096 chars. Inline buttons ~25 chars on mobile. No markdown tables.
5. **Python 3.10+**, no heavy frameworks. Only `requests` + `python-dotenv`.
6. **Hooks respond JSON.** Format: `{"decision": {"behavior": "allow"}}` or `deny`. Timeout = auto-deny.
7. **_CLAUDE_BIN constant.** All subprocess calls to claude must use `_CLAUDE_BIN`, not bare `'claude'` (systemd PATH issue).
8. **stream-json --verbose.** Required since Claude Code 2.1.x. Message content is nested dict.
9. **Ephemeral General.** All messages in General topic auto-delete (5-15s), only pinned stays.
10. **Worker thread owns TurnState.** Only the worker thread manages status indicators, main thread hands off.

## Git
- Commit messages in English, present tense.
- One logical change per commit.
