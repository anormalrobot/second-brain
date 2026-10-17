#!/usr/bin/env python3
"""Shared run boards for agent orchestration, and the spool that keeps hook writes while the vault lock is busy.

A run's source of truth is .brain/runs/RUN.json; memory/runs/RUN.md is regenerated from it on every change.
Every mutation happens under the vault writer lock, so claims are atomic across processes.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import time
import uuid

try:
    from . import brain, records
    from .redact import bounded, excerpt, oneline
except ImportError:
    import brain
    import records
    from redact import bounded, excerpt, oneline

NAME = re.compile(r'[A-Za-z0-9._:-]{1,64}')  # Agent names and task ids.
POSTED = ('in_progress', 'blocked', 'done', 'failed')
TITLE_LIMIT, GOAL_LIMIT, NOTE_LIMIT, LINE_LIMIT = 200, 2000, 4000, 300
ACTIVITY_SHOWN = 50
ACTIVITY_KEEP, HOOK_IDS = 2000, 5000  # Bounded run state keeps every apply cheap; older lines are only counted.
LEASE_MINUTES, LEASE_MAX = 60, 7 * 24 * 60
SUMMARY_LIMIT = 1500
SPOOL_LIMIT = 20  # Items attempted per drain; the time budget below also bounds a drain inside a 10 s hook.
SPOOL_SECONDS = 2.0
SPOOL_MAX_AGE = 7 * 86400
# Free text is rendered inert: no links, embeds, code, HTML, comments, tables, or headings can be injected.
ESCAPES = {c: f'&#{ord(c)};' for c in '[]|`%~#<>\\'}
ESCAPES['&'] = '&amp;'


def inline(text):
    return ''.join(ESCAPES.get(c, c) for c in ' '.join(str(text).split()))


def link_label(text, fallback):
    return ' '.join(re.sub(r'[^\w .,:;()/+-]+', ' ', text).split())[:80] or fallback


def check_name(value, what):
    if not isinstance(value, str) or not NAME.fullmatch(value) or excerpt(value) != value:
        raise ValueError(what + ' must be 1-64 characters from A-Z a-z 0-9 . _ : - and not look like a credential')
    return value


def state_path(root, run):
    if not isinstance(run, str) or not brain.RUN_ID.fullmatch(run):
        raise ValueError('Invalid run id; use the id that board open returned')
    return brain.internal(root, '.brain/runs/' + run + '.json')


def pointer(root, project):
    return brain.internal(root, '.brain/automation/active-run-' + brain.digest(project.encode())[:24] + '.json')


def load(root, run, project=None):
    path = state_path(root, run)
    if not path.is_file():
        raise ValueError('Unknown run: ' + run)
    data = brain.load_json(path, {})
    if not isinstance(data, dict) or data.get('schema_version') != 1 or data.get('run') != run:
        raise ValueError('Unsupported run state: ' + run)
    if project and data.get('project') != project:
        raise ValueError('Run belongs to another project: ' + run)
    return data


def pointed(root, project):
    """The run id the project's active-run pointer names (it may be closed if a close was interrupted), or None."""
    try:
        run = brain.load_json(pointer(root, project), {}).get('run')
    except (ValueError, OSError, AttributeError):
        return None
    return run if isinstance(run, str) and brain.RUN_ID.fullmatch(run) else None


def active(root, project):
    """The project's active run state, or None."""
    try:
        data = load(root, pointed(root, project), project)
    except (ValueError, OSError, AttributeError):
        return None
    return data if data['status'] == 'active' else None


def env_run(root, project):
    """(state, warning) for env SECOND_BRAIN_RUN: an active run of this project, else None and a one-line warning.

    A closed run counts as unknown: workers must not post into a closed board.
    """
    value = os.environ.get('SECOND_BRAIN_RUN', '')
    if not value:
        return None, None
    try:
        data = load(root, value, project)
        if data['status'] == 'active':
            return data, None
        problem = 'is closed'
    except (ValueError, OSError):
        problem = 'does not name a run of this project'
    shown = value if brain.RUN_ID.fullmatch(value) else '(invalid id)'
    return None, f'second-brain warning: SECOND_BRAIN_RUN={shown} {problem}; continuing without a run board.'


def hook_runs(root, project, board=True):
    """(worker, board, warning), the one run resolution every hook uses.

    worker is the run env SECOND_BRAIN_RUN selects (it tags the session record); board is where results go: the
    worker run, else the project's active run. An explicit SECOND_BRAIN_RUN that is unknown, closed, or another
    project's suppresses the active-run fallback everywhere. board=False skips looking up the active run.
    """
    worker, warning = env_run(root, project)
    if worker or warning:
        return worker, worker, warning
    return None, active(root, project) if board else None, None


def resolve(root, project, run=None):
    """--run, then env SECOND_BRAIN_RUN, then the project's active run."""
    run = run or os.environ.get('SECOND_BRAIN_RUN')
    if run:
        return load(root, run, project)
    data = active(root, project)
    if not data:
        raise ValueError('No active run for this project; open one with board open or pass --run RUN')
    return data


def require_open(data):
    if data['status'] != 'active':
        raise ValueError('Run is closed: ' + data['run'])


def find(data, task_id):
    task = next((t for t in data['tasks'] if t['id'] == task_id), None)
    if not task:
        raise ValueError('Unknown task: ' + str(task_id)[:64])
    return task


def log(data, who, text, ident=None):
    data['activity'].append({'at': brain.timestamp(), 'who': who, 'text': oneline(text, LINE_LIMIT)})
    over = len(data['activity']) - ACTIVITY_KEEP
    if over > 0:
        del data['activity'][:over]
        data['activity_dropped'] = data.get('activity_dropped', 0) + over
    if ident:  # Hook lines (joins, subagent results) stay idempotent after their text is dropped.
        data['hook_ids'] = (data.get('hook_ids', []) + [ident])[-HOOK_IDS:]


def render(root, data):
    run, tasks, shown = data['run'], data['tasks'], data['activity'][-ACTIVITY_SHOWN:]
    # Pure: everything comes from run state (updated_day is stamped by save), so the note can be re-rendered to match.
    out = (f'---\ncreated: {data["day"]}\nupdated: {max(data["day"], data.get("updated_day") or data["day"])}\n'
           f'status: {data["status"]}\nrun: {run}\n---\n'
           f'# Run — {inline(data["title"])}\n\n'
           f'Project: [[{brain.link_target(root, data["project"])}|Project state]].\n\n'
           '## Goal\n\n' + (inline(data['goal']) or 'Not stated.') + '\n')
    if data.get('summary'):
        out += '\n## Summary\n\n' + records.fenced(data['summary']) + '\n'
    out += '\n## Tasks\n\n'
    if tasks:
        out += '| id | title | status | owner | lease expiry | depends |\n| --- | --- | --- | --- | --- | --- |\n'
        for t in tasks:
            lease = t['lease_until'] if t['lease_until'] and t['status'] not in ('done', 'failed') else '—'
            out += (f'| {t["id"]} | {inline(t["title"])} | {t["status"]} | {t["owner"] or "—"} | {lease} | '
                    f'{", ".join(t["depends"]) or "—"} |\n')
    else:
        out += 'No tasks yet.\n'
    out += '\n## Activity\n\n'
    older, dropped = len(data['activity']) - len(shown), data.get('activity_dropped', 0)
    out += f'{older} older entries in run state' if older else ''
    out += ('; ' if older else '') + f'{dropped} earliest entries no longer kept' if dropped else ''
    out += '.\n\n' if older or dropped else ''
    out += ''.join(f'- {e["at"]} — ' + (inline(e['who']) + ': ' if e['who'] else '') + inline(e['text']) + '\n' for e in shown)
    out += '\n## Results\n\n'
    done = [t for t in tasks if t['status'] == 'done']
    for t in done:
        links = ', '.join(brain.evidence_link(root, p) for p in t['evidence']) or 'none'
        out += (f'### {t["id"]} — {inline(t["title"])}\n\nDone by {t["owner"]} at {t["done_at"]}. Evidence: {links}.\n\n'
                + records.fenced(t['result']) + '\n\n')
    out += '' if done else 'No completed tasks yet.\n\n'
    return out + f'Generated from `.brain/runs/{run}.json`; change it only through `second-brain run board` commands.\n'


def save(root, data):
    data.update(updated=brain.timestamp(), updated_day=brain.day())
    brain.save_json(state_path(root, data['run']), data)
    sync_note(root, data)


def sync_note(root, data):
    """Make memory/runs/RUN.md match run state. Rendering is pure, so this never adds activity; every board path
    calls it before returning early, so a note write that failed after its JSON was saved is repaired on retry."""
    path = brain.scoped(root, brain.run_note(data['run']), area='memory/runs')
    text = render(root, data)
    if not path.is_file() or path.read_text(encoding='utf-8') != text:
        brain.atomic_write(path, text)


def locked_run(root, project, run=None):
    """resolve() for a board command holding the lock: the note is brought in line with run state first, so a
    command that then refuses (closed run, conflict, bad task) still leaves an up-to-date note."""
    data = resolve(root, project, run)
    sync_note(root, data)
    return data


def summary(data, limit=SUMMARY_LIMIT):
    """A compact board view for agent context: header, every task that fits, then recent activity."""
    head = f'Run {data["run"]} ({data["status"]}): {oneline(data["title"], TITLE_LIMIT)}\n'
    head += f'Goal: {oneline(data["goal"], 300)}\n' if data['goal'] else ''
    lines = []
    for t in data['tasks']:
        state = t['status'] + (f', owner {t["owner"]}' if t['owner'] else '')
        state += f', lease until {t["lease_until"]}' if t['lease_until'] and t['status'] not in ('done', 'failed') else ''
        lines.append(f'- {t["id"]} [{state}] {oneline(t["title"], 120)}'
                     + (f' (depends: {", ".join(t["depends"])})' if t['depends'] else '') + '\n')
    recent = [f'- {e["at"]} {e["who"] + ": " if e["who"] else ""}{oneline(e["text"], 160)}\n' for e in data['activity'][-5:]]
    out = head + ('Tasks:\n' if lines else 'Tasks: none yet.\n')
    for number, line in enumerate(lines):
        more = f'[{len(lines) - number} more tasks; run board show]\n'
        if len(out) + len(line) + len(more) > limit:
            return (out + more)[:limit]
        out += line
    if recent and len(out) + len('Recent activity:\n') + len(recent[-1]) <= limit:
        out += 'Recent activity:\n'
        for line in reversed(recent):  # Newest lines first, as many as fit.
            if len(out) + len(line) > limit:
                break
            out += line
    return out[:limit]


def usage(command, run):
    """Two lines telling an agent how to claim and post with the bundled command."""
    return (f'Run board {run}: claim a task before working on it, then post progress and results; keep one --agent '
            f'NAME (1-64 of A-Z a-z 0-9 . _ : -). Bundled command (second-brain below): {command}\n'
            f'second-brain run board claim TASK --agent NAME --run {run}; second-brain run board post TASK --agent NAME '
            f'--run {run} --status in_progress|blocked|done|failed --note "..." [--evidence VAULT_PATH ...]\n')


def set_run_pointer(root, project, line):
    """Set the project's Active run: / Latest run: line (outside code); an unchanged line writes nothing."""
    path = brain.scoped(root, project, area='memory/projects')
    old = path.read_text(encoding='utf-8')
    text = brain.set_body_line(old, ('Active run:', 'Latest run:'), line)
    if text != old:
        brain.atomic_write(path, brain.stamp(text, brain.day()))


def run_link(root, data):
    return f'[[{brain.link_target(root, brain.run_note(data["run"]))}|{link_label(data["title"], data["run"])}]]'


def open_run(root, project, title, goal=''):
    root = Path(root).resolve()
    if not brain.scoped(root, project, area='memory/projects').is_file():
        raise ValueError('Project note does not exist')
    title, goal = oneline(title, TITLE_LIMIT), oneline(goal or '', GOAL_LIMIT)
    if not title:
        raise ValueError('A run title is required')
    with brain.writer(root):
        current = active(root, project)
        if current:
            raise ValueError(f'Run {current["run"]} is still active; close it first, or pass --run to use it')
        slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:32].strip('-') or 'run'
        run = f'{brain.day()}-{slug}-{uuid.uuid4().hex[:6]}'
        while state_path(root, run).exists():
            run = f'{brain.day()}-{slug}-{uuid.uuid4().hex[:6]}'
        stamp = brain.timestamp()
        data = {'schema_version': 1, 'run': run, 'project': project, 'title': title, 'goal': goal, 'status': 'active',
                'created': stamp, 'day': brain.day(), 'updated': stamp, 'closed': None, 'closed_day': None,
                'summary': '', 'tasks': [], 'activity': []}
        log(data, 'board', 'opened the run: ' + title)
        save(root, data)
        brain.save_json(pointer(root, project), {'run': run, 'project': project})
        brain.add_index(root, brain.run_note(run), 'Run: ' + link_label(title, run))
        set_run_pointer(root, project, f'Active run: {run_link(root, data)}.')
    return {'run': run, 'path': brain.run_note(run), 'status': 'active'}


def show(root, project, run=None):
    root = Path(root).resolve()
    data = resolve(root, project, run)
    return {'run': data['run'], 'path': brain.run_note(data['run']), 'status': data['status'], 'title': data['title'],
            'goal': data['goal'], 'summary': data['summary'], 'tasks': data['tasks'],
            'activity': data['activity'][-ACTIVITY_SHOWN:], 'older_activity': max(0, len(data['activity']) - ACTIVITY_SHOWN),
            'activity_dropped': data.get('activity_dropped', 0),
            'queued': brain.queued_writes(root, project)}  # show is read-only: queued hook writes are not applied.


def add_task(root, project, title, task_id=None, depends=(), run=None):
    root = Path(root).resolve()
    title = oneline(title, TITLE_LIMIT)
    if not title:
        raise ValueError('A task title is required')
    if task_id is not None:
        check_name(task_id, 'Task id')
    with brain.writer(root):
        data = locked_run(root, project, run)
        require_open(data)
        ids = {t['id'] for t in data['tasks']}
        if task_id in ids:
            raise ValueError('Task id already exists: ' + task_id)
        number = len(ids) + 1
        while task_id is None and f't{number}' in ids:
            number += 1
        task_id = task_id or f't{number}'
        for name in depends:
            if name not in ids:
                raise ValueError('Unknown dependency: ' + str(name)[:64])
        task = {'id': task_id, 'title': title, 'status': 'open', 'owner': None, 'lease_until': None, 'lease_minutes': None,
                'depends': list(dict.fromkeys(depends)), 'note': '', 'result': '', 'evidence': [], 'done_at': None,
                'updated': brain.timestamp()}
        data['tasks'].append(task)
        log(data, 'board', f'added {task_id}: {title}')
        save(root, data)
    return {'run': data['run'], 'task': task}


def take(data, task, agent, minutes, now):
    """Claim rules shared by claim and a post's implicit claim of an unclaimed task; the caller holds the lock."""
    if task['status'] == 'done':
        raise ValueError(f'Task {task["id"]} is done')
    until = task['lease_until']
    if task['owner'] and task['owner'] != agent and until and datetime.fromisoformat(until) > now:
        raise ValueError(f'Task {task["id"]} is claimed by {task["owner"]} until {until}')
    waiting = [d for d in task['depends'] if find(data, d)['status'] != 'done']
    if waiting:
        raise ValueError(f'Task {task["id"]} waits for unfinished dependencies: ' + ', '.join(waiting))
    if task['owner'] != agent or task['status'] in ('open', 'failed'):
        task['status'] = 'claimed'
    task.update(owner=agent, lease_minutes=minutes,
                lease_until=(now + timedelta(minutes=minutes)).isoformat(timespec='seconds'))


def claim(root, project, task_id, agent, minutes=LEASE_MINUTES, run=None):
    root = Path(root).resolve()
    check_name(agent, 'Agent name')
    if not isinstance(minutes, int) or not 1 <= minutes <= LEASE_MAX:
        raise ValueError(f'Lease minutes must be 1-{LEASE_MAX}')
    with brain.writer(root):
        data = locked_run(root, project, run)
        require_open(data)
        task = find(data, task_id)
        take(data, task, agent, minutes, datetime.now(timezone.utc))
        task['updated'] = brain.timestamp()
        log(data, agent, f'claimed {task["id"]} (lease until {task["lease_until"]})')
        save(root, data)
    return {'run': data['run'], 'task': task}


def evidence_paths(root, paths):
    found = []
    for name in paths:
        path = brain.scoped(root, name)
        if not path.is_file():
            raise ValueError('Evidence does not exist: ' + str(name)[:200])
        relative = path.relative_to(root).as_posix()
        brain.evidence_link(root, relative)  # Refuses names whose wikilink would not resolve to this exact file.
        found.append(relative)
    return found


def post(root, project, task_id, agent, status, note, evidence=(), run=None):
    root = Path(root).resolve()
    check_name(agent, 'Agent name')
    if status not in POSTED:
        raise ValueError('Status must be one of: ' + ', '.join(POSTED))
    note = bounded(note or '', NOTE_LIMIT).strip()
    if not note:
        raise ValueError('A note is required')
    evidence = evidence_paths(root, evidence)
    with brain.writer(root):
        data = locked_run(root, project, run)
        require_open(data)
        task = find(data, task_id)
        now = datetime.now(timezone.utc)
        if task['owner'] and task['owner'] != agent:  # Even after its lease expires; take over with claim.
            raise ValueError(f'Task {task["id"]} is claimed by {task["owner"]}; only the claimant posts '
                             '(claim it first to take over an expired lease)')
        if not task['owner']:
            take(data, task, agent, task['lease_minutes'] or LEASE_MINUTES, now)  # An unclaimed task is claimed implicitly.
        if task['status'] == 'done':
            raise ValueError(f'Task {task["id"]} is done')
        stamp = brain.timestamp()
        task.update(status=status, note=note, evidence=list(dict.fromkeys(task['evidence'] + evidence)), updated=stamp)
        if status == 'done':
            task.update(result=note, done_at=stamp)
        # Progress renews the lease; a finished task holds none, and a failed one is unclaimed so anyone may retry it.
        task['lease_until'] = (None if status in ('done', 'failed') else
                               (now + timedelta(minutes=task['lease_minutes'] or LEASE_MINUTES)).isoformat(timespec='seconds'))
        if status == 'failed':
            task['owner'] = None
        log(data, agent, f'posted {task["id"]} {status}: {note}')
        save(root, data)
    return {'run': data['run'], 'task': task}


def release(root, project, task_id, agent, run=None):
    root = Path(root).resolve()
    check_name(agent, 'Agent name')
    with brain.writer(root):
        data = locked_run(root, project, run)
        require_open(data)
        task = find(data, task_id)
        if task['owner'] != agent or task['status'] in ('open', 'done', 'failed'):
            raise ValueError(f'Only the current claimant can release task {task["id"]}')
        task.update(owner=None, lease_until=None, status='open', updated=brain.timestamp())
        log(data, agent, f'released {task["id"]}')
        save(root, data)
    return {'run': data['run'], 'task': task}


def close_run(root, project, summary_text='', run=None):
    """Close a run. Closing an already-closed run completes what an interrupted close left out (the summary of the
    first close stays): every step after the JSON save is an idempotent ensure."""
    root = Path(root).resolve()
    summary_text = bounded(summary_text or '', NOTE_LIMIT).strip()
    with brain.writer(root):
        # After an interrupted close the pointer still names the (now closed) run, so a plain retry finds it.
        data = locked_run(root, project, run or os.environ.get('SECOND_BRAIN_RUN') or pointed(root, project))
        again = data['status'] == 'closed'
        if not again:
            data.update(status='closed', closed=brain.timestamp(), closed_day=brain.day(), summary=summary_text)
            log(data, 'board', 'closed the run' + (': ' + summary_text if summary_text else ''))
            save(root, data)
        link = '[[' + brain.link_target(root, brain.run_note(data['run'])) + '|'
        # A repeated close never takes the project's run line from a run opened (or closed) since.
        note = brain.scoped(root, project, area='memory/projects').read_text(encoding='utf-8')
        current = brain.body_lines(note, ('Active run:', 'Latest run:'))[:1]
        if not again or not current or link in current[0][2]:
            set_run_pointer(root, project, f'Latest run: {run_link(root, data)}.')
        logged = (root / 'log.md').read_text(encoding='utf-8') if (root / 'log.md').is_file() else ''
        if not (again and 'Closed run ' + link in logged):
            done = sum(t['status'] == 'done' for t in data['tasks'])
            brain.append_log(root, f'Closed run {run_link(root, data)}: {done} of {len(data["tasks"])} tasks done', brain.day())
        # Last: until every step above succeeded, the pointer lets a plain `board close` find this run again.
        if pointed(root, project) == data['run']:
            pointer(root, project).unlink()
    return {'run': data['run'], 'path': brain.run_note(data['run']), 'status': 'closed', **({'already_closed': True} if again else {})}


def note_activity(root, run, project, text, ident):
    """Add one hook activity line to an active run, once per ident; the caller holds the lock."""
    try:
        data = load(root, run, project)
    except (ValueError, OSError):
        return False
    if data['status'] != 'active' or ident in data.get('hook_ids', []):
        sync_note(root, data)  # Already applied (or closed): only repair a note left stale by a failed write.
        return False
    log(data, '', text, ident)
    save(root, data)
    return True


def join(root, run, project, key):
    return note_activity(root, run, project, f'session {key} joined', 'join:' + key)


def apply(root, item):
    """Apply one hook item (a turn, a subagent result, or a session joining a run); the caller holds the lock.

    Every step is idempotent, so a spooled item applied twice, or after a partial failure, changes nothing more.
    """
    root = Path(root)
    kind = item.get('kind')
    run = item.get('run')
    if run is not None and not (isinstance(run, str) and brain.RUN_ID.fullmatch(run)
                                and (root / brain.run_note(run)).is_file()):
        run = None  # A record links only an existing board.
    if kind == 'join':
        if not run or not isinstance(item.get('key'), str) or not records.HEX.fullmatch(item['key']):
            raise ValueError('Malformed join item')
        join(root, run, records.project_note(root, item.get('project')), item['key'])
        return None
    relative = records.write_entry(root, dict(item, run=run))
    if run:
        join(root, run, item['project'], item['key'])
    board = item.get('board')
    if kind == 'subagent' and isinstance(board, str) and brain.RUN_ID.fullmatch(board):
        note_activity(root, board, item['project'], str(item.get('activity', ''))[:LINE_LIMIT], 's:' + item['mark'])
    return relative


def spool_names(root):
    """Queued spool file names, oldest first; none when the folder is missing or unreadable."""
    try:
        return sorted(name for name in os.listdir(brain.internal(root, brain.SPOOL)) if brain.SPOOL_NAME.fullmatch(name))
    except (OSError, ValueError):
        return []  # A symlinked spool folder is never read; spool() refuses to write into it.


def queued(root, key):
    """True while this session has spooled items; its next item must queue behind them to keep their order."""
    return any(brain.SPOOL_NAME.fullmatch(name)[2] == key for name in spool_names(root))


def spool(root, item):
    """Keep an already-redacted hook item for a later lock holder: a unique name, written atomically, no lock."""
    now, key = time.time_ns(), str(item.get('key', ''))
    key = key if records.HEX.fullmatch(key) else '0' * 24
    brain.save_json(brain.internal(root, f'{brain.SPOOL}/{now:020d}-{key}-{uuid.uuid4().hex}.json'),
                    dict(item, spooled=brain.timestamp(), spooled_ns=now))


def failed(folder, name, item, error=None):
    """Note a failed attempt in the item itself, keeping its name and so its place in the queue; move it to failed/
    once older than SPOOL_MAX_AGE, or at once when it is unreadable."""
    first = item.get('spooled_ns') if isinstance(item, dict) else None
    first = first if isinstance(first, int) else int(name[:20])
    try:
        if not isinstance(item, dict) or time.time_ns() - first > SPOOL_MAX_AGE * 10 ** 9:
            if (folder / 'failed').is_symlink():
                raise ValueError('Spool failed/ folder must not be a symlink')
            (folder / 'failed').mkdir(exist_ok=True)
            os.replace(folder / name, folder / 'failed' / name)
        elif error is not None:
            brain.save_json(folder / name, dict(item, attempts=int(item.get('attempts') or 0) + 1,
                                                error=str(error)[:300], failed_at=brain.timestamp()))
    except (OSError, TypeError, ValueError):
        pass  # Left as it was; the next drain retries it.


def drain(root, allowed=None, seconds=SPOOL_SECONDS, limit=SPOOL_LIMIT):
    """Apply spooled items oldest first; the caller holds the writer lock and has already persisted its own item.
    Returns True when every queued item was attempted or had to wait.

    allowed is the set of project notes bound to this vault with automatic memory on. A session's items are never
    reordered: once one fails, or must wait because its project is not allowed, that session's later items wait for
    a later drain, while other sessions continue. At most `limit` items are attempted (failures included), and the
    deadline is checked before each one; each apply is bounded (a record part, at most RECOVER_PARTS older parts,
    and run state capped at ACTIVITY_KEEP lines).
    """
    deadline = time.monotonic() + seconds
    try:
        folder = brain.internal(root, brain.SPOOL)
    except ValueError:
        return True  # Nothing can be applied from a symlinked spool.
    attempts, waiting = 0, set()
    for name in spool_names(root):
        session = brain.SPOOL_NAME.fullmatch(name)[2]
        if session in waiting:
            continue
        if attempts >= limit or time.monotonic() >= deadline:
            return False
        path = folder / name
        try:
            if path.is_symlink():
                raise ValueError('Spool items cannot be symlinks')
            item = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(item, dict):
                raise ValueError('Spool items are objects')
        except FileNotFoundError:
            continue
        except (ValueError, OSError):
            failed(folder, name, None)  # Unreadable: it can never be applied.
            continue
        if allowed is not None and item.get('project') not in allowed:
            waiting.add(session)  # Its project is unbound or paused: not an attempt; it expires after SPOOL_MAX_AGE.
            failed(folder, name, item)
            continue
        attempts += 1
        try:
            apply(root, item)
        except (ValueError, OSError, KeyError, TypeError, AttributeError) as exc:
            waiting.add(session)
            failed(folder, name, item, exc)
            continue
        path.unlink(missing_ok=True)
    return True


def discard(root, project):
    """Drop a paused project's spooled items, like its pending session state; the caller holds the lock."""
    try:
        folder = brain.internal(root, brain.SPOOL)
    except ValueError:
        return
    for name in spool_names(root):
        path = folder / name
        try:
            if not path.is_symlink() and json.loads(path.read_text(encoding='utf-8')).get('project') == project:
                path.unlink()
        except (ValueError, OSError, AttributeError):
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(prog='second-brain run board', description='Shared run board for agent orchestration. '
                                     'Run resolution: --run, then env SECOND_BRAIN_RUN, then the project\'s active run.',
                                     allow_abbrev=False)
    parser.add_argument('--root', type=Path, required=True, help=argparse.SUPPRESS)
    parser.add_argument('--project', required=True, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest='command', required=True)
    add = lambda name, text: sub.add_parser(name, help=text, allow_abbrev=False)
    p = add('open', 'Open a run and make it the project\'s active run'); p.add_argument('--title', required=True); p.add_argument('--goal', default='')
    p = add('show', 'Show a run'); p.add_argument('--run')
    p = add('add', 'Add a task'); p.add_argument('--title', required=True); p.add_argument('--id'); p.add_argument('--depends', nargs='+', action='extend', default=[], metavar='ID'); p.add_argument('--run')
    p = add('claim', 'Claim a task with a lease'); p.add_argument('task'); p.add_argument('--agent', required=True); p.add_argument('--lease-minutes', type=int, default=LEASE_MINUTES); p.add_argument('--run')
    p = add('post', 'Post progress or a result as the claimant'); p.add_argument('task'); p.add_argument('--agent', required=True); p.add_argument('--status', required=True, choices=POSTED); p.add_argument('--note', required=True); p.add_argument('--evidence', nargs='+', action='extend', default=[], metavar='PATH'); p.add_argument('--run')
    p = add('release', 'Release your claim'); p.add_argument('task'); p.add_argument('--agent', required=True); p.add_argument('--run')
    p = add('close', 'Close the run; on a closed run, complete an interrupted close'); p.add_argument('--summary', default=''); p.add_argument('--run')
    args = parser.parse_args(argv)
    root, project = args.root.resolve(), args.project
    try:
        if args.command == 'open': result = open_run(root, project, args.title, args.goal)
        elif args.command == 'show': result = show(root, project, args.run)
        elif args.command == 'add': result = add_task(root, project, args.title, args.id, args.depends, args.run)
        elif args.command == 'claim': result = claim(root, project, args.task, args.agent, args.lease_minutes, args.run)
        elif args.command == 'post': result = post(root, project, args.task, args.agent, args.status, args.note, args.evidence, args.run)
        elif args.command == 'release': result = release(root, project, args.task, args.agent, args.run)
        else: result = close_run(root, project, args.summary, args.run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
