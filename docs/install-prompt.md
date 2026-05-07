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
5. **Hooks** — answer Y when asked.

## Telegram setup

After install, walk me through:
1. Create a Telegram group (any name)
2. Enable Topics: Group Settings > Topics > On
3. Add bot as admin with permissions: Manage Topics, Delete Messages
4. Send /setup in the group

## First run

```bash
cd ~/claude-bot && .venv/bin/python bot.py
```

Then send /new to create the first session. Make sure replies come through.

## Autostart (optional)

If I want the bot to start automatically, help me set up a systemd service or bashrc autostart for my OS.
