# Contributing to ClaudeLaude

Thanks for the interest. This is a single-maintainer project; every change ships through a pull request reviewed by [@ArseniyVinokourov](https://github.com/ArseniyVinokourov). Read this guide before opening a PR — CI enforces most of it and will block non-conforming PRs.

## How changes flow

1. Fork the repo (or create a branch if you have write access).
2. Branch off `main`.
3. Open a PR into `main`. CI runs automatically.
4. Maintainer reviews, requests changes, eventually approves and squash-merges.
5. The merge into `main` auto-creates a `vX.Y.Z` tag and a GitHub Release. Each PR = one new version.

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

Conventional Commits:

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
- `.github/scripts/*` — CI scripts (supply-chain protection)
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

## Why your PR might fail CI

When CI fails, a bot posts a single comment on your PR listing every issue with the exact fix. Don't panic — almost all failures are mechanical and quick to fix. The most common ones:

| Failure | What to do |
|---|---|
| Branch name doesn't match `<prefix>/<slug>` | Rename in your fork: `git branch -m feat/your-thing && git push -u origin feat/your-thing`, then re-open the PR from the renamed branch. |
| PR title isn't `<type>: <description>` | Edit the PR title (pencil icon next to title). |
| Branch prefix and PR title prefix differ | Pick one and align both. |
| `## Summary` section missing or empty | Edit the PR body — the template already has the heading; just fill it in. |
| Owner-only files modified | Revert changes to `VERSION`, `version.py`, `.github/workflows/`, `.github/scripts/`, or `CODEOWNERS`. Open an issue if you genuinely need to change one. |
| Python `py_compile` fails | Real syntax error somewhere — fix it locally and push again. |
| Bash `bash -n` fails | Same as above for shell scripts. |
| Ruff lint fails | Run `ruff check --fix .` locally; commit the auto-fixes. The remaining issues are listed in the bot comment. |

The bot comment auto-updates with each push, so just keep pushing fixes and the list shrinks.

## Versioning (reference)

Format: `vMAJOR.MINOR.PATCH`.

- `MAJOR` — from the `VERSION` file (`0` during pre-1.0 development; bumped by hand for breaking changes).
- `MINOR` — total commits on `main`. Each squash-merged PR adds one commit, so MINOR bumps by 1 per release.
- `PATCH` — number of commits in the merged PR (i.e. the size of the change). A 1-commit fix → `.1`, a 7-commit feature → `.7`.

The release workflow tags every merge to `main` as `vMAJOR.MINOR.PATCH` automatically. It does **not** create a GitHub Release — tags are for versioning and traceability only.

At runtime, `version.py` returns the latest tag. On a feature branch with N commits past the tag, it returns `MAJOR.MINOR.PATCH+N` (build identifier — informative, never used as an actual tag).

## Releasing (maintainer)

A **GitHub Release is the manual "available to update" gate**, decoupled from tags. Tags flow on every merge; an installed bot only offers an update when a Release is *published* (it polls `releases/latest`). So you batch as many merges as you like, then ship one Release when ready.

To ship: GitHub → **Releases → Draft a new release** → pick the tag for the commit you want users to land on → write the notes (these become the changelog shown in the bot) → **Publish**. The bot picks it up on its next check and applies up to that tag (never to bare `main` HEAD).

**Pre-release checklist** (before publishing a Release):

1. **State-schema migration check — load-bearing.** Did anything in this batch change the on-disk shape of `.state.json`, `.sessions.json`, or `.mirrors.json` (renamed/removed/restructured keys)?
   - **Yes** → register a migration in `config._STATE_MIGRATIONS` and bump `config.SCHEMA_VERSION`. Without it, an updated bot silently misreads an existing user's data. Add a revert-and-fail test (see `tests/test_migrations.py`).
   - **No** → nothing to do; the existing shape carries forward.
2. Removed/renamed an `.env` setting or a command? Say so plainly in the release notes — that's what users read before tapping Update (there is no separate "breaking" flag).
3. New dependency? Confirm it's in `requirements.txt` (update.sh runs `pip install -r` on apply).

## Project conventions worth knowing

These are not enforced by CI but make review faster:

- **Docs are in English by default.** Russian only when explicitly requested.
- **Telegram constraints**: messages truncate at 4096 chars; inline buttons truncate at ~25 chars on mobile; markdown tables don't render — use lists.
- **Single-user bot.** `OWNER_ID` is the only authorized user. No multi-tenant code.
- **`_CLAUDE_BIN`**: subprocess calls to `claude` must use the constant in `sessions.py`, not a bare `'claude'` string (PATH issue under systemd).
- See `CLAUDE.md` for the full list.

## Reporting bugs / requesting features

Use the issue templates. Include the bot version (`python3 version.py`) and Claude Code version (`claude --version`) for bugs.
