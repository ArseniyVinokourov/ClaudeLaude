"""Tests for the versioning scheme: scripts/compute-version.sh + version.py.

A dotted VERSION (e.g. "1.0.0") whose tag does not exist yet is cut verbatim as
the milestone release. Once tagged, MINOR counts merges since the commit that
set VERSION, and version.py reports the latest tag (never the frozen file).

Both are exercised hermetically against throwaway git repos. Each test notes
how to make it go red by reverting the change it guards.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
COMPUTE_SH = REPO / "scripts" / "compute-version.sh"
VERSION_PY = REPO / "version.py"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _commit(cwd, msg):
    _git(cwd, "add", "-A")
    _git(cwd, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", msg)


def _set_version(cwd: Path, value: str, msg: str):
    (cwd / "VERSION").write_text(value + "\n")
    _commit(cwd, msg)


def _bump(cwd: Path, msg: str):
    """A normal merge that does not touch VERSION."""
    n = len(list(cwd.glob("f_*")))
    (cwd / f"f_{n}").write_text("x")
    _commit(cwd, msg)


def _compute(cwd: Path, patch: str) -> str:
    out = subprocess.run(["bash", str(COMPUTE_SH), patch], cwd=cwd,
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A repo with some 0.x history, then VERSION bumped to 1.0.0 (untagged)."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _set_version(r, "0", "root")
    _bump(r, "early work (#1)")
    _bump(r, "more work (#2)")
    _set_version(r, "1.0.0", "release 1.0.0 (#3)")
    return r


def test_dotted_milestone_is_cut_verbatim(repo):
    # VERSION=1.0.0 not tagged yet -> exactly 1.0.0, ignoring PATCH.
    # Revert: drop the milestone branch in compute-version.sh and it computes
    # 1.0.<patch> ("1.0.7") from the baseline instead.
    assert _compute(repo, "7") == "1.0.0"


def test_minor_counts_merges_since_version_bump(repo):
    _git(repo, "tag", "v1.0.0")          # milestone now released

    _bump(repo, "first feature (#4)")
    # Revert: change MINOR back to `git rev-list --count HEAD` (total history)
    # and this becomes 1.5.4 instead of 1.1.4.
    assert _compute(repo, "4") == "1.1.4"

    _bump(repo, "second feature (#5)")
    assert _compute(repo, "2") == "1.2.2"


def test_new_major_resets_minor_from_its_own_commit(repo):
    _git(repo, "tag", "v1.0.0")
    _bump(repo, "feature (#4)")
    assert _compute(repo, "1") == "1.1.1"

    _set_version(repo, "2.0.0", "release 2.0.0 (#5)")
    assert _compute(repo, "9") == "2.0.0"     # new untagged milestone
    _git(repo, "tag", "v2.0.0")
    _bump(repo, "post-2.0 work (#6)")
    assert _compute(repo, "3") == "2.1.3"     # baseline moved to the 2.0.0 commit


def _run_version_py(cwd: Path) -> str:
    out = subprocess.run([sys.executable, str(cwd / "version.py")],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def test_version_py_reports_latest_tag_not_frozen_file(repo):
    # version.py lives in the repo it reports on; copy it in and tag 1.1.4.
    (repo / "version.py").write_text(VERSION_PY.read_text())
    _git(repo, "tag", "v1.0.0")
    _bump(repo, "feature (#4)")
    _git(repo, "tag", "v1.1.4")
    # VERSION still says 1.0.0, but the latest tag is v1.1.4.
    # Revert: restore `if "." in raw: return raw` and this returns "1.0.0".
    assert _run_version_py(repo) == "1.1.4"

    _bump(repo, "wip (#5)")
    assert _run_version_py(repo) == "1.1.4+1"   # one commit past the tag


def test_version_py_uses_baked_value_without_git(tmp_path):
    d = tmp_path / "pkg"           # not a git repo: a packaged build
    d.mkdir()
    (d / "version.py").write_text(VERSION_PY.read_text())

    (d / "VERSION").write_text("1.0.0\n")
    assert _run_version_py(d) == "1.0.0"

    (d / "VERSION").write_text("1\n")
    assert _run_version_py(d) == "1.0.0"
