#!/usr/bin/env bash
set -euo pipefail

# Exit codes: 0=updated, 1=error, 2=up-to-date, 3=needs-merge

C_RESET='\033[0m'
C_BOLD='\033[1m'
C_GREEN='\033[32m'
C_YELLOW='\033[33m'
C_RED='\033[31m'

ok()   { echo -e "${C_GREEN}✓${C_RESET} $*"; }
warn() { echo -e "${C_YELLOW}⚠${C_RESET} $*"; }
err()  { echo -e "${C_RED}✗${C_RESET} $*"; }
bold() { echo -e "${C_BOLD}$*${C_RESET}"; }

# ── parse flags ────────────────────────────────────────────────────
NON_INTERACTIVE=false
POLICY_OVERRIDE=""

for arg in "$@"; do
    case "$arg" in
        --non-interactive) NON_INTERACTIVE=true ;;
        --policy=*) POLICY_OVERRIDE="${arg#--policy=}" ;;
    esac
done

# ── find bot directory ──────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -f "$SCRIPT_DIR/bot.py" ]; then
    BOT_DIR="$SCRIPT_DIR"
else
    DEFAULT_DIR="$HOME/claude-bot"
    if [ "$NON_INTERACTIVE" = true ]; then
        BOT_DIR="$DEFAULT_DIR"
    else
        echo "Bot directory:"
        read -rp "  [$DEFAULT_DIR]: " BOT_DIR
        BOT_DIR="${BOT_DIR:-$DEFAULT_DIR}"
    fi
fi

if [ ! -f "$BOT_DIR/bot.py" ]; then
    err "No bot found at $BOT_DIR (missing bot.py)"
    echo "  To install, run: bash install.sh"
    exit 1
fi

if [ ! -d "$BOT_DIR/.git" ]; then
    err "$BOT_DIR is not a git repository"
    echo "  Re-install via: bash install.sh"
    exit 1
fi

cd "$BOT_DIR"

bold "ClaudeLaude Bot — Update"
echo ""

# ── fetch latest ───────────────────────────────────────────────────
bold "Checking for updates..."
git fetch --tags origin 2>/dev/null

CURRENT_VER=""
if command -v python3 &>/dev/null && [ -f "$BOT_DIR/version.py" ]; then
    CURRENT_VER=$(python3 "$BOT_DIR/version.py" 2>/dev/null || echo "")
fi
if [ -z "$CURRENT_VER" ] && [ -f "$BOT_DIR/VERSION" ]; then
    CURRENT_VER=$(cat "$BOT_DIR/VERSION" | tr -d '[:space:]')
fi
CURRENT_VER="${CURRENT_VER:-unknown}"

LATEST_TAG=$(git tag -l 'v*' | sort -V | tail -1)
LATEST_VER="${LATEST_TAG#v}"

if [ -z "$LATEST_VER" ]; then
    LATEST_VER="unknown"
fi

echo "  Current: $CURRENT_VER"
echo "  Latest:  $LATEST_VER"
echo ""

LOCAL_HEAD=$(git rev-parse HEAD 2>/dev/null)
REMOTE_HEAD=$(git rev-parse origin/main 2>/dev/null || echo "")

if [ "$LOCAL_HEAD" = "$REMOTE_HEAD" ] && [ -n "$REMOTE_HEAD" ]; then
    ok "Already up to date."
    exit 2
fi

# ── check for local code changes ──────────────────────────────────
MODIFIED_FILES=()

if [ -f "$BOT_DIR/.dist_checksums" ]; then
    while IFS='  ' read -r expected_hash filepath; do
        [ -z "$filepath" ] && continue
        [ ! -f "$BOT_DIR/$filepath" ] && continue
        actual_hash=$(sha256sum "$BOT_DIR/$filepath" | cut -d' ' -f1)
        if [ "$actual_hash" != "$expected_hash" ]; then
            MODIFIED_FILES+=("$filepath")
        fi
    done < "$BOT_DIR/.dist_checksums"
fi

# ── determine policy ──────────────────────────────────────────────
POLICY="${POLICY_OVERRIDE}"
if [ -z "$POLICY" ] && [ -f "$BOT_DIR/.env" ]; then
    POLICY=$(grep '^AUTO_UPDATE_POLICY=' "$BOT_DIR/.env" 2>/dev/null | cut -d= -f2 || true)
fi
POLICY="${POLICY:-replace}"

# ── handle local changes ─────────────────────────────────────────
if [ ${#MODIFIED_FILES[@]} -gt 0 ]; then
    warn "Local changes detected in ${#MODIFIED_FILES[@]} file(s):"
    for f in "${MODIFIED_FILES[@]}"; do
        echo "    $f"
    done
    echo ""

    if [ "$POLICY" = "merge" ]; then
        warn "Policy is 'merge'. Use /update in the bot for guided merge."
        exit 3
    fi

    # policy = replace: backup modified files, then overwrite
    BACKUP_DIR="$BOT_DIR/.backup_$(date +%Y%m%d_%H%M%S)/modified"
    mkdir -p "$BACKUP_DIR"
    for f in "${MODIFIED_FILES[@]}"; do
        mkdir -p "$BACKUP_DIR/$(dirname "$f")"
        cp "$BOT_DIR/$f" "$BACKUP_DIR/$f"
    done
    ok "Modified files backed up to $BACKUP_DIR"

    if [ "$NON_INTERACTIVE" = false ]; then
        echo "  Proceeding will overwrite these files with the new version."
        read -rp "  Continue? [Y/n]: " CONFIRM
        if [[ "$CONFIRM" =~ ^[Nn] ]]; then
            echo "Aborted. Your files are unchanged."
            exit 0
        fi
    fi

    # discard local changes to modified tracked files
    git checkout -- "${MODIFIED_FILES[@]}" 2>/dev/null || true
fi

# ── check if bot is running ──────────────────────────────────────
if pgrep -f "python.*bot\.py" &>/dev/null; then
    if [ "$NON_INTERACTIVE" = false ]; then
        warn "Bot appears to be running."
        echo "  It will be restarted after the update."
        read -rp "  Continue? [Y/n]: " STOPPED
        if [[ "$STOPPED" =~ ^[Nn] ]]; then
            echo "Stop the bot first, then re-run update.sh."
            exit 0
        fi
    fi
fi

# ── pull updates ──────────────────────────────────────────────────
bold "Updating..."
git pull origin main --ff-only
ok "Code updated"

# ── update venv ───────────────────────────────────────────────────
bold "Updating dependencies..."
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt
ok "Dependencies updated"

# ── regenerate checksums ──────────────────────────────────────────
git ls-files | xargs sha256sum > .dist_checksums 2>/dev/null || true
ok "Checksums updated"

# ── offer hooks update ────────────────────────────────────────────
if [ "$NON_INTERACTIVE" = false ]; then
    echo ""
    bold "Update Claude Code hooks?"
    echo "  Recommended if this is a major update."
    read -rp "  Update hooks? [Y/n]: " UPDATE_HOOKS
    UPDATE_HOOKS="${UPDATE_HOOKS:-Y}"

    if [[ "$UPDATE_HOOKS" =~ ^[Yy] ]]; then
        HOOK_PORT=$(grep '^HOOK_PORT=' "$BOT_DIR/.env" 2>/dev/null | cut -d= -f2)
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
import json
settings = json.loads('''$EXISTING''')
hooks = settings.setdefault('hooks', {})
hooks['Notification'] = [{'hooks': [{'type': 'command', 'command': '''$NOTIFY_CMD''', 'timeout': 10}]}]
hooks['PermissionRequest'] = [{'hooks': [{'type': 'command', 'command': '''$PERM_CMD''', 'timeout': 130}]}]
json.dump(settings, open('$SETTINGS_FILE', 'w'), indent=2)
"
        ok "Hooks updated"
    fi
fi

# ── done ──────────────────────────────────────────────────────────
echo ""
bold "--- Update complete ---"
echo ""
echo "  Updated to: $LATEST_VER"
echo "  Start the bot: cd $BOT_DIR && .venv/bin/python bot.py"
echo ""

if [ ${#MODIFIED_FILES[@]} -gt 0 ]; then
    warn "Your modified files were backed up to:"
    echo "    $BACKUP_DIR"
    echo "  Review with: diff $BACKUP_DIR/<file> <file>"
fi
