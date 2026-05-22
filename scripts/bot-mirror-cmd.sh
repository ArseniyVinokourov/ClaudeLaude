#!/usr/bin/env bash
# Implementation of the /bot-mirror slash command. Lives outside the
# slash command .md so the markdown body stays under ~50 tokens (every
# slash command invocation transmits the whole .md to the model).
#
# Three paths picked from the environment:
#   1. CLAUDELAUDE_DTACH_SOCKET set    → already wrapped, just open topic.
#   2. CLAUDELAUDE_SWAP_SENTINEL set   → request a swap (write sentinel
#                                       + SIGTERM claude). The shell
#                                       wrapper relaunches under dtach.
#   3. Neither                          → output-only mirror + tip.

PORT="${BOT_HOOK_PORT:-9853}"
SOCK="${CLAUDELAUDE_DTACH_SOCKET:-}"
SENT="${CLAUDELAUDE_SWAP_SENTINEL:-}"
SID="${CLAUDE_CODE_SESSION_ID:-}"

call_open_in_bot() {
    local sock="$1"
    curl -sS --max-time 8 -X POST "http://127.0.0.1:${PORT}/hook/open_in_bot" \
        -H 'Content-Type: application/json' \
        --data-raw "{\"hook_event_name\":\"open_in_bot\",\"session_id\":\"$SID\",\"cwd\":\"$PWD\",\"dtach_socket\":\"$sock\"}" \
        2>/dev/null
}

print_resp() {
    local resp="$1" bridge_label="$2"
    local url err bridge
    url=$(echo "$resp"    | grep -oE '"topic_url":"[^"]+"'    | cut -d'"' -f4)
    err=$(echo "$resp"    | grep -oE '"error":"[^"]+"'        | cut -d'"' -f4)
    bridge=$(echo "$resp" | grep -oE '"input_bridge":(true|false)' | cut -d: -f2)
    if [ -n "$url" ]; then
        printf 'mirror: %s\n' "$url"
        if [ -n "$bridge_label" ]; then
            if [ "$bridge" = "true" ]; then
                printf 'input bridge: on (type from the topic — bytes go straight to claude stdin)\n'
            else
                printf '%s\n' "$bridge_label"
            fi
        fi
    elif [ -n "$err" ]; then
        printf 'error: %s\n' "$err"
    else
        echo "$resp"
    fi
}

if [ -n "$SOCK" ]; then
    RESP=$(call_open_in_bot "$SOCK") || { echo "bot unreachable on 127.0.0.1:${PORT}"; exit 0; }
    print_resp "$RESP" "output-only (the dtach socket binding is missing on the bot side)"
    exit 0
fi

if [ -n "$SENT" ] && [ -n "$SID" ]; then
    if ! command -v dtach >/dev/null 2>&1; then
        echo "dtach not installed — run 'sudo apt install dtach' (or 'brew install dtach') and try again."
        exit 0
    fi
    TARGET_SOCK="/tmp/clmirror-${SID}.sock"
    umask 077
    if ! { cat > "$SENT" <<EOF
CLSWAP_SID="$SID"
CLSWAP_CWD="$PWD"
CLSWAP_SOCK="$TARGET_SOCK"
CLSWAP_PORT="$PORT"
EOF
    } 2>/dev/null; then
        echo "claudelaude: failed to write swap sentinel at $SENT — staying in current session." >&2
        exit 0
    fi
    echo "Reopening this session under dtach so the Telegram topic can type back."
    echo "You'll see the same history continue in a moment, and the topic will appear in TG."
    # SIGTERM is the clean shutdown signal. SIGINT (Ctrl-C) is often
    # caught by the TUI to show a confirmation prompt instead of exit.
    kill -TERM "$PPID" 2>/dev/null
    exit 0
fi

RESP=$(call_open_in_bot "") || { echo "bot unreachable on 127.0.0.1:${PORT}"; exit 0; }
print_resp "$RESP" "output-only (the swap wrapper is not installed in this shell — run setup.sh or 'bash scripts/install-claude-swap.sh' once, then start a new terminal)"
