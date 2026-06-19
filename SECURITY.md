# Security Policy

## Threat model

ClaudeLaude runs Claude Code as a local subprocess and relays it through a Telegram bot — which means **anyone who can message the bot as the owner has a shell on your machine.**

The full model — assets, in-scope threats and their mitigations, what is explicitly out of scope, and the residual risk — lives in [`THREAT_MODEL.md`](THREAT_MODEL.md). Read it before deciding whether and how to run the bot.

In short: the bot answers only `OWNER_ID`. There is a one-directional kill switch, an append-only audit log, brute-force protection on unlock, and optional device monitoring. There is **no** secondary authentication (PIN, passphrase, 2FA) and no idle re-lock — so your Telegram account security remains the single line of defense for normal operation.

## Supported versions

Only the latest release on `main` receives security fixes. There are no backports to older tags.

## Reporting a vulnerability

**Do not open a public issue for security vulnerabilities.**

Email [a.vinokourov418@gmail.com](mailto:a.vinokourov418@gmail.com) with:

1. Description of the vulnerability
2. Steps to reproduce
3. Impact assessment (what an attacker gains)

You will receive an acknowledgement within 72 hours. Fixes for confirmed vulnerabilities are released as soon as practical, with credit to the reporter unless they prefer anonymity.

## Hardening recommendations

If you run the bot:

- **Lock your Telegram account.** Enable 2FA (cloud password), review active sessions regularly, set a short auto-lock on mobile.
- **Do not expose the hook port.** The server binds to `127.0.0.1` by default. Do not change this unless you know what you are doing.
- **Treat the bot token as a secret.** It lives in `.env`, which is gitignored. Never commit it. Rotate it via @BotFather if you suspect a leak.
- **Run with least privilege.** The bot does not need root. Run it as a normal user. Consider a dedicated user account with limited filesystem access if your machine hosts sensitive data.
- **Monitor your sessions.** Review what Claude is doing in the Telegram topics. The bot shows tool calls and permission requests inline — read them.
- **Watch the cost of Burn mode.** Burn mode (`/mode burn`) deliberately maximizes capability: Opus with the 1M-context model, `max` reasoning effort, and parallel agents. It can spend real money fast over a single session. There is a per-run budget cap (`--max-budget-usd`, currently `$5.0` in `sessions.py`), but no global daily limit — switch back out of Burn mode when you no longer need it, and keep an eye on `/usage`. This is a self-inflicted operational risk, not an attack, but it is the easiest way to lose money with the bot.
