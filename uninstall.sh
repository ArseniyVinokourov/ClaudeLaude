#!/usr/bin/env bash
# ClaudeLaude Bot — Uninstall
#
# Removes everything setup.sh / the running bot put on this machine, leaving no
# tails: local state + venvs, the Claude Code hooks and /bot-mirror command in
# ~/.claude, the shell swap wrapper, downloaded Whisper models, and temp files.
#
# What it CANNOT remove (and tells you about): the bot on @BotFather, the
# Telegram group/topics, and the Windows "keep WSL alive" scheduled task.
#
#   bash uninstall.sh [--dry-run] [--yes] [--keep-whisper] [--purge-dir]
#
#     --dry-run       list what would be removed, change nothing
#     --yes           don't prompt (assume yes); does NOT imply --purge-dir
#     --keep-whisper  keep downloaded Whisper models in ~/.cache/huggingface
#     --purge-dir     also delete the bot directory itself at the end
set -uo pipefail

C_RESET='\033[0m'; C_BOLD='\033[1m'; C_GREEN='\033[32m'; C_YELLOW='\033[33m'; C_RED='\033[31m'
ok()   { echo -e "${C_GREEN}✓${C_RESET} $*"; }
warn() { echo -e "${C_YELLOW}⚠${C_RESET} $*"; }
err()  { echo -e "${C_RED}✗${C_RESET} $*"; }
bold() { echo -e "${C_BOLD}$*${C_RESET}"; }

DRY_RUN=false; ASSUME_YES=false; KEEP_WHISPER=false; PURGE_DIR=false
for arg in "$@"; do
    case "$arg" in
        --dry-run)      DRY_RUN=true ;;
        --yes|-y)       ASSUME_YES=true ;;
        --keep-whisper) KEEP_WHISPER=true ;;
        --purge-dir)    PURGE_DIR=true ;;
        -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) err "Unknown option: $arg"; exit 2 ;;
    esac
done

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ ! -f "$BOT_DIR/bot.py" ]; then
    err "This doesn't look like a ClaudeLaude install (no bot.py at $BOT_DIR)."
    echo "  Run uninstall.sh from inside the bot directory."
    exit 1
fi

# Honor the same env overrides config.py uses, so relocated state is found too.
STATE_FILE="${BOT_STATE_FILE:-$BOT_DIR/.state.json}"
SESSIONS_FILE="${BOT_SESSIONS_FILE:-$BOT_DIR/.sessions.json}"
MIRRORS_FILE="${BOT_MIRRORS_FILE:-$BOT_DIR/.mirrors.json}"
SETTINGS_FILE="$HOME/.claude/settings.json"
CMD_DIR="$HOME/.claude/commands"
HF_HUB="${HF_HOME:-$HOME/.cache/huggingface}/hub"
UPLOAD_DIR="${UPLOAD_DIR:-/tmp/bot_uploads}"   # incoming-media scratch dir

# rm helper that respects --dry-run and never fails the script on absent paths.
_rm() {
    local p
    for p in "$@"; do
        [ -e "$p" ] || [ -L "$p" ] || continue
        if $DRY_RUN; then echo "    would remove: $p"
        else rm -rf "$p" && echo "    removed: $p"; fi
    done
}

bold "ClaudeLaude Bot — Uninstall"
$DRY_RUN && warn "DRY RUN — nothing will be changed."
echo "  Bot directory: $BOT_DIR"
echo ""

# ── 1. stop a running bot ────────────────────────────────────────────
BOT_PIDS=$(pgrep -f "python.*bot\.py" 2>/dev/null || true)
# Only consider PIDs whose cwd is THIS bot dir (don't touch unrelated processes).
OUR_PIDS=""
for pid in $BOT_PIDS; do
    cwd=$(readlink -f "/proc/$pid/cwd" 2>/dev/null || echo "")
    [ "$cwd" = "$BOT_DIR" ] && OUR_PIDS="$OUR_PIDS $pid"
done
OUR_PIDS="$(echo "$OUR_PIDS" | xargs 2>/dev/null || true)"
if [ -n "$OUR_PIDS" ]; then
    warn "The bot is running (PID $OUR_PIDS)."
    if $DRY_RUN; then
        echo "    would stop it before removing files."
    elif $ASSUME_YES; then
        kill $OUR_PIDS 2>/dev/null && ok "Stopped the bot."
    else
        read -rp "  Stop it now? [Y/n]: " S
        if [[ "${S:-Y}" =~ ^[Yy] ]]; then
            kill $OUR_PIDS 2>/dev/null && ok "Stopped the bot."
        else
            err "Stop the bot first, then re-run uninstall.sh."
            exit 1
        fi
    fi
fi

# ── confirmation ─────────────────────────────────────────────────────
if ! $DRY_RUN && ! $ASSUME_YES; then
    echo ""
    bold "This will remove:"
    echo "  • bot state, virtualenvs, logs and backups in $BOT_DIR"
    echo "  • the Claude Code hooks + /bot-mirror command in ~/.claude"
    echo "  • the shell 'claude' swap wrapper in your rc file"
    $KEEP_WHISPER || echo "  • downloaded Whisper models in ${HF_HUB}"
    echo "  • temp files in /tmp (uploads, sockets)"
    $PURGE_DIR && echo "  • the bot directory itself ($BOT_DIR)"
    echo ""
    read -rp "Proceed? [y/N]: " GO
    [[ "${GO:-N}" =~ ^[Yy] ]] || { echo "Aborted. Nothing removed."; exit 0; }
fi

# ── 2. local state, venvs, logs, caches ──────────────────────────────
echo ""
bold "Local state"
_rm "$STATE_FILE" "$SESSIONS_FILE" "$MIRRORS_FILE" \
    "$BOT_DIR/.audit.log" "$BOT_DIR/.known_devices.json" \
    "$BOT_DIR/.kill" "$BOT_DIR/.update_state" "$BOT_DIR/.dist_checksums" \
    "$BOT_DIR/.tg_monitor.session" "$BOT_DIR/.tg_monitor.session-journal" \
    "$BOT_DIR/bot.log" "$BOT_DIR/nohup.out"
shopt -s nullglob
_rm "$BOT_DIR"/.backup_*
shopt -u nullglob
bold "Virtual environments"
_rm "$BOT_DIR/.venv" "$BOT_DIR/.venv-stt"
# byte-compiled caches anywhere in the tree (venvs removed above, so the sweep
# stays cheap and doesn't descend into a multi-thousand-file virtualenv).
bold "Byte-compiled caches"
while IFS= read -r _pyc; do _rm "$_pyc"; done < <(find "$BOT_DIR" -type d -name __pycache__ 2>/dev/null)

# ── 3. Claude Code hooks (surgical: only ours) ───────────────────────
echo ""
bold "Claude Code hooks (~/.claude/settings.json)"
if [ -f "$SETTINGS_FILE" ]; then
    if $DRY_RUN; then
        echo "    would remove our Notification/PermissionRequest hooks (others kept)"
    else
        python3 - "$SETTINGS_FILE" <<'PYEOF'
import json, os, sys
p = sys.argv[1]
try:
    with open(p) as f:
        s = json.load(f)
except Exception as e:
    sys.stderr.write(f"settings.json not valid JSON ({e}); left untouched.\n")
    sys.exit(0)
hooks = s.get("hooks", {})
changed = False
def ours(entry):
    cmds = " ".join(h.get("command", "") for h in entry.get("hooks", []))
    return "127.0.0.1" in cmds and ("/hook/notification" in cmds or "/hook/permission" in cmds)
for key in ("Notification", "PermissionRequest"):
    if key in hooks:
        kept = [e for e in hooks[key] if not ours(e)]
        if kept != hooks[key]:
            changed = True
            if kept: hooks[key] = kept
            else: del hooks[key]
if not hooks and "hooks" in s:
    del s["hooks"]; changed = True
if changed:
    tmp = p + ".tmp"
    with open(tmp, "w") as f: json.dump(s, f, indent=2)
    os.replace(tmp, p)
    print("    removed our hooks (kept everything else)")
else:
    print("    no ClaudeLaude hooks found — nothing to remove")
PYEOF
    fi
else
    echo "    no settings.json — nothing to remove"
fi

# ── 4. /bot-mirror command ───────────────────────────────────────────
echo ""
bold "/bot-mirror command"
_rm "$CMD_DIR/bot-mirror.md" "$CMD_DIR/bot-mirror-cmd.sh" "$CMD_DIR/bot.md"

# ── 5. shell swap wrapper (strip our marked block; remove backup) ────
echo ""
bold "Shell swap wrapper"
_strip_block() {  # $1 = rc file
    local rc="$1"
    [ -f "$rc" ] || return 0
    if grep -qF "claudelaude swap" "$rc" 2>/dev/null || grep -qF "claudelaude mirror" "$rc" 2>/dev/null; then
        if $DRY_RUN; then
            echo "    would strip the claudelaude block from $rc"
        else
            python3 - "$rc" <<'PYEOF'
import pathlib, re, sys
p = pathlib.Path(sys.argv[1]); src = p.read_text()
for begin, end in [("# >>> claudelaude swap >>>", "# <<< claudelaude swap <<<"),
                   ("# >>> claudelaude mirror >>>", "# <<< claudelaude mirror <<<")]:
    src = re.sub(r'\n?' + re.escape(begin) + r'.*?' + re.escape(end) + r'\n?', '\n', src, flags=re.S)
p.write_text(src)
PYEOF
            echo "    stripped claudelaude block from $rc"
        fi
    fi
}
_strip_block "$HOME/.bashrc"
_strip_block "$HOME/.zshrc"
_rm "$HOME/.bashrc.before-claudelaude" "$HOME/.zshrc.before-claudelaude"
# fish: per-function file, only if it's ours
FISH_FN="$HOME/.config/fish/functions/claude.fish"
if [ -f "$FISH_FN" ] && grep -qF "claudelaude-swap" "$FISH_FN" 2>/dev/null; then
    _rm "$FISH_FN"
fi
_rm "$HOME/.config/fish/functions/claude.fish.before-claudelaude"

# ── 6. Whisper models ────────────────────────────────────────────────
echo ""
bold "Whisper models"
if $KEEP_WHISPER; then
    echo "    kept (--keep-whisper)"
elif [ -d "$HF_HUB" ]; then
    shopt -s nullglob
    MODELS=("$HF_HUB"/models--Systran--faster-whisper-* "$HF_HUB"/models--guillaumekln--faster-whisper-*)
    shopt -u nullglob
    if [ ${#MODELS[@]} -eq 0 ]; then
        echo "    none found"
    elif $DRY_RUN || $ASSUME_YES; then
        _rm "${MODELS[@]}"
    else
        echo "    found ${#MODELS[@]} model dir(s) in $HF_HUB (these can be large):"
        for m in "${MODELS[@]}"; do echo "      $(basename "$m")"; done
        read -rp "    Remove them? [Y/n]: " W
        [[ "${W:-Y}" =~ ^[Yy] ]] && _rm "${MODELS[@]}" || echo "    kept."
    fi
else
    echo "    no huggingface cache — nothing to remove"
fi

# ── 7. temp files ────────────────────────────────────────────────────
echo ""
bold "Temp files"
shopt -s nullglob
_rm "$UPLOAD_DIR" /tmp/claudelaude_smoke.log
_rm /tmp/claudelaude-swap-*.env /tmp/clmirror-*
shopt -u nullglob

# ── 8. things we can't remove from here ──────────────────────────────
echo ""
bold "Manual steps (can't be done from this machine)"
echo "  • Delete the bot on Telegram: message @BotFather → /deletebot"
echo "  • Delete the Telegram group/topics in your Telegram client"
echo "  • Windows only — remove the keep-WSL-alive scheduled task:"
echo "      in PowerShell: Unregister-ScheduledTask -TaskName KeepWSLAlive -Confirm:\$false"

# ── 9. the bot directory itself ──────────────────────────────────────
echo ""
if $PURGE_DIR; then
    bold "Bot directory"
    if $DRY_RUN; then
        echo "    would remove: $BOT_DIR"
    else
        cd /
        rm -rf "$BOT_DIR" && ok "Removed $BOT_DIR"
    fi
elif ! $DRY_RUN && ! $ASSUME_YES; then
    read -rp "Also delete the bot directory ($BOT_DIR)? [y/N]: " D
    if [[ "${D:-N}" =~ ^[Yy] ]]; then
        cd /
        rm -rf "$BOT_DIR" && ok "Removed $BOT_DIR"
    else
        echo "Kept $BOT_DIR (the code). Delete it manually to finish: rm -rf \"$BOT_DIR\""
    fi
else
    echo "Kept the bot directory ($BOT_DIR). Remove it with --purge-dir or: rm -rf \"$BOT_DIR\""
fi

echo ""
$DRY_RUN && bold "--- Dry run complete (nothing changed) ---" || bold "--- Uninstall complete ---"
