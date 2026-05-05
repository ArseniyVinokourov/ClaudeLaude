"""Version resolution: MAJOR from VERSION file, MINOR/PATCH from git.

Dev:  VERSION = "0"          -> "0.<commits-on-main>.<commits-ahead-of-main>"
Dist: VERSION = "0.42.3"     -> returned as-is (baked at packaging time)
"""
import os
import subprocess

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))


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
    if "." in raw:
        return raw
    major = raw or "0"
    if not _git("rev-parse", "--git-dir"):
        return f"{major}.0.0"
    minor = _git("rev-list", "--count", "main") or "0"
    patch = _git("rev-list", "--count", "HEAD", "^main") or "0"
    return f"{major}.{minor}.{patch}"


if __name__ == "__main__":
    print(get_version())
