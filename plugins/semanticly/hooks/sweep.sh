#!/usr/bin/env bash
#
# SessionStart, async. Posts every session the project's
# transcript directory holds past the server's cursor, so a session that ended
# without a `SessionEnd` still reaches the repository. The directory is the
# one holding the payload's `transcript_path`, and each `*.jsonl` in it is one
# session whose stem is its id.
#
# The repository comes from the nearest `.semanticly` at or above
# CLAUDE_PROJECT_DIR. With none the hook does nothing, because session-start.sh
# already reminds the person to bind. Cowork's project dir is /private/var/empty
# and its transcripts are not a Claude Code project's, so it runs no sweep.
#
# This wrapper execs the bundled CLI and speaks no HTTP of its own. Every path
# exits 0 with at most one line on stderr, which is the hook log.

set -u

note() { printf 'semanticly[sweep]: %s\n' "$1" >&2; }

COWORK_PROJECT_DIR=/private/var/empty

# hook_inputs.py's EXIT_STOP.
INPUTS_STOP=3

if [ "${CLAUDE_PROJECT_DIR:-}" = "$COWORK_PROJECT_DIR" ]; then
  exit 0
fi

root="${CLAUDE_PLUGIN_ROOT:-}"
if [ -z "$root" ]; then
  root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)" || root=""
fi
cli="$root/bin/semanticly"

if [ ! -x "$cli" ]; then
  note "no runnable CLI at <plugin root>/bin/semanticly; nothing swept"
  exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
  note "python3 is not on PATH; nothing swept"
  exit 0
fi

# Each child's stderr lands here, so the log carries one line of it and no
# more. The cleanup trap is armed only for a file mktemp made, so the
# /dev/null fallback is never removed.
errlog="$(mktemp)" && trap 'rm -f "$errlog"' EXIT || errlog=/dev/null
last_line() { grep -v '^[[:space:]]*$' "$errlog" | tail -n 1; }

# The bound repository, then the directory of the payload's
# `transcript_path`, one per line.
reader_exit=0
located="$(python3 "$root/hooks/hook_inputs.py" sweep 2>"$errlog")" || reader_exit=$?
case "$reader_exit" in
  0) ;;
  "$INPUTS_STOP") note "$(last_line)"; exit 0 ;;
  *) note "the hook's input reader failed: $(last_line); nothing swept"; exit 0 ;;
esac
repo="${located%%$'\n'*}"
directory=""
case "$located" in *$'\n'*) directory="${located#*$'\n'}" ;; esac

if [ -z "$repo" ]; then
  exit 0
fi

if [ -z "$directory" ]; then
  note "the hook payload named no transcript; nothing swept"
  exit 0
fi

# The CLI's stdout is the per-session answer, which nobody reads in a
# background hook.
: >"$errlog"
"$cli" ingest --repo "$repo" --sweep "$directory" --surface claude-code \
  >/dev/null 2>"$errlog"

said="$(last_line)"
if [ -n "$said" ]; then
  note "${said#semanticly: }"
fi

exit 0
