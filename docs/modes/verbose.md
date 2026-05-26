# verbose

Universal rules and section structure: see `contract.md`.

## 1. Identity
- Slug: `verbose`
- One-liner: full reasoning, context, tradeoffs.
- When to pick: design questions, debugging, anything where seeing the
  model's reasoning matters as much as the conclusion.

## 2. Semantic scope
- In-scope:
  - the direct answer;
  - the reasoning that led to it ("because…");
  - relevant context the user needs to evaluate the answer;
  - alternatives considered and tradeoffs between them;
  - citations (file:line) when discussing real code in the workspace.
- Out-of-scope: nothing additional beyond the universal blacklist in
  `contract.md`. Verbose may be long — but every clause still has to
  carry meaning.

## 3. Operational contract
- `--permission-mode`: `auto`.
- `--model` / `--effort` / `--max-budget-usd`: not set.
- Tools allowed: all.
- Side effects: none.

## 4. Hard "must not" rules
- Conclusion stated without supporting reasoning when the question is
  non-trivial.
- Reply is structurally identical to a terse reply (no expansion into
  the in-scope content above) on a non-trivial question.

## 5. Implementation refs
- `sessions.py:46-54` (`MODE_PRESETS["verbose"]`).
- `style`:
  > Response style: verbose. Explain context, alternatives, and tradeoffs.
  > Walk through reasoning before conclusions. Cite specific files and
  > lines when relevant.
- Help label: `full reasoning and context`.

## 6. Acceptance probes
- `verbose-q1` — prompt: "Что такое hash table?" — verify: `llm-judge`;
  rubric:
  - reply explains what a hash table is AND walks through the mechanism
    (hash function, bucket, collision resolution);
  - reply mentions at least one tradeoff (e.g., O(1) average vs. O(n)
    worst case; chaining vs. open addressing);
  - every clause carries information (no padding paragraphs that
    restate the same idea);
  severity: must.
- `verbose-q2` — prompt: "When should I use a hash table vs. a balanced
  tree?" — verify: `llm-judge`; rubric:
  - reply presents at least two distinct tradeoff axes (e.g., complexity,
    ordering, cache behavior, worst-case);
  - reply gives a clear recommendation per axis, not just a list of
    considerations;
  severity: must.
- `verbose-q3` — prompt: "Why does Python's GIL exist?" — verify:
  `llm-judge`; rubric:
  - reply covers both the historical motivation and the current mechanism;
  - reply mentions at least one alternative that was considered or
    proposed (e.g., per-object locks, sub-interpreters, no-GIL builds);
  severity: should.
