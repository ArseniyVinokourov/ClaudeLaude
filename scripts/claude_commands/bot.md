---
description: Mirror this Claude session to a ClaudeLaude Telegram topic
allowed-tools: Bash
---

Open this terminal Claude session in a ClaudeLaude Telegram topic so
the owner can follow along (and, if the bot's PATH shim is active,
type from the phone).

Use exactly one Bash call. Fill in your real session id where the
template shows `<YOUR-SESSION-ID>` — you know your own session id
from your context. Do not invent or modify the JSON shape.

```bash
PORT="${BOT_HOOK_PORT:-9853}"
SOCK="${BOT_DTACH_SOCK:-}"
RESP=$(curl -sS --max-time 8 -X POST "http://127.0.0.1:${PORT}/hook/open_in_bot" \
  -H 'Content-Type: application/json' \
  --data-raw '{"hook_event_name":"open_in_bot","session_id":"<YOUR-SESSION-ID>","cwd":"'"$PWD"'","dtach_sock":"'"$SOCK"'"}')
echo "$RESP"
```

Then read the JSON response.

- If it contains `topic_url`, print that URL on its own line so the
  owner can open it in Telegram.
- If it contains `error`, print the error verbatim.

If the curl itself fails (connection refused, timeout), tell the
owner that the ClaudeLaude bot does not appear to be running locally
and that they should start it.
