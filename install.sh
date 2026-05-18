#!/usr/bin/env bash
set -euo pipefail

C_RESET='\033[0m'
C_BOLD='\033[1m'
C_GREEN='\033[32m'
C_RED='\033[31m'

ok()   { echo -e "${C_GREEN}✓${C_RESET} $*"; }
err()  { echo -e "${C_RED}✗${C_RESET} $*"; }
bold() { echo -e "${C_BOLD}$*${C_RESET}"; }

# Portable sha256: sha256sum on Linux/WSL, shasum -a 256 on macOS.
# Stored as a command (not a function) so it can be invoked via xargs.
if command -v sha256sum &>/dev/null; then
    SHA256=(sha256sum)
else
    SHA256=(shasum -a 256)
fi

REPO_URL="https://github.com/ArseniyVinokourov/ClaudeLaude.git"
DEFAULT_DIR="$HOME/claude-bot"

bold "ClaudeLaude Bot — Install"
echo ""

# ── prerequisites ──────────────────────────────────────────────────
if ! command -v git &>/dev/null; then
    err "git not found. Install git first."
    exit 1
fi

# ── choose install directory ──────────────────────────────────────
echo "Install directory (default: $DEFAULT_DIR):"
read -rp "  [$DEFAULT_DIR]: " INSTALL_DIR
INSTALL_DIR="${INSTALL_DIR:-$DEFAULT_DIR}"

if [ -d "$INSTALL_DIR" ] && [ -f "$INSTALL_DIR/bot.py" ]; then
    err "Bot already installed at $INSTALL_DIR"
    echo "  To update, run: cd $INSTALL_DIR && bash update.sh"
    exit 1
fi

# ── clone ──────────────────────────────────────────────────────────
bold "Cloning repository..."
git clone "$REPO_URL" "$INSTALL_DIR"
ok "Cloned to $INSTALL_DIR"

# ── run setup ──────────────────────────────────────────────────────
bold ""
bold "Running setup..."
cd "$INSTALL_DIR"
bash setup.sh

# ── generate checksums ─────────────────────────────────────────────
git ls-files | xargs "${SHA256[@]}" > .dist_checksums 2>/dev/null || true

# ── post-install instructions ──────────────────────────────────────
echo ""
bold "--- Telegram setup ---"
echo ""
echo "  1. Create a Telegram group (any name)"
echo "  2. Enable Topics: Group Settings > Topics > On"
echo "  3. Add bot as admin (Manage Topics, Delete Messages)"
echo "  4. Send /setup in the group"
echo ""
bold "--- Start the bot ---"
echo ""
echo "  cd $INSTALL_DIR && .venv/bin/python bot.py"
echo ""
echo "  Then send /new to create your first session."
echo ""
