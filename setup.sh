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

    cat > .env <<EOF
BOT_TOKEN=$BOT_TOKEN
OWNER_ID=$OWNER_ID
PROJECTS_DIR=$PROJECTS_DIR
HOOK_PORT=$HOOK_PORT
EOF

    ok ".env created"
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
    # Load port from .env
    HOOK_PORT=$(grep '^HOOK_PORT=' .env | cut -d= -f2)
    HOOK_PORT="${HOOK_PORT:-9853}"

    FALLBACK_SCRIPT="$SCRIPT_DIR/hook_fallback.py"

    # Create fallback script (works when bot is offline)
    cat > "$FALLBACK_SCRIPT" <<'PYEOF'
#!/usr/bin/env python3
"""Fallback hook — sends Telegram DM when the bot daemon is NOT running."""
import sys, json, os, urllib.request

def main():
    dotenv = os.path.join(os.path.dirname(__file__), ".env")
    cfg = {}
    if os.path.exists(dotenv):
        for line in open(dotenv):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    token = cfg.get("BOT_TOKEN", "")
    chat_id = cfg.get("OWNER_ID", "")
    if not token or not chat_id:
        return
    api = f"https://api.telegram.org/bot{token}"
    event = sys.argv[1] if len(sys.argv) > 1 else "notification"
    raw = sys.stdin.read()
    try:
        inp = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        inp = {}
    esc = lambda t: str(t).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    def send(text):
        data = json.dumps({"chat_id": int(chat_id), "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(f"{api}/sendMessage", data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass
    if event == "permission":
        tool = inp.get("tool_name", "?")
        ti = inp.get("tool_input", {})
        detail = ti.get("command", ti.get("file_path", tool))[:200]
        send(f"⚠️ Auto-allowed (bot offline): <b>{esc(tool)}</b>\n<code>{esc(detail)}</code>")
        json.dump({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                          "permissionDecision": "allow"}}, sys.stdout)
    else:
        text = next((inp.get(k) for k in ("message","title","text","body") if inp.get(k)), "notification")
        send(f"🔔 {esc(text)}")

if __name__ == "__main__":
    main()
PYEOF
    chmod +x "$FALLBACK_SCRIPT"
    ok "Fallback hook script created"

    SETTINGS_FILE="$HOME/.claude/settings.json"
    mkdir -p "$(dirname "$SETTINGS_FILE")"

    NOTIFY_CMD="INPUT=\$(cat); printf '%s' \"\$INPUT\" | curl -sf --max-time 8 -X POST http://127.0.0.1:${HOOK_PORT}/hook/notification -H 'Content-Type: application/json' -d @- 2>/dev/null || printf '%s' \"\$INPUT\" | python3 ${FALLBACK_SCRIPT} notification"
    PERM_CMD="INPUT=\$(cat); printf '%s' \"\$INPUT\" | curl -sf --max-time 125 -X POST http://127.0.0.1:${HOOK_PORT}/hook/permission -H 'Content-Type: application/json' -d @- 2>/dev/null || printf '%s' \"\$INPUT\" | python3 ${FALLBACK_SCRIPT} permission"

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

# ── done ─────────────────────────────────────────────────────────────
bold "\n━━━ Setup complete ━━━"
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
