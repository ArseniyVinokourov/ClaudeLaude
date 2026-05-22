"""Functional test of the on-demand mirror swap shell wrapper.

Runs the installer into a temp HOME, drives the wrapper with fake
`claude`, `dtach`, and `curl` binaries, and checks the round-trip:

  bare claude → /bot-mirror writes sentinel → wrapper reads it →
  wrapper hits the bot's open_in_bot → wrapper relaunches claude
  under dtach with --resume.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "scripts" / "install-claude-swap.sh"

_TEST_HARNESS = r'''#!/usr/bin/env bash
set -euo pipefail
export WORK="$1"
INSTALLER="$2"

cd "$WORK"
mkdir -p bin
export LOG="$WORK/log.txt"
: > "$LOG"

cat > bin/claude <<'CLAUDE_EOF'
#!/usr/bin/env bash
if [ -n "${CLAUDELAUDE_DTACH_SOCKET:-}" ]; then
    echo "FAKE_CLAUDE_ROUND2_SOCK=$CLAUDELAUDE_DTACH_SOCKET" >> "$LOG"
    echo "FAKE_CLAUDE_ROUND2_ARGS=$*" >> "$LOG"
    exit 0
fi
if [ -n "${CLAUDELAUDE_SWAP_SENTINEL:-}" ]; then
    echo "FAKE_CLAUDE_ROUND1_SENTINEL=$CLAUDELAUDE_SWAP_SENTINEL" >> "$LOG"
    cat > "$CLAUDELAUDE_SWAP_SENTINEL" <<SENT_EOF
CLSWAP_SID="abc-123"
CLSWAP_CWD="/fake/cwd"
CLSWAP_SOCK="$WORK/fake.sock"
CLSWAP_PORT="19999"
SENT_EOF
    exit 0
fi
echo "FAKE_CLAUDE_UNKNOWN_INVOCATION" >> "$LOG"
exit 1
CLAUDE_EOF
chmod +x bin/claude

cat > bin/dtach <<'DTACH_EOF'
#!/usr/bin/env bash
echo "FAKE_DTACH_ARGS=$*" >> "$LOG"
seen_winch=0
INNER=()
for a in "$@"; do
    if [ "$seen_winch" = "1" ]; then
        INNER+=("$a")
    fi
    [ "$a" = "winch" ] && seen_winch=1
done
"${INNER[@]}"
DTACH_EOF
chmod +x bin/dtach

cat > bin/curl <<'CURL_EOF'
#!/usr/bin/env bash
echo "FAKE_CURL_ARGS=$*" >> "$LOG"
exit 0
CURL_EOF
chmod +x bin/curl

export PATH="$WORK/bin:$PATH"
export HOME="$WORK"
bash "$INSTALLER" bash >/dev/null

bash -c '. "$HOME/.bashrc"; claude --help' >> "$LOG" 2>&1
cat "$LOG"
'''


@pytest.fixture
def harness(tmp_path: Path):
    sh = tmp_path / "run.sh"
    sh.write_text(_TEST_HARNESS)
    sh.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    proc = subprocess.run(
        ["bash", str(sh), str(work), str(INSTALLER)],
        capture_output=True, text=True, timeout=15,
    )
    yield proc, work
    shutil.rmtree(work, ignore_errors=True)


def test_swap_writes_sentinel_calls_bot_and_relaunches_under_dtach(harness):
    proc, work = harness
    assert proc.returncode == 0, proc.stderr
    log = (work / "log.txt").read_text()

    # Round 1: bare claude received the sentinel env var and wrote
    # the file.
    assert "FAKE_CLAUDE_ROUND1_SENTINEL=/tmp/claudelaude-swap-" in log
    # The wrapper then called the bot's open_in_bot endpoint with the
    # values it read from the sentinel.
    assert "FAKE_CURL_ARGS=" in log
    assert "open_in_bot" in log
    assert "abc-123" in log
    # Port from the sentinel, not the hardcoded 9853.
    assert "127.0.0.1:19999" in log
    # dtach was invoked with the target socket and --resume <sid>.
    assert f"FAKE_DTACH_ARGS=-A {work}/fake.sock -E -z -r winch claude --resume abc-123" in log
    # Round 2: the inner claude saw CLAUDELAUDE_DTACH_SOCKET and got
    # --resume <sid> as its args.
    assert f"FAKE_CLAUDE_ROUND2_SOCK={work}/fake.sock" in log
    assert "FAKE_CLAUDE_ROUND2_ARGS=--resume abc-123" in log


def test_wrapper_passthrough_without_sentinel(tmp_path: Path):
    """If `claude` exits without writing the sentinel, the wrapper
    is a transparent no-op: no curl, no dtach."""
    work = tmp_path / "work"
    work.mkdir()
    log = work / "log.txt"
    log.write_text("")
    (work / "bin").mkdir()

    fake_claude = work / "bin" / "claude"
    fake_claude.write_text(
        f'#!/usr/bin/env bash\n'
        f'echo "PLAIN_RUN args=$*" >> "{log}"\n'
        f'exit 0\n'
    )
    fake_claude.chmod(0o755)
    # dtach must exist so the wrapper does not short-circuit through
    # the `command claude` fast path.
    fake_dtach = work / "bin" / "dtach"
    fake_dtach.write_text(
        f'#!/usr/bin/env bash\necho "DTACH_RAN" >> "{log}"\nexit 1\n'
    )
    fake_dtach.chmod(0o755)
    fake_curl = work / "bin" / "curl"
    fake_curl.write_text(
        f'#!/usr/bin/env bash\necho "CURL_RAN" >> "{log}"\nexit 1\n'
    )
    fake_curl.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{work}/bin:" + env["PATH"]
    env["HOME"] = str(work)
    subprocess.check_call(
        ["bash", str(INSTALLER), "bash"], env=env)
    subprocess.check_call(
        ["bash", "-c", '. "$HOME/.bashrc"; claude foo bar'], env=env)

    log_text = log.read_text()
    assert "PLAIN_RUN args=foo bar" in log_text
    assert "DTACH_RAN" not in log_text
    assert "CURL_RAN" not in log_text


def test_wrapper_does_not_eval_sentinel_values(tmp_path: Path):
    """The wrapper MUST NOT `source` the sentinel — values are
    user-controlled (cwd may contain shell metas), so any eval is a
    local RCE vector. Plant a sentinel whose cwd would `touch
    /tmp/pwned-...` if sourced; verify the file is NOT created.
    """
    import os
    work = tmp_path / "work"
    work.mkdir()
    log = work / "log.txt"
    log.write_text("")
    pwned = tmp_path / "pwned_marker"

    (work / "bin").mkdir()
    # Fake claude writes a sentinel containing a malicious cwd.
    fake_claude = work / "bin" / "claude"
    fake_claude.write_text(
        f'#!/usr/bin/env bash\n'
        f'if [ -n "${{CLAUDELAUDE_SWAP_SENTINEL:-}}" ]; then\n'
        f'  cat > "$CLAUDELAUDE_SWAP_SENTINEL" <<EOF\n'
        f'CLSWAP_SID="abc-123"\n'
        f'CLSWAP_CWD="/safe"; touch {pwned}; #"\n'
        f'CLSWAP_SOCK="{work}/sock"\n'
        f'CLSWAP_PORT="9853"\n'
        f'EOF\n'
        f'fi\n'
        f'exit 0\n'
    )
    fake_claude.chmod(0o755)

    # dtach + curl: drain stdin and record args, no side effects.
    for name in ("dtach", "curl"):
        p = work / "bin" / name
        p.write_text(
            f'#!/usr/bin/env bash\necho "{name.upper()}_RAN" >> "{log}"\n'
            f'exec >/dev/null\nexit 0\n'
        )
        p.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{work}/bin:" + env["PATH"]
    env["HOME"] = str(work)
    subprocess.check_call(
        ["bash", str(INSTALLER), "bash"], env=env)

    subprocess.run(
        ["bash", "-c", '. "$HOME/.bashrc"; claude'],
        env=env, capture_output=True, timeout=10,
    )

    assert not pwned.exists(), (
        f"sentinel parse must not eval values — `touch {pwned}` ran "
        f"even though the wrapper should be line-parsing the file"
    )


def test_installer_strips_legacy_block(tmp_path: Path):
    """Upgrading from the old always-on `claudelaude mirror` block
    leaves the rc with exactly one `claude()` definition (ours)."""
    home = tmp_path / "home"
    home.mkdir()
    rc = home / ".bashrc"
    rc.write_text(
        "# user stuff\n"
        "alias ll='ls -la'\n"
        "\n"
        "# >>> claudelaude mirror >>>\n"
        "claude() { echo old; }\n"
        "# <<< claudelaude mirror <<<\n"
        "\n"
        "export FOO=bar\n"
    )
    env = os.environ.copy()
    env["HOME"] = str(home)
    subprocess.check_call(
        ["bash", str(INSTALLER), "bash"], env=env)
    text = rc.read_text()
    assert text.count("claude() {") == 1
    assert "# >>> claudelaude swap >>>" in text
    assert "# >>> claudelaude mirror >>>" not in text
    # Unrelated user content survived.
    assert "alias ll='ls -la'" in text
    assert "export FOO=bar" in text
