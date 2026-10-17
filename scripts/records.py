#!/usr/bin/env python3
"""Session record files: one per agent session part, with a section per turn or subagent result.

Writers here assume the caller holds the vault writer lock. Hooks and the spool (runs.drain) share them, so a
write applied late is deduplicated exactly like a retried hook.
"""
from pathlib import Path
import re

try:
    from . import brain
except ImportError:
    import brain

TURN_KEYS = 500  # [I, R] pairs kept in session state, oldest first, so retries need no reads of old parts.
RECENT_TURNS = 50  # An id-less retry is matched by response hash among this many latest turns.
RECOVER_PARTS = 5  # A lost index is rebuilt from at most this many newest parts, bounding time under the lock.
SUBAGENT_MARKS = 2000  # Subagent result digests kept in session state, apart from the turn index (results are rare).
STATE_NAME = re.compile(r'session-[0-9a-f]{32}\.json')
HEX = re.compile(r'[0-9a-f]{24}')
LABEL = re.compile(r'[A-Za-z0-9._:-]{1,64}')


def fenced(text):
    fence = '`' * max(3, 1 + max((len(m[0]) for m in re.finditer(r'`+', text)), default=0))
    return fence + 'text\n' + text + '\n' + fence


def receipt_path(root, project):
    return brain.internal(root, '.brain/automation/project-' + brain.digest(project.encode())[:24] + '.json')


def receipt(root, project, kind, **fields):
    path = receipt_path(root, project)
    data = brain.load_json(path, {})
    data[kind] = {'at': brain.timestamp(), **fields}
    brain.save_json(path, data)


def record_header(root, project, relative, key, when, run=None):
    part = int(brain.RECORD_NAME.fullmatch(Path(relative).name)[2] or 1)
    return (f'---\ncreated: {when}\nupdated: {when}\nstatus: recorded\nsession: {key}\ncompiled_through: 0\n'
            + (f'run: {run}\n' if run else '') + '---\n'
            + brain.RECORD_TITLE + (f' (part {part})' if part > 1 else '') + '\n\n'
            f'Project: [[{brain.link_target(root, project)}|Project state]].\n\n'
            + (f'Run: [[{brain.link_target(root, brain.run_note(run))}|Run board]].\n\n' if run else '') +
            'Source: native UserPromptSubmit, Stop, and SubagentStop events from one local agent session, one section '
            'per turn or subagent result.\n'
            'These are attributed excerpts, not an independently verified handoff. '
            'Assistant statements are claims; proposals are not accepted decisions. '
            'Excerpts are bounded and recognizable credentials are redacted.\n')


def record_pattern(key):
    return r'memory/sessions/\d{4}-\d\d-\d\d-auto-' + key + r'(?:-[0-9]+)?\.md'


def part_number(relative):
    return int(brain.RECORD_NAME.fullmatch(Path(relative).name)[2] or 1)


def next_part(relative):
    base, part = re.fullmatch(r'(.+-auto-[0-9a-f]{24})(?:-([0-9]+))?\.md', relative).groups()
    return f'{base}-{int(part or 1) + 1}.md'


def session_parts(root, key, limit=None):
    """The newest `limit` (default all) record parts of this session as (part, relative), oldest first.

    Names are ordered before any file is opened, and no candidate is opened unless it is inside the vault.
    """
    names = ['memory/sessions/' + found.name for found in (root / 'memory/sessions').glob('*-auto-' + key + '*.md')]
    parts = []
    for relative in sorted((r for r in names if re.fullmatch(record_pattern(key), r)), key=part_number, reverse=True):
        if limit is not None and len(parts) >= limit:
            break
        try:
            path = brain.scoped(root, relative, area='memory/sessions')
        except ValueError:
            continue
        if path.is_file():
            with path.open(encoding='utf-8') as handle:
                if brain.frontmatter(handle.read(1000)).get('session') == key:
                    parts.append((part_number(relative), relative))
    return parts[::-1]


def is_record(root, relative):
    try:
        return brain.scoped(root, relative, area='memory/sessions').is_file()
    except ValueError:
        return False


def session_record(root, state, key):
    """This session's current record part. State remembers it, so the file keeps its date past midnight."""
    current = state.get('record')
    if isinstance(current, str) and re.fullmatch(record_pattern(key), current):
        # A rollover whose state write failed left a newer part behind; one stat per step finds it.
        while is_record(root, next_part(current)):
            current = next_part(current)
        return current
    parts = session_parts(root, key, 1)
    return parts[-1][1] if parts else f'memory/sessions/{brain.day()}-auto-{key}.md'


def own_entries(text):
    """This part's turn [I, R] pairs and subagent marks, each in file order, from one scan."""
    found = brain.entries(text)
    return ([[ident, answer] for _, ident, answer in found if not ident.startswith('s:')],
            [ident[2:] for _, ident, _ in found if ident.startswith('s:')])


def recover_pairs(root, key, current, marks=False):
    """Turn [I, R] pairs (or subagent marks) of the other newest parts, oldest first: at most RECOVER_PARTS parts
    and TURN_KEYS pairs (SUBAGENT_MARKS marks)."""
    found, bound = [], SUBAGENT_MARKS if marks else TURN_KEYS
    for _, relative in reversed(session_parts(root, key, RECOVER_PARTS)):
        if relative != current:
            text = brain.scoped(root, relative, area='memory/sessions').read_text(encoding='utf-8')
            found = own_entries(text)[1 if marks else 0] + found
            if len(found) >= bound:
                break
    return found[-bound:]


def known_pairs(root, key, state, current, own):
    """This session's recent [I, R] pairs in file order: the index without the current part, then that part.

    The current part's own markers come last, so a turn whose state write failed is still known, and keys
    the index evicted are never placed after newer ones.
    """
    cached = state.get('turn_keys')
    valid = isinstance(cached, list) and all(isinstance(p, list) and len(p) == 2 for p in cached)
    older = cached if valid else recover_pairs(root, key, current)
    present = {tuple(pair) for pair in own}
    return [pair for pair in older if tuple(pair) not in present] + own


def known_marks(root, key, state, current, own):
    """This session's recent subagent marks: the state list (rebuilt by bounded recovery when lost), then this part's."""
    cached = state.get('subagent_marks')
    valid = isinstance(cached, list) and all(isinstance(m, str) for m in cached)
    older = cached if valid else recover_pairs(root, key, current, marks=True)
    return [m for m in older if m not in set(own)] + own


def duplicate(pairs, ident, answer):
    """A retry matches by id, or by response among the latest turns when either side lacks an id."""
    if ident and any(i == ident for i, _ in pairs):
        return True
    return any(r == answer and (not ident or not i or i == ident) for i, r in pairs[-RECENT_TURNS:])


def state_file(root, name):
    if not isinstance(name, str) or not STATE_NAME.fullmatch(name):
        raise ValueError('Invalid session state name')
    return brain.internal(root, '.brain/automation/' + name)


def project_note(root, project):
    path = brain.scoped(root, project, area='memory/projects') if isinstance(project, str) else None
    if not path or path.suffix != '.md' or not path.is_file():
        raise ValueError('Project note does not exist: ' + str(project)[:200])
    return project


def checked(root, item):
    """Validate a hook item before any path is built from it; spooled items are read back from disk."""
    kind = item.get('kind')
    keys = ('mark', 'agent_type', 'agent', 'response') if kind == 'subagent' else ('label', 'prompt', 'response')
    if kind not in ('turn', 'subagent') or not all(isinstance(item.get(k), str) for k in ('project', 'key', 'state', 'time') + keys):
        raise ValueError('Malformed record item')
    if not HEX.fullmatch(item['key']) or not re.fullmatch(r'\d\d:\d\d', item['time']):
        raise ValueError('Malformed record item')
    if kind == 'subagent' and not (HEX.fullmatch(item['mark']) and LABEL.fullmatch(item['agent_type']) and LABEL.fullmatch(item['agent'])):
        raise ValueError('Malformed subagent item')
    if kind == 'turn' and not (isinstance(item.get('ident'), str) and isinstance(item.get('answer'), str)
                               and (not item['ident'] or HEX.fullmatch(item['ident'])) and HEX.fullmatch(item['answer'])
                               and item['label'] in ('Request', 'System notification')):
        raise ValueError('Malformed turn item')
    run = item.get('run')
    if run is not None and not (isinstance(run, str) and brain.RUN_ID.fullmatch(run)):
        raise ValueError('Malformed run id')
    project_note(root, item['project'])
    return state_file(root, item['state'])


def write_entry(root, item):
    """Append a turn or subagent section to its session's current record part; the caller holds the lock.

    The text in item is already redacted and bounded. The record is durable before state names its key, so a
    failed write leaves a retry (or the spool) able to write it. Returns the part that holds the entry.
    """
    root = Path(root)
    state_path = checked(root, item)
    key, project, run = item['key'], item['project'], item.get('run')
    state = brain.load_json(state_path, {})
    relative = session_record(root, state, key)
    target = brain.scoped(root, relative, area='memory/sessions')
    text = target.read_text(encoding='utf-8') if target.is_file() else ''
    own, own_marks = own_entries(text)
    if item['kind'] == 'subagent':
        # Marks have their own list, so 500 later turns cannot evict a result's identity.
        marks = known_marks(root, key, state, relative, own_marks)
        found = item['mark'] in marks
        update = {'marks': (marks + ([] if found else [item['mark']]))[-SUBAGENT_MARKS:]}
        heading = lambda number: f'\n## Subagent {item["agent_type"]} ({item["agent"]}) — {item["time"]}\n'
        body = f'<!-- subagent:{item["mark"]} -->\n\n### Response\n\n' + fenced(item['response']) + '\n'
    else:
        pairs = known_pairs(root, key, state, relative, own)
        pair = [item['ident'], item['answer']]
        found = duplicate(pairs, *pair)
        update = {'keys': (pairs + ([] if found else [pair]))[-TURN_KEYS:]}
        heading = lambda number: f'\n## Turn {number} — {item["time"]}\n'
        body = (f'<!-- turn:I={pair[0]};R={pair[1]} -->\n\n### {item["label"]}\n\n' + fenced(item['prompt'])
                + '\n\n### Response\n\n' + fenced(item['response']) + '\n')
    event = 'SubagentStop' if item['kind'] == 'subagent' else 'Stop'
    if not found:
        count = len(own)
        when = brain.day()
        if text and len(text) + len(heading(count + 1)) + len(body) > brain.RECORD_LIMIT:
            relative = next_part(relative)
            target = brain.scoped(root, relative, area='memory/sessions')
            text, count = '', 0
        if not text:
            text = record_header(root, project, relative, key, when, run)
        else:
            fields = dict(status='recorded', updated=when)  # New sections are uncompiled.
            if run and brain.frontmatter(text).get('run') != run:
                fields['run'] = run
            text = brain.set_fields(text, **fields)
        brain.atomic_write(target, text.rstrip('\n') + '\n' + heading(count + 1) + body)
    # Navigation is ensured before the entry is acknowledged (state, receipt, spool deletion), on the duplicate path
    # too: a retry after a failure between these writes completes the links instead of leaving them missing.
    if target.is_file():
        link_record(root, project, relative, latest=not found)
    remember_record(state_path, relative, **update)
    receipt(root, project, event, path=relative)
    return relative


def names_record(root, plain):
    """True when a pointer line (its code-free form) names an existing record file."""
    target = brain.pointer_target(plain)
    try:
        return bool(target) and brain.scoped(root, brain.link_relative(root, target), area='memory/sessions').is_file()
    except ValueError:
        return False


def link_record(root, project, relative, latest=True):
    """Ensure every navigation link to a record part; each step is idempotent, so a retry completes what a failure
    left out. The project's Latest automatic record pointer moves to this part for a new section; a retry sets it
    only when it is missing or names no existing file, never moving a valid pointer. Then the records index line,
    its project-note link, and index.md."""
    when = brain.day()
    part = brain.RECORD_NAME.fullmatch(Path(relative).name)[2]
    label = Path(relative).name[:10] + ' conversation' + (f' part {part}' if part else '')
    project_path = brain.scoped(root, project, area='memory/projects')
    old = project_path.read_text(encoding='utf-8')
    pointer = f'Latest automatic record: [[{brain.link_target(root, relative)}|{label}]].'
    # The pointer is the first such line outside code; examples in fenced or inline code are never rewritten.
    current = brain.body_lines(old, 'Latest automatic record:')[:1]
    if latest or not current or not names_record(root, current[0][2]):
        note = brain.stamp(brain.set_body_line(old, 'Latest automatic record:', pointer), when).rstrip() + '\n'
        if note != old:
            brain.atomic_write(project_path, note)
    add_record(root, project, relative, when, label)


def remember_record(state_path, relative, keys=None, marks=None):
    """Track this session's record files (current part last) so its prompts do not retrieve them back,
    and its recent [I, R] pairs and subagent marks so a retry needs no reads of older parts."""
    state = brain.load_json(state_path, {})
    before = dict(state)
    state['records'] = [r for r in state.get('records', []) if r != relative] + [relative]
    state['record'] = relative
    if keys is not None:
        state['turn_keys'] = keys
    if marks is not None:
        state['subagent_marks'] = marks
    if state != before:
        brain.save_json(state_path, state)


def records_index(project):
    """One per-project list of automatic records keeps index.md and log.md concise."""
    # Mirroring the project path keeps nested notes (a/b.md vs a--b.md) from sharing a list.
    return 'memory/sessions/records/' + project[len('memory/projects/'):]


def add_record(root, project, relative, when, label):
    """Ensure the records index lists this part and is linked from the project note and index.md. Each step is
    checked on its own (no early return), so a retry after a failure in any of them completes the rest."""
    index = records_index(project)
    path = brain.scoped(root, index, area='memory/sessions')
    line = f'- [[{brain.link_target(root, relative)}|{label}]]\n'
    if not path.is_file():
        # Logged first: a failure before the index exists repeats the line on retry rather than losing it.
        brain.append_log(root, f'Created automatic records index [[{brain.link_target(root, index)}|{Path(project).stem}]]', when)
        # The Project line scopes this note to its own project in matching_notes.
        brain.atomic_write(path, f'---\ncreated: {when}\nupdated: {when}\nstatus: active\n---\n'
                                 f'# Automatic records — {Path(project).stem}\n\n'
                                 f'Project: [[{brain.link_target(root, project)}|Project state]].\n\n'
                                 'Automatic conversation records for this project, one per session part, oldest first. '
                                 'They are attributed excerpts, not verified facts.\n\n' + line)
    else:
        text = path.read_text(encoding='utf-8')
        if line not in text:
            brain.atomic_write(path, brain.stamp(text, when).rstrip() + '\n' + line)
    project_path = brain.scoped(root, project, area='memory/projects')
    note = project_path.read_text(encoding='utf-8')
    if not brain.body_lines(note, 'Automatic records:'):  # A quoted example in code is not the link.
        brain.atomic_write(project_path, note.rstrip() + f'\n\nAutomatic records: [[{brain.link_target(root, index)}|All automatic records]].\n')
    brain.add_index(root, index, Path(project).stem + ' automatic records')
