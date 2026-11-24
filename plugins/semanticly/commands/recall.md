---
description: Recall what this project's Semantic.ly repository knows about something.
argument-hint: [what to look for]
---

Recall this from the project's Semantic.ly repository.

$ARGUMENTS

Run the bundled CLI with the Bash tool, as
`"$CLAUDE_PLUGIN_ROOT/bin/semanticly" recall "<query>"`. Leave out `--repo`,
because the project's `.semanticly` file names the repository and the CLI
never guesses one. Add `--token-budget <n>` to cap how much comes back, or
`--ref <tag>` to read a published tag.

The CLI prints one block, and the semanticly skill teaches how to read its
labels. A line marked `UNREVIEWED` sits on this connection's branch and nobody
has reviewed it yet. A `[contradicts #N]` line is the other side of line N, so
present both sides and never drop one silently.

When the CLI prints a refusal, relay it to the person word for word. A
`no_branch` refusal names where the person chooses a branch, so do not retry.
When nothing matches, say so instead of answering from the conversation.
