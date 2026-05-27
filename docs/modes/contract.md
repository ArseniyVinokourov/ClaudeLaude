# Mode contract

The universal rules every mode obeys, the template every mode fills, and
the verification protocol every probe runs under. This file does **not**
describe specific modes — those live in their own files in this folder
(see `README.md` for the catalog).

When this contract and a mode file disagree, the contract wins. When the
contract and the implementation in `sessions.py:MODE_PRESETS` disagree,
the contract wins. The contract is the source of truth.

A mode is not "an `--append-system-prompt` blob". A mode is a complete,
testable behavioral contract. Adding a new mode without filling every
section of the template below is not allowed.

---

## Universal contract

Every mode obeys these rules. Mode-specific contracts add to these — they
never weaken them.

### No semantic-empty content

Every clause in the reply must carry information the user did not already
have. If a clause can be deleted without damaging the user's understanding
of the answer, it must be deleted. This is independent of reply length —
a long answer is fine if every part of it is load-bearing; the rule is
about density, not brevity. (Brevity-as-envelope is a separate, mode-
specific contract that some modes adopt.)

The following content is forbidden in every mode:

- **Preamble / acknowledgement** of the question. "Great question",
  "Sure", "Of course", "Let me explain", "Конечно", "Хороший вопрос".
- **Recap or paraphrase** of what the user just asked.
- **Trailing summary / tl;dr** restating what was just said.
- **Throat-clearing transitions** ("Now, let's look at…", "Moving on,…",
  "Так вот,…", "Итак,…").
- **Apologies for length, format, or inability** ("Sorry for the long
  answer", "I hope this helps", "Hopefully that's clear").
- **Hedges on verifiable facts** — "probably", "should work", "I think"
  — when the model has read the relevant code or the claim is otherwise
  checkable. Hedges are allowed only when the model genuinely cannot
  verify and is signalling uncertainty as information.
- **Sycophancy** — agreeing with the user's premise without grounds,
  praising the question, complimenting the framing.

### Tone

Direct, factual. No emoji unless the user requested them. No filler
emphasis ("really important", "absolutely critical") unless the
intensifier is itself the information.

### Faithfulness

The reply must reflect what the model actually did. If the model didn't
verify a claim, the claim is marked as unverified. If the model used a
tool, the result of the tool drives the answer (no asserting from memory
when a tool returned different output).

---

## Mode template

Every mode file describes its mode using exactly these sections, in this
order. Empty sections are spelled out as "N/A — <reason>", never silently
omitted.

### 1. Identity
- **Slug** — the string passed to `/mode <slug>`.
- **One-liner** — the elevator pitch. Shown in `/mode` catalog.
- **When to pick** — the user-facing rationale. What problem this mode
  solves that the others don't.

### 2. Semantic scope
The mode-specific definition of what content carries meaning.

- **In-scope content** — what the user is paying for. Including this
  content is the mode's job; omitting it is a fail.
- **Out-of-scope content** — content that, for THIS mode specifically,
  is filler (in addition to the universal blacklist).

### 3. Operational contract
What happens behind the scenes — CLI flags and runtime conditions,
reproducible from `ps --ppid <bot-pid> -o args=`.

- **`--permission-mode`** — value passed to claude.
- **`--model`** — `None` unless the mode pins a specific model.
- **`--effort`** — `None` unless the mode pins effort.
- **`--max-budget-usd`** — `None` unless the mode pins a budget cap.
- **Tools allowed** — implicit (everything claude can do) unless restricted
  by `permission_mode`.
- **Side effects** — any artifact created on the filesystem outside the
  conversation (e.g., plan mode writes a plan file).

### 4. Hard "must not" rules
Failures of these are blocking regardless of how good the rest of the
reply is. Each rule is a single observable property of the reply or the
filesystem.

### 5. Implementation refs
- `MODE_PRESETS` slot in `sessions.py` (line range).
- Full text of the `style` string (the `--append-system-prompt` payload).
- Help-string label in `bot.py`.
- Any output-style file if applicable.

### 6. Acceptance probes
The test set. Each probe is a tuple:

| Field      | Meaning                                                       |
|------------|---------------------------------------------------------------|
| `id`       | Stable identifier (`terse-q1`, `plan-write-1`, …).            |
| `prompt`   | Exact user message to send.                                   |
| `verify`   | One of: `llm-judge` / `fs-check` / `ps-check`.                |
| `rubric`   | For `llm-judge`: bullet list of pass/fail criteria the judge applies. For `fs-check` / `ps-check`: explicit assertion. |
| `severity` | `must` (failure blocks the mode) / `should` (warning).        |

Probes listed in each mode file are mirrored 1:1 in
`temp/mode_spec_test.py`. Adding a probe in code without an entry in the
relevant mode file is a spec violation.

---

## LLM-judge protocol

The judge is not allowed to be vague. Every `llm-judge` probe runs the
judge under the following protocol — no exceptions.

### 1. Judge instance

- Fresh, independent Claude instance per probe. The judge has no
  conversation history, no memory of previous probes, no access to the
  test code, only the prompt described below.
- Model: Opus (highest fidelity). Effort: low (favors determinism over
  exploration).
- Prompt template stored at `temp/mode_judge_prompt.md`, byte-identical
  across all probes. Only the rubric and the reply-under-test are
  substituted in. No per-probe prompt drift.

### 2. Pre-filters before the judge

Cheap deterministic checks run before invoking the judge. If a pre-filter
catches a universal-blacklist violation, that category is `fail`
deterministically and the judge is **not** asked about it. The judge then
only adjudicates the mode-specific rubric.

Pre-filters run for every probe:

- **Preamble**: reply does not begin (after optional leading whitespace)
  with any of: `Great`, `Sure`, `Of course`, `Absolutely`, `Certainly`,
  `Let me`, `Конечно`, `Хороший вопрос`, `Безусловно`, `Разумеется`.
- **Hedge keywords**: reply contains none of `probably`, `should work`,
  `I think`, `maybe`, `вероятно`, `наверное`, `кажется`, `должно
  работать`, outside of fenced code blocks. (`burn` mode raises this to
  `must`-blocking; other modes treat it as a universal-filler check.)
- **Recap / trailing summary markers**: reply contains none of `In short`,
  `In summary`, `TL;DR`, `To summarize`, `Итого:`, `Подытоживая`,
  `Кратко:`.
- **Apology patterns**: reply contains none of `Sorry for`, `I apologize`,
  `I hope this helps`, `Извините за`, `Надеюсь, это помог`.

Pre-filter regexes live at `temp/mode_judge_prefilters.py` and are
versioned with this document.

### 3. Judge input

The judge is given:

1. The universal contract from §"Universal contract" (verbatim, quoted).
2. The probe's mode-specific rubric as a numbered list of yes/no
   questions. Each item begins `cN:` where `N` is the index.
3. The reply under test, in a clearly delimited block.

The judge is told: answer each `cN` independently. Quote the reply when
you say `fail`. Do not invent criteria not in the list.

### 4. Required output

The judge must return strict JSON matching this schema:

```json
{
  "criteria": [
    {
      "id": "c1",
      "verdict": "pass" | "fail",
      "evidence_quote": "<verbatim substring from the reply, or null>",
      "reason": "<short explanation, or null>"
    }
  ]
}
```

Rules:

- Exactly one entry per criterion in the rubric. Extra or missing entries
  → response invalid → retry once → mark `judge_invalid` if still wrong.
- **Presence-of-bad-content fail** (criterion forbids X, X is present):
  `evidence_quote` MUST be a verbatim substring of the reply showing X.
  `reason` may be null.
- **Absence-of-required-content fail** (criterion requires X, X is
  missing): `evidence_quote` may be null because there is no offending
  text to quote; `reason` MUST be non-null and concretely state what is
  missing.
- `pass` verdict: both `evidence_quote` and `reason` may be null.
- A `fail` with both `evidence_quote: null` AND `reason: null` is
  rejected as invalid.
- A non-null `evidence_quote` that is not a verbatim substring of the
  reply is rejected as invalid.
- The judge does **not** emit an overall verdict. Aggregation is
  mechanical (harness-side): overall pass iff every criterion AND every
  pre-filter category is `pass`.

### 5. Self-consistency

Every `llm-judge` probe runs the judge twice, with independent instances.

- If both runs agree on every criterion → final verdict is that agreement.
- If they disagree on any criterion → probe is marked
  `judge_inconsistent`. The harness records both verdicts and the
  divergence; the probe requires human review and does **not** count as
  pass.

Two runs over twenty-odd probes is cheap; mood-dependent judge verdicts
are the failure mode this guards against.

### 6. Failure modes and their meaning

- `pass` — probe passes; no human action needed.
- `fail` — probe fails with a specific criterion + evidence quote; the
  mode does not meet spec; fix the implementation or revise the
  contract.
- `judge_invalid` — judge could not produce a parseable response after
  one retry; needs human review of the judge prompt and the reply.
- `judge_inconsistent` — two judge runs disagreed; needs human review
  of the rubric (likely ambiguous).
- `judge_failed` — Claude API errored out; retry the probe.

`judge_invalid` / `judge_inconsistent` are diagnostic states, not pass.
Treating them as pass defeats the purpose.

---

## Meta-rules for adding a new mode

1. Create a new file `docs/modes/<slug>.md` and fill all six template
   sections. Empty sections must be `N/A — <reason>`, never silently
   omitted.
2. Add the slug + one-liner to `docs/modes/README.md` (the catalog).
3. Add the preset to `MODE_PRESETS` in `sessions.py`. The `style` string
   in code must be byte-identical to the one quoted in §5 of the new
   mode file.
4. Add probes to `temp/mode_spec_test.py` mirroring §6 of the new mode
   file, 1:1.
5. Run the harness. The new mode must pass all `must`-severity probes.
   `should`-severity failures are recorded but not blocking.
6. Open a PR. The PR description states which acceptance probes the new
   mode introduces and the pass/fail line for each.

A mode that cannot be observed from outside (no probes possible) is not a
mode — it's a comment. Reject it.
