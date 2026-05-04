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

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ARCHIVE="$SCRIPT_DIR/claude-bot.tar.gz"

bold "╔══════════════════════════════════════╗"
bold "║   ClaudeLaude Bot — Install          ║"
bold "╚══════════════════════════════════════╝"
echo ""

# ── find archive ────────────────────────────────────────────────────
if [ ! -f "$ARCHIVE" ]; then
    ARCHIVE="$(find "$SCRIPT_DIR" -maxdepth 1 -name 'claude-bot*.tar.gz' -print -quit 2>/dev/null || true)"
fi
if [ ! -f "$ARCHIVE" ]; then
    ARCHIVE="$HOME/Downloads/claude-bot.tar.gz"
fi
if [ ! -f "$ARCHIVE" ]; then
    err "Не могу найти claude-bot.tar.gz"
    echo "  Положи архив рядом с этим скриптом или в ~/Downloads/"
    exit 1
fi
ok "Архив найден: $ARCHIVE"

# ── choose install directory ────────────────────────────────────────
DEFAULT_DIR="$HOME/claude-bot"
echo ""
bold "Куда установить бота?"
echo "  По умолчанию: $DEFAULT_DIR"
read -rp "  Папка [$DEFAULT_DIR]: " INSTALL_DIR
INSTALL_DIR="${INSTALL_DIR:-$DEFAULT_DIR}"

if [ -d "$INSTALL_DIR" ] && [ -f "$INSTALL_DIR/bot.py" ]; then
    warn "В $INSTALL_DIR уже есть бот."
    echo "  Если хочешь обновить — используй update.sh"
    echo "  Если хочешь установить заново — удали папку и запусти снова"
    exit 1
fi

# ── unpack ──────────────────────────────────────────────────────────
bold "\nРаспаковка..."
mkdir -p "$INSTALL_DIR"
tar xzf "$ARCHIVE" --strip-components=1 -C "$INSTALL_DIR"
ok "Распаковано в $INSTALL_DIR"

# ── run setup ───────────────────────────────────────────────────────
bold "\nЗапускаю настройку...\n"
cd "$INSTALL_DIR"
bash setup.sh

# ── telegram instructions ───────────────────────────────────────────
echo ""
bold "━━━ Осталось настроить Telegram ━━━"
echo ""
echo "  Сделай это в Telegram на телефоне или десктопе:"
echo ""
echo "  1. Создай новую группу (любое название)"
echo "     Меню → New Group → добавь бота → Create"
echo ""
echo "  2. Включи Topics (темы) в группе"
echo "     Настройки группы → Topics → включить"
echo ""
echo "  3. Сделай бота админом"
echo "     Настройки группы → Administrators → Add → выбери бота"
echo "     Дай права: Manage Topics, Delete Messages"
echo ""
echo "  4. Напиши /setup в группе"
echo "     Бот ответит что привязался"
echo ""
bold "━━━ Запуск бота ━━━"
echo ""
echo "  cd $INSTALL_DIR && .venv/bin/python bot.py"
echo ""
echo "  После запуска напиши /new боту в личку — создастся первая сессия."
echo ""
