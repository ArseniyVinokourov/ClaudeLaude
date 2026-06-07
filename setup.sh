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

# ── speech & video recognition (optional) ────────────────────────────
# Two tiers, asked separately:
#   speech (Whisper)  — transcribes voice notes AND speech in videos;
#                       includes the video decoder as a dependency
#   frames-only       — just the video decoder (PyAV): scene frames from
#                       videos, no speech transcription
# Skipping both is fine — the bot offers the install in-chat when a
# voice/video message arrives.

_env_set() {  # _env_set KEY VALUE — idempotent .env writer
    if grep -q "^$1=" .env 2>/dev/null; then
        sed -i.bak "s|^$1=.*|$1=$2|" .env && rm -f .env.bak
    else
        echo "$1=$2" >> .env
    fi
}

bold "\nSpeech recognition (optional)"
echo "Transcribes voice notes and speech in videos locally with Whisper."
echo "Nothing leaves this machine. Costs ~450MB of libraries plus the"
echo "model you pick. If you skip, the bot offers the install in-chat"
echo "when a voice/video message arrives."
echo ""
read -rp "Install speech recognition? [Y/n]: " SETUP_STT
SETUP_STT="${SETUP_STT:-Y}"

if [[ "$SETUP_STT" =~ ^[Yy] ]]; then
    echo ""
    echo "Whisper model (accuracy vs size — pick for your hardware):"
    echo "  1) base   — ~145MB, fastest, ok for weak/old machines"
    echo "  2) small  — ~460MB, good accuracy (default)"
    echo "  3) medium — ~1.5GB, best accuracy, slow without a beefy CPU"
    read -rp "Model [1/2/3 or name, default small]: " WHISPER_CHOICE
    case "${WHISPER_CHOICE:-2}" in
        1) WHISPER_CHOICE=base ;;
        2) WHISPER_CHOICE=small ;;
        3) WHISPER_CHOICE=medium ;;
        tiny|base|small|medium|large-v3) ;;
        *) warn "Unknown model '$WHISPER_CHOICE' — using 'small'"; WHISPER_CHOICE=small ;;
    esac
    # Persist so stt.py picks it up (config.py loads .env into the environment).
    _env_set WHISPER_MODEL "$WHISPER_CHOICE"

    if [ ! -d .venv-stt ]; then
        python3 -m venv .venv-stt
        ok "STT virtual environment created"
    else
        ok "STT virtual environment exists"
    fi
    .venv-stt/bin/pip install -q --upgrade pip
    # faster-whisper (speech→text) + pillow (saving sampled video frames). PyAV
    # ships with faster-whisper and bundles its own codecs, so NO system ffmpeg
    # is needed — voice and video both decode in-process.
    .venv-stt/bin/pip install -q faster-whisper pillow
    ok "faster-whisper + pillow installed (PyAV bundled — no system ffmpeg needed)"
    # Pre-fetch the model so the first voice/video message isn't slow.
    echo "Downloading whisper model '${WHISPER_CHOICE}' (one-time)..."
    .venv-stt/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_CHOICE}', device='cpu', compute_type='int8')" \
        && ok "Model '${WHISPER_CHOICE}' ready" \
        || warn "Model pre-download failed; it will download on first use."
else
    ok "Skipped."
    echo ""
    bold "Video frames without speech (optional)"
    echo "The video decoder alone (~250MB) lets the bot pull scene frames"
    echo "out of videos and video stickers — speech stays untranscribed."
    read -rp "Install the video decoder? [y/N]: " SETUP_DECODER
    if [[ "${SETUP_DECODER:-N}" =~ ^[Yy] ]]; then
        if [ ! -d .venv-stt ]; then
            python3 -m venv .venv-stt
            ok "STT virtual environment created"
        fi
        .venv-stt/bin/pip install -q --upgrade pip
        .venv-stt/bin/pip install -q av pillow numpy
        ok "Video decoder installed (frames-only tier)"
    else
        ok "Skipped — voice/video messages will offer the install in-chat."
    fi
fi

# ── temp media folder limits ─────────────────────────────────────────
bold "\nMedia storage alerts"
echo "Incoming photos/voice/videos land in /tmp/bot_uploads; files older"
echo "than 48h are cleaned automatically (referenced ones are kept). The"
echo "bot DMs you when the folder outgrows a threshold (max once a day)."
echo ""
echo "Alert threshold:"
echo "  1) 100MB   — weak/old machine, tight disk"
echo "  2) 250MB"
echo "  3) 500MB   (default)"
echo "  4) 1GB     — plenty of disk, fewer alerts"
read -rp "Threshold [1/2/3/4 or MB number, default 500]: " WARN_CHOICE
case "${WARN_CHOICE:-3}" in
    1) WARN_MB=100 ;;
    2) WARN_MB=250 ;;
    3) WARN_MB=500 ;;
    4) WARN_MB=1024 ;;
    *) if [[ "$WARN_CHOICE" =~ ^[0-9]+$ ]]; then WARN_MB="$WARN_CHOICE";
       else warn "Not a number — using 500"; WARN_MB=500; fi ;;
esac
_env_set UPLOAD_WARN_MB "$WARN_MB"
ok "Alert threshold: ${WARN_MB}MB (UPLOAD_WARN_MB in .env; cleanup age via UPLOAD_TTL_S)"

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

# ── Claude slash command: /bot-mirror ────────────────────────────────
bold "\n/bot-mirror slash command"
echo "Installs ~/.claude/commands/bot-mirror.md so you can run /bot-mirror"
echo "inside any Claude session to open a Telegram topic for it."
echo ""
read -rp "Install /bot-mirror command? [Y/n]: " SETUP_BOT_CMD
SETUP_BOT_CMD="${SETUP_BOT_CMD:-Y}"

if [[ "$SETUP_BOT_CMD" =~ ^[Yy] ]]; then
    CMD_SRC="$SCRIPT_DIR/scripts/claude_commands/bot-mirror.md"
    CMD_IMPL_SRC="$SCRIPT_DIR/scripts/bot-mirror-cmd.sh"
    CMD_DST_DIR="$HOME/.claude/commands"
    CMD_DST="$CMD_DST_DIR/bot-mirror.md"
    CMD_IMPL_DST="$CMD_DST_DIR/bot-mirror-cmd.sh"
    if [ -f "$CMD_SRC" ] && [ -f "$CMD_IMPL_SRC" ]; then
        mkdir -p "$CMD_DST_DIR"
        cp "$CMD_SRC" "$CMD_DST"
        cp "$CMD_IMPL_SRC" "$CMD_IMPL_DST"
        chmod +x "$CMD_IMPL_DST"
        # Drop the old `/bot mirror` name so the rename is clean.
        rm -f "$CMD_DST_DIR/bot.md"
        ok "Installed $CMD_DST + $CMD_IMPL_DST"
    else
        warn "Slash command source missing — skipping ($CMD_SRC, $CMD_IMPL_SRC)"
    fi
fi

# ── Mirror swap wrapper (on-demand dtach reopen) ─────────────────────
bold "\nTerminal-mirror swap wrapper"
echo "Installs a tiny function in your shell config (bash/zsh/fish) so"
echo "/bot-mirror, run inside a plain 'claude' session, can reopen the"
echo "same session under 'dtach' — letting the Telegram topic type back"
echo "into your terminal. The wrapper is a transparent passthrough by"
echo "default — every 'claude' invocation behaves like the bare binary"
echo "until /bot-mirror asks for a swap."
echo ""

if ! command -v dtach &>/dev/null; then
    warn "'dtach' is not installed — mirror input bridge requires it."
    echo "  Debian/Ubuntu: sudo apt install dtach"
    echo "  macOS:         brew install dtach"
    echo "Skipping wrapper install; run setup.sh again after installing dtach."
else
    case "${SHELL:-}" in
        */zsh)  SWAP_SHELL=zsh  ;;
        */fish) SWAP_SHELL=fish ;;
        *)      SWAP_SHELL=bash ;;
    esac
    if bash "$SCRIPT_DIR/scripts/install-claude-swap.sh" "$SWAP_SHELL"; then
        ok "Installed swap wrapper for $SWAP_SHELL"
        echo "  Open a new terminal (or source your rc) for it to take effect."
    else
        warn "Could not install swap wrapper — see message above."
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
