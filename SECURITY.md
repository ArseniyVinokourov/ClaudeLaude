# Security Policy

## Threat model

ClaudeLaude is a Telegram bot that runs Claude Code as a local subprocess. Claude Code has unrestricted shell access — it can read and write files, run arbitrary commands, and modify system state. The bot relays this power through Telegram messages.

**This means: anyone who can send messages to the bot as the owner has full shell access to the host machine.**

Attack vectors to be aware of:

- **Stolen Telegram session** — if an attacker gains access to your Telegram account (session hijack, SIM swap, unlocked phone, malware on a device with an active session), they control the bot and, through it, the machine.
- **Bot token leak** — the bot token alone does not grant access (the bot only responds to `OWNER_ID`), but a leaked token combined with a spoofed or compromised Telegram account is a path to exploitation.
- **Local network exposure** — the hooks HTTP server listens on `localhost` only. If your machine exposes the hook port to the network (misconfigured firewall, port forwarding, VPN split tunnel), an attacker on that network can send hook responses and approve operations on your behalf.
- **Prompt injection via untrusted input** — if a Claude session processes untrusted content (pastes from the web, files from unknown sources), that content could manipulate Claude into running destructive commands. This is a Claude Code risk in general, not specific to the bot, but the bot makes it easier to trigger remotely.

### What is NOT in scope yet

The bot currently relies solely on Telegram's `OWNER_ID` check. There is no secondary authentication (PIN, passphrase, 2FA), no idle timeout, no audit log, and no kill switch. These are tracked as future work (see issue backlog). Until they ship, treat your Telegram account security as the single line of defense.

## Supported versions

Only the latest release on `main` receives security fixes. There are no backports to older tags.

## Reporting a vulnerability

**Do not open a public issue for security vulnerabilities.**

Email [arseny@tentixo.com](mailto:arseny@tentixo.com) with:

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
