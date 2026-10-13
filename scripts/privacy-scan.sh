#!/usr/bin/env bash
#
# Privacy deny-list scan.
#
# This repository is public: it ships a product, never its author's data.
#
#   ./scripts/privacy-scan.sh            scan tracked files in the working tree
#   ./scripts/privacy-scan.sh --history  scan every blob in the entire git history
#   ./scripts/privacy-scan.sh --staged   scan staged content (used by a pre-commit hook)
#
# Options, combinable with any mode:
#   --redact-hits    report file and line number only, never the matched content
#   --require-local  fail if the gitignored local supplement is absent
#
# Exits non-zero on any match.

set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

DENYLIST=".github/privacy-denylist.txt"
LOCAL_DENYLIST=".github/privacy-denylist.local.txt"   # gitignored; the author's vocabulary
IGNOREFILE=".privacyignore"

MODE=""
REDACT_HITS=0
REQUIRE_LOCAL=0
for arg in "$@"; do
  case "$arg" in
    --worktree|--staged|--history) MODE="$arg" ;;
    --redact-hits)   REDACT_HITS=1 ;;
    --require-local) REQUIRE_LOCAL=1 ;;
    *) echo "privacy-scan: unknown argument '$arg'" >&2; exit 2 ;;
  esac
done
MODE="${MODE:---worktree}"

[[ -f "$DENYLIST" ]] || { echo "privacy-scan: missing $DENYLIST" >&2; exit 2; }

# Build the pattern list: public deny-list, plus the local supplement when present.
#
# The supplement is gitignored because it holds the author's own vocabulary — and
# gitignored meant CI could not see it, so the class it exists for passed the public
# patterns and only an operator's machine could catch it. It now reaches CI from a
# secret (.github/workflows/privacy.yml), so the job runs the same patterns without
# publishing them. `--require-local` is how a job says it must not run blind: a partial
# scan that reports `clean` is worse than a red one, because it looks like a pass.
PATTERNS="$(mktemp)"; trap 'rm -f "$PATTERNS"' EXIT
grep -vE '^\s*(#|$)' "$DENYLIST" > "$PATTERNS"
if [[ -f "$LOCAL_DENYLIST" ]]; then
  grep -vE '^\s*(#|$)' "$LOCAL_DENYLIST" >> "$PATTERNS"
  echo "privacy-scan: including local supplement ($(wc -l < "$LOCAL_DENYLIST") lines)"
elif (( REQUIRE_LOCAL )); then
  echo "privacy-scan: FAILED — $LOCAL_DENYLIST is absent and --require-local was given." >&2
  echo "privacy-scan: without it this scan cannot see the author's own vocabulary." >&2
  exit 2
else
  echo "privacy-scan: WARNING — no local supplement; public patterns only, so the" >&2
  echo "privacy-scan: author's own vocabulary is NOT covered by this run." >&2
fi
echo "privacy-scan: $(wc -l < "$PATTERNS") patterns, mode ${MODE#--}"

ignored() {
  [[ -f "$IGNOREFILE" ]] || return 1
  while IFS= read -r entry; do
    [[ -z "$entry" || "$entry" == \#* ]] && continue
    [[ "$1" == "$entry"* || "$1" == *"/$entry"* ]] && return 0
  done < "$IGNOREFILE"
  return 1
}

hits=0

scan_stream() {   # scan_stream <label> ; content on stdin
  local label="$1" out
  # -a: a vendored binary (a font, a minified bundle) must be scanned as text too,
  # rather than skipped as "binary file matches" and silently let through.
  if out="$(grep -naEi -f "$PATTERNS" 2>/dev/null)"; then
    echo "  ✗ $label"
    if (( REDACT_HITS )); then
      # Line numbers only. On a public run — CI, or a pull request's checks — the
      # matched content IS the leak, so a gate that helpfully prints it publishes
      # the thing it exists to prevent, into a log that is as permanent as git.
      echo "$out" | head -3 | cut -d: -f1 | sed 's/^/      line /'
    else
      # Local run: at most 3 matches per file, each truncated, since the whole
      # point is to avoid reprinting the sensitive content we just found.
      echo "$out" | head -3 | cut -c1-120 | sed 's/^/      /'
    fi
    hits=$((hits + 1))
  fi
}

case "$MODE" in
  --worktree)
    while IFS= read -r f; do
      ignored "$f" && continue
      [[ -f "$f" ]] || continue
      scan_stream "$f" < "$f"
    done < <(git ls-files)
    ;;

  --staged)
    while IFS= read -r f; do
      ignored "$f" && continue
      scan_stream "$f (staged)" < <(git show ":$f" 2>/dev/null || true)
    done < <(git diff --cached --name-only --diff-filter=ACM)
    ;;

  --history)
    # Every blob ever committed. Public history is permanent, so this is the
    # check that actually matters before the repository is made public.
    echo "privacy-scan: walking full history, this may take a moment"
    while read -r _ sha path; do
      [[ -z "${path:-}" ]] && continue
      ignored "$path" && continue
      scan_stream "$path @ ${sha:0:8}" < <(git cat-file -p "$sha" 2>/dev/null || true)
    done < <(git rev-list --objects --all \
             | git cat-file --batch-check='%(objecttype) %(objectname) %(rest)' \
             | awk '$1 == "blob" && $3 != ""')
    ;;

  *)
    echo "privacy-scan: unknown mode '$MODE'" >&2; exit 2 ;;
esac

if (( hits > 0 )); then
  cat >&2 <<EOF

privacy-scan: FAILED — $hits file(s) matched the deny-list.

This repository is public. Do not "just delete the file and commit": if it was
already pushed, the content stays in history and in every clone.

If a match is a false positive, narrow the pattern in $DENYLIST
or add a specific path to $IGNOREFILE — every ignore entry is a hole
in the control, so keep it minimal.
EOF
  exit 1
fi

echo "privacy-scan: clean"
