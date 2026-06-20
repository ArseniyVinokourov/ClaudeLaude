"""Functional tests for uninstall.sh.

Focus on the parts where a bug would be destructive: surgical removal of only
*our* Claude Code hooks (never the user's), dry-run inertness, and stripping the
shell wrapper block without eating surrounding rc lines. Runs against a fake
install in a sandbox HOME — never the real machine.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
UNINSTALL = REPO / "uninstall.sh"
HOOKS = REPO / "scripts" / "configure-claude-hooks.sh"


@pytest.fixture
def install(tmp_path):
    """A fake bot install + sandbox HOME with hooks, command, wrapper, state."""
    bot = tmp_path / "bot"
    bot.mkdir()
    (bot / "bot.py").write_text("# stub\n")
    (bot / "uninstall.sh").write_text(UNINSTALL.read_text())
    for f in (".state.json", ".sessions.json", ".mirrors.json", ".kill",
              ".audit.log", ".known_devices.json"):
        (bot / f).write_text("x")
    (bot / ".venv" / "bin").mkdir(parents=True)
    (bot / ".venv" / "bin" / "python").write_text("")

    home = tmp_path / "home"
    (home / ".claude" / "commands").mkdir(parents=True)
    (home / ".claude" / "commands" / "bot-mirror.md").write_text("x")
    settings = home / ".claude" / "settings.json"
    subprocess.run(["bash", str(HOOKS), str(settings), "9853"], check=True,
                   capture_output=True)
    # add an UNRELATED hook + key the uninstall must preserve
    d = json.loads(settings.read_text())
    d["model"] = "opus"
    d["hooks"]["Stop"] = [{"hooks": [{"type": "command", "command": "echo mine"}]}]
    settings.write_text(json.dumps(d, indent=2))
    # a realistic .bashrc: user content around our managed block
    (home / ".bashrc").write_text(
        "export PATH=$HOME/bin:$PATH\n"
        "# >>> claudelaude swap >>>\nclaude() { command claude \"$@\"; }\n"
        "# <<< claudelaude swap <<<\n"
        "alias ll='ls -la'\n")
    (home / ".bashrc.before-claudelaude").write_text("export PATH=$HOME/bin:$PATH\n")

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["UPLOAD_DIR"] = str(tmp_path / "uploads")
    (tmp_path / "uploads").mkdir()

    def run(*args):
        return subprocess.run(["bash", str(bot / "uninstall.sh"), *args],
                              env=env, capture_output=True, text=True)

    return SimpleNamespace(bot=bot, home=home, settings=settings, run=run)


def test_dry_run_changes_nothing(install):
    r = install.run("--dry-run")
    assert r.returncode == 0, r.stderr
    assert (install.bot / ".state.json").exists()
    assert (install.bot / ".venv").exists()
    assert "claudelaude swap" in (install.home / ".bashrc").read_text()
    assert (install.home / ".claude" / "commands" / "bot-mirror.md").exists()


def test_removes_state_venv_command(install):
    r = install.run("--yes")
    assert r.returncode == 0, r.stderr
    for f in (".state.json", ".sessions.json", ".kill", ".audit.log",
              ".known_devices.json", ".venv"):
        assert not (install.bot / f).exists(), f
    assert not (install.home / ".claude" / "commands" / "bot-mirror.md").exists()


def test_hook_removal_is_surgical(install):
    install.run("--yes")
    d = json.loads(install.settings.read_text())
    # ours gone…
    assert "Notification" not in d.get("hooks", {})
    assert "PermissionRequest" not in d.get("hooks", {})
    # …the user's Stop hook and unrelated keys preserved
    assert "Stop" in d["hooks"]
    assert d["model"] == "opus"


def test_wrapper_block_stripped_keeps_user_lines(install):
    install.run("--yes")
    rc = (install.home / ".bashrc").read_text()
    assert "claudelaude swap" not in rc
    assert "claude()" not in rc
    assert "export PATH=$HOME/bin:$PATH" in rc   # user line above kept
    assert "alias ll='ls -la'" in rc             # user line below kept
    assert not (install.home / ".bashrc.before-claudelaude").exists()


def test_refuses_outside_install(tmp_path):
    # No bot.py → must refuse, touch nothing.
    d = tmp_path / "notabot"
    d.mkdir()
    (d / "uninstall.sh").write_text(UNINSTALL.read_text())
    r = subprocess.run(["bash", str(d / "uninstall.sh"), "--yes"],
                       capture_output=True, text=True)
    assert r.returncode == 1
    assert "doesn't look like" in (r.stdout + r.stderr)
