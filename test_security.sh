#!/usr/bin/env bash
# Manual security test script — run with bot already started.
# Tests each layer, prints PASS/FAIL.
set -u

# Strip \r from read input (WSL terminal compat)
sanitize() { tr -d '\r'; }

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BOT_DIR" || exit 1

GREEN='\033[32m'
RED='\033[31m'
RESET='\033[0m'

pass() { echo -e "${GREEN}PASS${RESET} $1"; }
fail() { echo -e "${RED}FAIL${RESET} $1"; }

echo "=== Layer 1: Kill Switch ==="
echo "--- Activating kill..."
touch "$BOT_DIR/.kill"
sleep 1
echo "  Send any message to bot in TG now."
echo "  Expected: bot ignores it completely (no response)."
read -rp "  Did bot ignore? [y/n]: " ans; ans=$(echo "$ans" | sanitize)
if [[ "$ans" == "y" ]]; then pass "kill blocks messages"; else fail "kill blocks messages"; fi

echo "--- Now send /unkill in TG."
echo "  Expected: command deleted, '🔓 Bot unkilled' appears briefly."
read -rp "  Did it work? [y/n]: " ans; ans=$(echo "$ans" | sanitize)
if [[ "$ans" == "y" ]]; then pass "/unkill restores"; else fail "/unkill restores"; fi

if [ ! -f "$BOT_DIR/.kill" ]; then
    pass ".kill file removed"
else
    fail ".kill file still exists"
    rm -f "$BOT_DIR/.kill"
fi
echo ""

echo "=== Layer 3: Unlock Word ==="
echo "--- Set UNLOCK_WORD=test123 in .env, then restart bot."
read -rp "  Done? [y/n]: " ans; ans=$(echo "$ans" | sanitize)
if [[ "$ans" != "y" ]]; then
    echo "  Skipping unlock word tests."
else
    echo "--- Send any message (not the unlock word)."
    read -rp "  Did bot say '🔒 Locked. Send unlock word...'? [y/n]: " ans; ans=$(echo "$ans" | sanitize)
    if [[ "$ans" == "y" ]]; then pass "locked state blocks"; else fail "locked state blocks"; fi

    echo "--- Now send: test123"
    read -rp "  Did bot delete your message and show '🔓 Unlocked'? [y/n]: " ans; ans=$(echo "$ans" | sanitize)
    if [[ "$ans" == "y" ]]; then pass "unlock word works"; else fail "unlock word works"; fi

    echo "--- Send a normal message now."
    read -rp "  Does it go through to Claude? [y/n]: " ans; ans=$(echo "$ans" | sanitize)
    if [[ "$ans" == "y" ]]; then pass "unlocked session works"; else fail "unlocked session works"; fi

    echo "--- Send /lock"
    read -rp "  Did bot say '🔒 Locked'? [y/n]: " ans; ans=$(echo "$ans" | sanitize)
    if [[ "$ans" == "y" ]]; then pass "/lock works"; else fail "/lock works"; fi

    echo "--- Send /unlock toggle"
    read -rp "  Did bot say 'Unlock word disabled'? [y/n]: " ans; ans=$(echo "$ans" | sanitize)
    if [[ "$ans" == "y" ]]; then pass "toggle off works"; else fail "toggle off works"; fi

    echo "--- Send a message now (feature disabled)."
    read -rp "  Does it go through without asking for unlock? [y/n]: " ans; ans=$(echo "$ans" | sanitize)
    if [[ "$ans" == "y" ]]; then pass "disabled = no lock"; else fail "disabled = no lock"; fi
fi
echo ""

echo "=== Layer 4: Device Monitor ==="
echo "  (Requires TG_API_ID + TG_API_HASH in .env and completed Telethon auth)"
read -rp "  Is device monitoring configured? [y/n]: " ans; ans=$(echo "$ans" | sanitize)
if [[ "$ans" == "y" ]]; then
    echo "  Restart bot. Check General for device alerts."
    echo "  First run saves all current devices — no alert expected."
    read -rp "  No crashes? Bot running? [y/n]: " ans; ans=$(echo "$ans" | sanitize)
    if [[ "$ans" == "y" ]]; then pass "device monitor runs"; else fail "device monitor runs"; fi
else
    echo "  Skipped."
fi

echo ""
echo "=== Done ==="
echo "Remove UNLOCK_WORD from .env if you don't want it permanently."
