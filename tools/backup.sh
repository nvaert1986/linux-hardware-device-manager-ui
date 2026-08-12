#!/usr/bin/env bash
#
# Back the checkout up to a timestamped tarball beside it, excluding whatever .gitignore excludes.
#
#     tools/backup.sh              # -> ../hardware-ui-backup-YYYYmmdd-HHMM.tar.gz
#     tools/backup.sh /mnt/stick   # somewhere else
#
# **One list, two consumers.** The exclusions come from .gitignore rather than being retyped here,
# because that is exactly how a backup ends up carrying a stale .ruff_cache that `git status` would
# never have shown. Add a pattern once and both honour it.
#
# The archive is verified before this exits: it is extracted to a temporary directory and the test
# suite runs from the restored copy. An archive that has not been opened is a guess, and a backup
# nobody has restored is the kind that turns out to be empty on the day it matters.

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
name="$(basename "$root")"
destination="${1:-$(dirname "$root")}"
archive="$destination/$name-backup-$(date +%Y%m%d-%H%M).tar.gz"

ignore="$root/.gitignore"
[[ -f $ignore ]] || { echo "no .gitignore at $ignore" >&2; exit 1; }

# .gitignore -> tar --exclude patterns. Comments and blanks go; a leading slash (anchored to the
# repository root) and a trailing slash (directories only) are dropped, because tar matches on the
# path fragment either way. Negations are refused rather than silently ignored -- tar has no
# equivalent, and quietly dropping one would exclude a file the author meant to keep.
mapfile -t patterns < <(
    grep -vE '^\s*(#|$)' "$ignore" | sed -e 's#^/##' -e 's#/$##'
)
for pattern in "${patterns[@]}"; do
    [[ $pattern == !* ]] && { echo "backup.sh cannot honour negation: $pattern" >&2; exit 1; }
done

excludes=()
for pattern in "${patterns[@]}"; do
    excludes+=(--exclude="$pattern")
done

echo "archiving $name -> $archive"
tar czf "$archive" -C "$(dirname "$root")" "${excludes[@]}" "$name"

printf '  %s bytes, %s files\n' \
    "$(stat -c%s "$archive")" "$(tar tzf "$archive" | wc -l)"

# Verify by restoring. A tarball that lists correctly can still be missing something the tests
# need, and that is only discoverable by running them.
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
tar xzf "$archive" -C "$scratch"
if (cd "$scratch/$name" && python3 -m pytest -q >/dev/null 2>&1); then
    echo "  verified: the restored copy passes its own test suite"
else
    echo "  WARNING: the restored copy does not pass its tests" >&2
    exit 1
fi
