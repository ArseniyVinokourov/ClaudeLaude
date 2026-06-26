# Sticker sending

The bot can already *read* incoming stickers (emoji + pack name + the image
itself go to Claude). This feature lets it *send* them back.

Claude only produces text, so it can't attach a sticker directly. Instead the
bot keeps a small **catalog** of sendable stickers and teaches the session an
inline marker. When a reply genuinely calls for a sticker, the model writes
`⟦sticker:<id>⟧` at the right spot; the bot strips the marker and fires
`sendSticker` right after the text lands.

The feature is **inert** when the catalog is empty or `STICKERS_ENABLED=0` — a
zero-config install behaves exactly as before.

## How a sticker gets sent

1. **Catalog** — each entry is a Telegram `file_id` plus the `emoji` /
   description it maps to. A bot may send any sticker it has a `file_id` for.
2. **Prompt** — when the catalog is non-empty, `stickers.session_suffix()`
   injects the catalog listing + the marker instruction into the bot session's
   `--append-system-prompt` (bot-spawned sessions only).
3. **Marker** — the model emits `⟦sticker:s3⟧`. The instruction keeps it rare
   and deliberate: only as a real reaction/accent, never in neutral or
   technical replies, at most one or two per turn.
4. **Send** — `turncontroller` calls `stickers.extract(text)`, which strips the
   markers and resolves known ids to `file_id`s (deduped, capped at
   `MAX_PER_TURN`). The cleaned text is sent, then each sticker as its own
   message. Unknown ids are dropped silently.

No marker in a turn → a cheap regex check, no catalog read, no extra calls.

## Filling the catalog

Two sources, both feeding the same deduped catalog (`sticker_catalog` in
`.state.json`):

- **Seed a pack** — `build_from_set(name)` pulls a whole sticker set via
  `getStickerSet`. Configure `STICKER_SETS` (comma-separated set names) and the
  bot seeds them at startup (off the startup path, idempotent).
- **Auto-learn** — every sticker the owner sends to the bot is remembered
  (`stickers.learn`), so the catalog grows toward the stickers actually in use.
  Cold start: empty until a pack is seeded or a sticker is received.

## Configuration

| Variable           | Default | Meaning                                                        |
|--------------------|---------|----------------------------------------------------------------|
| `STICKERS_ENABLED` | `1`     | `0` disables the feature entirely (no prompt injection).       |
| `STICKER_SETS`     | (empty) | Comma-separated sticker-set names to seed at startup.          |
| `STICKER_ALLOW`    | (empty) | Restrict sendable stickers to these ids — comma-separated ids and/or ranges, e.g. `s43-s63,s10`. Empty = all. Useful to limit a mixed pack to one character's stickers; also trims the prompt to just those ids. |

## Components

- `telegram.py` — `send_sticker(chat_id, file_id, thread_id)` and
  `get_sticker_set(name)`.
- `stickers.py` — catalog storage, seeding, auto-learn, prompt surface, marker
  parsing.
- `config.py` — `get_sticker_catalog()` / `set_sticker_catalog()` (additive
  `.state.json` key; no schema migration needed).
- `sessions.py` — injects the catalog + marker instruction.
- `turncontroller.py` — strips markers and sends the stickers.
- `bot.py` — auto-learns inbound stickers; seeds `STICKER_SETS` at startup.

## Not yet

- Vision-generated descriptions per sticker (the catalog currently keys on
  emoji + set name; richer descriptions would sharpen the model's choice).
- A `setup.sh` opt-in block.
