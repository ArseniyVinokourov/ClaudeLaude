#!/usr/bin/env bash
# Configure (or refresh) the ClaudeLaude Claude Code hooks in a settings.json.
#
#   Usage: configure-claude-hooks.sh <settings_file> <hook_port>
#
# Sets the Notification and PermissionRequest hooks to POST to the bot's local
# hook server, preserving every other key in the file.
#
# Why a dedicated script (not inline in setup.sh / update.sh): the previous
# inline version substituted the existing file content into the Python program
# text — `json.loads('''$EXISTING''')`. The stored hook commands contain `\"`
# and `'`, so re-reading them corrupted the escapes and crashed json.loads on
# *every* re-run (a fresh setup worked because the file was still `{}`). Here
# the file is read and written by path, and the command strings are built from
# the port inside Python, so nothing user- or file-controlled is ever spliced
# into the program text. setup.sh and update.sh share this one copy.
set -euo pipefail

SETTINGS_FILE="${1:?settings file path required}"
HOOK_PORT="${2:-9853}"

mkdir -p "$(dirname "$SETTINGS_FILE")"

python3 - "$SETTINGS_FILE" "$HOOK_PORT" <<'PYEOF'
import json, os, sys

settings_file, port = sys.argv[1], sys.argv[2]

notify_cmd = (
    "INPUT=$(cat); printf '%s' \"$INPUT\" | curl -sf --max-time 8 "
    f"-X POST http://127.0.0.1:{port}/hook/notification "
    "-H 'Content-Type: application/json' -d @- 2>/dev/null"
)
perm_cmd = (
    "INPUT=$(cat); printf '%s' \"$INPUT\" | curl -sf --max-time 125 "
    f"-X POST http://127.0.0.1:{port}/hook/permission "
    "-H 'Content-Type: application/json' -d @- 2>/dev/null"
)

settings = {}
if os.path.exists(settings_file):
    try:
        with open(settings_file) as f:
            settings = json.load(f)
        if not isinstance(settings, dict):
            raise ValueError("top-level JSON is not an object")
    except (json.JSONDecodeError, ValueError) as e:
        sys.stderr.write(
            f"settings.json is not valid JSON ({e}); leaving it untouched.\n"
            "Fix the file (or remove it) and re-run, or configure the hooks "
            "manually.\n"
        )
        sys.exit(4)

hooks = settings.setdefault("hooks", {})
hooks["Notification"] = [
    {"hooks": [{"type": "command", "command": notify_cmd, "timeout": 10}]}
]
hooks["PermissionRequest"] = [
    {"hooks": [{"type": "command", "command": perm_cmd, "timeout": 130}]}
]

tmp = settings_file + ".tmp"
with open(tmp, "w") as f:
    json.dump(settings, f, indent=2)
os.replace(tmp, settings_file)
print("ok")
PYEOF
