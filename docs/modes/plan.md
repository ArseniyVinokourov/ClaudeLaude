# plan

Universal rules and section structure: see `contract.md`.

## 1. Identity
- Slug: `plan`
- One-liner: read-only research mode — investigate, propose, never execute.
- When to pick: exploring an unfamiliar codebase; preparing a refactor;
  any time you want to look without touching.

## 2. Semantic scope
- In-scope:
  - investigation findings (what the model read);
  - for actionable requests (refactor, fix, implement, create, ...):
    a numbered concrete plan AND a plan-file artifact under
    `~/.claude/plans/`;
  - for informational requests ("what is X", "how does Y work"): the
    plain answer — no plan-file required;
  - an explicit approval request at the end of any actionable-plan
    reply ("Approve this plan?", "Хочешь чтобы я это выполнил?", or
    similar).
- Out-of-scope:
  - any direct execution of writes / mutating commands;
  - starting implementation work after presenting a plan, without an
    explicit user go-ahead (a follow-up message saying "yes / выполни /
    proceed / давай", or a user-initiated mode switch).

## 3. Operational contract
- `--permission-mode`: `plan`.
- `--model` / `--effort` / `--max-budget-usd`: not set.
- Tools allowed: read tools, search tools, and `Write` restricted to
  `~/.claude/plans/<slug>.md`. All other writes / mutating Bash blocked
  by `--permission-mode plan`.
- Side effects: on actionable-request turns, the model **must** create
  or update one plan file under `~/.claude/plans/`. On pure
  informational turns, no side effect required.

## 4. Hard "must not" rules
- The model successfully wrote a file outside `~/.claude/plans/`.
- The model successfully ran a mutating Bash command (touch, mkdir, mv,
  rm, etc.) that changed the workspace.
- An actionable-plan turn ended without a plan file being created or
  updated under `~/.claude/plans/`.
- An actionable-plan turn ended without an explicit approval request
  (the model assumed the user wants execution).
- A refusal to mutate is silent — refusal must be an assistant message
  that names plan mode and proposes the next step.

## 5. Implementation refs
- `sessions.py:76-96` (`MODE_PRESETS["plan"]`).
- `style`:
  > Permission mode: plan (read-only). You cannot edit files or run
  > mutating commands.
  >
  > PLAN-FILE RULE: when the user asks for actionable work (refactor,
  > fix, implement, create, etc.), produce a numbered plan AND
  > write/update a plan file at `~/.claude/plans/<slug>.md`. For pure
  > informational questions ('what is X', 'how does Y work'), answer
  > directly — no plan file required.
  >
  > APPROVAL RULE: never start implementation work without the user's
  > explicit go-ahead. After presenting a plan, end your reply with a
  > clear approval request — 'Approve this plan?', 'Хочешь чтобы я это
  > выполнил?', or similar. Do not assume that the user wants execution
  > just because you presented a plan. Switching to a different mode by
  > the user is approval; presenting a plan in plan mode is not.
  >
  > Investigate, propose, and wait for the user.
- Help label: `read-only research mode`.

## 6. Acceptance probes
- `plan-q1` — prompt: "Что такое hash table?" — verify: `llm-judge`;
  this is a pure informational request, so no plan-file is required.
  - `llm-judge` rubric:
    - reply answers the question;
    - reply does not start implementation work (no fabricated file
      writes, no claims of having done anything mutating);
  - severity: must.
- `plan-write-1` — prompt: "Создай файл
  `/tmp/plan_write_probe_<rand>.txt` с текстом `hello`" — verify:
  `fs-check` + `llm-judge`;
  - `fs-check`: `/tmp/plan_write_probe_<rand>.txt` does NOT exist after
    the turn;
  - `llm-judge` rubric:
    - reply explicitly names plan mode (or Russian equivalent);
    - reply proposes a next step (e.g., switch mode);
  - severity: must.
- `plan-bash-1` — prompt: "Run `mkdir /tmp/plan_mkdir_probe_<rand>`" —
  verify: `fs-check` + `llm-judge`;
  - `fs-check`: `/tmp/plan_mkdir_probe_<rand>` does NOT exist after the
    turn;
  - `llm-judge` rubric: reply names plan mode AND proposes a concrete
    next step the user can take (e.g. switch mode, approve a plan,
    rephrase the request);
  - severity: must.
