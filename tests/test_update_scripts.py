"""Functional tests for the install/update shell scripts.

update.sh is driven hermetically: a fake `curl` on PATH returns the release
tag (no network), a fake `.venv/bin/pip` no-ops the dependency step, and real
git runs against a throwaway local upstream. Each test documents how to make it
go red by reverting the fix it guards.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
UPDATE_SH = REPO / "update.sh"
HOOKS_SH = REPO / "scripts" / "configure-claude-hooks.sh"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _commit(cwd, msg):
    _git(cwd, "add", "-A")
    _git(cwd, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", msg)


@pytest.fixture
def repo(tmp_path):
    """Build an upstream (origin) with base→v1 history and a working clone.

    Returns a small namespace with the clone dir and helpers. `v1` is the
    release the fake curl advertises; `base` is one commit behind it.
    """
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q")
    # Files an update touches / needs.
    (upstream / "bot.py").write_text("# stub\n")
    (upstream / "requirements.txt").write_text("")
    (upstream / "update.sh").write_text(UPDATE_SH.read_text())
    (upstream / "scripts").mkdir()
    (upstream / "scripts" / "configure-claude-hooks.sh").write_text(HOOKS_SH.read_text())
    (upstream / "README.md").write_text("title\nBASE\ntail\n")
    _commit(upstream, "base")
    _git(upstream, "tag", "base")
    (upstream / "README.md").write_text("title\nV1\ntail\n")
    _commit(upstream, "v1")
    _git(upstream, "tag", "v1")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(upstream), str(clone))

    # Fake binaries: curl → release JSON; pip → no-op (so the deps step is fast).
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    (fakebin / "curl").write_text(
        '#!/usr/bin/env bash\necho \'{"tag_name":"v1"}\'\nexit 0\n')
    os.chmod(fakebin / "curl", 0o755)
    venvbin = clone / ".venv" / "bin"
    venvbin.mkdir(parents=True)
    (venvbin / "pip").write_text('#!/usr/bin/env bash\nexit 0\n')
    os.chmod(venvbin / "pip", 0o755)

    env = dict(os.environ)
    env["PATH"] = f"{fakebin}:" + env["PATH"]
    env["HOME"] = str(tmp_path / "home")
    (tmp_path / "home").mkdir()

    class R:
        pass
    r = R()
    r.clone = clone
    r.upstream = upstream
    r.env = env

    def run(*strategy_args):
        p = subprocess.run(
            ["bash", str(clone / "update.sh"), "--non-interactive", *strategy_args],
            cwd=clone, env=env, capture_output=True, text=True)
        return p.returncode, p.stdout + p.stderr
    r.run = run

    def checkout(ref):
        _git(clone, "checkout", "-q", "-f", ref)
        _git(clone, "clean", "-xfdq", "-e", ".venv")
        # restore fake pip wiped by clean
        if not (venvbin / "pip").exists():
            (venvbin / "pip").write_text('#!/usr/bin/env bash\nexit 0\n')
            os.chmod(venvbin / "pip", 0o755)
    r.checkout = checkout

    def head(ref="HEAD"):
        return subprocess.run(["git", "rev-parse", ref], cwd=clone,
                              capture_output=True, text=True).stdout.strip()
    r.head = head

    def gen_checksums():
        files = subprocess.run(["git", "ls-files"], cwd=clone,
                               capture_output=True, text=True).stdout.split()
        out = subprocess.run(["sha256sum", *files], cwd=clone,
                             capture_output=True, text=True).stdout
        (clone / ".dist_checksums").write_text(out)
    r.gen_checksums = gen_checksums
    return r


def test_clean_fast_forward(repo):
    repo.checkout("base")
    rc, out = repo.run()
    assert rc == 0, out
    assert repo.head() == repo.head("v1^{commit}")


def test_up_to_date_exits_2(repo):
    repo.checkout("v1")
    rc, out = repo.run()
    assert rc == 2, out
    assert "up to date" in out.lower()


def test_replace_discards_local_edits(repo):
    # Direct replace with NO prior stash: regression guard for the
    # drop_our_stash `set -e` abort. Revert the `|| true` on the ref= line in
    # update.sh and this goes rc 1.
    repo.checkout("base")
    repo.gen_checksums()
    (repo.clone / "README.md").write_text("title\nMY LOCAL EDIT\ntail\n")
    rc, out = repo.run("--strategy=replace")
    assert rc == 0, out
    assert repo.head() == repo.head("v1^{commit}")
    assert (repo.clone / "README.md").read_text() == "title\nV1\ntail\n"
    assert any(p.name.startswith(".backup_") for p in repo.clone.iterdir())


def test_auto_conflict_exits_3(repo):
    repo.checkout("base")
    repo.gen_checksums()
    (repo.clone / "README.md").write_text("title\nLOCAL CLASH\ntail\n")
    rc, out = repo.run("--strategy=auto")
    assert rc == 3, out
    assert (repo.clone / ".update_state").read_text().strip() == "v1"


def test_finalize_after_conflict(repo):
    # Drive the real conflict → resolve → finalize flow. Regression guard for
    # the up-to-date guard short-circuiting finalize. Move the finalize branch
    # back below the guard and this goes rc 2 with .update_state left behind.
    repo.checkout("base")
    repo.gen_checksums()
    (repo.clone / "README.md").write_text("title\nLOCAL CLASH\ntail\n")
    rc, _ = repo.run("--strategy=auto")
    assert rc == 3
    # resolve in place (like the bot's merge session: edit, no commit)
    (repo.clone / "README.md").write_text("title\nRESOLVED\ntail\n")
    _git(repo.clone, "add", "-A")
    rc, out = repo.run("--strategy=finalize")
    assert rc == 0, out
    assert not (repo.clone / ".update_state").exists()
    stashes = subprocess.run(["git", "stash", "list"], cwd=repo.clone,
                             capture_output=True, text=True).stdout
    assert stashes.strip() == ""


def test_ahead_of_release_is_up_to_date(repo):
    # HEAD newer than the latest published release (dev / pre-release build).
    # Regression guard for the merge-base ancestor check. Without it, ff-only to
    # an ancestor no-ops yet the script claims "updated to <older>".
    repo.checkout("v1")
    (repo.clone / "NEWFILE").write_text("ahead\n")
    _commit(repo.clone, "ahead of release")
    ahead = repo.head()
    rc, out = repo.run()
    assert rc == 2, out
    assert "newer than the latest release" in out
    assert repo.head() == ahead  # not moved


# ── hooks helper ──────────────────────────────────────────────────────────
def _run_hooks(settings, port="9853"):
    return subprocess.run(["bash", str(HOOKS_SH), str(settings), port],
                          capture_output=True, text=True)


def test_hooks_helper_creates_then_rerun_is_safe(tmp_path):
    # The stored hook commands contain \" and ' — re-parsing them via the old
    # inline json.loads('''$EXISTING''') crashed. Re-running must stay valid.
    sf = tmp_path / "settings.json"
    assert _run_hooks(sf).returncode == 0
    assert set(json.loads(sf.read_text())["hooks"]) == {"Notification", "PermissionRequest"}
    r = _run_hooks(sf)  # the re-parse that used to crash
    assert r.returncode == 0, r.stderr
    json.loads(sf.read_text())  # still valid


def test_hooks_helper_preserves_other_keys(tmp_path):
    sf = tmp_path / "settings.json"
    _run_hooks(sf)
    d = json.loads(sf.read_text())
    d["model"] = "opus"
    d["hooks"]["Stop"] = [{"keep": 1}]
    sf.write_text(json.dumps(d))
    assert _run_hooks(sf, "9999").returncode == 0
    d2 = json.loads(sf.read_text())
    assert d2["model"] == "opus"
    assert "Stop" in d2["hooks"]
    assert "9999" in d2["hooks"]["Notification"][0]["hooks"][0]["command"]


def test_hooks_helper_leaves_corrupt_file_untouched(tmp_path):
    sf = tmp_path / "settings.json"
    sf.write_text("{not valid json")
    r = _run_hooks(sf)
    assert r.returncode == 4
    assert sf.read_text() == "{not valid json"
