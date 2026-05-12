#!/usr/bin/env bash
# Validates a PR against the ClaudeLaude contributor flow rules.
# Writes friendly Markdown for any violations to $ERRORS_FILE.
# Always exits 0 — the caller checks $ERRORS_FILE size to decide pass/fail.
#
# Required env: BRANCH TITLE BODY AUTHOR PR OWNER_LOGIN ERRORS_FILE
# Required tools on PATH: python3, ruff, gh

set -uo pipefail

: "${BRANCH:?missing}"
: "${TITLE:?missing}"
: "${BODY:?missing}"
: "${AUTHOR:?missing}"
: "${PR:?missing}"
: "${OWNER_LOGIN:?missing}"
: "${ERRORS_FILE:?missing}"

: > "$ERRORS_FILE"

append() {
  printf '\n%s\n' "$1" >> "$ERRORS_FILE"
}

PREFIX_RE='^(feat|fix|ui|chore|ops|security|docs|refactor)'

# 1. Branch name pattern
if [[ ! "$BRANCH" =~ ${PREFIX_RE}/[a-z0-9._-]+$ ]]; then
  append "❌ **Branch name** \`$BRANCH\` doesn't match the required pattern.

   Allowed prefixes: \`feat/\` \`fix/\` \`ui/\` \`chore/\` \`ops/\` \`security/\` \`docs/\` \`refactor/\`
   Format: \`<prefix>/<short-slug>\` — slug uses lowercase letters, digits, \`.\`, \`_\`, \`-\`.
   Example: \`feat/sticker-reactions\`

   To fix: rename the branch in your fork (\`git branch -m <new-name>\`), push it, and re-open this PR from the renamed branch."
fi

# 2. PR title pattern (Conventional Commits)
if [[ ! "$TITLE" =~ ${PREFIX_RE}:\ .+ ]]; then
  append "❌ **PR title** must follow Conventional Commits: \`<type>: <description>\`.

   Got: \`$TITLE\`
   Allowed types: \`feat\` \`fix\` \`ui\` \`chore\` \`ops\` \`security\` \`docs\` \`refactor\`
   Example: \`fix: race condition in worker thread\`

   To fix: edit the PR title above (the small pencil icon next to it)."
fi

# 3. PR description has a non-empty `## Summary` section
summary=$(printf '%s\n' "$BODY" | awk '
  /^## Summary[[:space:]]*$/ { in_section = 1; next }
  /^## / && in_section { exit }
  in_section { print }
')
stripped=$(printf '%s' "$summary" | sed 's/<!--[^>]*-->//g' | tr -d '[:space:]')
if [ -z "$stripped" ]; then
  append "❌ **PR description missing a non-empty \`## Summary\` section.**

   The PR template (auto-loaded when you open a PR) has a \`## Summary\` heading.
   Fill it in with one or two lines on **what** changes and **why**.
   Edit the PR description above to add it."
fi

# 4. Owner-only files (skip for the maintainer)
if [ "$AUTHOR" != "$OWNER_LOGIN" ]; then
  changed=$(gh pr diff "$PR" --name-only 2>/dev/null || true)
  forbidden=$(printf '%s\n' "$changed" | grep -E '^(VERSION$|version\.py$|\.github/workflows/|\.github/scripts/|\.github/CODEOWNERS$|CODEOWNERS$)' || true)
  if [ -n "$forbidden" ]; then
    files_list=$(printf '%s\n' "$forbidden" | sed 's/^/   - `/' | sed 's/$/`/')
    append "❌ **Owner-only files modified.**

   These files can only be changed by @$OWNER_LOGIN:
$files_list

   Why: \`VERSION\` and \`version.py\` are managed by the maintainer.
   \`.github/workflows/\`, \`.github/scripts/\`, and \`CODEOWNERS\` are
   protected from supply-chain tampering.

   To fix: revert the changes to these files, or open an issue first to discuss."
  fi
fi

# 5. Python syntax
py_err_file="${RUNNER_TEMP:-/tmp}/py_err.txt"
if ! python3 -m py_compile *.py 2>"$py_err_file"; then
  append "❌ **Python syntax error.**

\`\`\`
$(cat "$py_err_file")
\`\`\`

   Run locally: \`python3 -m py_compile *.py\`"
fi

# 6. Bash syntax
bash_errs=""
for f in *.sh; do
  [ -f "$f" ] || continue
  if ! err=$(bash -n "$f" 2>&1); then
    bash_errs+="${f}: ${err}"$'\n'
  fi
done
if [ -n "$bash_errs" ]; then
  append "❌ **Bash syntax error.**

\`\`\`
$bash_errs
\`\`\`

   Run locally: \`for f in *.sh; do bash -n \"\$f\"; done\`"
fi

# 7. Ruff lint
if ! ruff_out=$(ruff check . 2>&1); then
  append "❌ **Ruff lint failed.**

\`\`\`
$ruff_out
\`\`\`

   Run locally: \`pip install ruff==0.6.9 && ruff check .\`
   Many issues auto-fix: \`ruff check --fix .\`"
fi

exit 0
