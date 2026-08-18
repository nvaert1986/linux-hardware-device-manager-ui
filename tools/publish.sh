#!/usr/bin/env bash
#
# Mirror the checkout into a clean, publishable copy beside it.
#
#     tools/publish.sh                 # -> ../hardware-ui-prd
#     tools/publish.sh /path/to/dest   # somewhere else
#
# **One list, three consumers.** Like tools/backup.sh, the exclusions come from .gitignore rather
# than being retyped here. Anything that must never be committed must never be published either,
# and the way that guarantee breaks is by keeping a second copy of the list.
#
# What this is *not*: it does not push anywhere, does not create a git repository, and does not
# touch the source tree. It produces a directory. Publishing it is a separate, deliberate act.
#
# The copy is verified before this exits: the test suite runs from the copy, and the copy is
# scanned for the two things that must not leave this machine -- vendor data that is not ours to
# redistribute, and identifiers belonging to whoever ran this.

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
name="$(basename "$root")"
destination="${1:-$(dirname "$root")/$name-prd}"

ignore="$root/.gitignore"
[[ -f $ignore ]] || { echo "no .gitignore at $ignore" >&2; exit 1; }

# .gitignore -> rsync --exclude patterns. Comments and blanks go; a leading slash (anchored to the
# repository root) is dropped because rsync would otherwise anchor it to the transfer root, which
# is the same thing here but stops matching if this is ever pointed at a subdirectory. Trailing
# slashes are kept -- unlike tar, rsync uses them to mean "directories only", which is the intent.
# Negations are refused rather than silently dropped: rsync spells them differently and quietly
# mistranslating one would publish a file the author meant to withhold.
mapfile -t patterns < <(grep -vE '^\s*(#|$)' "$ignore" | sed -e 's#^/##')
excludes=()
for pattern in "${patterns[@]}"; do
    [[ $pattern == !* ]] && { echo "publish.sh cannot honour negation: $pattern" >&2; exit 1; }
    excludes+=(--exclude="$pattern")
done

echo "publishing $name -> $destination"

# --delete so a re-run is a mirror and not an accumulation: a file deleted upstream has to
# disappear here too, or the published copy slowly becomes a superset of the real project.
# --delete-excluded extends that to the excluded patterns, which matters because the verification
# below runs pytest *inside* the copy and leaves __pycache__ behind. Without it, every re-run
# inherits the last run's build droppings and reports a file count that is not the project's.
#
# `protect .git/` is not optional, and the reason is worth spelling out because getting it wrong is
# silent and unrecoverable. The destination is where publishing actually happens: someone runs
# `git init` there, adds a remote, commits. That `.git/` matches --exclude, and --delete-excluded
# deletes what it matches *on the receiving side* -- so without a protect rule, the next publish
# would take the repository's history, its remote and its branches with it. A protect rule exempts
# a path from deletion without transferring it, which is exactly the distinction wanted here: the
# source's own `.git/` is still never copied.
mkdir -p "$destination"
rsync -a --delete --delete-excluded --filter='protect .git/' \
      "${excludes[@]}" --exclude='.git/' "$root"/ "$destination"/

# Counted without `.git/`, which is the repository rather than the project: including it would
# make the published tree appear to grow with every commit made in it.
printf '  %s files, %s\n' \
    "$(find "$destination" -path "$destination/.git" -prune -o -type f -print | wc -l)" \
    "$(du -sh --exclude=.git "$destination" | cut -f1)"

# ---------------------------------------------------------------- what must not have come along
#
# Two categories, checked by pattern rather than by memory.
#
# Vendor data: GN Audio's property catalogue and Poly's setting catalogues are fetched or unpacked
# on the user's own machine, from the user's own installer, precisely because they are not ours to
# ship. They live in the user's data directory, so a copy inside the tree means someone dropped one
# there for testing -- which is exactly the accident .gitignore exists to catch, checked again here
# because a publish is the point of no return.
#
# Identifiers: whoever ran this. A home directory path or an email address in a committed file is
# not a licensing problem, it is a personal one, and it is trivially easy to introduce.
#
# Two exemptions, both narrow. The reserved documentation domains of RFC 2606 (example.com and
# friends) belong to nobody by design, which is the entire reason the RFC set them aside, and a
# test fixture needs *some* address to assert on. And licence, notice and provenance files exist
# precisely to carry other people's names and addresses -- refusing to publish an upstream
# copyright line because it contains an email would be the guard defeating the compliance it is
# meant to protect. Everything else is refused: prose that wants to credit someone can name them
# and point at the notice file, which is where the authoritative copy lives anyway.
strays="$(find "$destination" \( -name properties.json -o -name '*.asar' -o -name '*.msi' \
                                 -o -name DeviceSettings.zip -o -name presets.json \
                                 -o -name device.png \) -print)"
if [[ -n $strays ]]; then
    echo "  REFUSING: vendor data that is not ours to redistribute:" >&2
    printf '%s\n' "$strays" | sed 's/^/    /' >&2
    exit 1
fi

# Two scans, because the two identifiers do not deserve the same exemptions.
#
# A home directory path is refused everywhere. No licence, no notice and no upstream source file
# has any business containing one, so there is nothing to carve out -- and carving one out is how a
# real leak hides inside a file named LICENSE.
#
# `.git/` is skipped by both scans. Not an exemption for content -- it is not content. Once the
# destination is a real repository, its logs and reflog carry the committer's address by design,
# and every object in it is a compressed copy of files these scans already read in the working
# tree. Scanning it flags the author of the commits for being the author of the commits.
home='/home/[a-z][a-z0-9_-]*'
leaks=()
while IFS= read -r file; do leaks+=("$file"); done < <(
    grep -rIlE "$home" --exclude-dir=third_party --exclude-dir=.git "$destination" 2>/dev/null \
        || true)

# An email address is refused too, but licence, notice and provenance files exist precisely to
# carry other people's names and addresses. Refusing to publish an upstream copyright line because
# it contains an email would be the guard defeating the compliance it is meant to protect. Prose
# that wants to credit someone can name them and point at the notice file, which is where the
# authoritative copy lives anyway.
mail='[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}'
while IFS= read -r file; do
    # Per line, not per file: a file holding both a fixture address and a real one is still caught.
    # RFC 2606's reserved domains belong to nobody by design, and a test needs *some* address.
    if grep -hE "$mail" "$file" | grep -qvE 'example\.(com|org|net)'; then
        leaks+=("$file")
    fi
done < <(grep -rIlE "$mail" --exclude-dir=third_party --exclude-dir=.git \
             --exclude='LICENSE*' --exclude='COPYING*' --exclude='AUTHORS*' \
             --exclude='NOTICE*' --exclude='PROVENANCE*' "$destination" 2>/dev/null || true)

if (( ${#leaks[@]} )); then
    echo "  REFUSING: files carrying a home directory or an email address:" >&2
    printf '    %s\n' "${leaks[@]}" | sort -u >&2
    echo "  (third_party/ is exempt -- upstream's own authorship belongs in it)" >&2
    exit 1
fi

# Verify by running. A directory that looks right can still be missing something the tests need,
# and the only way to find out is from inside the copy rather than from the original.
# PYTHONDONTWRITEBYTECODE so the proof does not litter the thing it is proving. The sweep below
# stays anyway: anything else run inside the copy -- the CLI, an editor, an import from a shell --
# writes __pycache__ too, and a published tree should be swept on the way out regardless of who
# dirtied it.
if (cd "$destination" && PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q >/dev/null 2>&1); then
    echo "  verified: the published copy passes its own test suite"
else
    echo "  WARNING: the published copy does not pass its tests" >&2
    exit 1
fi

# Leave no trace. What is published should be what was counted above, and nothing else.
find "$destination" -name __pycache__ -type d -prune -exec rm -rf {} +
rm -rf "$destination/.pytest_cache"
