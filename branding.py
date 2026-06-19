"""Single source of truth for brand identity and the forum-topic icon set.

Pure leaf module — it imports nothing from the project, so even the lowest
layers (``telegram.py``) can pull from it without risking an import cycle.
Only put a value here when it is shared across modules or is a "magic"
identifier that must change in lockstep; per-feature copy stays beside the
code that uses it.
"""

# Product name. Embedded into help, the dashboard header, the tour and the
# system prompt handed to Claude. One constant so a rename touches one line.
PRODUCT_NAME = "ClaudeLaude"

# Custom-emoji IDs for forum-topic icons (Telegram premium emoji set). Shared
# between bot.py (topic lifecycle) and telegram.py (createForumTopic default).
ICON_ACTIVE = "5417915203100613993"    # 💬 live bot session / default
ICON_TERMINAL = "5350554349074391003"  # 💻 terminal mirror
ICON_STOPPED = ""                       # removes custom emoji → color dot
