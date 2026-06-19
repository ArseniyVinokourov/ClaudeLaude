"""In-bot guided tour — a button-driven walkthrough that shows a new owner
what the bot does, instead of dumping a flat list of commands.

Hosted in General as a single persistent message (no auto-delete timer, so the
General sweep — which only touches the pending-delete registry — leaves it
alone). Navigation edits that one message in place; Close deletes it, leaving
General clean again. We track the current message id in state and delete a
previous tour message before posting a new one, so repeated /tour never piles
up cards in General.

View-only: screen copy, the nav keyboard, and post/paint/close helpers. All
callback wiring lives in bot.py's dispatch (`tr:` branch).
"""

import telegram as tg
from branding import PRODUCT_NAME
from config import get_tour_msg_id, set_tour_msg_id

# Mode blurbs kept deliberately plain and tour-specific (NOT the technical
# MODE_PRESETS labels): the owner wants short, jargon-free wording here.
_MODES = (
    "• default — normal\n"
    "• terse — short answers\n"
    "• verbose — full reasoning\n"
    "• beginner — explains as it goes\n"
    "• plan — plans first, no changes\n"
    "• burn — maximum effort"
)

_TRY_NEW = [{"text": "\U0001f680 Start my first session",
             "callback_data": "tr:go:new"}]
_TRY_PICKER = [{"text": "\U0001f195 Open project picker",
                "callback_data": "tr:go:new"}]
_TRY_HELP = [{"text": "❓ Full help", "callback_data": "tr:go:help"}]


# Each screen: title + body (mobile-width, terse, present-tense) and optional
# "try-it" button rows shown above the nav row. Emojis are kept to the
# universally-rendered set (Unicode 6.0) — newer glyphs showed as boxes.
TOUR: list[dict] = [
    {
        "title": f"\U0001f44b {PRODUCT_NAME}",
        "body": (
            "Claude Code, driven from Telegram.\n"
            "Each session is its own topic —\n"
            "you type, Claude works, and the\n"
            "replies stream back here.\n\n"
            "7 cards. Tap <b>Next ▶</b>"
        ),
    },
    {
        "title": "\U0001f4c1 Sessions",
        "body": (
            "Each chat with Claude is a topic.\n\n"
            "/new — start one in a project\n"
            "/sessions — your bot sessions:\n"
            "  jump in or fork one\n"
            "/resume — pull in a session from\n"
            "  outside (e.g. one from a\n"
            "  terminal)\n\n"
            "Stop / restart / interrupt live in\n"
            "each topic's panel."
        ),
        "extra": [_TRY_PICKER],
    },
    {
        "title": "\U0001f3a8 Modes",
        "body": (
            "Change how Claude answers, per\n"
            "session:\n\n"
            + _MODES
            + "\n\nTap Mode in a topic's panel."
        ),
    },
    {
        "title": "\U0001f4ce Send anything in",
        "body": (
            "Drop it into a session topic:\n\n"
            "• Photos & albums\n"
            "• Voice messages — transcribed\n"
            "• Videos & video messages —\n"
            "  transcript + frames\n"
            "• Stickers\n"
            "• Files & documents\n\n"
            "React to a reply and Claude sees it."
        ),
    },
    {
        "title": "\U0001f518 Control & questions",
        "body": (
            "Every topic has a pinned panel:\n"
            "Mode · Display · Stop · Usage ·\n"
            "History — repainted as it runs.\n\n"
            "/display flips mobile ↔ desktop.\n"
            "When Claude asks a question you\n"
            "get inline buttons to answer."
        ),
    },
    {
        "title": "\U0001f517 Mirror a terminal",
        "body": (
            "Run /bot-mirror inside a terminal\n"
            "Claude session and it streams into\n"
            "a topic — type back from your phone.\n\n"
            "Terminal closed? Continue it as a\n"
            "bot session with one tap."
        ),
    },
    {
        "title": "⚙️ Settings & safety",
        "body": (
            "/usage — tokens & cost\n"
            "/settings — whisper model, media\n"
            "  cleanup, alert thresholds\n\n"
            "\U0001f512 Kill switch, audit log and a\n"
            "brute-force lock keep it yours.\n\n"
            "You're set — start a session \U0001f447"
        ),
        "extra": [_TRY_NEW, _TRY_HELP],
    },
]


def _screen_text(idx: int) -> str:
    s = TOUR[idx]
    n = len(TOUR)
    return f"<b>{s['title']}</b>\n\n{s['body']}\n\n<i>{idx + 1}/{n}</i>"


def _nav_rows(idx: int) -> list:
    """Try-it rows (if any) + a Back/Next row + Close."""
    rows = list(TOUR[idx].get("extra", []))
    nav = []
    if idx > 0:
        nav.append({"text": "◀ Back", "callback_data": f"tr:nav:{idx - 1}"})
    if idx < len(TOUR) - 1:
        nav.append({"text": "Next ▶", "callback_data": f"tr:nav:{idx + 1}"})
    if nav:
        rows.append(nav)
    # Tour-specific close so the handler can clear the tracked message id.
    rows.append([{"text": "✕ Close", "callback_data": "tr:close"}])
    return rows


def paint(msg_id: int, chat_id: int, idx: int) -> None:
    """Repaint the tour message at screen `idx`. Out-of-range is a no-op.
    Buttons are re-passed on every edit so the keyboard survives."""
    if not (0 <= idx < len(TOUR)):
        return
    tg.edit(msg_id, _screen_text(idx), chat_id, buttons=_nav_rows(idx))


def open_tour(fid: int) -> int | None:
    """Post the welcome card into General as a single persistent message.
    Deletes a previous tour message first (dedup), so General never piles up
    cards. Returns the new message id, or None if no forum is configured."""
    if not fid:
        return None
    old = get_tour_msg_id()
    if old:
        tg.delete(old, fid)
    # persist=True: the tour is onboarding, dismissed by its Close button —
    # it opts out of the General auto-reap backstop.
    mid = tg.send(_screen_text(0), fid, buttons=_nav_rows(0), persist=True)
    set_tour_msg_id(mid)
    return mid


def close_tour(msg_id: int, chat_id: int) -> None:
    """Delete the tour message and clear the tracked id."""
    tg.delete(msg_id, chat_id)
    if get_tour_msg_id() == msg_id:
        set_tour_msg_id(None)
