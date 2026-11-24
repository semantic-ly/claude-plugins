---
description: Show the Semantic.ly repositories you can write to and where to bind this project.
---

Show the person where this project's Semantic.ly memory can live.

Run the bundled CLI with the Bash tool, as
`"$CLAUDE_PLUGIN_ROOT/bin/semanticly" init`. Leave out `--repo`, because the
project's `.semanticly` file names the repository when one exists.

Run from here, `init` binds nothing. The Bash tool gives it no terminal, so it
only prints. It prints the repositories the person can write to and the
console link where a connection is bound to a branch. It prints the branches
of the repository `.semanticly` names only when the keychain holds that
repository's token. Otherwise it prints a sentence saying the branch list
needs `semanticly init` or `semanticly login` in a terminal. It writes no
`.semanticly`, no token and no binding.

Relay what it prints. Then tell the person they choose the repository and the
branch themselves, either at that console link or by running
`semanticly init` in a terminal. Never choose either for them.
