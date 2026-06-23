#!/usr/bin/env bash
# Compute the release version string from the VERSION file + git history.
#
# Stdout: the version with no leading "v" (e.g. "1.0.0" or "1.3.6").
# Used by .github/workflows/release.yml; unit-tested in tests/test_versioning.py.
#
# Scheme:
#   MAJOR — the part of VERSION before the first dot (bare "0" or "1" works too).
#   A dotted VERSION whose tag does NOT yet exist is cut verbatim as the
#   milestone release (so VERSION="1.0.0" -> first tag "v1.0.0"). Once that tag
#   exists, every later merge auto-counts:
#   MINOR — merges since the commit that last changed VERSION (the baseline),
#           so the merge right after the bump is .1, the next .2, ...
#   PATCH — commits in the merged PR, passed in as $1 (CI reads it from the
#           GitHub API); defaults to 0.
#
# Run with the repo as the working directory.
set -euo pipefail

patch_in="${1:-0}"

raw=$(cat VERSION 2>/dev/null | tr -d '[:space:]')
major="${raw%%.*}"
major="${major:-0}"

# Dotted milestone not tagged yet -> emit it verbatim, ignore the auto-count.
if [[ "$raw" == *.* ]] && ! git rev-parse -q --verify "refs/tags/v$raw" >/dev/null 2>&1; then
    echo "$raw"
    exit 0
fi

# MINOR = merges since the commit that last touched VERSION.
baseline=$(git log -1 --format=%H -- VERSION 2>/dev/null || true)
if [ -n "$baseline" ]; then
    minor=$(git rev-list --count "${baseline}..HEAD")
else
    minor=$(git rev-list --count HEAD)
fi

echo "${major}.${minor}.${patch_in}"
