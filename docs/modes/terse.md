# terse

Universal rules and section structure: see `contract.md`.

## 1. Identity
- Slug: `terse`
- One-liner: only the direct answer; nothing else.
- When to pick: the user knows the topic, wants the answer, not an
  explanation of the answer.

## 2. Semantic scope
- In-scope: the literal answer to the literal question. The minimum
  content that, if removed, would damage the user's ability to use the
  answer.
- Out-of-scope (mode-specific filler):
  - Context the user did not ask for.
  - Alternatives and tradeoffs (unless the question explicitly asks for
    a comparison).
  - Examples beyond the minimum needed to make the answer usable.
  - Multi-section structure — the answer fits in one prose block or one
    short list; no `##` headers, no nested subsections.

## 3. Operational contract
- `--permission-mode`: `auto`.
- `--model` / `--effort` / `--max-budget-usd`: not set.
- Tools allowed: all.
- Side effects: none.

## 4. Hard "must not" rules
- The reply contains a `## ` or `### ` markdown header.
- The reply spans more than one prose block AND more than one list/code
  block — i.e., a multi-section response was produced.
- The reply opens with a universal-blacklist preamble.

## 5. Implementation refs
- `sessions.py:31-45` (`MODE_PRESETS["terse"]`).
- `style`:
  > Response style: terse. Answer ONLY the literal question — nothing more.
  > Do NOT add sections the user did not ask for (no 'Implementation:', no
  > 'Applications:', no 'How it works:' unless the question explicitly
  > requests them). Do NOT use `## ` or `### ` markdown headers anywhere in
  > your reply. Stay in one prose block. If the question is 'what is X',
  > answer in 1-3 sentences without expanding. No preambles, no recap of
  > the question, no trailing summary. Lists only when items are genuinely
  > distinct entities.
- Help label: `1-3 sentence answers`.

## 6. Acceptance probes
- `terse-q1` — prompt: "Что такое hash table?" — verify: `llm-judge`;
  rubric:
  - reply answers what a hash table is in plain prose;
  - reply contains no section headers (`## ` / `### `);
  - reply does not include a sectioned or bullet-listed enumeration of
    alternative implementations, libraries, or trade-offs (parenthetical
    mentions of essential mechanism alternatives like "chaining or open
    addressing" are fine; what is forbidden is a structured catalog the
    user did not ask for);
  - reply does not include a sectioned or bullet-listed catalog of
    examples or use-cases (e.g., a list of language implementations or
    applications);
  - reply does not restate the question;
  severity: must.
- `terse-q2` — prompt: "How do I list files in Linux?" — verify:
  `llm-judge`; rubric:
  - reply gives the direct procedure (the `ls` command);
  - reply does not branch into common flags / alternatives / edge cases
    unless they are essential to the literal question;
  - no headers; no preamble;
  severity: must.
- `terse-q3` — prompt: "Explain async/await in JavaScript." — verify:
  `llm-judge`; rubric:
  - reply explains what async/await does, nothing more;
  - reply does not include extended examples, history, or "vs. callbacks"
    comparisons unless one short snippet is genuinely the answer;
  - no multi-section structure;
  severity: must.
- `terse-q4` — prompt: "Что такое Big O?" — verify: `llm-judge`; rubric:
  - reply opens directly with the definition;
  - no opening phrases like "Great", "Sure", "Конечно", "Хороший",
    "Let me", "Of course";
  severity: must.
