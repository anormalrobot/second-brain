# second-brain

Give Codex and Claude Code a shared memory across sessions and projects. Save
your preferences, decisions, research, and project progress in an Obsidian vault,
then ask either agent to pick up where you left off.

Your notes are ordinary Markdown files. You can read and edit them in Obsidian,
and explore their connections through `[[wikilinks]]`, backlinks, and the graph.

## Before you start

You need:

- Codex or Claude Code with plugin support and access to local files.
- Python 3.9 or newer, available as `python3`.
- Git and GitHub SSH access to download the repository.
- Optionally, Obsidian to browse and edit your notes visually.

second-brain works through the agent's chat and question controls. It does not
have a separate dashboard. Use a local session that can run Python and access
your vault; installing locally does not give a cloud session access to your files.

Prepare the plugin below, then follow [Codex](#use-with-codex),
[Claude Code](#use-with-claude-code), or both. Both agents can use the same vault.

## Prepare the plugin once

Run these commands in a terminal, or ask your agent to run them:

```bash
git clone git@github.com:anormalrobot/second-brain.git
cd second-brain
python3 scripts/package_plugin.py --output dist/second-brain
```

The output directory must be new. Keep the generated folder: it is the local
plugin source used by the clients. Register it as described below to make
second-brain available in their plugin browsers.

## Use with Codex

### 1. Install the plugin

Ask Codex to install the prepared package, supplying the folder's absolute path:

> Register /absolute/path/to/second-brain/dist/second-brain as a local plugin
> marketplace and install second-brain from second-brain-local for Codex.

To install through the desktop plugin browser, ask Codex to register the source
only. Restart the app, open **Plugins Directory**, choose the **second-brain**
source (`second-brain-local`), and install `second-brain`.

<details>
<summary>Alternative: terminal installation</summary>

Run these from the downloaded repository:

```bash
codex plugin marketplace add "$PWD/dist/second-brain"
codex plugin add second-brain@second-brain-local
```

</details>

Restart Codex after installation and open your code project in a new local
session. See [OpenAI's local plugin guide](https://developers.openai.com/plugins/build/plugins#build-your-own-curated-plugin-list).

### 2. Choose your vault

Select the `second-brain` plugin's `init` skill in your conversation:

- **ChatGPT desktop, in Codex mode:** type `@` and select the skill.
- **Codex CLI or IDE extension:** open `/skills` or type `$` and select the skill.

Then send:

> Set up shared memory for this project.

Follow the [vault choices](#choose-your-vault) below. Select the plugin's `init`
skill; the client's built-in `/init` sets up project instructions instead.
See the [OpenAI skill guide](https://learn.chatgpt.com/docs/build-skills#how-chatgpt-and-codex-use-skills).

### 3. Example: remember a preference across sessions

Select the `second-brain` memory skill using the same picker and send:

> Remember that I prefer concise explanations with Python examples.

Codex saves this preference in the shared vault. In a later session, select the
memory skill again and ask:

> What are my saved preferences for explanations and code examples?

Codex reads the saved note. Claude Code can recall the same preference when it
uses this vault. In the Codex CLI or IDE, you can also send:

```text
$second-brain What are my saved preferences for explanations and code examples?
```

## Use with Claude Code

### 1. Install the plugin

In a Claude Code conversation, register the prepared package. Replace the example
path with the absolute path to your generated folder:

```text
/plugin marketplace add /absolute/path/to/second-brain/dist/second-brain
```

Open `/plugin`, find `second-brain` in `second-brain-local`, and install it for
your user so it is available across projects. In Claude Code Desktop, use
**+ → Plugins → Add plugin** to browse the configured source; **Manage plugins**
shows whether the plugin is enabled.

<details>
<summary>Alternative: terminal installation</summary>

Run these from the downloaded repository:

```bash
claude plugin marketplace add "$PWD/dist/second-brain"
claude plugin install second-brain@second-brain-local --scope user
```

</details>

Restart the client and open your code project. In Claude Code Desktop, use a
local session in the **Code** tab. See the [Claude plugin guide](https://code.claude.com/docs/en/discover-plugins)
and [Desktop plugin controls](https://code.claude.com/docs/en/desktop#install-plugins).

### 2. Choose your vault

Send this in the conversation:

```text
/second-brain:init
```

In Desktop, you can also type `/` or open **+ → Slash commands** and select
`second-brain:init`. Follow the [vault choices](#choose-your-vault) below. If you
already configured a vault with Codex, setup reuses it.

Use the plugin's `/second-brain:init`; Claude's built-in `/init` sets up project
instructions. See [Claude's skill controls](https://code.claude.com/docs/en/desktop#use-skills).

### 3. Example: continue a project in a new session

After working on your project, send:

```text
/second-brain:second-brain Save our progress, decisions, pending work, and the next step for this project.
```

Claude Code saves a project handoff. Open the same project in a later session and
send:

```text
/second-brain:second-brain Read this project's latest handoff and tell me what to do next.
```

Claude Code reads the saved progress and next step. In Desktop, you can select
`second-brain:second-brain` from the slash-command menu, then type your request.
These are conversation inputs, not shell commands.

## Choose your vault

On first setup, either agent offers three choices:

1. **Create `~/second-brain-vault`** — the default location.
2. **Create a new vault at another path** — provide your preferred folder.
3. **Use an existing Obsidian vault** — provide that vault's folder.

Choose through the client's question controls, or reply in chat. The agent
connects your project and reports the vault location. In Obsidian, use
**Open folder as vault** to open that folder.

For an ordinary existing Obsidian vault, the plugin creates a `second-brain/`
folder and preserves your notes and settings. Existing notes are not imported
automatically; ask the agent to process the ones you want to include.
Previously initialized vaults keep their existing folder names and links.

If a vault is already configured, setup reuses it. To choose another, ask:

> Use second-brain to change this project's vault.

## Use the same memory in other projects

Open another code project and run the plugin's `init` skill there. It reuses your
configured vault and connects the new project automatically. Your preferences and
knowledge stay shared; each project has its own progress and handoffs.

To switch between Codex and Claude Code on the same computer, install the plugin
in both clients. Changing one project's vault leaves other projects' existing
connections in place.

## Everyday use

Select the `second-brain` memory skill and ask it to recall a decision, process a
document, connect related notes, or save your progress. Before ending a session,
ask it to save what you completed and the next step.

The plugin does not automatically record every conversation. Maintained notes
are written in English; original source material keeps its language.

## If something does not work

- **The skill is missing:** check that second-brain is enabled in that client's
  plugin manager, restart the client, and open a new session.
- **The agent cannot access the vault:** allow access through the client's normal
  controls. For CLI sessions, the optional launcher below can include the vault.

<details>
<summary>Optional terminal commands and launcher</summary>

Chat workflows do not require a separate terminal command. To add one, run this
from the downloaded repository after preparing the plugin:

```bash
./dist/second-brain/plugins/second-brain/bin/second-brain install --command-only
```

Then, from any code project:

```bash
second-brain init           # Set up this project or reuse your shared vault
second-brain brief          # Read saved context for this project
second-brain init --choose  # Choose a different vault
```

To start a CLI session with access to the vault, use `second-brain launch codex`
or `second-brain launch claude`. If the command is not found, use
`~/.local/bin/second-brain` or add `~/.local/bin` to your shell's PATH.

</details>

## Your files stay yours

The vault is separate from the plugin installation. Notes remain readable and
editable without the plugin. No Obsidian community plugin is required.

Use one writer at a time when agents and Obsidian edit the same notes. Sharing
across projects uses the local vault; syncing between computers and remote
backups require your own setup.

## Built with AI

This plugin was developed using artificial intelligence, with **OpenAI Codex** and
**Anthropic Claude Code** contributing to implementation, code review, and smoke
testing.
