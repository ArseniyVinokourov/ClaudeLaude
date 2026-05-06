# Contributing to ClaudeLaude

Thanks for the interest. This is a single-maintainer project; every change ships through a pull request reviewed by [@ArseniyVinokourov](https://github.com/ArseniyVinokourov). Read this guide before opening a PR — CI enforces most of it and will block non-conforming PRs.

## How changes flow

1. Fork the repo (or create a branch if you have write access).
2. Branch off `pre-v1` (active dev) — not `main`.
3. Open a PR into `pre-v1`. CI runs automatically.
4. Maintainer reviews, requests changes, eventually approves and squash-merges.
5. Releases happen by merging `pre-v1` → `main`. Each merge into `main` auto-creates a `vX.Y.Z` tag and a GitHub Release.

## Branch names

Branch must start with one of these prefixes:

```
feat/      new feature
fix/       bug fix
ui/        UI / UX change (Telegram surface)
chore/     build, deps, tooling
ops/       ops, distribution, scripts
security/  security fix or hardening
docs/      docs only
refactor/  refactor without behavior change
```

Examples: `feat/sticker-reactions`, `fix/permissions-lifecycle`, `docs/contributing-guide`.

CI rejects branch names that don't match.

## PR titles

Conventional Commits, **with the same prefix as your branch**:

```
feat: add sticker reactions
fix: race condition between worker and main thread
docs: clarify hook timeout behavior
```

The PR title becomes the squash commit message and feeds into release notes — write it as if it were a changelog line.

## PR description

Use the template. The "## Summary" section must be present and non-empty. Write the **why**, not the **what** — the diff already shows the what.

## Commit messages

- English, present tense ("add" not "added").
- One logical change per commit. Use `git rebase -i` to clean up before opening the PR if needed.
- Subject line ≤ 72 chars.

Squash merging means your individual commit messages don't end up on `main`, but a clean history makes review easier.

## What you cannot modify in a PR

CI blocks non-maintainer PRs that touch:

- `VERSION` — version is managed by the maintainer
- `version.py` — version resolution logic
- `.github/workflows/*` — CI definitions (supply-chain protection)
- `CODEOWNERS` — review routing

If you have a real reason to change one of these, open an issue first.

## Style enforcement

Run these locally before opening a PR. CI runs the same checks.

```bash
# Python syntax
python3 -m py_compile *.py

# Bash syntax
for f in *.sh; do bash -n "$f"; done

# Lint (ruff config in ruff.toml — soft, only catches real issues)
pip install ruff==0.6.9
ruff check .
```

## Versioning (reference)

Format: `vMAJOR.MINOR.PATCH`.

- `MAJOR` is read from the `VERSION` file (`0` during pre-v1 development).
- `MINOR` = number of commits on `main`.
- `PATCH` = commits on the current branch ahead of `main` (always `0` for tagged main commits).

The `version.py` module computes this; the release workflow tags every push to `main` automatically.

## Project conventions worth knowing

These are not enforced by CI but make review faster:

- **Docs are in English by default.** Russian only when explicitly requested.
- **Telegram constraints**: messages truncate at 4096 chars; inline buttons truncate at ~25 chars on mobile; markdown tables don't render — use lists.
- **Single-user bot.** `OWNER_ID` is the only authorized user. No multi-tenant code.
- **`_CLAUDE_BIN`**: subprocess calls to `claude` must use the constant in `sessions.py`, not a bare `'claude'` string (PATH issue under systemd).
- See `CLAUDE.md` for the full list.

## Reporting bugs / requesting features

Use the issue templates. Include the bot version (`python3 version.py`) and Claude Code version (`claude --version`) for bugs.
