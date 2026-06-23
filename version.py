"""Version resolution.

Dev:  VERSION = "0"          -> derived from the latest git tag
                                ("0.M.P" exactly on a tag, "0.M.P+N" with N
                                commits past the tag)
Dist: VERSION = "0.42.3"     -> returned as-is (baked at packaging time)

The release workflow tags every merge to main as v<MAJOR>.<MINOR>.<PATCH>:
- MINOR = total commits on main (so each squash-merged PR bumps it by 1)
- PATCH = commits in the merged PR (size of the change)
"""
import os
import re
import subprocess

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
_TAG_RE = re.compile(r"^v(\d+\.\d+\.\d+)$")


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", _BOT_DIR, *args],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except Exception:
        return ""


def get_version() -> str:
    try:
        with open(os.path.join(_BOT_DIR, "VERSION")) as f:
            raw = f.read().strip()
    except Exception:
        raw = "0"

    # No .git/ -> packaged build: VERSION holds the full version baked at
    # packaging time (or a bare MAJOR). Use it as-is.
    if not _git("rev-parse", "--git-dir"):
        return raw if "." in raw else f"{(raw or '0')}.0.0"

    # In a repo, the latest tag is the source of truth (a dotted VERSION like
    # "1.0.0" is the milestone the next tag flows from, not a frozen value).
    major = (raw.split(".")[0] or "0")
    tag = _git("describe", "--tags", "--abbrev=0", "--match=v*")
    m = _TAG_RE.match(tag) if tag else None
    if not m:
        return f"{major}.0.0"
    base = m.group(1)
    ahead = _git("rev-list", "--count", f"{tag}..HEAD") or "0"
    if ahead == "0":
        return base
    return f"{base}+{ahead}"


if __name__ == "__main__":
    print(get_version())
