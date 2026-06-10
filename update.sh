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

# Portable sha256: sha256sum on Linux/WSL, shasum -a 256 on macOS.
# Stored as a command (not a function) so it can be invoked via xargs.
if command -v sha256sum &>/dev/null; then
    SHA256=(sha256sum)
else
    SHA256=(shasum -a 256)
fi

# ── parse flags ────────────────────────────────────────────────────
NON_INTERACTIVE=false
POLICY_OVERRIDE=""
STRATEGY_OVERRIDE=""

for arg in "$@"; do
    case "$arg" in
        --non-interactive) NON_INTERACTIVE=true ;;
        --policy=*) POLICY_OVERRIDE="${arg#--policy=}" ;;
        --strategy=*) STRATEGY_OVERRIDE="${arg#--strategy=}" ;;
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

# Target = the latest *manually published* GitHub Release tag, not the newest
# tag on main. Tags flow on every merge (versioning); a Release is the owner's
# explicit "available now" gate. Query the public REST API (no auth needed).
SLUG=$(git remote get-url origin 2>/dev/null \
    | sed -E 's#.*github\.com[:/]+([^/]+/[^/]+)#\1#; s#\.git$##; s#/$##')
TARGET_TAG=""
if [ -n "$SLUG" ] && command -v curl &>/dev/null && command -v python3 &>/dev/null; then
    REL_JSON=$(curl -sf --max-time 10 -H 'Accept: application/vnd.github+json' \
        "https://api.github.com/repos/$SLUG/releases/latest" 2>/dev/null || echo "")
    if [ -n "$REL_JSON" ]; then
        TARGET_TAG=$(printf '%s' "$REL_JSON" \
            | python3 -c "import sys,json; print(json.load(sys.stdin).get('tag_name',''))" 2>/dev/null || echo "")
    fi
fi

if [ -z "$TARGET_TAG" ]; then
    ok "No published release yet — nothing to update to."
    exit 2
fi

LATEST_VER="${TARGET_TAG#v}"
echo "  Current: $CURRENT_VER"
echo "  Latest:  $LATEST_VER"
echo ""

LOCAL_HEAD=$(git rev-parse HEAD 2>/dev/null)
TARGET_HEAD=$(git rev-parse "${TARGET_TAG}^{commit}" 2>/dev/null || echo "")

if [ -z "$TARGET_HEAD" ]; then
    err "Release tag $TARGET_TAG not found locally even after fetch."
    exit 1
fi

if [ "$LOCAL_HEAD" = "$TARGET_HEAD" ]; then
    ok "Already up to date."
    exit 2
fi

# ── resolve strategy ──────────────────────────────────────────────
# auto    — try a real 3-way merge of local edits onto the release; on conflict
#           leave the tree resolvable and exit 3 (the bot then offers a choice).
# replace — back up local edits, discard them, fast-forward to the release.
# finalize— user already resolved conflicts in a session; drop our stash and
#           run the post-update steps (deps + checksums).
# Legacy AUTO_UPDATE_POLICY maps replace→replace, anything else→auto.
STRATEGY="${STRATEGY_OVERRIDE}"
if [ -z "$STRATEGY" ]; then
    POLICY="${POLICY_OVERRIDE}"
    if [ -z "$POLICY" ] && [ -f "$BOT_DIR/.env" ]; then
        POLICY=$(grep '^AUTO_UPDATE_POLICY=' "$BOT_DIR/.env" 2>/dev/null | cut -d= -f2 || true)
    fi
    case "${POLICY:-auto}" in
        replace) STRATEGY=replace ;;
        *)       STRATEGY=auto ;;
    esac
fi

STASH_MSG="claudelaude-update"
UPDATE_STATE="$BOT_DIR/.update_state"

# Drop only the stash WE pushed (matched by message), never a user's own.
drop_our_stash() {
    local ref
    ref=$(git stash list 2>/dev/null | grep -F "$STASH_MSG" | head -1 | cut -d: -f1)
    [ -n "$ref" ] && git stash drop "$ref" >/dev/null 2>&1 || true
}

# ── finalize: conflicts already resolved by the user in a session ─
if [ "$STRATEGY" = "finalize" ]; then
    bold "Finalizing resolved update..."
    git add -A 2>/dev/null || true
    drop_our_stash
    rm -f "$UPDATE_STATE"
    ok "Conflicts finalized at $TARGET_TAG"
else
    # ── detect local code changes ──────────────────────────────────
    MODIFIED_FILES=()
    if [ -f "$BOT_DIR/.dist_checksums" ]; then
        while IFS='  ' read -r expected_hash filepath; do
            [ -z "$filepath" ] && continue
            [ ! -f "$BOT_DIR/$filepath" ] && continue
            actual_hash=$("${SHA256[@]}" "$BOT_DIR/$filepath" | cut -d' ' -f1)
            if [ "$actual_hash" != "$expected_hash" ]; then
                MODIFIED_FILES+=("$filepath")
            fi
        done < "$BOT_DIR/.dist_checksums"
    fi

    # ── check if bot is running ────────────────────────────────────
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

    bold "Updating..."

    if [ ${#MODIFIED_FILES[@]} -eq 0 ]; then
        # Clean tree → straight fast-forward to the release commit.
        git merge --ff-only "$TARGET_TAG" || { err "Fast-forward failed."; exit 1; }
        ok "Code updated to $TARGET_TAG"
    else
        warn "Local changes detected in ${#MODIFIED_FILES[@]} file(s):"
        for f in "${MODIFIED_FILES[@]}"; do echo "    $f"; done
        echo ""

        # Always back up local edits before touching them.
        BACKUP_DIR="$BOT_DIR/.backup_$(date +%Y%m%d_%H%M%S)/modified"
        mkdir -p "$BACKUP_DIR"
        for f in "${MODIFIED_FILES[@]}"; do
            mkdir -p "$BACKUP_DIR/$(dirname "$f")"
            cp "$BOT_DIR/$f" "$BACKUP_DIR/$f"
        done
        ok "Local edits backed up to $BACKUP_DIR"

        if [ "$STRATEGY" = "replace" ]; then
            # Discard local edits and land exactly on the release. reset --hard
            # also clears a half-finished merge / conflict markers when we're
            # recovering from a prior auto attempt (HEAD already at the tag).
            git merge --abort 2>/dev/null || true
            git reset --hard "$TARGET_TAG" >/dev/null || { err "Reset failed."; exit 1; }
            drop_our_stash
            rm -f "$UPDATE_STATE"
            ok "Code updated to $TARGET_TAG (local edits replaced; backup kept)"
        else
            # auto: stash local edits, fast-forward, replay edits 3-way.
            if [ "$NON_INTERACTIVE" = false ]; then
                echo "  Will merge your edits onto the new version."
                read -rp "  Continue? [Y/n]: " CONFIRM
                if [[ "$CONFIRM" =~ ^[Nn] ]]; then
                    echo "Aborted. Your files are unchanged."
                    exit 0
                fi
            fi
            git stash push -m "$STASH_MSG" -- "${MODIFIED_FILES[@]}" >/dev/null 2>&1 \
                || git stash push -m "$STASH_MSG" >/dev/null 2>&1 || true
            git merge --ff-only "$TARGET_TAG" || { err "Fast-forward failed."; drop_our_stash; exit 1; }
            if git stash pop >/dev/null 2>&1; then
                ok "Code updated to $TARGET_TAG (local edits merged)"
            else
                printf '%s\n' "$TARGET_TAG" > "$UPDATE_STATE"
                err "Merge conflict — local edits clash with the update."
                echo "  Resolve in the bot, or re-run with --strategy=replace."
                exit 3
            fi
        fi
    fi
fi

# ── update venv ───────────────────────────────────────────────────
bold "Updating dependencies..."
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt
ok "Dependencies updated"

# ── regenerate checksums ──────────────────────────────────────────
git ls-files | xargs "${SHA256[@]}" > .dist_checksums 2>/dev/null || true
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
