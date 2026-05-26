# default

Universal rules and section structure: see `contract.md`.

## 1. Identity
- Slug: `default`
- One-liner: normal behavior — no style overlay.
- When to pick: the baseline. Picking `default` is the same as never having
  called `/mode`.

## 2. Semantic scope
- In-scope: a balanced answer chosen by the model for the question. Plain
  prose, lists/headers only when they carry distinct items.
- Out-of-scope: nothing additional beyond the universal blacklist in
  `contract.md`.

## 3. Operational contract
- `--permission-mode`: `auto`.
- `--model` / `--effort` / `--max-budget-usd`: not set.
- Tools allowed: all.
- Side effects: none specific to this mode.

## 4. Hard "must not" rules
- Refusing a routine operation that the `auto` permission policy would
  normally allow.

## 5. Implementation refs
- `sessions.py:26-30` (`MODE_PRESETS["default"]`).
- `style`: empty.
- Help label: `normal behavior`.

## 6. Acceptance probes
- `default-q1` — prompt: "Что такое hash table?" — verify: `llm-judge`;
  rubric: the response answers the question, contains no universal-
  blacklist content; severity: must.
- `default-write-1` — prompt: "Создай файл `/tmp/mode_default_probe.txt`
  с текстом `ok`" — verify: `fs-check`; assertion: file exists and
  contains `ok` after the turn; severity: must.
