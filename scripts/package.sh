#!/usr/bin/env bash
#
# Packages the plugin's skill for upload to Claude.ai.
#
# The skill is authored once, at plugins/semanticly/skills/semanticly/SKILL.md,
# and this script ships that file unchanged.
#
# The upload wants a zip whose internal root is the skill's own directory, so
# the archive is staged under `semanticly/` and holds `semanticly/SKILL.md`
# and nothing else.
#
#     ./scripts/package.sh            # -> dist/semanticly-skill.zip
#     ./scripts/package.sh /tmp/x.zip # -> /tmp/x.zip
#
# Run from anywhere; every path is derived from this file.

set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
skill="$here/../plugins/semanticly/skills/semanticly/SKILL.md"
name="semanticly"

out="${1:-$here/../dist/$name-skill.zip}"
case "$out" in
  /*) ;;
   *) out="$PWD/$out" ;;
esac

if [ ! -f "$skill" ]; then
  printf 'package.sh: no SKILL.md at %s\n' "$skill" >&2
  exit 1
fi

if ! command -v zip >/dev/null 2>&1; then
  printf 'package.sh: `zip` is not installed, and it is what builds the archive\n' >&2
  exit 1
fi

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

mkdir -p "$stage/$name" "$(dirname -- "$out")"
cp "$skill" "$stage/$name/SKILL.md"

# Removed first, because `zip` ADDS to an archive that already exists — a
# second run would otherwise ship whatever the first run held.
rm -f "$out"
( cd "$stage" && zip -q -r -X "$out" "$name" )

printf '%s\n' "$out"
