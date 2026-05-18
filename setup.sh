#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

C_RESET='\033[0m'
C_BOLD='\033[1m'
C_GREEN='\033[32m'
C_YELLOW='\033[33m'
C_RED='\033[31m'

ok()   { echo -e "${C_GREEN}✓${C_RESET} $*"; }
warn() { echo -e "${C_YELLOW}⚠${C_RESET} $*"; }
err()  { echo -e "${C_RED}✗${C_RESET} $*"; }
bold() { echo -e "${C_BOLD}$*${C_RESET}"; }

# ── prerequisites ────────────────────────────────────────────────────
bold "Checking prerequisites..."

if ! command -v python3 &>/dev/null; then
    err "python3 not found. Install Python 3.10+."
    exit 1
fi
ok "python3 found: $(python3 --version)"

if ! command -v claude &>/dev/null; then
    warn "claude CLI not found in PATH."
    echo "  Install: https://docs.anthropic.com/en/docs/claude-code/getting-started"
    echo "  The bot won't be able to create sessions until claude is available."
else
    ok "claude CLI found"
fi

# ── .env ─────────────────────────────────────────────────────────────
if [ -f .env ]; then
    warn ".env already exists. Skipping creation."
    echo "  Edit it manually if needed: $SCRIPT_DIR/.env"
else
    bold "\nCreating .env config..."
    echo ""

    echo "1) Create a Telegram bot via @BotFather and paste the token:"
    read -rp "   BOT_TOKEN: " BOT_TOKEN
    if [ -z "$BOT_TOKEN" ]; then
        err "Token is required."
        exit 1
    fi

    echo ""
    echo "2) Your Telegram user ID (send /start to @userinfobot to find out):"
    read -rp "   OWNER_ID: " OWNER_ID
    if [ -z "$OWNER_ID" ]; then
        err "Owner ID is required."
        exit 1
    fi

    DEFAULT_PROJECTS="$HOME/Projects"
    echo ""
    echo "3) Directory with your projects (default: $DEFAULT_PROJECTS):"
    read -rp "   PROJECTS_DIR [$DEFAULT_PROJECTS]: " PROJECTS_DIR
    PROJECTS_DIR="${PROJECTS_DIR:-$DEFAULT_PROJECTS}"

    DEFAULT_PORT=9853
    echo ""
    echo "4) Hook server port (default: $DEFAULT_PORT):"
    read -rp "   HOOK_PORT [$DEFAULT_PORT]: " HOOK_PORT
    HOOK_PORT="${HOOK_PORT:-$DEFAULT_PORT}"

    echo ""
    bold "5) Security: unlock word (optional but recommended)"
    echo ""
    echo "   If someone gets access to your Telegram account, they can"
    echo "   control the bot and run commands on your machine."
    echo ""
    echo "   The /kill command instantly stops all sessions and locks"
    echo "   the bot. To restore access from Telegram, you need an"
    echo "   unlock word — a secret phrase only you know."
    echo ""
    echo "   Without an unlock word, /kill can only be reversed by"
    echo "   deleting the .kill file directly on your machine."
    echo ""
    read -rp "   UNLOCK_WORD: " UNLOCK_WORD

    cat > .env <<EOF
BOT_TOKEN=$BOT_TOKEN
OWNER_ID=$OWNER_ID
PROJECTS_DIR=$PROJECTS_DIR
HOOK_PORT=$HOOK_PORT
UNLOCK_WORD=$UNLOCK_WORD
EOF

    ok ".env created"
fi

# ── device monitoring ────────────────────────────────────────────────
bold "\nDevice monitoring (Telethon)"
echo "Monitor active Telegram sessions for unauthorized devices."
echo "Requires API credentials from https://my.telegram.org"
echo ""
read -rp "Set up device monitoring? [Y/n]: " SETUP_MONITOR
SETUP_MONITOR="${SETUP_MONITOR:-Y}"

if [[ "$SETUP_MONITOR" =~ ^[Yy] ]]; then
    read -rp "   TG_API_ID: " TG_API_ID
    read -rp "   TG_API_HASH: " TG_API_HASH
    if [ -n "$TG_API_ID" ] && [ -n "$TG_API_HASH" ]; then
        echo "TG_API_ID=$TG_API_ID" >> .env
        echo "TG_API_HASH=$TG_API_HASH" >> .env
        ok "API credentials added to .env"
        echo ""
        echo "  After setup, run this once to authenticate:"
        bold "  cd $SCRIPT_DIR && .venv/bin/python -c \"import device_monitor; device_monitor.get_sessions()\""
        echo "  (Enter phone number and code when prompted)"
    else
        warn "Skipping — both API_ID and API_HASH are required."
    fi
fi

# ── python venv ──────────────────────────────────────────────────────
bold "\nSetting up Python environment..."

if [ ! -d .venv ]; then
    python3 -m venv .venv
    ok "Virtual environment created"
else
    ok "Virtual environment exists"
fi

.venv/bin/pip install -q -r requirements.txt
ok "Dependencies installed"

# ── Claude Code hooks ────────────────────────────────────────────────
bold "\nClaude Code hooks (optional)"
echo "Hooks let the bot receive notifications and permission requests"
echo "from Claude Code sessions you start in the terminal."
echo ""
read -rp "Set up Claude Code hooks? [Y/n]: " SETUP_HOOKS
SETUP_HOOKS="${SETUP_HOOKS:-Y}"

if [[ "$SETUP_HOOKS" =~ ^[Yy] ]]; then
    HOOK_PORT=$(grep '^HOOK_PORT=' .env | cut -d= -f2)
    HOOK_PORT="${HOOK_PORT:-9853}"

    SETTINGS_FILE="$HOME/.claude/settings.json"
    mkdir -p "$(dirname "$SETTINGS_FILE")"

    NOTIFY_CMD="INPUT=\$(cat); printf '%s' \"\$INPUT\" | curl -sf --max-time 8 -X POST http://127.0.0.1:${HOOK_PORT}/hook/notification -H 'Content-Type: application/json' -d @- 2>/dev/null"
    PERM_CMD="INPUT=\$(cat); printf '%s' \"\$INPUT\" | curl -sf --max-time 125 -X POST http://127.0.0.1:${HOOK_PORT}/hook/permission -H 'Content-Type: application/json' -d @- 2>/dev/null"

    if [ -f "$SETTINGS_FILE" ]; then
        EXISTING=$(cat "$SETTINGS_FILE")
    else
        EXISTING='{}'
    fi

    python3 -c "
import json, sys

settings = json.loads('''$EXISTING''')
hooks = settings.setdefault('hooks', {})

hooks['Notification'] = [{'hooks': [{'type': 'command', 'command': '''$NOTIFY_CMD''', 'timeout': 10}]}]
hooks['PermissionRequest'] = [{'hooks': [{'type': 'command', 'command': '''$PERM_CMD''', 'timeout': 130}]}]

json.dump(settings, open('$SETTINGS_FILE', 'w'), indent=2)
print('ok')
"
    ok "Claude Code hooks configured in $SETTINGS_FILE"
fi

# ── smoke test ───────────────────────────────────────────────────────
bold "\nSmoke test"

if .venv/bin/python -c "import bot, telegram, sessions, hooks, config, version" 2>/tmp/claudelaude_smoke.log; then
    ok "Python imports OK"
else
    warn "Python import check failed (see /tmp/claudelaude_smoke.log)"
fi

if command -v curl &>/dev/null; then
    BOT_TOKEN_CHECK=$(grep '^BOT_TOKEN=' .env 2>/dev/null | cut -d= -f2-)
    if [ -n "$BOT_TOKEN_CHECK" ]; then
        if curl -fsS --max-time 10 "https://api.telegram.org/bot${BOT_TOKEN_CHECK}/getMe" \
            | grep -q '"ok":true'; then
            ok "Telegram getMe OK"
        else
            warn "Telegram getMe failed — check BOT_TOKEN in .env"
        fi
    fi
fi

# ── done ─────────────────────────────────────────────────────────────
bold "\n--- Setup complete ---"
echo ""
echo "Next steps:"
echo "  1. Create a Telegram group with Topics enabled"
echo "  2. Add your bot as admin (manage topics, send/delete messages)"
echo "  3. Start the bot:"
echo ""
bold "     cd $SCRIPT_DIR && .venv/bin/python bot.py"
echo ""
echo "  4. Send /setup in the group to link it"
echo "  5. Send /new to create your first Claude session"
echo ""
