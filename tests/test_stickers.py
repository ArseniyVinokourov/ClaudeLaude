"""Unit tests for the sticker catalog + marker pipeline (stickers.py).

State is redirected to a per-test tempdir so the live .state.json is never
touched. Telegram network calls (getStickerSet) are monkeypatched.
"""
import config
import stickers


import pytest


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    # Never touch the real .state.json — the running bot uses it.
    monkeypatch.setattr(config, "_STATE_FILE", str(tmp_path / "state.json"))
    # _ENABLED is read from env at import; force it on for deterministic tests.
    monkeypatch.setattr(stickers, "_ENABLED", True)
    yield


def test_add_and_dedup():
    assert stickers.add("FID1", emoji="😎", set_name="pack") is True
    assert stickers.add("FID1", emoji="😎") is False  # same file_id → no dup
    assert len(stickers.items()) == 1
    assert stickers.items()[0]["id"] == "s1"


def test_add_refreshes_missing_metadata():
    stickers.add("FID1")                 # learned with no emoji yet
    stickers.add("FID1", emoji="🔥")     # dup, but fills the missing emoji
    assert stickers.items()[0]["emoji"] == "🔥"


def test_ids_increment_and_reuse_gaps():
    stickers.add("A")
    stickers.add("B")
    assert [e["id"] for e in stickers.items()] == ["s1", "s2"]


def test_learn_is_add():
    assert stickers.learn("FIDL", emoji="👍", set_name="p") is True
    assert stickers.items()[0]["file_id"] == "FIDL"


def test_extract_strips_and_resolves():
    stickers.add("FIDX", emoji="😎")     # → s1
    clean, fids = stickers.extract("hello ⟦sticker:s1⟧ world")
    assert clean == "hello world"
    assert fids == ["FIDX"]


def test_extract_unknown_id_dropped():
    clean, fids = stickers.extract("hi ⟦sticker:nope⟧")
    assert clean == "hi"
    assert fids == []


def test_extract_dedup_and_cap():
    for i in range(5):
        stickers.add(f"F{i}", emoji="x")   # s1..s5 → F0..F4
    text = "⟦sticker:s1⟧⟦sticker:s2⟧⟦sticker:s3⟧⟦sticker:s1⟧"
    clean, fids = stickers.extract(text)
    assert clean == ""
    assert fids == ["F0", "F1"]            # deduped + capped at MAX_PER_TURN


def test_no_marker_no_work():
    assert stickers.extract("plain text") == ("plain text", [])
    assert stickers.has_marker("plain") is False
    assert stickers.has_marker("a ⟦sticker:s1⟧ b") is True


def test_inactive_when_empty():
    assert stickers.is_active() is False
    assert stickers.catalog_prompt() == ""
    assert stickers.session_suffix() == ""


def test_active_and_prompt_when_nonempty():
    stickers.add("FID", emoji="😎", desc="cool")
    assert stickers.is_active() is True
    prompt = stickers.catalog_prompt()
    assert "s1" in prompt and "😎" in prompt and "cool" in prompt
    assert stickers.MARKER_INSTRUCTION in stickers.session_suffix()


def test_disabled_is_inactive():
    stickers.add("FID", emoji="😎")
    stickers._ENABLED = False  # monkeypatch fixture restores it after
    try:
        assert stickers.is_active() is False
        assert stickers.session_suffix() == ""
    finally:
        stickers._ENABLED = True


def test_build_from_set(monkeypatch):
    fake = {"stickers": [
        {"file_id": "A", "emoji": "😀"},
        {"file_id": "B", "emoji": "😢"},
    ]}
    monkeypatch.setattr(stickers.tg, "get_sticker_set", lambda name: fake)
    assert stickers.build_from_set("packname") == 2
    assert {e["file_id"] for e in stickers.items()} == {"A", "B"}
    assert stickers.build_from_set("packname") == 0   # idempotent (dedup)


def test_build_from_set_unreachable(monkeypatch):
    monkeypatch.setattr(stickers.tg, "get_sticker_set", lambda name: None)
    assert stickers.build_from_set("missing") == 0
    assert stickers.items() == []
