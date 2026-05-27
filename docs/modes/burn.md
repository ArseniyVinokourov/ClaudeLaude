# burn

Universal rules and section structure: see `contract.md`.

## 1. Identity
- Slug: `burn`
- One-liner: max effort, max budget, parallel agents — for the hard ones.
- When to pick: the task is genuinely hard, the answer needs to be right,
  cost is not the bottleneck.

## 2. Semantic scope
- In-scope:
  - the direct answer;
  - the reasoning;
  - citations for every verifiable claim (file:line, captured command
    output, tool result);
  - aggressive use of the `Agent` tool for research and independent
    verification of non-trivial claims, including parallel `Agent`
    calls when sub-questions are independent.
- Out-of-scope: hedges on verifiable facts (see Hard rules below).

## 3. Operational contract
- `--permission-mode`: `auto`.
- `--model`: `claude-opus-4-7[1m]`.
- `--effort`: `max`.
- `--max-budget-usd`: `5.0`.
- Tools allowed: all, with the `Agent` tool explicitly encouraged.
- Side effects: none specific.

## 4. Hard "must not" rules
- A claim about something the model has verified is hedged with
  "probably", "should work", "I think", "maybe", "вероятно", "наверное",
  "должно работать", "кажется".
- A non-trivial research task is answered without invoking the `Agent`
  tool.
- A verifiable claim is asserted without a citation (file:line, command
  output, or tool result).

## 5. Implementation refs
- `sessions.py:93-107` (`MODE_PRESETS["burn"]`).
- `style`:
  > Burn mode: do not economize tokens or time. Push through to a complete,
  > verified result — don't stop at 'probably' or 'should work'. Use the
  > Agent tool aggressively, including parallel Agent calls, for research
  > and independent verification of any non-trivial claim. Cite verifica-
  > tions (file:line, command output) rather than asserting from memory.
- Help label: `Opus 1M + max effort + parallel agents`.

## 6. Acceptance probes
- `burn-cli-1` — prompt: any (e.g., "ok"), and capture the claude
  subprocess argv via `ps --ppid <bot-pid> -o args=` while the turn is
  live — verify: `ps-check`; assertion: argv contains `--model
  claude-opus-4-7[1m]`, `--effort max`, `--max-budget-usd 5.0`;
  severity: must.
- `burn-research-1` — prompt: "Investigate how `_resolve_hook_session`
  picks a session when only `cwd` is given. Cite file:line for the
  decision points." — verify: `llm-judge`; rubric:
  - reply names at least one `sessions.py:` or `bot.py:` line-numbered
    citation;
  - reply describes a real decision path that can be confirmed against
    the cited lines;
  severity: must.
- `burn-no-hedge-1` — same prompt as `burn-research-1` — verify:
  `llm-judge`; rubric:
  - reply contains zero hedges on the verifiable claims about the cited
    code: "probably", "should work", "I think", "maybe", "вероятно",
    "наверное", "должно работать", "кажется";
  severity: must.
