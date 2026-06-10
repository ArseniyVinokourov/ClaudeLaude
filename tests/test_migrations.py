"""State-schema migration framework (config.migrate_state).

Covers the contract the update model relies on: migrations run in order, the
result is idempotent, and a migration that raises must NOT stamp a version it
never reached (so a failed migration is retried on the next start, never
silently skipped). Revert-and-fail: drop the `return` in migrate_state's
except branch and the fail-safe assertion below goes red.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture
def config_mod(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    monkeypatch.setenv("BOT_TOKEN", "fake")
    monkeypatch.setenv("OWNER_ID", "1")
    monkeypatch.setenv("BOT_STATE_FILE", str(state_file))
    sys.modules.pop("config", None)
    cfg = importlib.import_module("config")
    return cfg, state_file


def test_migration_chain_runs_in_order_and_stamps(config_mod):
    cfg, state_file = config_mod
    state_file.write_text(json.dumps({"forum_chat_id": 42}))  # unversioned (v0)

    def m0(s):
        s["step0"] = True
        return s

    def m1(s):
        assert s.get("step0"), "0->1 must run before 1->2"
        s["step1"] = True
        return s

    cfg._STATE_MIGRATIONS = {0: m0, 1: m1}
    cfg.SCHEMA_VERSION = 2
    cfg.migrate_state()

    st = json.loads(state_file.read_text())
    assert st["schema_version"] == 2
    assert st["step0"] and st["step1"]
    assert st["forum_chat_id"] == 42  # untouched data preserved


def test_migration_is_idempotent(config_mod):
    cfg, state_file = config_mod
    state_file.write_text(json.dumps({"schema_version": 0}))
    cfg._STATE_MIGRATIONS = {0: lambda s: {**s, "x": 1}}
    cfg.SCHEMA_VERSION = 1

    cfg.migrate_state()
    first = state_file.read_text()
    cfg.migrate_state()
    assert state_file.read_text() == first  # second run is a no-op


def test_failed_migration_does_not_stamp_version(config_mod):
    cfg, state_file = config_mod
    state_file.write_text(json.dumps({"schema_version": 0}))

    def boom(_s):
        raise ValueError("nope")

    cfg._STATE_MIGRATIONS = {0: boom}
    cfg.SCHEMA_VERSION = 1
    cfg.migrate_state()

    st = json.loads(state_file.read_text())
    # Must remain at 0 so the next start retries — never stamp past a failure.
    assert st.get("schema_version", 0) == 0


def test_absent_state_file_is_a_noop(config_mod):
    cfg, state_file = config_mod
    assert not state_file.exists()
    cfg.migrate_state()  # must not raise or create the file
    assert not state_file.exists()
