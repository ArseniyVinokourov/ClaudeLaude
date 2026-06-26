"""Sticker sending for the bot.

Claude can't attach a Telegram sticker on its own — it only emits text. This
module gives it a way: the bot keeps a small catalog of stickers (each a
Telegram ``file_id`` plus the emoji / description it maps to), injects that
catalog into the session's system prompt, and teaches the persona an inline
marker convention. When a reply genuinely calls for a sticker, the model
writes ``⟦sticker:<id>⟧`` at the right spot; the bot strips the marker and
fires ``sendSticker`` right after the text lands.

Two ways the catalog fills up:
- ``build_from_set(name)`` — pull a whole pack via getStickerSet (a bot may
  send any sticker from a set it can fetch). Seeded from ``STICKER_SETS`` at
  startup.
- ``learn(file_id, ...)`` — remember a sticker the owner sends to the bot, so
  the catalog grows toward the stickers actually in use.

The feature is inert (no prompt injection, no markers honored) when the
catalog is empty or ``STICKERS_ENABLED=0`` — a zero-config install behaves
exactly as before.
"""

import os
import re

import config
import telegram as tg

# Off only when explicitly disabled; still a no-op while the catalog is empty,
# so leaving it on costs nothing until stickers exist.
_ENABLED = os.environ.get("STICKERS_ENABLED", "1") not in ("0", "false", "")
# Comma-separated sticker-set names to seed the catalog from at startup.
STICKER_SETS = [s.strip() for s in os.environ.get("STICKER_SETS", "").split(",")
                if s.strip()]
# Hard cap on stickers sent per turn — a reply is text first, a sticker is an
# accent, not a flood.
MAX_PER_TURN = 2

_MARKER = re.compile(r"⟦sticker:([^⟧]+)⟧")


# ── catalog storage ──────────────────────────────────────────────────────

def items() -> list[dict]:
    """All catalog entries: [{id, file_id, emoji, set_name, desc}, ...]."""
    return config.get_sticker_catalog().get("items", [])


def _index() -> dict:
    """id -> file_id, for fast marker resolution."""
    return {e["id"]: e["file_id"] for e in items() if e.get("id")}


def _next_id(existing: list[dict]) -> str:
    """Short stable slug for the marker (s1, s2, …). Reuses the lowest free
    number so removed entries don't leave permanent gaps."""
    used = {e["id"] for e in existing if e.get("id")}
    n = 1
    while f"s{n}" in used:
        n += 1
    return f"s{n}"


def add(file_id: str, emoji: str = "", set_name: str = "",
        desc: str = "") -> bool:
    """Add one sticker to the catalog. Dedups by file_id (a re-seen sticker
    just refreshes its emoji/desc). Returns True if a NEW entry was created."""
    if not file_id:
        return False
    catalog = config.get_sticker_catalog()
    entries = catalog.setdefault("items", [])
    for e in entries:
        if e.get("file_id") == file_id:
            # Already known — fill in any newly-available metadata.
            if emoji and not e.get("emoji"):
                e["emoji"] = emoji
            if desc and not e.get("desc"):
                e["desc"] = desc
            config.set_sticker_catalog(catalog)
            return False
    entries.append({
        "id": _next_id(entries),
        "file_id": file_id,
        "emoji": emoji,
        "set_name": set_name,
        "desc": desc,
    })
    config.set_sticker_catalog(catalog)
    return True


def learn(file_id: str, emoji: str = "", set_name: str = "") -> bool:
    """Remember a sticker the owner sent (auto-learn). Thin wrapper over add()
    kept separate so the call site reads intentionally."""
    return add(file_id, emoji=emoji, set_name=set_name)


def build_from_set(name: str) -> int:
    """Pull a whole sticker set into the catalog. Returns the number of NEW
    stickers added (0 if the set is unreachable or already fully known)."""
    st = tg.get_sticker_set(name)
    if not st:
        return 0
    added = 0
    for s in st.get("stickers", []):
        if add(s.get("file_id", ""), emoji=s.get("emoji", ""), set_name=name):
            added += 1
    return added


def seed_from_env() -> int:
    """Seed the catalog from STICKER_SETS at startup. Idempotent (dedup by
    file_id). Returns total new stickers added across all sets."""
    return sum(build_from_set(name) for name in STICKER_SETS)


# ── prompt surface ─────────────────────────────────────────────────────────

def is_active() -> bool:
    """True when the feature should touch a session at all: enabled AND the
    catalog has at least one sticker to offer."""
    return _ENABLED and bool(items())


def catalog_prompt() -> str:
    """Compact catalog listing for the system prompt: one line per sticker,
    ``id emoji desc``. Empty string when inactive (caller skips injection)."""
    if not is_active():
        return ""
    lines = []
    for e in items():
        tag = e.get("emoji", "") or "—"
        desc = e.get("desc", "")
        lines.append(f"{e['id']} {tag}" + (f" — {desc}" if desc else ""))
    return "AVAILABLE STICKERS (id, emoji, meaning):\n" + "\n".join(lines)


# Teaches the marker convention + when to use it. Mirrors the project's other
# inline-marker conventions: rare, deliberate, never a tic.
MARKER_INSTRUCTION = (
    "STICKERS: you may send a Telegram sticker by writing the marker "
    "⟦sticker:<id>⟧ inline, using an <id> from the AVAILABLE STICKERS list. "
    "The bot strips the marker and sends that sticker right after your text. "
    "Use a sticker ONLY when it genuinely fits as a reaction or accent — a "
    "real emotional beat, a joke landing, a greeting/farewell. NEVER in "
    "neutral, technical or informational replies, and never as a filler or "
    "reflexive sign-off. Most replies have NO sticker. Send at most one or "
    "two, and only when the chosen id's meaning actually matches the moment; "
    "if no id fits, send none. Do not write the bare word 'sticker' or invent "
    "ids that are not in the list."
)


def session_suffix() -> str:
    """The full system-prompt addition for a bot session: catalog + how to use
    it. Empty when inactive."""
    cat = catalog_prompt()
    if not cat:
        return ""
    return cat + "\n\n" + MARKER_INSTRUCTION


# ── outgoing-reply processing ───────────────────────────────────────────────

def has_marker(text: str) -> bool:
    return bool(_MARKER.search(text))


def extract(text: str) -> tuple[str, list[str]]:
    """Split a reply into (clean_text, file_ids_to_send).

    Strips every ⟦sticker:id⟧ marker from the text and resolves known ids to
    file_ids in order, de-duplicated and capped at MAX_PER_TURN. Unknown ids
    are dropped silently (stripped, not sent). Returns the original text and
    an empty list when there are no markers (no work, no catalog read)."""
    if not _MARKER.search(text):
        return text, []
    index = _index()
    file_ids: list[str] = []
    for m in _MARKER.finditer(text):
        sid = m.group(1).strip()
        fid = index.get(sid)
        if fid and fid not in file_ids and len(file_ids) < MAX_PER_TURN:
            file_ids.append(fid)
    clean = _MARKER.sub("", text)
    # Tidy the hole the marker left: stray double spaces and space-before-punct.
    clean = re.sub(r"[ \t]{2,}", " ", clean)
    clean = re.sub(r"[ \t]+([,.!?;:…\n])", r"\1", clean)
    return clean.strip(), file_ids
