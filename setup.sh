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

# ── Mirror (~/.bashrc claude() function) ─────────────────────────────
bold "\nTerminal-mirror (optional)"
echo "When enabled, the bot can stream input from a Telegram topic into"
echo "your running terminal Claude — exactly as if you were typing at"
echo "the keyboard. The mechanism: a tiny bash function in ~/.bashrc"
echo "transparently runs 'claude' inside a tmux pane. The bot then uses"
echo "'tmux send-keys' to type into that pane from Telegram."
echo ""
echo "Your everyday workflow stays identical — you keep typing 'claude'"
echo "as before. The tmux pane is created with a status bar disabled, so"
echo "visually it looks like a plain claude session. To bypass entirely"
echo "for one shell, set CLAUDELAUDE_NO_TMUX=1 before running 'claude'."
echo ""

if ! command -v tmux &>/dev/null; then
    warn "'tmux' is not installed. Mirror input bridge requires tmux."
    echo "  Debian/Ubuntu: sudo apt install tmux"
    echo "  macOS:         brew install tmux"
    echo "Skipping mirror setup; install tmux and re-run setup.sh."
else
    read -rp "Enable terminal mirror (adds a bash function to ~/.bashrc)? [Y/n]: " INSTALL_MIRROR
    INSTALL_MIRROR="${INSTALL_MIRROR:-Y}"

    if [[ "$INSTALL_MIRROR" =~ ^[Yy] ]]; then
        BASHRC="$HOME/.bashrc"
        MARK_BEGIN="# >>> claudelaude mirror >>>"
        MARK_END="# <<< claudelaude mirror <<<"

        # Refuse to install if user has their own claude() / alias and
        # it's not ours. Our marker block is exempt from this check.
        if [ -f "$BASHRC" ] && grep -q "$MARK_BEGIN" "$BASHRC"; then
            ok "Mirror block already present in ~/.bashrc — refreshing"
            # Strip existing block before re-inserting (idempotent).
            python3 -c "
import re, pathlib
p = pathlib.Path('$BASHRC')
src = p.read_text()
new = re.sub(r'\n?$MARK_BEGIN.*?$MARK_END\n?', '\n', src, flags=re.S)
p.write_text(new)
"
        elif [ -f "$BASHRC" ] && grep -qE '^[[:space:]]*(alias[[:space:]]+claude=|claude\(\)[[:space:]]*\{|function[[:space:]]+claude[[:space:]]*[\{(])' "$BASHRC"; then
            warn "An existing 'claude' alias/function was found in ~/.bashrc."
            echo "  Refusing to overwrite. Remove or rename it, then re-run setup.sh."
            echo "  To use the mirror without our wrapper, run 'clmirror' explicitly"
            echo "  (set CLAUDELAUDE_MIRROR_FALLBACK_CMD=clmirror) — not yet shipped."
            INSTALL_MIRROR=N
        fi

        if [[ "$INSTALL_MIRROR" =~ ^[Yy] ]]; then
            # Backup ~/.bashrc once before our first edit.
            if [ -f "$BASHRC" ] && [ ! -f "$BASHRC.before-claudelaude" ]; then
                cp "$BASHRC" "$BASHRC.before-claudelaude"
                ok "Backed up ~/.bashrc to ~/.bashrc.before-claudelaude"
            fi

            cat >> "$BASHRC" <<'BASHRC_BLOCK'

# >>> claudelaude mirror >>>
# Routes plain `claude` invocations through a dedicated tmux pane so the
# ClaudeLaude bot can mirror the session into Telegram. To bypass for one
# shell, set CLAUDELAUDE_NO_TMUX=1.
claude() {
    if [ -n "$TMUX" ] || [ -n "${CLAUDELAUDE_NO_TMUX:-}" ] \
       || ! command -v tmux >/dev/null 2>&1; then
        command claude "$@"
        return $?
    fi
    local session_name="${CLAUDELAUDE_SESSION:-clmirror-$$}"
    local cmd="command claude"
    local a
    for a in "$@"; do
        cmd+=" $(printf '%q' "$a")"
    done
    if ! tmux has-session -t "$session_name" 2>/dev/null; then
        tmux new-session -d -s "$session_name" "$cmd"
        tmux set-option -t "$session_name" status off >/dev/null 2>&1 || true
        tmux set-option -t "$session_name" mouse on >/dev/null 2>&1 || true
        tmux set-option -t "$session_name" history-limit 100000 >/dev/null 2>&1 || true
    fi
    tmux attach-session -t "$session_name"
}
# <<< claudelaude mirror <<<
BASHRC_BLOCK
            ok "Installed mirror block in ~/.bashrc"
            echo "  Open a new terminal (or 'source ~/.bashrc') for it to take effect."
        fi
    else
        ok "Skipping mirror. /bot mirror will run output-only when invoked."
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
