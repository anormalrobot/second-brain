# second-brain

Give Codex and Claude Code a shared memory across sessions and projects. Save
your preferences, decisions, research, and project progress in an Obsidian vault,
then ask either agent to pick up where you left off.

After `init`, the native plugin loads memory during ordinary project work and
saves a conversation record when the agent finishes responding. You do not need
to invoke the memory skill for every task.

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

In Codex, open **`/hooks`** and review and trust the five `second-brain` hooks:
`SessionStart`, `UserPromptSubmit`, `Stop`, `SubagentStart`, and `SubagentStop`.
Restart the session afterward.
Codex skips hooks until this review is complete; installing the plugin alone
does not activate automatic memory. See [Codex hook activation](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks).

### 3. Example: remember a preference across sessions

After setup, send a normal message:

> I prefer concise explanations with Python examples.

Codex receives the memory workflow automatically, and the completed exchange is
recorded in the vault. In a later session in the same project, ask:

> How should you explain code to me?

Codex receives the saved context before answering. Claude Code can use the same
vault. You can still select the plugin's `memory` skill for a specific memory
task: type `$`, select it, then type your request, for example:

```text
What are my saved preferences for explanations and code examples?
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

Once setup completes, continue with normal messages. The plugin's native hooks
load memory before work and record completed responses. If you installed or
updated the plugin during this session, restart Claude Code first.

### 3. Example: continue a project in a new session

For example, send a normal task without a slash command:

```text
Add setup instructions to this project's README and check that the commands work.
```

Claude Code performs the task with saved context available. The plugin records
its final response, and the memory workflow asks it to maintain useful decisions
and next steps. Open the same project in a later session and ask:

```text
Where did we leave off, and what remains to be done?
```

Claude Code receives the previous progress automatically. For explicit archive
maintenance or ingestion, you can still invoke `/second-brain:memory`.
In Desktop, you can select
`second-brain:memory` from the slash-command menu, then type your request.
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

Work normally in a project where you have run `init`. The plugin:

- Loads your profile, project state, and recent records once at session start
  (and again after compaction); later prompts add only newly relevant note excerpts.
- Guides the agent to maintain useful preferences, decisions, and next steps.
- Saves bounded excerpts of your request and the final response under
  `memory/sessions/`: one record per agent session, with one section per turn.
  Each project keeps one records list, `memory/sessions/records/PROJECT.md`,
  linked once from the project note and vault index, so `index.md` and `log.md`
  do not grow with every turn. Client notices that arrive as prompts (background
  task notifications, subagent hand-backs) are saved as system notifications and
  add no note excerpts.
- Compiles records into curated knowledge. When a session starts and other
  sessions left uncompiled records (or a run board was closed), the agent is asked to fold their durable
  facts into the project note (Goal, Decisions, Status, Open questions, Next
  steps), your profile, `memory/decisions/`, or `wiki/topics/`, citing the
  records, before or right after your first task. The current agent does this; no
  background model runs. You can also ask for it with `/second-brain:compile`
  in Claude Code or the `compile` skill in Codex. `run compile --status` and
  `run doctor` show what is pending.
- Names and dates notes using your computer's local date.

Automatic records preserve what was said; they do not prove that a task succeeded.
The agent keeps accepted facts separate from proposals and unverified claims.
Maintained notes are in English; quoted source material keeps its language.

Only initialized projects participate. The plugin does not read entire chat
transcripts, tool outputs, or hidden reasoning, and does not run a background
model. Interrupted turns without a final response are not captured. Recognizable
credentials are redacted, but avoid putting secrets in conversations you record.

To pause or check the feature, ask your agent to run `second-brain automation off`,
`second-brain automation on`, or `second-brain automation status` using its bundled
helper. Status includes timestamps for hooks that actually ran. Previously
initialized projects also gain automatic memory after upgrading and activating
the hooks; pausing preserves their existing notes. Records saved by earlier
versions (one file per turn) stay in place and are compiled like newer ones.

### Growing the wiki

The vault becomes more useful as good answers are kept. When an answer combines
several notes or sources and would help again, the agent files it as a page in
`wiki/topics/` with `run file-answer`, citing its sources; later findings are
appended to the same page as dated updates (`--update`, which refuses if the page
changed since it was read). `run search` ranks notes by where the words appear
(title and path first, then headings, then text), puts notes matching every word
first, and prefers curated notes over automatic records; the same ranking picks
the excerpts added to your prompts. `run doctor` also lists notes to review: an
empty project note despite records, wiki or decision notes without evidence
links, and notes only `index.md` links to.

## Agent orchestration

second-brain also shares memory between agents working together in one project.

- **Subagents** (Claude Code or Codex) receive a short brief when they start: your
  profile, the project's goal, decisions, status, and next steps, and the active
  run board. When a subagent finishes, its final message is saved in the parent
  session's record as a `## Subagent` section.
- **CLI workers** (`codex exec` or `claude -p` started in the project) are ordinary
  sessions: they load memory and record their turns. Start them with
  `SECOND_BRAIN_RUN=RUN` to attach them to a run board.
- **Run boards** coordinate the work: tasks with claims, leases, dependencies,
  progress notes, and results. Each board is a generated note in `memory/runs/`,
  linked from the project note and `index.md`.

A typical orchestration, using the bundled helper (`second-brain` below):

```bash
second-brain run board open --title "Add CSV export" --goal "Export reports as CSV"
second-brain run board add --title "Write the exporter"            # t1
second-brain run board add --title "Document the export" --depends t1  # t2
SECOND_BRAIN_RUN=RUN codex exec "Claim t1 on the run board, do it, and post the result."
SECOND_BRAIN_RUN=RUN claude -p "Claim t2 on the run board when t1 is done, do it, and post the result."
second-brain run board show
second-brain run board close --summary "Export shipped and documented."
```

Replace `RUN` with the id that `board open` printed, or use
`second-brain launch --run RUN codex ...`. Workers claim a task before working on
it (`run board claim t1 --agent NAME`), so two agents never hold the same task;
a claim expires after 60 minutes unless the worker posts progress. Workers report
with `run board post t1 --agent NAME --status in_progress|blocked|done|failed
--note "..." [--evidence VAULT_PATH]`. After the board is closed, the next session's
compile step folds its results into the project note.

Only the claimant posts to a task. When a claim expires, another worker takes it
over with `claim`; a task posted as `failed` becomes unclaimed. If
`SECOND_BRAIN_RUN` names an unknown or closed run, the worker is warned and works
without a board. Codex subagent hooks are registered too, but that Codex reports
the parent's session id for subagents is unverified (it was confirmed for Claude
Code); if it does not, a Codex subagent result is saved in its own record instead
of the parent's.

Board notes are agent claims, not verified facts. When many agents finish at once
and the vault is busy, hook writes wait in a queue under `.brain/` instead of being
lost. They are applied by the next `SessionStart`, `UserPromptSubmit`, `Stop`, or
`SubagentStop` hook that gets the vault lock, by `run compile` (the listing and
`--done`), and by the board commands that change a board (`open`, `add`, `claim`,
`post`, `release`, `close`). `SubagentStart`, `run board show`, `run compile
--status`, and other commands leave the queue alone; `board show` and
`compile --status` report this project's queued writes as `queued`, and
`run doctor` reports the backlog.

## If something does not work

- **The skill is missing:** check that second-brain is enabled in that client's
  plugin manager, restart the client, and open a new session.
- **The agent cannot access the vault:** allow access through the client's normal
  controls. For CLI sessions, the optional launcher below can include the vault.
- **Normal messages do not load or save memory:** ask the agent to check
  `automation status`. If there are no `SessionStart`, `UserPromptSubmit`, or `Stop` events, restart
  the client and check its hook settings. In Codex, review the plugin in `/hooks`.
  A vault existing on disk does not mean the hooks are active.

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
second-brain run compile --status  # Show automatic records waiting to be compiled
second-brain run board show        # Show the project's active run board
second-brain run search "terms"    # Find notes, best matches first
second-brain run doctor            # Check the vault and list notes to review
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
