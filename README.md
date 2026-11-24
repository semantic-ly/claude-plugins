# Semantic.ly for Claude

Semantic.ly is a memory store for AI agents. This plugin connects Claude to a
Semantic.ly repository, so facts your team has recorded are recalled into each
session and new facts are extracted from your sessions.

The service is invitation-only during testing. Nothing here works until we
have invited you.

## What the plugin sends to Semantic.ly

The plugin sends nothing until a project is bound to a repository with
`semanticly init`. After that, its hooks talk to `https://api.semantic.ly`.

| When | What is sent |
|---|---|
| Session start, resume, clear and compact | A recall request for the bound repository. The facts that come back are added to the session's context. |
| Every prompt you submit | The text of the prompt, as a search query. A prompt longer than a few thousand characters is cut to its head. You can turn this off. |
| Before compaction and at session end | The session transcript since the last upload, and the project and feedback entries from Claude Code's own memory directory for the project. The server extracts facts from both. The memory upload is labelled with a hash of Claude Code's folder name for the project, so the local path stays on your machine. |
| Session start, in the background | The transcript of every earlier session in the project that has not been uploaded yet. This includes sessions from before you installed the plugin. |

A transcript upload holds the text of your messages and of Claude's replies,
verbatim, with each message's id and role. Tool calls, tool results and
thinking blocks are left out. If a secret appears in the text of the
conversation, it is part of the upload.

Transcript and memory uploads happen only when your sign-in can write to the
repository.

## Prerequisites

- An invitation from Semantic.ly
- `python3` on your `PATH`, version 3.9 or later
- An OS keychain for the CLI's sign-in, which is `security` on macOS,
  `secret-tool` on Linux and the Credential Manager on Windows
- On Windows, [Git for Windows](https://git-scm.com/download/win), because
  the plugin's hooks are bash scripts and Claude Code runs them through Git
  Bash

## Claude Code

1. Add the marketplace and install the plugin.

   ```
   /plugin marketplace add semantic-ly/claude-plugins
   /plugin install semanticly@semanticly
   ```

2. Download the `semanticly` file from the
   [latest release](https://github.com/semantic-ly/claude-plugins/releases/latest),
   make it executable and put it on your `PATH`.

   ```bash
   chmod +x semanticly
   ```

3. Sign in. A browser window opens, and the token is stored in your OS
   keychain.

   ```bash
   semanticly login
   ```

4. Bind your project. Run this in a terminal, inside the project. It writes a
   `.semanticly` file naming the repository and asks you to pick a branch.

   ```bash
   semanticly init --repo <org>/<repo>
   ```

5. Start a Claude Code session in the project.

The CLI reaches `https://api.semantic.ly` with no configuration. You need no
environment variable.

### Windows

The steps are the same, with three differences.

- Skip `chmod`. In PowerShell or Command Prompt, run the CLI through Python,
  as `python semanticly login` and `python semanticly init --repo <org>/<repo>`.
  With the `py` launcher, write `py -3 semanticly login`. A bare `py semanticly`
  fails.
- Run `init` in PowerShell, Command Prompt or Windows Terminal. In the Git
  Bash window `init` cannot tell that it has a terminal, so it only lists the
  repositories and binds nothing.
- Python can be on `PATH` as `python3`, `python` or `py`.

The plugin adds three commands.

| Command | What it does |
|---|---|
| `/semanticly:recall` | Searches the repository |
| `/semanticly:remember` | Writes a fact |
| `/semanticly:init` | Shows the repositories you can write to and where to bind this project |

A project with no `.semanticly` file gets one reminder at session start and
nothing else. The hooks never block a session. When something fails, they
write one line to the hook log and exit.

## Claude.ai and Claude Desktop

1. Open Customize > Connectors and add a custom connector with the URL
   `https://api.semantic.ly/mcp`. On a Team or Enterprise plan, only an Owner
   can add the connector.
2. Download `semanticly-skill.zip` from the
   [latest release](https://github.com/semantic-ly/claude-plugins/releases/latest)
   and upload it under Customize > Skills.

## Cowork

1. Open Customize > Plugins > Add marketplace and enter
   `semantic-ly/claude-plugins`.
2. Install the `semanticly` plugin.
3. Connect the Semantic.ly connector. Installing the plugin does not connect
   it.

A Cowork task has no `.semanticly` file. The plugin takes the repository from
the first recall the task makes.

## Turning off per-prompt recall

Per-prompt recall is on at install. To turn it off, set `prompt_recall` to
`false` in `~/.semanticly/config.json`.

```json
{
  "prompt_recall": false
}
```

`semanticly login` creates this file, so add the key to what is already
there. The change needs no reinstall. The session-start recall and the
transcript uploads keep running.

## Uninstalling

```bash
claude plugin uninstall semanticly@semanticly
claude plugin marketplace remove semanticly
```

Then delete the `semanticly` file you downloaded, the `~/.semanticly`
directory and the `.semanticly` file in each bound project. The CLI's tokens
stay in your OS keychain under the service name `semanticly` until you remove
them there.

## Development

The tests use only the Python standard library. Run them from the repository
root.

```bash
python3 -m unittest discover -s tests/cli -t tests/cli
python3 -m unittest discover -s tests/plugin -t tests/plugin
python3 -m unittest discover -s tests/package -t tests/package
python3 -m unittest discover -s tests/smoke -t tests/smoke
```

One plugin test runs `claude plugin validate --strict` and is skipped when
`claude` is not on your `PATH`.

`scripts/package.sh` builds the skill zip for Claude.ai into `dist/`, which
git ignores.

The tests need Python 3.11 or later. The CLI and the hooks themselves run on
Python 3.9.

CI runs the tests on every pull request. To release, set the new version in
`plugins/semanticly/.claude-plugin/plugin.json` and in `VERSION` in
`plugins/semanticly/bin/semanticly`, merge, then push a tag `v<version>`. The
release workflow publishes `semanticly`, `semanticly-skill.zip` and
`SHA256SUMS`.
