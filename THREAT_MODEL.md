# Threat Model

ClaudeLaude runs Claude Code as a local subprocess and relays it through a
Telegram bot. Claude Code has unrestricted shell access on the host: it reads
and writes files, runs arbitrary commands, and changes system state. The bot
exposes that power over Telegram messages.

The single fact this whole document follows from:

> **Anyone who can send messages to the bot as the owner has a shell on your
> machine.**

This is not a bug. It is what the tool is for — driving your own machine from
your phone. The model below is about keeping *only you* on the sending end, and
being honest about where that guarantee ends.

## What you are protecting

- **The host machine** — its filesystem, processes, network access, and any
  credentials reachable from it (SSH keys, cloud tokens, other `.env` files).
- **Your data** — anything Claude can read on that machine.
- **Your Telegram account** — the key to all of the above.
- **Your money** — Claude Code runs against a paid plan or API key; sustained
  or high-tier use spends real money (see Burn mode in `SECURITY.md`).

## The trust boundary

The bot answers exactly one Telegram user: `OWNER_ID`. Every inbound message and
callback is checked against it (`bot.py`); everything from any other account is
ignored. There is no multi-tenant mode and no second user.

That check is the whole boundary. There is **no secondary authentication** —
no PIN, passphrase, or app-level 2FA on normal operation, and no idle re-lock.
So the boundary is only as strong as your Telegram account. Defend that account
first; everything else here is depth behind it.

## In scope — threats the bot tries to address

### Stolen or hijacked Telegram session

Session hijack, SIM swap, an unlocked phone, or malware on a device with an
active Telegram session all hand an attacker the owner identity, and through it
the machine.

What the bot does about it:

- **Kill switch** (`/kill`) freezes the bot and stops every session
  immediately. It is **one-directional**: you cannot undo it from Telegram. An
  attacker holding your Telegram cannot reverse a lockdown from the same place.
  Recovery happens on a *different channel* — sending the configured unlock word
  in the General topic, or deleting the `.kill` file on the machine.
- **Brute-force protection** on the unlock word: 3 attempts per 5 minutes,
  constant-time comparison (`hmac.compare_digest`), no regex. Failed attempts
  and rate-limits are logged.
- **Audit log** (`.audit.log`, append-only) records security events so you can
  see after the fact what happened.
- **Device monitor** (optional, via Telethon) periodically checks your active
  Telegram sessions and alerts you when a new device appears.

These are detection and containment, not prevention. They shorten the window
and leave a trail; they do not stop an attacker who already holds your account
from acting before you notice.

### Bot token leak

The bot token alone is inert: the bot only answers `OWNER_ID`, so a leaked token
cannot drive it. The risk is a token leak *combined* with a compromised or
spoofed Telegram account. Treat the token as a secret anyway — it lives in
`.env` (gitignored). Rotate it via @BotFather if you suspect exposure.

### Local network exposure of the hook port

The hooks HTTP server binds to `127.0.0.1` only (`hooks.py`). If you expose that
port to the network — misconfigured firewall, port forwarding, a VPN split
tunnel — anyone who can reach it could answer permission hooks and approve
operations on your behalf. Do not change the bind address.

### Prompt injection via untrusted content

If a session processes untrusted input (web pastes, files or media from unknown
sources), that content can try to steer Claude into running destructive
commands. This is a general Claude Code risk, not unique to the bot, but the bot
makes it easier to trigger remotely. Mitigation is the permission flow: tool
calls and permission requests surface inline in the topic, so you can read what
Claude is about to do and deny it. Incoming voice and video are transcribed by
`faster-whisper` running in an isolated side-venv subprocess, not in the bot's
main process — a deliberate blast-radius limit on media parsing.

## Out of scope — accepted by design

These are real risks the bot does **not** defend against, by choice. Naming them
is the point of this document.

- **Physical access or malware on the host.** If an attacker is already on the
  machine — at the keyboard or running code on it — the bot offers no
  protection. They have what the bot would have given them anyway, directly.
- **Claude Code's unrestricted shell access.** The bot does not sandbox Claude.
  Full shell access *is* the feature; constraining it would defeat the tool.
  This is an accepted property, not an unaddressed vulnerability.
- **Dependency supply chain.** Compromise of upstream packages
  (`requests`, `python-dotenv`, `telethon`, `faster-whisper`, and their
  transitive deps) is out of scope for this model. Use a trusted index and pin
  what you install.
- **Recovering from a Telegram-account takeover using Telegram itself.** By
  design, you cannot unlock the bot from Telegram. If your account is taken,
  recovery requires a *different* channel (machine access). An attacker with
  your Telegram cannot recover either — that is the intended asymmetry.

## Residual risk

After all of the above: your Telegram account security is the single line of
defense for normal operation. There is no PIN, passphrase, app-level 2FA, or
idle re-lock between "owns your Telegram" and "owns your shell." The kill
switch, audit log, brute-force limit, and device monitor add containment and
visibility *after* a compromise — they are not a second factor *before* one.

Reduce this risk where it actually lives: lock down the Telegram account.
Enable 2FA (a cloud password), review active sessions regularly, set a short
auto-lock on mobile, and run the bot as a least-privilege user. See the
hardening recommendations in [`SECURITY.md`](SECURITY.md).

## Reporting

Found a way to cross a boundary this document claims to hold? Do not open a
public issue — see the reporting process in [`SECURITY.md`](SECURITY.md).
