#!/usr/bin/env bash
#
# UserPromptSubmit. Recalls facts for the prompt just submitted and
# hands them to Claude Code as `additionalContext`.
#
# It runs from install. Only `"prompt_recall": false` in
# `~/.semanticly/config.json` turns it off, and then it exits having emitted,
# called and written nothing. The rule is the CLI's `prompt_recall_setting`.
#
# The repository comes from the nearest `.semanticly` at or above
# CLAUDE_PROJECT_DIR. Cowork's project dir is /private/var/empty, so a Cowork
# task's repository is the one its own `recall` named, which
# `hooks/session_repo.py` reads from the transcript. With neither, the
# hook calls nothing. `hooks/prompt_inputs.py` reads all of this.
#
# This wrapper execs the bundled CLI and speaks no HTTP of its own. The call
# sends `standing: false` inside a fixed 600-token budget, because a block
# injected per turn pays its cost per turn. `recall --session` excludes what
# the session already holds and prints nothing when no term or semantic line
# is new.
#
# Every path exits 0 with at most one line on stderr, which is the hook log. A
# memory tool that blocks a coding session is worse than one that misses a turn.

set -u

note() { printf 'semanticly[prompt-recall]: %s\n' "$1" >&2; }

COWORK_PROJECT_DIR=/private/var/empty
PROMPT_BUDGET=600

# hook_inputs.py's EXIT_STOP and prompt_inputs.py's EXIT_OFF.
INPUTS_STOP=3
INPUTS_OFF=4

root="${CLAUDE_PLUGIN_ROOT:-}"
if [ -z "$root" ]; then
  root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)" || root=""
fi
cli="$root/bin/semanticly"

if [ ! -x "$cli" ]; then
  note "no runnable CLI at <plugin root>/bin/semanticly; nothing recalled"
  exit 0
fi

py=""
[ -r "$root/hooks/find-python.sh" ] && . "$root/hooks/find-python.sh"
if [ -z "$py" ]; then
  note "no Python 3.9 or later is on PATH; nothing recalled"
  exit 0
fi

cowork=""
surface="claude-code"
if [ "${CLAUDE_PROJECT_DIR:-}" = "$COWORK_PROJECT_DIR" ]; then
  cowork=1
  surface="cowork"
fi

# Each child's stderr lands here, so the log carries one line of it and no
# more. The cleanup trap is armed only for a file mktemp made, so the
# /dev/null fallback is never removed.
errlog="$(mktemp)" && trap 'rm -f "$errlog"' EXIT || errlog=/dev/null
last_line() { grep -v '^[[:space:]]*$' "$errlog" | tail -n 1; }

# The one encoder. It reads the CLI's rendered block on stdin and writes the
# hook's output. Text reaches json.dumps whole, so a fact holding a quote, a
# newline or a backslash cannot break out of its string.
ENCODE_PY=$(cat <<'PY'
import json
import sys

text = sys.stdin.read().strip()
if not text:
    raise SystemExit(0)

# The call passes no --ref, so the only non-main rows the server returns are
# the connection's bound branch's.
header = (f"Facts from `{sys.argv[1]}`, recalled by the Semantic.ly plugin for this "
          "prompt. UNREVIEWED marks a fact on this connection's bound branch that "
          "nobody has reviewed.")

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": header + "\n\n" + text,
    },
}))
PY
)

reader_exit=0
inputs="$("$py" "$root/hooks/prompt_inputs.py" "$cowork" 2>"$errlog")" || reader_exit=$?
case "$reader_exit" in
  0) ;;
  "$INPUTS_OFF") exit 0 ;;
  "$INPUTS_STOP") note "$(last_line)"; exit 0 ;;
  *) note "the hook's input reader failed: $(last_line); nothing recalled"; exit 0 ;;
esac
repo="${inputs%%$'\n'*}"; inputs="${inputs#*$'\n'}"
session="${inputs%%$'\n'*}"; inputs="${inputs#*$'\n'}"
notes="${inputs%%$'\n'*}"; prompt="${inputs#*$'\n'}"

# `--` before the prompt, so a prompt opening with a dash is a query.
: >"$errlog"
recalled=1
facts="$("$py" "$cli" recall --repo "$repo" --session "$session" \
  --token-budget "$PROMPT_BUDGET" --surface "$surface" -- "$prompt" 2>"$errlog")" \
  || recalled=0

said="$(last_line)"
said="${said#semanticly: }"
if [ -z "$said" ] && [ "$recalled" = 0 ]; then
  said="recall failed; this turn gets no remembered facts"
fi

output=""
if [ "$recalled" = 1 ]; then
  output="$(printf '%s' "$facts" | "$py" -c "$ENCODE_PY" "$repo")" \
    || said="${said:+$said; }could not encode the recalled facts; this turn goes without them"
fi

# The reader's notes and the recall's outcome share the one log line.
message="$notes"
if [ -n "$said" ]; then
  message="${message:+$message; }$said"
fi
if [ -n "$message" ]; then
  note "$message"
fi
if [ -n "$output" ]; then
  printf '%s\n' "$output"
fi

exit 0
