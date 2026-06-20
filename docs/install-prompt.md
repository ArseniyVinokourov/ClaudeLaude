# Installing ClaudeLaude

Help me install ClaudeLaude — a Telegram bot for Claude Code.

## Prerequisites

Check that I have:
- Python 3.10+ (`python3 --version`)
- git (`git --version`)
- Claude Code CLI installed and logged in (`claude --version`)

If anything is missing, help me install it step by step.

## Install

Run this one-liner:

```bash
bash <(curl -s https://raw.githubusercontent.com/ArseniyVinokourov/ClaudeLaude/main/install.sh)
```

Or manually:

```bash
git clone https://github.com/ArseniyVinokourov/ClaudeLaude.git ~/claude-bot
cd ~/claude-bot
bash setup.sh
```

The setup script will ask for:
1. **BOT_TOKEN** — Telegram bot token. If I don't have one, explain how to create a bot via @BotFather (2 minutes).
2. **OWNER_ID** — my Telegram user ID. Explain how to get it via @userinfobot.
3. **PROJECTS_DIR** — directory with my projects.
4. **HOOK_PORT** — just press Enter for default.
5. **UNLOCK_WORD** — a secret phrase to reverse the `/kill` safety switch from Telegram. Explain why it matters, then let me pick one.

It then offers several optional steps — all safe to skip with Enter, the bot still works without them:
- **Device monitoring** — needs API credentials from my.telegram.org; watches my account for unauthorized devices.
- **Speech recognition** — a Whisper model, or a frames-only video decoder. Can also be installed later, on the first voice/video message.
- **Media storage alert threshold** — when to DM me about the uploads folder size.
- **Claude Code hooks** — answer Y to also catch events from terminal Claude sessions, not just bot-spawned ones.
- **/bot-mirror command** — answer Y to enable mirroring a terminal session into Telegram.

## Telegram setup

After install, walk me through:
1. Create a Telegram group (any name)
2. Enable Topics: Group Settings > Topics > On
3. Add bot as admin with permissions: Manage Topics, Delete Messages

(The bot must be running before /setup can answer — start it first, below.)

## First run

```bash
cd ~/claude-bot && .venv/bin/python bot.py
```

With the bot running, send /setup in the group to link it, then /new to create the first session. Make sure replies come through.

## Autostart (optional)

If I want the bot to start automatically, help me set up a systemd service or bashrc autostart for my OS. On Windows/WSL, `scripts/windows/Install-KeepWSLAlive.ps1` keeps WSL (and the bot) alive.

## Uninstall

To remove the bot completely later, run `bash uninstall.sh` from the bot directory — it clears state, virtualenvs, the `~/.claude` hooks and `/bot-mirror` command, the shell wrapper, Whisper models, and temp files. `--dry-run` previews; `--purge-dir` also deletes the folder. Deleting the bot on @BotFather and the Telegram group is manual.
