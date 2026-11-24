---
description: Write one fact to this project's Semantic.ly repository.
argument-hint: [what to remember]
---

Write this to the project's Semantic.ly repository as one fact.

$ARGUMENTS

Do it in four steps.

1. Rewrite it as one standalone sentence. An agent with none of this
   session's context reads it back months later, so resolve every pronoun and
   name the thing rather than pointing at it.
2. Choose a topic. Prefer a topic the repository already uses where one fits.
3. Run the bundled CLI with the Bash tool, as
   `"$CLAUDE_PLUGIN_ROOT/bin/semanticly" remember --topic <topic> "<fact>"`.
   Leave out `--repo`, because the project's `.semanticly` file names the
   repository and the CLI never guesses one.
4. Report back what the CLI prints.

The fact lands on this connection's branch, and recall shows it marked
`UNREVIEWED` until somebody reviews it.

The server refuses secret shapes that announce themselves through a fixed
prefix, such as an AWS access key id or a PEM private key header, and answers
`422 refused_content`. Never reword a refused value to get it past the check.

Nothing refuses a secret that looks like ordinary text. A password, a database
connection string, an AWS secret access key and any provider key without a
fixed prefix pass straight through, and so does third-party personal data. You
are the only control over all of it, so decline to write any of it. A fact is
append-only, and nobody can take one back.

When the CLI prints any other refusal, relay it to the person word for word. A
`no_branch` refusal names where the person chooses a branch, so do not retry.
