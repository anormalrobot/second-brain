---
name: init
description: Set up second-brain for this project by creating a home-folder vault, creating a vault at a chosen path, or using an existing Obsidian vault. Use when the user asks to initialize or configure shared memory.
---

# Initialize shared memory

Use the bundled entry point: `python3 scripts/second_brain.py`, resolving that
path relative to this skill's discovered directory. Keep the current code project
as the working directory, or pass its absolute path with `--cwd` before `init`.

Run `init --options` first. It only inspects configuration. If the user simply
wants to initialize another project and a valid shared vault is already selected,
run `init` to reuse it and connect this project automatically. Preserve existing
project state. If the user wants to change vaults, present the choices below.

For a first setup, present one concise question with exactly these three choices,
using the actual default path returned by the command:

1. Create a new vault in the home folder, normally `~/second-brain-vault` (default).
2. Create a new vault at another path.
3. Use an existing vault at a supplied path.

Use the host's question UI when available; otherwise ask in the conversation.
Ask for a path only for choices 2 and 3, unless already supplied. Default is a
recommendation, not permission to create files before the user chooses. Honor an
already explicit choice without reconfirming it. In a noninteractive agent tool,
do not open the terminal wizard or treat EOF as a selection.

Apply the choice using `init --default`, `init --new PATH`, or `init --existing
PATH`. Quote each path as one shell argument. The helper registers the vault,
selects it for future unbound projects, and connects the current project. Do not
make the user choose aliases, project IDs, or run create/connect/bind separately.

If initialization reports an occupied path or unavailable vault, explain the
specific issue and collect a corrected path. Do not replace existing files or
silently create a different brain. A configured but missing vault needs an explicit
choice; plain init must not discard it.

After setup, run `brief` and read the resolved vault's AGENTS.md. Apply the bundled
[memory skill](../memory/SKILL.md) to the current conversation so setup
immediately leads into memory use. Report the vault path and project connection.

Native plugin hooks (`SessionStart`, `UserPromptSubmit`, `Stop`, `SubagentStart`,
`SubagentStop`) load the brief at session start, add newly relevant note excerpts
to later prompts, and save an attributed record when a response finishes. Tell
the user to restart after installing or updating the plugin. Codex also requires
reviewing the plugin hooks in `/hooks`;
do not bypass hook trust or silently modify trust settings. Use `automation status`
to inspect actual event timestamps; a configured vault alone does not prove hooks
ran. Ordinary work needs no memory slash command once hooks are active. Do not
claim hooks are installed by the standalone skill or terminal helper alone.
