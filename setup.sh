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

# ── Claude slash command: /bot ───────────────────────────────────────
bold "\n/bot mirror slash command"
echo "Installs ~/.claude/commands/bot.md so you can run '/bot mirror'"
echo "inside any Claude session to open a Telegram topic for it."
echo ""
read -rp "Install /bot mirror command? [Y/n]: " SETUP_BOT_CMD
SETUP_BOT_CMD="${SETUP_BOT_CMD:-Y}"

if [[ "$SETUP_BOT_CMD" =~ ^[Yy] ]]; then
    CMD_SRC="$SCRIPT_DIR/scripts/claude_commands/bot.md"
    CMD_DST_DIR="$HOME/.claude/commands"
    CMD_DST="$CMD_DST_DIR/bot.md"
    if [ -f "$CMD_SRC" ]; then
        mkdir -p "$CMD_DST_DIR"
        cp "$CMD_SRC" "$CMD_DST"
        ok "Installed $CMD_DST"
    else
        warn "Slash command source missing at $CMD_SRC — skipping"
    fi
fi

# ── Mirror PATH shim (~/.local/bin/claude) ───────────────────────────
bold "\nTerminal-mirror shim (optional)"
echo "Installs a tiny wrapper at ~/.local/bin/claude that routes"
echo "interactive 'claude' invocations through dtach so the bot's"
echo "/bot mirror command can stream input from your phone into the"
echo "running session. Non-interactive runs (-p, --version, piped)"
echo "are forwarded straight to the real claude."
echo ""

SHIM_SRC="$SCRIPT_DIR/scripts/claude-shim"
SHIM_BIN_DIR="$HOME/.local/bin"
SHIM_CLAUDE="$SHIM_BIN_DIR/claude"
SHIM_CLAUDEBOT="$SHIM_BIN_DIR/claude-bot"

if ! command -v dtach &>/dev/null; then
    warn "'dtach' is not installed. Mirror will be output-only until you install it."
    echo "  Debian/Ubuntu: sudo apt install dtach"
    echo "  macOS:         brew install dtach"
fi

REAL_CLAUDE=""
# Resolve real claude by skipping anything in ~/.local/bin/.
old_IFS="$IFS"; IFS=:
for d in $PATH; do
    cand="$d/claude"
    [ -x "$cand" ] || continue
    cand_real="$(readlink -f "$cand" 2>/dev/null || echo "$cand")"
    # Skip if this resolves to either of our planned shim paths.
    [ "$cand_real" = "$(readlink -f "$SHIM_CLAUDE" 2>/dev/null || echo "$SHIM_CLAUDE")" ] && continue
    [ "$cand_real" = "$(readlink -f "$SHIM_CLAUDEBOT" 2>/dev/null || echo "$SHIM_CLAUDEBOT")" ] && continue
    REAL_CLAUDE="$cand"
    break
done
IFS="$old_IFS"

if [ -z "$REAL_CLAUDE" ]; then
    warn "No 'claude' binary found on PATH — skipping shim install."
    echo "Install Claude Code first, then re-run setup.sh."
else
    echo "Real claude: $REAL_CLAUDE"
    read -rp "Install transparent shim at $SHIM_CLAUDE? [Y/n]: " INSTALL_SHIM
    INSTALL_SHIM="${INSTALL_SHIM:-Y}"

    if [[ "$INSTALL_SHIM" =~ ^[Yy] ]]; then
        mkdir -p "$SHIM_BIN_DIR"
        cp "$SHIM_SRC" "$SHIM_CLAUDE"
        cp "$SHIM_SRC" "$SHIM_CLAUDEBOT"
        chmod +x "$SHIM_CLAUDE" "$SHIM_CLAUDEBOT"
        ok "Installed $SHIM_CLAUDE and $SHIM_CLAUDEBOT"

        # Verify PATH ordering — shim must come before the real claude.
        case ":$PATH:" in
            *:"$SHIM_BIN_DIR":*) PATH_OK=1 ;;
            *) PATH_OK=0 ;;
        esac
        if [ "$PATH_OK" = "0" ]; then
            warn "$SHIM_BIN_DIR is not in your PATH."
            echo "Add this to your shell rc (~/.bashrc / ~/.zshrc):"
            echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
        else
            # Confirm the shim wins. `command -v claude` should now
            # point at our shim (or at least, our shim should come
            # first in PATH lookups). We don't try to verify it from
            # a fresh shell — just trust the ordering.
            ok "PATH includes $SHIM_BIN_DIR"
        fi
    else
        ok "Skipping transparent shim. Use 'claude-bot' explicitly if you want mirror."
    fi
fi

# ── bot profile (one-time branding) ──────────────────────────────────
bold "\nBot profile"
echo "Set the bot's display name, short description (search preview),"
echo "and long description (shown in the empty-chat screen)."
echo "Press Enter to keep current values."
echo ""

BOT_TOKEN_BRAND=$(grep '^BOT_TOKEN=' .env 2>/dev/null | cut -d= -f2-)
if [ -n "$BOT_TOKEN_BRAND" ] && command -v curl &>/dev/null; then
    read -rp "   Bot name [ClaudeLaude]: " BOT_NAME
    BOT_NAME="${BOT_NAME:-ClaudeLaude}"
    read -rp "   Short description [Claude Code in Telegram]: " BOT_SHORT
    BOT_SHORT="${BOT_SHORT:-Claude Code in Telegram}"
    DEFAULT_LONG="Run Claude Code sessions from Telegram. Forum topics per session, permission requests, file/image upload, /usage, /history."
    read -rp "   Long description [default]: " BOT_LONG
    BOT_LONG="${BOT_LONG:-$DEFAULT_LONG}"

    API="https://api.telegram.org/bot${BOT_TOKEN_BRAND}"
    brand_ok=true
    curl -fsS --max-time 10 -X POST "${API}/setMyName" \
        --data-urlencode "name=${BOT_NAME}" >/dev/null || brand_ok=false
    curl -fsS --max-time 10 -X POST "${API}/setMyShortDescription" \
        --data-urlencode "short_description=${BOT_SHORT}" >/dev/null || brand_ok=false
    curl -fsS --max-time 10 -X POST "${API}/setMyDescription" \
        --data-urlencode "description=${BOT_LONG}" >/dev/null || brand_ok=false
    if $brand_ok; then
        ok "Bot profile updated"
    else
        warn "Bot profile partially updated — check token and try again."
    fi

    if [ -f assets/bot_avatar.png ]; then
        curl -fsS --max-time 30 -X POST "${API}/setMyProfilePhoto" \
            -F "photo=@assets/bot_avatar.png" >/dev/null \
            && ok "Bot avatar set from assets/bot_avatar.png" \
            || warn "Failed to set bot avatar."
    fi
else
    warn "Skipping profile setup (no BOT_TOKEN or curl)."
fi

# ── smoke test ───────────────────────────────────────────────────────
bold "\nSmoke test"

if .venv/bin/python -c "import bot, telegram, sessions, hooks, config, version, terminal_mirror" 2>/tmp/claudelaude_smoke.log; then
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
