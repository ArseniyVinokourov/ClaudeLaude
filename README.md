	CLAUDELAUDE - the stupid session tracker

"claudelaude" can mean anything, depending on your mood.

 - childish reduplication that pairs Claude with itself. The fact that
   it sounds like a four-year-old naming a stuffed animal may or may
   not be relevant.
 - "Claude, loud": when topics multiply, hooks fire, and your phone
   refuses to shut up.
 - "claude applaude": you typed /new, it actually started, and the
   prompt came back. Angels sing, and a light suddenly fills the room.
 - "Claude, lord help me": when it breaks.

This is a single-user Telegram bot that puts Claude Code behind a
Forum Topics UI. Each topic is one Claude session. Hooks, permissions,
status, compact/expand — all through Telegram, all on your machine.
Nothing leaves the box.


What it does
------------

 - /new spawns a Claude Code session in a project of your choice and
   gives it its own forum topic. You write into the topic; the message
   goes to Claude. Claude's replies and tool calls come back live.
 - Project picker: bare /new lists the four most recently touched
   projects, with "show all" for the rest.
 - /resume picks up an existing JSONL session after a restart, a fork,
   or after the bot was down.
 - Topics rename themselves from the conversation context.
 - /compact folds long output into a preview; /expand puts it back.
 - One live status message per turn from the worker thread, instead
   of a tool-call-per-message firehose.
 - Permission requests from terminal Claude sessions arrive with
   inline Allow / Deny buttons.
 - Notifications from any Claude session — bot-spawned or terminal —
   land in General.
 - General is ephemeral: everything self-deletes in 5–15 seconds
   except the pinned message.
 - Healthcheck pings live sessions and revives the stuck ones.
 - /usage reports real context-window % via PTY, not the generic
   text the CLI prints to a pipe.
 - /display flips a topic between mobile (lists) and desktop (tables).
 - Unknown /commands are forwarded as plain text to the active
   session, so Claude's own slash commands keep working.


Quick install
-------------

You need:

 - Python 3.10+
 - Claude Code CLI, installed and logged in
 - A Telegram bot token from @BotFather
 - A Telegram group with Topics enabled

Then:

	git clone <repo-url>
	cd claude-bot
	bash setup.sh

setup.sh asks for:

 - BOT_TOKEN     — from @BotFather
 - OWNER_ID      — your Telegram user ID (ask @userinfobot)
 - PROJECTS_DIR  — where your projects live (default: ~/Projects)
 - HOOK_PORT     — local port for hooks (default: 9853)

Run it:

	.venv/bin/python bot.py

In Telegram:

 1. Create a group, enable Topics (Group Settings → Topics).
 2. Add the bot as admin (manage topics, send/delete messages).
 3. Send /setup in the group.
 4. Send /new to start your first session.


Commands
--------

	/setup                        bind a forum group to the bot
	/new [path] [name]            new session, or open the project picker
	/sessions                     list active sessions
	/resume                       resume a JSONL session
	/history [N]                  last N events in the current topic
	/usage                        real context-window % for this session
	/display [mobile|desktop]     formatting mode for this topic
	/menu                         inline menu with quick actions
	/help                         full reference
	/stop                         stop the session in this topic
	/restart                      restart the bot
	/stop_bot                     shut the bot down


Hooks
-----

If setup.sh wires hooks into ~/.claude/settings.json, the bot also
catches events from Claude sessions you run in the terminal — not
just the ones it spawned itself:

 - Notification         → message in General
 - PermissionRequest    → inline Allow / Deny

When the bot is not running, a fallback script DMs you and
auto-allows, so the terminal session is not blocked forever.


Architecture
------------

	your machine
	├── bot.py        Telegram long-polling, commands, session lifecycle
	├── sessions.py   Claude Code subprocess, stream-json parser
	├── telegram.py   Bot API wrapper
	├── hooks.py      HTTP server on localhost:HOOK_PORT
	└── config.py     .env + .state.json

	Claude Code CLI
	├── Notification hook       → POST /hook/notification
	└── PermissionRequest hook  → POST /hook/permission

Everything is local. Data stays on the machine.


Files
-----

	bot.py             main module
	config.py          .env + .state.json
	telegram.py        Bot API wrapper
	sessions.py        Claude Code session manager
	hooks.py           HTTP server for hooks
	hook_fallback.py   fallback when the bot is down (created by setup.sh)
	setup.sh           interactive install
	install.sh         unpack from the distribution archive
	update.sh          update an existing install
	docs/              install/update prompts for end users
	.env.example       config template
	.env               your config (do not commit)
	.state.json        forum group binding (created at runtime)
	requirements.txt   Python dependencies
