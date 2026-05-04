#!/usr/bin/env bash
set -euo pipefail

C_RESET='\033[0m'
C_BOLD='\033[1m'
C_GREEN='\033[32m'
C_YELLOW='\033[33m'
C_RED='\033[31m'

ok()   { echo -e "${C_GREEN}✓${C_RESET} $*"; }
warn() { echo -e "${C_YELLOW}⚠${C_RESET} $*"; }
err()  { echo -e "${C_RED}✗${C_RESET} $*"; }
bold() { echo -e "${C_BOLD}$*${C_RESET}"; }

bold "╔══════════════════════════════════════╗"
bold "║   ClaudeLaude Bot — Update           ║"
bold "╚══════════════════════════════════════╝"
echo ""

# ── find bot directory ──────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# If running from inside the bot dir (update.sh next to bot.py)
if [ -f "$SCRIPT_DIR/bot.py" ]; then
    BOT_DIR="$SCRIPT_DIR"
# Otherwise ask
else
    DEFAULT_DIR="$HOME/claude-bot"
    echo "Где установлен бот?"
    read -rp "  Папка [$DEFAULT_DIR]: " BOT_DIR
    BOT_DIR="${BOT_DIR:-$DEFAULT_DIR}"
fi

if [ ! -f "$BOT_DIR/bot.py" ]; then
    err "Не нашёл бота в $BOT_DIR (нет bot.py)"
    echo "  Если бот ещё не установлен — используй install.sh"
    exit 1
fi

# ── find archive ────────────────────────────────────────────────────
ARCHIVE=""
for candidate in \
    "$SCRIPT_DIR/claude-bot.tar.gz" \
    "$BOT_DIR/claude-bot.tar.gz" \
    "$HOME/Downloads/claude-bot.tar.gz"; do
    if [ -f "$candidate" ]; then
        ARCHIVE="$candidate"
        break
    fi
done

if [ -z "$ARCHIVE" ]; then
    err "Не могу найти claude-bot.tar.gz"
    echo "  Положи архив рядом с этим скриптом, в папку бота или в ~/Downloads/"
    exit 1
fi
ok "Архив: $ARCHIVE"

# ── show versions ───────────────────────────────────────────────────
OLD_VER="?"
if [ -f "$BOT_DIR/VERSION" ]; then
    OLD_VER=$(cat "$BOT_DIR/VERSION" | tr -d '[:space:]')
fi

TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT
tar xzf "$ARCHIVE" --strip-components=1 -C "$TMPDIR"

NEW_VER="?"
if [ -f "$TMPDIR/VERSION" ]; then
    NEW_VER=$(cat "$TMPDIR/VERSION" | tr -d '[:space:]')
fi

echo "  Текущая версия: $OLD_VER"
echo "  Новая версия:   $NEW_VER"
echo ""

if [ "$OLD_VER" = "$NEW_VER" ]; then
    warn "Версии совпадают. Обновить всё равно?"
    read -rp "  Продолжить? [y/N]: " CONFIRM
    if [[ ! "$CONFIRM" =~ ^[Yy] ]]; then
        echo "Отменено."
        exit 0
    fi
fi

# ── check if bot is running ────────────────────────────────────────
if pgrep -f "python.*bot\.py" &>/dev/null; then
    warn "Похоже, бот сейчас запущен."
    echo "  Останови его перед обновлением (Ctrl+C или /stop_bot в Telegram)."
    read -rp "  Бот остановлен? [Y/n]: " STOPPED
    if [[ "$STOPPED" =~ ^[Nn] ]]; then
        echo "Останови бота и запусти update.sh снова."
        exit 0
    fi
fi

# ── backup user data ───────────────────────────────────────────────
bold "Сохраняю данные..."
BACKUP_DIR="$BOT_DIR/.backup_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"

for f in .env .state.json .sessions.json; do
    if [ -f "$BOT_DIR/$f" ]; then
        cp "$BOT_DIR/$f" "$BACKUP_DIR/$f"
        ok "  $f → backup"
    fi
done
ok "Бэкап: $BACKUP_DIR"

# ── replace code files ─────────────────────────────────────────────
bold "\nОбновляю код..."

CODE_FILES="bot.py sessions.py telegram.py hooks.py config.py setup.sh install.sh update.sh requirements.txt README.md VERSION .env.example .gitignore"

for f in $CODE_FILES; do
    if [ -f "$TMPDIR/$f" ]; then
        cp "$TMPDIR/$f" "$BOT_DIR/$f"
    fi
done
ok "Файлы обновлены"

# ── restore user data ──────────────────────────────────────────────
bold "\nВосстанавливаю данные..."
for f in .env .state.json .sessions.json; do
    if [ -f "$BACKUP_DIR/$f" ]; then
        cp "$BACKUP_DIR/$f" "$BOT_DIR/$f"
        ok "  $f восстановлен"
    fi
done

# ── update venv ─────────────────────────────────────────────────────
bold "\nОбновляю зависимости..."
cd "$BOT_DIR"
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt
ok "Зависимости обновлены"

# ── offer hooks update ──────────────────────────────────────────────
echo ""
bold "Обновить хуки Claude Code?"
echo "  Если не уверен — лучше обнови (хуже не станет)."
read -rp "  Обновить хуки? [Y/n]: " UPDATE_HOOKS
UPDATE_HOOKS="${UPDATE_HOOKS:-Y}"

if [[ "$UPDATE_HOOKS" =~ ^[Yy] ]]; then
    HOOK_PORT=$(grep '^HOOK_PORT=' "$BOT_DIR/.env" 2>/dev/null | cut -d= -f2)
    HOOK_PORT="${HOOK_PORT:-9853}"

    FALLBACK_SCRIPT="$BOT_DIR/hook_fallback.py"

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
import json
settings = json.loads('''$EXISTING''')
hooks = settings.setdefault('hooks', {})
hooks['Notification'] = [{'hooks': [{'type': 'command', 'command': '''$NOTIFY_CMD''', 'timeout': 10}]}]
hooks['PermissionRequest'] = [{'hooks': [{'type': 'command', 'command': '''$PERM_CMD''', 'timeout': 130}]}]
json.dump(settings, open('$SETTINGS_FILE', 'w'), indent=2)
"
    ok "Хуки обновлены"
fi

# ── done ────────────────────────────────────────────────────────────
echo ""
bold "━━━ Обновление завершено ━━━"
echo ""
echo "  Запусти бота:"
echo "  cd $BOT_DIR && .venv/bin/python bot.py"
echo ""
echo "  Бэкап данных сохранён в: $BACKUP_DIR"
echo "  (можно удалить, когда убедишься что всё работает)"
echo ""
