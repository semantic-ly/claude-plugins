#!/usr/bin/env bash
#
# PreCompact and SessionEnd, async. Posts the project and feedback
# entries of Claude Code's own memory directory through `ingest`. The
# directory is `memory/` beside the payload's `transcript_path`.
#
# The repository comes from the nearest `.semanticly` at or above
# CLAUDE_PROJECT_DIR. With none the hook does nothing. Cowork's project dir is
# /private/var/empty and keeps no Claude Code memory, so it posts nothing.
#
# This wrapper execs the bundled CLI and speaks no HTTP of its own. The CLI
# gates on the level the session's last recall recorded, reads the
# directory and posts every kept entry. It reads no cursor, because the server
# drops the uuids it has already seen.
#
# Every path exits 0 with at most one line on stderr, which is the hook log.
# The line is the answer's headline, or the CLI's own last line when it
# answered nothing.

set -u

note() { printf 'semanticly[memory]: %s\n' "$1" >&2; }

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
  note "no runnable CLI at <plugin root>/bin/semanticly; nothing captured"
  exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
  note "python3 is not on PATH; nothing captured"
  exit 0
fi

# Each child's stderr lands here, so the log carries one line of it and no
# more. The cleanup trap is armed only for a file mktemp made, so the
# /dev/null fallback is never removed.
errlog="$(mktemp)" && trap 'rm -f "$errlog"' EXIT || errlog=/dev/null
last_line() { grep -v '^[[:space:]]*$' "$errlog" | tail -n 1; }

# The payload's session id, the memory directory beside its transcript,
# then the repository the project's `.semanticly` names, one per line.
reader_exit=0
located="$(python3 "$root/hooks/hook_inputs.py" memory 2>"$errlog")" || reader_exit=$?
case "$reader_exit" in
  0) ;;
  "$INPUTS_STOP") note "$(last_line)"; exit 0 ;;
  *) note "the hook's input reader failed: $(last_line); nothing captured"; exit 0 ;;
esac
session="$(printf '%s\n' "$located" | sed -n 1p)"
memory="$(printf '%s\n' "$located" | sed -n 2p)"
repo="$(printf '%s\n' "$located" | sed -n 3p)"

if [ -z "$repo" ]; then
  exit 0
fi

# The log line is the answer's headline, or the last line the CLI or the
# headline reader wrote when there is none.
: >"$errlog"
answer="$("$cli" ingest --repo "$repo" --session-id "$session" \
  --memory-dir "$memory" --json 2>"$errlog")"

said="$(printf '%s' "$answer" | python3 "$root/hooks/headline.py" 2>>"$errlog")"
if [ -z "$said" ]; then
  said="$(last_line)"
fi
if [ -n "$said" ]; then
  note "${said#semanticly: }"
fi

exit 0
