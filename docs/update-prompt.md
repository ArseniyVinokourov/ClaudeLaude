# Updating ClaudeLaude

Help me update ClaudeLaude — my Telegram bot for Claude Code.

## Quick update

```bash
cd ~/claude-bot && bash update.sh
```

The script will:
- Fetch the latest version from GitHub
- Detect any local code changes I've made
- Back up modified files before overwriting
- Update dependencies
- Offer to update Claude Code hooks

## From the bot

I can also send `/update` in Telegram — the bot checks for updates and can apply them with one button.

## If something breaks

Modified files are backed up in `.backup_*/modified/` inside the bot directory. I can compare with `diff` and restore if needed.
