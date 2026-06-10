"""Self-update mechanics — git/version probing and running update.sh.

Pure plumbing with no dependency on bot state or the Telegram client:
shells out to git and update.sh and inspects checksums. The bot-facing
command (`cmd_update`) and the background poll loop live in bot.py and
call into these helpers.

"Update available" is gated on a manually-published GitHub Release, not on
new commits/tags on main: `release.yml` tags every merge for versioning but
no longer creates a Release. The owner publishes a Release ("Draft a new
release" → pick a tag → Publish) when a batch of work is ready to ship; only
then does `releases/latest` return it and the bot offers the update. Apply
moves the local checkout to that release's tag, never to bare main HEAD.
"""

import os
import re
import subprocess
import sys

from config import BOT_DIR

_SLUG_RE = re.compile(r"github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$")


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", BOT_DIR, *args],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except Exception:
        return ""


def _repo_slug() -> str | None:
    """``owner/repo`` parsed from the origin remote URL (https or ssh form)."""
    m = _SLUG_RE.search(_git("remote", "get-url", "origin"))
    return m.group(1) if m else None


def _latest_release() -> tuple[str, str] | None:
    """(tag, changelog_body) of the latest *published* GitHub Release.

    Returns None when no release is published yet (404) or on any network /
    parse error — callers treat that as "no update available", never an error.
    Unauthenticated REST call; fine for an hourly poll on a public repo.
    """
    slug = _repo_slug()
    if not slug:
        return None
    try:
        import requests
        r = requests.get(
            f"https://api.github.com/repos/{slug}/releases/latest",
            headers={"Accept": "application/vnd.github+json"},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:
        return None
    tag = (data.get("tag_name") or "").strip()
    body = (data.get("body") or "").strip()
    return (tag, body) if tag else None


def _ver_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split(".") if x.isdigit())


def _is_newer(latest: str, current: str) -> bool:
    try:
        return _ver_tuple(latest) > _ver_tuple(current)
    except Exception:
        return latest != current and bool(latest)


def _check_update() -> tuple[str, str, str] | None:
    """Return (current_ver, latest_ver, changelog) if a newer published
    GitHub Release exists, else None. version.py still reflects the local tag;
    "available" is strictly the manually-published release (see module docs)."""
    rel = _latest_release()
    if not rel:
        return None
    latest_tag, body = rel
    latest = latest_tag.lstrip("v")
    from version import get_version
    current = get_version().split("+")[0]
    if not _is_newer(latest, current):
        return None
    return current, latest, body


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


def _conflicted_files() -> list[str]:
    """Tracked files left with unresolved merge markers (diff-filter=U)."""
    out = _git("diff", "--name-only", "--diff-filter=U")
    return [ln for ln in out.splitlines() if ln.strip()]


def _run_update(non_interactive=False, policy=None,
                strategy=None) -> tuple[int, str]:
    """Run update.sh; return (returncode, output).

    Exit codes (update.sh contract): 0=updated, 1=error, 2=up-to-date,
    3=conflict (local edits clash; tree left resolvable). `strategy` is one of
    auto / replace / finalize (see update.sh); `policy` is the legacy knob.
    """
    cmd = ["bash", os.path.join(BOT_DIR, "update.sh")]
    if non_interactive:
        cmd.append("--non-interactive")
    if strategy:
        cmd.append(f"--strategy={strategy}")
    if policy:
        cmd.append(f"--policy={policy}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=120, cwd=BOT_DIR)
        return r.returncode, r.stdout + r.stderr
    except Exception as e:
        return 1, str(e)


def _restart_bot():
    """Re-exec the bot process."""
    print("[update] restarting bot...", file=sys.stderr, flush=True)
    os.execv(sys.executable, [sys.executable] + sys.argv)
