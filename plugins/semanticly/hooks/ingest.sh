#!/usr/bin/env bash
#
# PreCompact and SessionEnd, async. Posts what the session's
# transcript holds past the server's cursor, as the wire slice the `ingest`
# tool takes.
#
# The repository comes from the nearest `.semanticly` at or above
# CLAUDE_PROJECT_DIR. Cowork's project dir is /private/var/empty, so a Cowork
# task's repository is the one its own `recall` named, which
# `hooks/session_repo.py` reads from the transcript.
#
# This wrapper execs the bundled CLI and speaks no HTTP of its own. The CLI
# gates on the level the session's last recall recorded, so a session below
# `write` opens no transcript and sends nothing. The CLI also reads the
# cursor, cuts the transcript and posts. Nothing here reads PreCompact's
# `trigger` or SessionEnd's `reason`, so both events do the same work.
#
# Every path exits 0 with at most one line on stderr, which is the hook log.
# The line is the answer's headline, or the CLI's own last line when it
# answered nothing.

set -u

note() { printf 'semanticly[ingest]: %s\n' "$1" >&2; }

COWORK_PROJECT_DIR=/private/var/empty

# hook_inputs.py's EXIT_STOP.
INPUTS_STOP=3

root="${CLAUDE_PLUGIN_ROOT:-}"
if [ -z "$root" ]; then
  root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)" || root=""
fi
cli="$root/bin/semanticly"

if [ ! -x "$cli" ]; then
  note "no runnable CLI at <plugin root>/bin/semanticly; nothing captured"
  exit 0
fi

py=""
[ -r "$root/hooks/find-python.sh" ] && . "$root/hooks/find-python.sh"
if [ -z "$py" ]; then
  note "no Python 3.9 or later is on PATH; nothing captured"
  exit 0
fi

# Each child's stderr lands here, so the log carries one line of it and no
# more. The cleanup trap is armed only for a file mktemp made, so the
# /dev/null fallback is never removed.
errlog="$(mktemp)" && trap 'rm -f "$errlog"' EXIT || errlog=/dev/null
last_line() { grep -v '^[[:space:]]*$' "$errlog" | tail -n 1; }

cowork=""
surface="claude-code"
if [ "${CLAUDE_PROJECT_DIR:-}" = "$COWORK_PROJECT_DIR" ]; then
  cowork=1
  surface="cowork"
fi

# The payload's session id and transcript path, then the repository the
# project's `.semanticly` names, one per line. Cowork skips the lookup.
reader_exit=0
located="$("$py" "$root/hooks/hook_inputs.py" ingest "$cowork" 2>"$errlog")" || reader_exit=$?
case "$reader_exit" in
  0) ;;
  "$INPUTS_STOP") note "$(last_line)"; exit 0 ;;
  *) note "the hook's input reader failed: $(last_line); nothing captured"; exit 0 ;;
esac
session="$(printf '%s\n' "$located" | sed -n 1p)"
transcript="$(printf '%s\n' "$located" | sed -n 2p)"
repo="$(printf '%s\n' "$located" | sed -n 3p)"

if [ -n "$cowork" ]; then
  if ! repo="$("$py" "$root/hooks/session_repo.py" "$transcript" 2>"$errlog")"; then
    note "$(last_line)"
    exit 0
  fi
  if [ -z "$repo" ]; then
    note "this Cowork task named no repository in a recall; nothing captured"
    exit 0
  fi
fi

if [ -z "$repo" ]; then
  exit 0
fi

# The log line is the answer's headline, or the last line the CLI or the
# headline reader wrote when there is none.
: >"$errlog"
answer="$("$py" "$cli" ingest --repo "$repo" --session-id "$session" \
  --transcript "$transcript" --surface "$surface" --json 2>"$errlog")"

said="$(printf '%s' "$answer" | "$py" "$root/hooks/headline.py" 2>>"$errlog")"
if [ -z "$said" ]; then
  said="$(last_line)"
fi
if [ -n "$said" ]; then
  note "${said#semanticly: }"
fi

exit 0
