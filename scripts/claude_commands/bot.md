---
description: Mirror this Claude session to a ClaudeLaude Telegram topic
allowed-tools: Bash
---

Mirror this terminal Claude session to a ClaudeLaude Telegram topic.
The bot tails the JSONL transcript and (if this Claude is running
inside tmux) can type into the live pane from Telegram.

Use exactly one Bash call. Do not invent or modify the JSON shape.

```bash
PORT="${BOT_HOOK_PORT:-9853}"
SOCKET="${TMUX%%,*}"
PANE="${TMUX_PANE:-}"
RESP=$(curl -sS --max-time 8 -X POST "http://127.0.0.1:${PORT}/hook/open_in_bot" \
  -H 'Content-Type: application/json' \
  --data-raw '{"hook_event_name":"open_in_bot","session_id":"'"$CLAUDE_CODE_SESSION_ID"'","cwd":"'"$PWD"'","tmux_socket":"'"$SOCKET"'","tmux_pane":"'"$PANE"'"}') || { echo "bot unreachable on 127.0.0.1:${PORT}"; exit 0; }
URL=$(echo "$RESP" | grep -oE '"topic_url":"[^"]+"' | cut -d'"' -f4)
ERR=$(echo "$RESP" | grep -oE '"error":"[^"]+"'    | cut -d'"' -f4)
if [ -n "$URL" ]; then
  printf 'mirror: %s\n' "$URL"
  [ -n "$PANE" ] \
    && printf 'tip: Ctrl-b [ then PgUp to scroll the terminal, q to exit\n' \
    || printf 'output-only (claude is not inside tmux)\n'
elif [ -n "$ERR" ]; then
  printf 'error: %s\n' "$ERR"
else
  echo "$RESP"
fi
```

After running the Bash call, just print the captured output as-is. Do
not add explanations, summaries, or extra commentary — the output
already tells the owner everything they need.
