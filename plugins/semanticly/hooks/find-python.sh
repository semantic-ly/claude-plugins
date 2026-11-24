# Sourced by every hook. Sets `py` to the first Python 3.9 or later on PATH,
# or leaves it empty. Windows often has `python` or `py` and no `python3`,
# and its `python3` can be a stub that only opens the Store, so each
# candidate has to run before it counts. The `py` launcher would act on the
# CLI's `#!/bin/sh` line, so it is asked for its interpreter's path.

py=""
for candidate in python3 python py; do
  command -v "$candidate" >/dev/null 2>&1 || continue
  if [ "$candidate" = py ]; then
    candidate="$(py -3 -c 'import sys; print(sys.executable)' 2>/dev/null | tr -d '\r')"
    [ -n "$candidate" ] || continue
  fi
  if "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
    py="$candidate"
    break
  fi
done

# Windows pipes default to a legacy code page. UTF-8 mode keeps a fact or a
# prompt outside it intact between the hook's Python processes.
export PYTHONUTF8=1
