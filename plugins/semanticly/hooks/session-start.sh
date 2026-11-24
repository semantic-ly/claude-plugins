#!/usr/bin/env bash
#
# SessionStart. Recalls the bound repository's standing layer into
# `additionalContext` and shows the person the session-writes receipt as a
# `systemMessage`. One hook output carries both.
#
# The repository comes from the nearest `.semanticly` at or above
# CLAUDE_PROJECT_DIR. With none, the person sees one reminder to bind, except
# on Cowork, whose project dir is /private/var/empty and whose task binds by
# its own `recall` call.
#
# This wrapper execs the bundled CLI and does nothing else over the network. It
# speaks no HTTP and reads no database, so the plugin carries one copy of the
# auth rule rather than two.
#
# Every path exits 0 with at most one line on stderr, which is the hook log. A
# memory tool that blocks a coding session is worse than one that misses one.

set -u

note() { printf 'semanticly[session-start]: %s\n' "$1" >&2; }

COWORK_PROJECT_DIR=/private/var/empty

# hook_inputs.py's EXIT_STOP.
INPUTS_STOP=3

# `${CLAUDE_PLUGIN_ROOT}` is what hooks.json interpolates into the command, so
# $0 is already absolute. The derivation covers an install that expands the
# command string without exporting the variable.
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

# Each child's stderr lands here, so the log carries one line of it and no
# more. The cleanup trap is armed only for a file mktemp made, so the
# /dev/null fallback is never removed.
errlog="$(mktemp)" && trap 'rm -f "$errlog"' EXIT || errlog=/dev/null
last_line() { grep -v '^[[:space:]]*$' "$errlog" | tail -n 1; }

# The one encoder. It reads `recall --json`'s document on stdin, or nothing
# for an unbound project, and writes the hook's output. The block goes to
# `additionalContext` when it holds a line. One `systemMessage` carries the
# reminder, the `no_branch` sentence, the read-only sentence or the receipt. A
# refusal other than `no_branch` writes nothing, and the CLI's own stderr line
# says why. Text reaches json.dumps whole, so a fact holding a quote, a
# newline or a backslash cannot break out of its string.
ENCODE_PY=$(cat <<'PY'
import json
import sys
from urllib.parse import parse_qs, urlsplit

UNBOUND = "Semantic.ly isn't bound to this project; run `/semanticly:init`."

repo = sys.argv[1]


def header() -> str:
    # The call passes no --ref, so the only non-main rows the server returns
    # are the connection's bound branch's.
    return (f"The standing facts of `{repo}`, recalled by the Semantic.ly plugin. "
            "UNREVIEWED marks a fact on this connection's bound branch that nobody "
            "has reviewed.")


def count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def receipt(writes: object) -> str:
    """One clause per non-zero count, then the console URL."""

    if not isinstance(writes, dict):
        return ""
    url = writes.get("url") if isinstance(writes.get("url"), str) else ""
    branch = parse_qs(urlsplit(url).query).get("branch", [""])[0]
    on = f" on `{branch}`" if branch else ""
    awaiting, quota, proposals = (
        writes.get(key) if isinstance(writes.get(key), int) else 0
        for key in ("awaiting_review", "waiting_on_quota", "open_proposals"))
    clauses = []
    if awaiting:
        verb = "awaits" if awaiting == 1 else "await"
        clauses.append(f"{count(awaiting, 'fact', 'facts')}{on} {verb} review")
    if quota:
        clauses.append(f"{count(quota, 'slice', 'slices')} waiting on quota")
    if proposals:
        clauses.append(count(proposals, "open proposal", "open proposals"))
    if not clauses:
        return ""
    return " · ".join(clauses) + (f" — {url}" if url else "")


def output(body: object) -> dict:
    if not repo:
        return {"systemMessage": UNBOUND}
    if not isinstance(body, dict):
        raise SystemExit(1)
    error = body.get("error")
    if isinstance(error, dict):
        if error.get("code") == "no_branch":
            message = str(error.get("message") or "")
            return {"systemMessage": message} if message else {}
        return {}
    out = {}
    text = body.get("text") if isinstance(body.get("text"), str) else ""
    if body.get("lines") and text.strip():
        out["hookSpecificOutput"] = {
            "hookEventName": "SessionStart",
            "additionalContext": header() + "\n\n" + text.strip(),
        }
    if body.get("level") == "read":
        message = f"You hold read on `{repo}`, so this session's conversation is not captured."
    else:
        message = receipt(body.get("recent_writes"))
    if message:
        out["systemMessage"] = message
    return out


raw = sys.stdin.read()
try:
    body = json.loads(raw) if raw.strip() else None
except ValueError:
    raise SystemExit(1)
encoded = output(body)
if encoded:
    print(json.dumps(encoded))
PY
)

# The repository the project's `.semanticly` names, then the payload's
# session id, one per line.
reader_exit=0
located="$("$py" "$root/hooks/hook_inputs.py" session-start 2>"$errlog")" || reader_exit=$?
case "$reader_exit" in
  0) ;;
  "$INPUTS_STOP") note "$(last_line)"; exit 0 ;;
  *) note "the hook's input reader failed: $(last_line); nothing recalled"; exit 0 ;;
esac
repo="${located%%$'\n'*}"
session=""
case "$located" in *$'\n'*) session="${located#*$'\n'}" ;; esac

if [ -z "$repo" ]; then
  if [ "${CLAUDE_PROJECT_DIR:-}" = "$COWORK_PROJECT_DIR" ]; then
    note "Cowork binds by its own recall call; nothing recalled"
    exit 0
  fi
  "$py" -c "$ENCODE_PY" "" </dev/null
  note "no .semanticly at or above the project; nothing recalled"
  exit 0
fi

if [ -z "$session" ]; then
  note "the hook payload named no session; nothing recalled"
  exit 0
fi

: >"$errlog"
answer="$("$py" "$cli" recall --repo "$repo" --session "$session" --standing --json 2>"$errlog")"

encoded=1
printf '%s' "$answer" | "$py" -c "$ENCODE_PY" "$repo" || encoded=0

said="$(last_line)"
if [ -n "$said" ]; then
  note "${said#semanticly: }"
elif [ "$encoded" = 0 ]; then
  note "could not read the recall answer; the session starts without it"
fi

exit 0
