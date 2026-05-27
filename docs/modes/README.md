# Modes — catalog

This folder is the source of truth for the bot's `/mode` system. The
universal contract every mode obeys is in `contract.md`. Each mode has
its own file.

When adding a new mode, follow the steps in `contract.md` → "Meta-rules
for adding a new mode".

## Modes

| Slug       | One-liner                                             | File          |
|------------|-------------------------------------------------------|---------------|
| `default`  | normal behavior — no style overlay                    | [default.md](default.md)   |
| `terse`    | only the direct answer; nothing else                  | [terse.md](terse.md)       |
| `verbose`  | full reasoning, context, tradeoffs                    | [verbose.md](verbose.md)   |
| `beginner` | explains jargon as it goes; friendly to non-experts   | [beginner.md](beginner.md) |
| `plan`     | read-only research mode — investigate, propose, never execute | [plan.md](plan.md) |
| `burn`     | max effort, max budget, parallel agents               | [burn.md](burn.md)         |
