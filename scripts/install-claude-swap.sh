#!/usr/bin/env bash
# Installs the ClaudeLaude on-demand mirror swap wrapper into the user's
# active shell config (bash, zsh, or fish — detected via $SHELL).
#
# The wrapper:
#   - By default, runs `claude` exactly like the bare binary. Zero overhead,
#     zero behavior change.
#   - Exports CLAUDELAUDE_SWAP_SENTINEL=/tmp/claudelaude-swap-<pid>.env so
#     /bot-mirror, invoked from inside the running claude, can request a
#     swap-to-dtach by writing that file and terminating claude.
#   - After claude exits, if the sentinel file exists, the wrapper reads
#     it (sid, cwd, target socket, bot port), tells the bot to open the
#     mirror topic, then relaunches claude under `dtach -A <socket>` with
#     `--resume <sid>` so the same conversation continues with the input
#     bridge live. When claude eventually quits, control returns to the
#     user's shell prompt as if they had just run `claude`.
#
# Idempotent: replaces any previous block keyed by the marker comments.
# Backs up the rc file once before the first edit.
#
# Refuses to install if the rc already defines an unmanaged `claude`
# function or alias.

set -euo pipefail

MARK_BEGIN="# >>> claudelaude swap >>>"
MARK_END="# <<< claudelaude swap <<<"

# Detect the user's active shell. Falls back to bash. $SHELL is the
# login shell, which matches the rc the user actually maintains in
# practice.
detect_shell() {
    case "${SHELL:-}" in
        */zsh)  echo zsh ;;
        */fish) echo fish ;;
        */bash) echo bash ;;
        *)
            # No usable $SHELL — guess from what exists.
            if [ -f "$HOME/.zshrc" ]; then echo zsh
            elif [ -f "$HOME/.config/fish/config.fish" ]; then echo fish
            else echo bash
            fi
            ;;
    esac
}

SHELL_KIND="${1:-$(detect_shell)}"

case "$SHELL_KIND" in
    bash|zsh|fish) : ;;
    *)
        echo "install-claude-swap: unknown shell '$SHELL_KIND' (need bash/zsh/fish)" >&2
        exit 2
        ;;
esac

# ── posix shells (bash, zsh): single-file rc, function block ─────────
install_posix() {
    local rc="$1"
    [ -f "$rc" ] || touch "$rc"

    # Strip our existing block AND the legacy always-on `claudelaude
    # mirror` block (pre-swap), so re-install is clean and an upgrade
    # doesn't leave two `claude()` functions defined.
    local LEGACY_BEGIN="# >>> claudelaude mirror >>>"
    local LEGACY_END="# <<< claudelaude mirror <<<"
    if grep -qF "$MARK_BEGIN" "$rc" || grep -qF "$LEGACY_BEGIN" "$rc"; then
        python3 - "$rc" "$MARK_BEGIN" "$MARK_END" "$LEGACY_BEGIN" "$LEGACY_END" <<'PYEOF'
import pathlib, re, sys
p = pathlib.Path(sys.argv[1])
src = p.read_text()
for begin, end in [(sys.argv[2], sys.argv[3]), (sys.argv[4], sys.argv[5])]:
    pat = re.compile(
        r'\n?' + re.escape(begin) + r'.*?' + re.escape(end) + r'\n?',
        re.S)
    src = pat.sub("\n", src)
p.write_text(src)
PYEOF
    elif grep -qE '^[[:space:]]*(alias[[:space:]]+claude=|claude\(\)[[:space:]]*\{|function[[:space:]]+claude[[:space:]]*[\{(])' "$rc"; then
        echo "install-claude-swap: $rc already defines a 'claude' alias/function." >&2
        echo "  Remove or rename it, then re-run." >&2
        exit 3
    fi

    # Back up the rc once, before our first edit.
    if [ ! -f "$rc.before-claudelaude" ]; then
        cp "$rc" "$rc.before-claudelaude"
    fi

    cat >> "$rc" <<'POSIX_BLOCK'

# >>> claudelaude swap >>>
# ClaudeLaude on-demand mirror swap. By default this wrapper is a
# transparent passthrough — `claude` behaves exactly like the bare
# binary. It activates only when /bot-mirror, run from inside Claude,
# writes the sentinel file, asking the wrapper to reopen the same
# session under `dtach` so the Telegram topic can type back into it.
# To disable the wrapper for one shell: `export CLAUDELAUDE_NO_WRAP=1`.
claude() {
    if [ -n "${CLAUDELAUDE_DTACH_SOCKET:-}" ] \
       || [ -n "${CLAUDELAUDE_NO_WRAP:-}" ] \
       || ! command -v dtach >/dev/null 2>&1; then
        command claude "$@"
        return $?
    fi
    local _cl_sent="/tmp/claudelaude-swap-$$.env"
    rm -f "$_cl_sent"
    CLAUDELAUDE_SWAP_SENTINEL="$_cl_sent" command claude "$@"
    local _cl_ec=$?
    if [ -f "$_cl_sent" ]; then
        # Parse sentinel line-by-line. We never `source` the file —
        # values are user-controlled (cwd may contain shell metas), and
        # `source`/`.` would let a malicious cwd inject commands.
        local CLSWAP_SID="" CLSWAP_CWD="" CLSWAP_SOCK="" CLSWAP_PORT=""
        local _cl_key="" _cl_val=""
        while IFS='=' read -r _cl_key _cl_val; do
            # Strip outer double-quotes if present.
            _cl_val="${_cl_val#\"}"
            _cl_val="${_cl_val%\"}"
            case "$_cl_key" in
                CLSWAP_SID)  CLSWAP_SID="$_cl_val"  ;;
                CLSWAP_CWD)  CLSWAP_CWD="$_cl_val"  ;;
                CLSWAP_SOCK) CLSWAP_SOCK="$_cl_val" ;;
                CLSWAP_PORT) CLSWAP_PORT="$_cl_val" ;;
            esac
        done < "$_cl_sent"
        rm -f "$_cl_sent"
        if [ -z "$CLSWAP_SID" ] || [ -z "$CLSWAP_SOCK" ]; then
            echo "claudelaude: swap sentinel incomplete — staying in plain shell" >&2
            return "$_cl_ec"
        fi
        local _cl_port="${CLSWAP_PORT:-9853}"
        echo "claudelaude: reopening Claude under dtach (--resume $CLSWAP_SID)..."
        # Ask the bot to open the mirror topic up front so the user sees
        # it appear in Telegram while dtach is still starting.
        curl -sS --max-time 8 -X POST \
            "http://127.0.0.1:${_cl_port}/hook/open_in_bot" \
            -H 'Content-Type: application/json' \
            --data-raw "{\"hook_event_name\":\"open_in_bot\",\"session_id\":\"${CLSWAP_SID}\",\"cwd\":\"${CLSWAP_CWD}\",\"dtach_socket\":\"${CLSWAP_SOCK}\"}" \
            >/dev/null 2>&1 \
            || echo "claudelaude: bot unreachable at 127.0.0.1:${_cl_port} — mirror topic may not open" >&2
        [ -e "$CLSWAP_SOCK" ] && rm -f "$CLSWAP_SOCK"
        # Terminal-close detection: when this terminal dies the OS HUPs
        # the shell. Tell the bot, so it can stop the now-detached
        # claude and offer continuing the session from Telegram. The
        # trap is cleared right after dtach returns on the normal path.
        trap 'curl -sS --max-time 2 -X POST "http://127.0.0.1:${_cl_port}/hook/terminal_closed" -H "Content-Type: application/json" --data-raw "{\"hook_event_name\":\"terminal_closed\",\"session_id\":\"${CLSWAP_SID}\"}" >/dev/null 2>&1' HUP
        CLAUDELAUDE_DTACH_SOCKET="$CLSWAP_SOCK" \
            dtach -A "$CLSWAP_SOCK" -E -z -r winch claude --resume "$CLSWAP_SID"
        local _cl_dec=$?
        trap - HUP
        return "$_cl_dec"
    fi
    return "$_cl_ec"
}
# <<< claudelaude swap <<<
POSIX_BLOCK

    echo "installed swap wrapper into $rc"
}

# ── fish: per-function file ──────────────────────────────────────────
install_fish() {
    local dir="$HOME/.config/fish/functions"
    local f="$dir/claude.fish"
    mkdir -p "$dir"

    if [ -f "$f" ] && ! grep -qF "claudelaude-swap" "$f"; then
        echo "install-claude-swap: $f exists and is not ours." >&2
        echo "  Remove or rename it, then re-run." >&2
        exit 3
    fi

    if [ ! -f "$f.before-claudelaude" ] && [ -f "$f" ]; then
        cp "$f" "$f.before-claudelaude"
    fi

    cat > "$f" <<'FISH_BLOCK'
# claudelaude-swap: on-demand mirror swap wrapper.
# Transparent by default; activates only when the sentinel file is
# written by /bot-mirror from inside Claude.
function claude
    if test -n "$CLAUDELAUDE_DTACH_SOCKET"
        or test -n "$CLAUDELAUDE_NO_WRAP"
        or not command -v dtach >/dev/null 2>&1
        command claude $argv
        return $status
    end
    set -l _cl_sent /tmp/claudelaude-swap-$fish_pid.env
    rm -f $_cl_sent
    set -lx CLAUDELAUDE_SWAP_SENTINEL $_cl_sent
    command claude $argv
    set -l _cl_ec $status
    if test -f $_cl_sent
        set -l CLSWAP_SID ""
        set -l CLSWAP_CWD ""
        set -l CLSWAP_SOCK ""
        set -l CLSWAP_PORT ""
        for _cl_line in (cat $_cl_sent)
            set -l _cl_kv (string split -m 1 = -- $_cl_line)
            if test (count $_cl_kv) -eq 2
                set -l _cl_key $_cl_kv[1]
                set -l _cl_val (string trim --chars='"' -- $_cl_kv[2])
                switch $_cl_key
                    case CLSWAP_SID;  set CLSWAP_SID  $_cl_val
                    case CLSWAP_CWD;  set CLSWAP_CWD  $_cl_val
                    case CLSWAP_SOCK; set CLSWAP_SOCK $_cl_val
                    case CLSWAP_PORT; set CLSWAP_PORT $_cl_val
                end
            end
        end
        rm -f $_cl_sent
        if test -z "$CLSWAP_SID"; or test -z "$CLSWAP_SOCK"
            echo "claudelaude: swap sentinel incomplete — staying in plain shell" >&2
            return $_cl_ec
        end
        if test -z "$CLSWAP_PORT"
            set CLSWAP_PORT 9853
        end
        echo "claudelaude: reopening Claude under dtach (--resume $CLSWAP_SID)..."
        curl -sS --max-time 8 -X POST \
            "http://127.0.0.1:$CLSWAP_PORT/hook/open_in_bot" \
            -H 'Content-Type: application/json' \
            --data-raw "{\"hook_event_name\":\"open_in_bot\",\"session_id\":\"$CLSWAP_SID\",\"cwd\":\"$CLSWAP_CWD\",\"dtach_socket\":\"$CLSWAP_SOCK\"}" \
            >/dev/null 2>&1
        or echo "claudelaude: bot unreachable at 127.0.0.1:$CLSWAP_PORT — mirror topic may not open" >&2
        test -e $CLSWAP_SOCK; and rm -f $CLSWAP_SOCK
        # Terminal-close detection (see posix wrapper). Handler vars go
        # through globals — fish event functions don't see locals.
        set -g _clswap_hup_port $CLSWAP_PORT
        set -g _clswap_hup_sid $CLSWAP_SID
        function _claudelaude_on_hup --on-signal SIGHUP
            curl -sS --max-time 2 -X POST \
                "http://127.0.0.1:$_clswap_hup_port/hook/terminal_closed" \
                -H 'Content-Type: application/json' \
                --data-raw "{\"hook_event_name\":\"terminal_closed\",\"session_id\":\"$_clswap_hup_sid\"}" \
                >/dev/null 2>&1
        end
        set -lx CLAUDELAUDE_DTACH_SOCKET $CLSWAP_SOCK
        dtach -A $CLSWAP_SOCK -E -z -r winch claude --resume $CLSWAP_SID
        set -l _cl_dec $status
        functions -e _claudelaude_on_hup
        set -e _clswap_hup_port
        set -e _clswap_hup_sid
        return $_cl_dec
    end
    return $_cl_ec
end
FISH_BLOCK

    echo "installed swap wrapper into $f"
}

case "$SHELL_KIND" in
    bash) install_posix "$HOME/.bashrc" ;;
    zsh)  install_posix "$HOME/.zshrc"  ;;
    fish) install_fish ;;
esac
