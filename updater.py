"""Self-update mechanics — git/version probing and running update.sh.

Pure plumbing with no dependency on bot state or the Telegram client:
shells out to git and update.sh and inspects checksums. The bot-facing
command (`cmd_update`) and the background poll loop live in bot.py and
call into these helpers.
"""

import os
import subprocess
import sys

from config import BOT_DIR


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", BOT_DIR, *args],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except Exception:
        return ""


def _check_update() -> tuple[str, str] | None:
    """Return (current_ver, latest_ver) if an update is available, else None."""
    _git("fetch", "--tags", "origin")
    from version import get_version
    current = get_version()
    tags = _git("tag", "-l", "v*")
    if not tags:
        return None
    latest_tag = sorted(tags.splitlines(), key=lambda t: [
        int(x) for x in t.lstrip("v").split(".") if x.isdigit()
    ])[-1]
    latest = latest_tag.lstrip("v")
    local_head = _git("rev-parse", "HEAD")
    remote_head = _git("rev-parse", "origin/main")
    if local_head == remote_head and local_head:
        return None
    if current.split("+")[0] == latest:
        return None
    return current, latest


def _has_local_changes() -> list[str]:
    """Return list of files modified compared to .dist_checksums."""
    checksums_path = os.path.join(BOT_DIR, ".dist_checksums")
    if not os.path.isfile(checksums_path):
        return []
    import hashlib
    modified = []
    with open(checksums_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("  ", 1)
            if len(parts) != 2:
                continue
            expected_hash, filepath = parts
            full = os.path.join(BOT_DIR, filepath)
            if not os.path.isfile(full):
                continue
            with open(full, "rb") as fh:
                actual = hashlib.sha256(fh.read()).hexdigest()
            if actual != expected_hash:
                modified.append(filepath)
    return modified


def _run_update(non_interactive=False, policy=None) -> tuple[bool, str]:
    """Run update.sh; return (success, output)."""
    cmd = ["bash", os.path.join(BOT_DIR, "update.sh")]
    if non_interactive:
        cmd.append("--non-interactive")
    if policy:
        cmd.append(f"--policy={policy}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=120, cwd=BOT_DIR)
        return r.returncode == 0, r.stdout + r.stderr
    except Exception as e:
        return False, str(e)


def _restart_bot():
    """Re-exec the bot process."""
    print("[update] restarting bot...", file=sys.stderr, flush=True)
    os.execv(sys.executable, [sys.executable] + sys.argv)
