#!/usr/bin/env bash
# check_kernel_invariant.sh — the load-bearing architectural guardrail (plan W5).
#
# The kernel (packages/kernel/) is domain-agnostic. It must NEVER name a
# semantic domain noun — those live only in a vertical package. This grep gate
# fails CI on any leak, so the seam can't silently erode over time.
#
# Structural/uninteresting hits are excluded (comments are still checked: a
# domain noun in a kernel comment is a design smell). Tune EXCLUDE as real
# false-positives appear, but never add a domain noun to the allowlist.
set -uo pipefail

KERNEL_DIR="${1:-packages/kernel/roster_kernel}"

# Semantic domain nouns that must not appear in the kernel. Word-boundaried.
# Roster is a deep-tech research vertical: these tech/finance nouns belong in the
# vertical package, never in the domain-agnostic kernel. (Retuned from the
# original utility-regulatory banlist inherited via noesis.)
PATTERN='\bstartup\b|\bvaluation\b|cap[_ ]?table|\bticker\b|\bcik\b|\bedgar\b|\bpatent(s|ed)?\b|\barxiv\b|crunchbase|pitchbook|funding[_ ]round|term[_ ]sheet|\bipo\b|\bcompetitor\b|\bmoat\b'

# Files/paths that are allowed to mention them (none in kernel by design).
# Vertical packages and docs are out of scope for this gate.
# Exclude the invariant checker itself (it necessarily contains the pattern),
# and honor an inline opt-out marker for the rare legitimate mention.
hits="$(grep -rniE "$PATTERN" "$KERNEL_DIR" \
        --include='*.py' \
        --exclude='test_kernel_invariant.py' \
        2>/dev/null | grep -v 'kernel-invariant: allow' || true)"

if [ -n "$hits" ]; then
  echo "✗ KERNEL INVARIANT VIOLATION — domain noun(s) leaked into the kernel:"
  echo "$hits"
  echo
  echo "Move the domain vocabulary/policy into a vertical package and expose it"
  echo "through the VerticalManifest contract. The kernel must stay domain-agnostic."
  exit 1
fi

echo "✓ kernel invariant holds — no domain nouns in $KERNEL_DIR"
exit 0
