---
name: compile
description: Compile this project's pending automatic conversation records and closed run boards into curated second-brain notes (project note sections, profile, decisions, wiki topics), citing them as evidence, then mark them compiled. Use when a session-start notice reports pending records or run boards, or when the user asks to compile, digest, or consolidate automatic memory.
---

# Compile automatic records

Use the bundled entry point: `python3 scripts/second_brain.py`, resolving that
path relative to this skill's discovered directory. It is abbreviated as
`second-brain` below. Keep the code project as the working directory, or pass
`--cwd PROJECT` before `run`. The project must already be initialized.

Native hooks save one automatic record per agent session under
`memory/sessions/` (a long session continues in `-2`, `-3`, ... parts, each its
own record), with one `## Turn N` section per turn and a `## Subagent` section
for each subagent result. Records from plugin 0.3.0 and earlier hold a single
turn. Records are attributed excerpts, not verified facts. Compiling turns
them into curated knowledge. Do it without asking the user, and do it before the
user's first task, or right after it if that task is urgent.

1. Run `second-brain run compile` (add `--limit N` for a smaller batch; default
   8). It returns a `token`, the batch `records` (each with `path`, `link`,
   `from_turn`, `through_turn`, `created`), closed run boards in `runs` (each
   with `run`, `path`, `link`, `title`, `closed`; present only when there are
   any), `pending_total`, the target notes, and a checklist. It also adds any
   missing standard headings to the project note. If there is no `token`
   (no `records` and no `runs`), stop: there is nothing to compile. A listing
   with only `runs` is a normal batch.
2. Read each record from `from_turn` through `through_turn`: everything after
   the `## Turn {from_turn - 1}` section (from the top when `from_turn` is 1),
   including `## Subagent` sections. Earlier turns were already compiled. When
   `from_turn` is past `through_turn`, only subagent results after the last
   turn are new. Read each closed run board in `runs` too (its `path`): its
   tasks, activity, and results are agent claims with evidence links, useful
   for the project's status, decisions, and next steps.
3. Fold durable facts into curated notes:
   - The project note sections: `## Goal`, `## Decisions`, `## Status`,
     `## Open questions`, `## Next steps`.
   - `memory/profile.md`, only for preferences the user stated.
   - `memory/decisions/`, for decisions the user accepted.
   - `wiki/topics/`, for reusable knowledge that applies across projects: edit
     the existing topic, or file a new page with `second-brain run file-answer
     --title "..." --text-file FILE --source RECORD_PATH [...]` (or append to one
     with `--slug SLUG --update --expected-sha SHA`). Its `## Sources` list
     cites the records, so the page can be passed to `--updated`.
4. Cite each record and run board you used with its wikilink (the `link` field;
   it already includes any link prefix), for example
   `Evidence: [[memory/sessions/...]]` or `[[memory/runs/...]]`.
   Link each new note from `index.md` or the project note, and keep the
   `created`, `updated`, and `status` frontmatter on every note you write.
5. Run `second-brain run compile --done TOKEN --updated NOTE [--updated NOTE ...]
   [--skip RECORD ...]`. Each updated note must cite at least one batch record
   or run. Every record and run must be cited or skipped; skip a run by its
   `path` (or its run id). Skip items that hold no durable content, such as
   trivial chat. A failed check changes nothing: fix the notes and run `--done`
   again. If the token is rejected, list again. A compiled run is marked in its
   run state and is not listed again.
6. If `pending_total` is still above zero, repeat with a new listing.
   `second-brain run compile --status` shows what is pending without starting
   a batch; its `queued` counts hook writes still waiting for the vault lock,
   which the next listing applies first.

Rules:

- Keep canonical notes in English. Quoted source text keeps its language.
- Distinguish user-stated facts, accepted decisions, proposals, and unverified
  assistant claims. Never promote an assistant claim to a fact. Record it as a
  claim or proposal, or verify it against real files first.
- Cite evidence for every statement you add. Never invent an evidence path.
- Update existing sections instead of appending duplicates. Read the target
  note first.
- Supersede outdated statements instead of deleting them. Keep the old text
  with a note such as `Superseded YYYY-MM-DD: ...` and the reason.
- Do not copy secrets. Records are already redacted, but do not reconstruct
  redacted values.
- Turns recorded after the listing stay pending for the next compile.

For general memory use, follow the bundled
[second-brain skill](../second-brain/SKILL.md).
