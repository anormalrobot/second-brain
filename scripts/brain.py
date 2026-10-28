#!/usr/bin/env python3
"""Local, standard-library operations for the shared memory vault (Python 3.9+)."""
import argparse
from contextlib import contextmanager
from datetime import date, datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import tempfile
import time
import uuid
import zipfile

try:
    import fcntl
except ImportError:  # Windows: lock byte 0 with msvcrt instead.
    fcntl = None
    import msvcrt

try:
    from .check_vault import check, link_prefix, without_examples, wiki_target, wiki_relative
    from .redact import excerpt
except ImportError:
    from check_vault import check, link_prefix, without_examples, wiki_target, wiki_relative
    from redact import excerpt

VERSION = 1
LEDGER = 'inbox/source-ledger.json'
LOCK_WAIT = 2.5  # Seconds; hooks time out after 10 s, so contention waits only briefly.
SPOOL = '.brain/automation/spool'  # Hook writes that could not get the lock wait here (see runs.drain).
# Queue time in ns (names sort oldest first), the session key (to keep one session's order), and a unique id.
SPOOL_NAME = re.compile(r'(\d{20})-([0-9a-f]{24})-([0-9a-f]{32})\.json')
RUN_ID = re.compile(r'\d{4}-\d\d-\d\d-[a-z0-9-]{1,32}-[0-9a-f]{6}')  # DATE-slug-hex; safe as a file name.
# Durable run-board state that snapshots keep; every other .brain file (locks, spool, sessions, caches) is transient.
KEPT_STATE = re.compile(r'\.brain/(?:runs/' + RUN_ID.pattern + r'|automation/active-run-[0-9a-f]{24})\.json')
RECORD_NAME = re.compile(r'(\d{4}-\d\d-\d\d-auto-[0-9a-f]{24})(?:-([0-9]+))?\.md')
RECORD_TITLE = '# Automatic conversation record'
RECORD_LIMIT = 200000  # Characters per session record part; a longer session starts a -2, -3, ... part.
POINTERS = ('Latest checkpoint:', 'Latest handoff:', 'Latest automatic record:', 'Automatic records:', 'Active run:',
            'Latest run:')  # Navigation lines: they link notes but never cite them.
# A turn marker carries its id hash I (empty without a client id) and response hash R; the older single-key
# form stays readable, its key acting as an opaque I.
TURN_MARK = re.compile(r'<!-- turn:(?:([0-9a-f]{24})|I=([0-9a-f]{24})?;R=([0-9a-f]{24})) -->')
SUBAGENT_MARK = re.compile(r'<!-- subagent:([0-9a-f]{24}) -->')  # A subagent result section in its parent's record.
PROJECT_SECTIONS = ('Goal', 'Decisions', 'Status', 'Open questions', 'Next steps')
COMPILE_LIMIT = 8
SESSION_KEY = re.compile(r'[0-9a-f]{24}')  # Names one session's record files (memory_hooks.session_key).
SELF_COMPILE_AGE = 86400  # Seconds a self-compile marker waits for its session's next turn.
COMPILE_STEPS = [
    'Read each record from from_turn through through_turn: everything after the "## Turn {from_turn - 1}" section '
    '(from the top when from_turn is 1), including "## Subagent" result sections; from_turn is past through_turn '
    'when only subagent results are new. A legacy record is one turn. Read each closed run board in runs too: its '
    'results are agent claims with evidence links.',
    'Fold durable facts into curated notes: the project note sections (Goal, Decisions, Status, Open questions, '
    'Next steps); memory/profile.md only for preferences the user stated; memory/decisions/ for accepted '
    'decisions; wiki/topics/ for reusable cross-project knowledge.',
    'Records are attributed excerpts: separate user-stated facts, accepted decisions, proposals, and unverified '
    'assistant claims. Never promote an assistant claim to a fact.',
    'Cite every record you use with its wikilink (link), keep notes in English, update existing sections instead '
    'of appending duplicates, and mark outdated statements as superseded instead of deleting them.',
    'Link new notes from index.md or the project note, then run compile --done TOKEN --updated NOTE '
    '[--updated NOTE ...] [--skip RECORD ...]; every record and run must be cited or skipped (a run by its path); '
    'skip ones with no durable content. Do not ask the user.',
]
STOP_WORDS = set('the and for with this that from have what how can you our are was '
                 'will please project about your into use using bir ve bu ile için '
                 'olan olarak nasıl nedir bana daha'.split())
WORD = re.compile(r'[^\W_]{3,}')  # A note or query word: 3+ letters or digits, casefolded.
WORD_RUN = re.compile(r'[^\W_]+')  # sre's word test is str.isalnum, as in word_at.
QUERY_TERMS = 64  # Distinct terms per query, so a pasted log cannot multiply the per-note cost.
HEADING_LINES = re.compile(r'^#{1,2} [^\n]*', re.M)  # Cheap gate before the fence-aware outline().
ANSWER_LIMIT = 20000  # Characters of text per file-answer call; a longer answer belongs on several pages.
ANSWER_SLUG = re.compile(r'[a-z0-9][a-z0-9-]{0,63}')
SOURCES_HEADING = re.compile(r'## sources[ \t]*\n?')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def day():
    return datetime.now().astimezone().date().isoformat()


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding='utf-8'))


def save_json(path, data):
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def scoped(root, relative, *, area=None):
    root = Path(root).resolve()
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts:
        raise ValueError('Use a vault-relative path without parent traversal')
    path = (root / rel).resolve()
    if not path.is_relative_to(root) or (area and not path.is_relative_to(root / area)):
        raise ValueError('Path is outside the permitted vault area')
    if any(p.startswith('.') for p in rel.parts):
        raise ValueError('Hidden paths are not valid content targets')
    return path


def internal(root, relative):
    """root/relative for internal state under root/.brain (run state, pointers, session state, spool, receipts,
    batches, the lock). Every read, write, and delete of that state goes through here: traversal and any symlinked
    component (including .brain itself) are refused, so state can never be read from or written outside the vault."""
    root = Path(root).resolve()
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts or rel.parts[:1] != ('.brain',):
        raise ValueError('Internal state must be under .brain/: ' + str(relative)[:200])
    path = root
    for part in rel.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError('Internal state path must not be a symlink: ' + path.relative_to(root).as_posix())
    return path


def internal_files(root, folder, pattern='*'):
    """Regular files in an internal folder matching pattern, sorted; symlinked entries are skipped, and a missing
    folder has none. A symlinked folder raises ValueError (internal)."""
    path = internal(root, folder)
    return sorted(p for p in path.glob(pattern) if not p.is_symlink() and p.is_file()) if path.is_dir() else []


def lock_file(fd, lock):
    """Non-blocking exclusive lock; the kernel releases it if the holder dies."""
    try:
        if fcntl:
            fcntl.flock(fd, (fcntl.LOCK_EX | fcntl.LOCK_NB) if lock else fcntl.LOCK_UN)
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK if lock else msvcrt.LK_UNLCK, 1)
    except BlockingIOError:
        return False
    except OSError as exc:
        if fcntl or not lock or exc.errno not in (errno.EACCES, errno.EDEADLK):
            raise
        return False
    return True


def describe_lock(path):
    try:
        owner = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        owner = None
    owner = owner if isinstance(owner, dict) else {}
    detail = ', '.join(f'{k} {owner[k]}' for k in ('pid', 'hostname', 'started') if k in owner)
    return detail or 'owner unknown'


class LockBusy(ValueError):
    """Another process kept the vault writer lock past the wait."""


def acquire(root, wait=None):
    # An OS advisory lock needs no staleness rules. Legacy .brain/write.lock directories
    # left by plugin <= 0.3.0 are ignored, and this file is never deleted.
    path = internal(root, '.brain/write.flock')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o644)
    try:
        deadline = time.monotonic() + (LOCK_WAIT if wait is None else wait)
        while not lock_file(fd, True):
            if time.monotonic() >= deadline:
                raise LockBusy(f'Another writer is active ({describe_lock(path)}). '
                               f'Retry shortly; the lock is released when that process exits: {path}')
            time.sleep(0.05)
    except BaseException:
        os.close(fd)
        raise
    try:
        # Owner details only inform the error message above; the kernel lock is authoritative.
        owner = json.dumps({'pid': os.getpid(), 'hostname': socket.gethostname(), 'started': timestamp()})
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, owner.encode())
    except OSError:
        pass
    return fd


def release(fd):
    try:
        lock_file(fd, False)
    finally:
        os.close(fd)


@contextmanager
def writer(root, wait=None):
    fd = acquire(root, wait)
    try:
        yield
    finally:
        release(fd)


@contextmanager
def try_writer(root, wait=None):
    """Yield True while holding the writer lock, or False (holding nothing) when it stayed busy past the wait."""
    try:
        fd = acquire(root, wait)
    except LockBusy:
        fd = None
    try:
        yield fd is not None
    finally:
        if fd is not None:
            release(fd)


def load_ledger(root):
    ledger = load_json(Path(root) / LEDGER, {'schema_version': VERSION, 'sources': {}})
    if ledger.get('schema_version') != VERSION or not isinstance(ledger.get('sources'), dict):
        raise ValueError('Unsupported source ledger schema')
    return ledger


def scan(root):
    """Content identity deduplicates byte-identical sources, while paths track revisions."""
    root = Path(root).resolve()
    with writer(root):
        ledger = load_ledger(root)
        intake = load_json(root / 'inbox/transcript-manifest.json', {'sources': []})
        urls = {e['sha256']: e.get('video_url') for e in intake.get('sources', [])}
        for entry in ledger['sources'].values():
            entry['paths'] = []
        for path in sorted((root / 'raw').rglob('*')):
            if path.is_symlink():
                raise ValueError('Raw sources cannot be symlinks: ' + str(path))
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            data = path.read_bytes()
            key = digest(data)
            entry = ledger['sources'].setdefault(key, {
                'id': key, 'bytes': len(data), 'state': 'pending', 'paths': [],
                'seen_paths': [], 'attempts': [], 'outputs': {}, 'receipt': None,
                'first_seen': timestamp(), 'source_url': urls.get(key),
            })
            entry['paths'].append(relative)
            if relative not in entry['seen_paths']:
                entry['seen_paths'].append(relative)
        for key, entry in ledger['sources'].items():
            entry['previous_revisions'] = sorted(k for k, old in ledger['sources'].items()
                                                if k != key and not old['paths'] and
                                                set(old['seen_paths']) & set(entry['paths']))
        save_json(root / LEDGER, ledger)
    return coverage(root)


def resolve_entry(ledger, key):
    matches = [e for k, e in ledger['sources'].items() if k.startswith(key) or key in e['paths']]
    if len(matches) != 1:
        raise ValueError('Source ID/path must resolve to exactly one current entry')
    return matches[0]


def verify_raw(root, entry):
    if not entry['paths']:
        raise ValueError('Source has no current path; scan the vault first')
    for relative in entry['paths']:
        p = scoped(root, relative, area='raw')
        if not p.is_file() or digest(p.read_bytes()) != entry['id']:
            raise ValueError('Raw source changed or disappeared; scan before processing')


def outputs_valid(root, entry):
    return bool(entry['outputs']) and all(
        scoped(root, name).is_file() and digest(scoped(root, name).read_bytes()) == value
        for name, value in entry['outputs'].items()
    )


def coverage(root):
    root = Path(root).resolve()
    ledger = load_ledger(root)
    rows = []
    registered = set()
    for entry in ledger['sources'].values():
        registered.update(entry['paths'])
        if not entry['paths']:
            state = 'historical'
        else:
            try:
                verify_raw(root, entry)
                state = entry['state']
                if state == 'processed' and not outputs_valid(root, entry):
                    state = 'needs_review'
            except ValueError:
                state = 'source_changed'
        rows.append({'id': entry['id'], 'paths': entry['paths'], 'state': state})
    discovered = {p.relative_to(root).as_posix() for p in (root / 'raw').rglob('*') if p.is_file()}
    return {'sources': rows, 'unregistered_paths': sorted(discovered - registered),
            'complete': not (discovered - registered) and all(r['state'] in ('processed', 'historical') for r in rows)}


def begin(root, key):
    root = Path(root).resolve()
    with writer(root):
        ledger = load_ledger(root)
        entry = resolve_entry(ledger, key)
        verify_raw(root, entry)
        if entry['state'] == 'processed' and outputs_valid(root, entry):
            return {'state': 'already_processed', 'id': entry['id'], 'outputs': entry['outputs']}
        if entry['state'] == 'processing':
            raise ValueError('Processing lease exists; fail it explicitly before retrying')
        token = uuid.uuid4().hex
        entry.update(state='processing', token=token)
        entry['attempts'].append({'token': token, 'started': timestamp(), 'outcome': 'processing'})
        save_json(root / LEDGER, ledger)
        return {'id': entry['id'], 'token': token, 'state': 'processing'}


def finish(root, key, token, outputs=None, error=None):
    root = Path(root).resolve()
    with writer(root):
        ledger = load_ledger(root)
        entry = resolve_entry(ledger, key)
        if entry['state'] != 'processing' or token != entry.get('token'):
            raise ValueError('A matching active processing token is required')
        if error:
            entry.update(state='failed', error=error)
        else:
            verify_raw(root, entry)
            if not outputs:
                raise ValueError('At least one attributed output note is required')
            hashes = {}
            raw_links = {link_target(root, p) for p in entry['paths']}
            for relative in outputs:
                path = scoped(root, relative)
                if path.suffix != '.md' or not any(path.is_relative_to(root / d) for d in ('wiki', 'memory')):
                    raise ValueError('Output must be a wiki or memory Markdown note')
                if not (link_targets(path.read_text(encoding='utf-8')) & raw_links):
                    raise ValueError('Each output must link directly to a current raw source path')
                hashes[relative] = digest(path.read_bytes())
            issues, notes, links = note_issues(root, outputs)
            if issues:
                raise ValueError('Structural validation failed: ' + '; '.join(issues[:5]))
            entry.update(state='processed', outputs=hashes, error=None,
                         receipt={'checked_at': timestamp(), 'notes': notes, 'links': links,
                                  'scope': 'file structure and direct source links; not factual verification'})
        entry['attempts'][-1].update(outcome=entry['state'], ended=timestamp())
        entry.pop('token', None)
        save_json(root / LEDGER, ledger)
        return {'id': entry['id'], 'state': entry['state']}


def link_target(root, relative):
    """The wikilink target check_vault resolves to exactly this file: .md is dropped only when what remains has
    no extension of its own (api.v2.md keeps it)."""
    stem = relative[:-3] if relative.endswith('.md') else None
    target = stem if stem is not None and not Path(stem).suffix else relative
    prefix = link_prefix(root)
    return prefix + '/' + target if prefix else target


def evidence_link(root, relative):
    """[[target|name]] for a vault-relative evidence file; refuses names a wikilink cannot address exactly."""
    if re.search(r'[\[\]|#^\x00-\x1f]', relative):
        raise ValueError('Evidence path cannot be linked (it contains [ ] | # ^ or a control character): ' + relative[:200])
    if not Path(relative).suffix:
        raise ValueError('Evidence needs a file extension so its link resolves to that file: ' + relative[:200])
    return f'[[{link_target(root, relative)}|{Path(relative).name}]]'


def link_targets(text):
    """Wikilink targets outside code, without aliases or heading anchors."""
    return {m.replace('\\|', '|').split('|', 1)[0].split('#', 1)[0]
            for m in re.findall(r'\[\[([^\]]+)\]\]', without_examples(text))}


def linked_files(root, text, skip=(), vault=None):
    """(brain-relative paths of existing files that wikilinks outside code name, as check_vault resolves them;
    whether any names an existing file outside the managed folder). Lines starting with skip are ignored."""
    root = Path(root).resolve()
    vault = vault or (root.parent if link_prefix(root) else root)
    if skip:
        text = ''.join(line for line in text.splitlines(True) if not line.startswith(skip))
    found, outside = set(), False
    for target in link_targets(text):
        target = target.strip()
        if not target or '..' in Path(target).parts:
            continue
        path = (vault / (target if Path(target).suffix else target + '.md')).resolve()
        if Path(target).suffix and not path.is_file():
            path = (vault / (target + '.md')).resolve()  # A pre-0.4 link to a dotted note, as check_vault resolves it.
        if not path.is_relative_to(vault) or not path.is_file():
            continue
        if path.is_relative_to(root):
            found.add(path.relative_to(root).as_posix())
        else:
            outside = True
    return found, outside


def note_issues(root, relatives):
    """Structural issues of these notes plus global ones; unrelated broken notes elsewhere do not block."""
    issues, notes, links = check(root)
    names = {str(scoped(root, r).relative_to(root)) for r in relatives}
    return [i for i in issues if not re.match(r'.+?\.md:', i) or re.match(r'(.+?\.md):', i)[1] in names], notes, links


def frontmatter(text):
    match = re.match(r'\A---\n(.*?)\n---(?:\n|$)', text, re.S)
    return dict(re.findall(r'^([a-z_]+):[ \t]*([^\n]*)$', match[1], re.M)) if match else {}


def set_fields(text, **fields):
    """Replace or append frontmatter fields; the body is never touched."""
    match = re.match(r'\A---\n(.*?)\n---(?:\n|$)', text, re.S)
    if not match:
        raise ValueError('Note has no frontmatter')
    head = match[1]
    for key, value in fields.items():
        line = f'{key}: {value}'
        head, found = re.subn(r'^' + key + r':.*$', lambda _: line, head, count=1, flags=re.M)
        head += '' if found else '\n' + line
    return text[:match.start(1)] + head + text[match.end(1):]


def section_stubs(names):
    return ''.join(f'\n## {name}\n\nNone recorded yet.\n' for name in names)


def ensure_sections(path):
    """Append missing standard project headings; headings in code examples do not count, and the existing
    text stays a byte-for-byte prefix."""
    text = path.read_text(encoding='utf-8')
    plain = without_examples(text)
    missing = [s for s in PROJECT_SECTIONS if not re.search(r'^## ' + re.escape(s) + r'[ \t]*$', plain, re.M | re.I)]
    if missing:
        atomic_write(path, text + ('' if text.endswith('\n') or not text else '\n') + section_stubs(missing))
    return missing


def plain_lines(text):
    """(offset, line, plain) per line, where plain blanks fenced and inline code.

    without_examples maps each input line to exactly one output line, so offsets refer to the original text.
    """
    offset = 0
    for line, plain in zip(text.splitlines(keepends=True), without_examples(text).splitlines(keepends=True)):
        yield offset, line, plain
        offset += len(line)


def sections(text):
    """{heading casefold: body} of each level-2 section outside code; a repeated heading keeps its first body."""
    found, name, start = {}, None, 0
    for offset, raw, plain in plain_lines(text):
        if raw.startswith('#') and plain.startswith('#') and re.match(r'#{1,2} ', raw):
            if name is not None:
                found.setdefault(name, text[start:offset])
            name = raw[3:].strip().casefold() if raw.startswith('## ') else None
            start = offset + len(raw)
    if name is not None:
        found.setdefault(name, text[start:])
    return found


def body_start(text):
    match = re.match(r'\A---\n(.*?)\n---(?:\n|$)', text, re.S)
    return match.end() if match else 0


def body_lines(text, prefix):
    """(offset, line, plain) of each body line starting with prefix (a string or tuple) outside code and
    frontmatter. Navigation pointers (POINTERS, Last compiled:) are read and rewritten only through this."""
    start = body_start(text)
    return [(offset, raw, plain) for offset, raw, plain in plain_lines(text)
            if offset >= start and raw.startswith(prefix) and plain.startswith(prefix)]


def set_body_line(text, prefix, line):
    """Replace the first body line starting with prefix outside code and frontmatter, else append it."""
    for offset, raw, _ in body_lines(text, prefix)[:1]:
        return text[:offset] + line + raw[len(raw.rstrip('\r\n')):] + text[offset + len(raw):]
    return text.rstrip() + '\n\n' + line + '\n'


def drop_body_lines(text, prefix):
    """text without its body lines that start with prefix outside code and frontmatter."""
    drop = {offset for offset, _, _ in body_lines(text, prefix)}
    return ''.join(raw for offset, raw, _ in plain_lines(text) if offset not in drop) if drop else text


def stamp(text, when):
    """Set the frontmatter updated field; a note without frontmatter keeps none (its body is never touched)."""
    try:
        return set_fields(text, updated=when)
    except ValueError:
        return text


def link_relative(root, target):
    """The vault-relative file a wikilink target names (the inverse of link_target): .md is added when the target
    has no extension of its own, or when only target.md exists, as pre-0.4 links to a dotted note (my.app.md
    linked as my.app) name it; so owners and pointers resolve the same way in every scan."""
    relative = wiki_relative(root, target)
    if not Path(relative).suffix:
        return relative + '.md'
    if not (Path(root) / relative).is_file() and (Path(root) / (relative + '.md')).is_file():
        return relative + '.md'
    return relative


def pointer_target(line):
    """The wikilink target of a navigation line, ignoring links inside inline code; plain is the code-free line."""
    match = re.search(r'\[\[([^|\]]+)', line)
    return match[1] if match else None


OWNER = re.compile(r'^Project: \[\[([^|\]\n]+)\|', re.M)


def owner(text):
    """The project link target of a record's or run board's first Project: line outside code (the header), or None.

    Only the first 4096 characters are read: the header comes before any recorded excerpt."""
    match = OWNER.search(without_examples(text[:4096]))
    return match[1] if match else None


def project_targets(root, project):
    """Link targets that name this project note: the exact one, plus the pre-0.4 form (.md always dropped) that
    older records of a dotted note name, so they keep their owner."""
    return {link_target(root, project), wiki_target(root, project)}


def entries(text):
    """(offset, I, R) of each turn and subagent section in file order. R is '' for an old-style turn marker; a
    subagent section has I 's:' + its marker key and R ''. Fenced excerpts are skipped, so a quoted marker never
    counts.

    Fences follow without_examples exactly; cheap prefixes gate every regex because each Stop scans its part.
    """
    found, fence, previous, offset = [], None, None, 0
    for line in text.splitlines(keepends=True):
        bare = line.rstrip('\n')
        head = bare.lstrip(' ')
        indent = len(bare) - len(head) <= 3
        if fence:
            if indent and head.startswith(fence) and re.fullmatch(r' {0,3}' + re.escape(fence[0]) + '{' + str(len(fence)) + r',}\s*', bare):
                fence = None
            previous = None
        elif indent and head[:3] in ('```', '~~~'):
            fence, previous = re.match(r' {0,3}(`{3,}|~{3,})', bare)[1], None
        else:
            match = TURN_MARK.fullmatch(bare) if bare.startswith('<!-- turn:') and previous else None
            if match and previous[1].startswith('## Turn '):
                found.append((previous[0], match[1] or match[2] or '', match[3] or ''))
            match = SUBAGENT_MARK.fullmatch(bare) if bare.startswith('<!-- subagent:') and previous else None
            if match and previous[1].startswith('## Subagent '):
                found.append((previous[0], 's:' + match[1], ''))
            previous = (offset, bare)
        offset += len(line)
    return found


def turns(text):
    """(offset, I, R) of each turn section; subagent result sections are not turns."""
    return [entry for entry in entries(text) if not entry[1].startswith('s:')]


def turn_count(text):
    return len(turns(text)) if frontmatter(text).get('session') else 1  # A legacy record is one turn.


# Our own section layout; a fence is longer than any backtick run inside it, so the first closing match is real.
TURN_PARTS = re.compile(r'(## Turn [^\n]*\n<!-- turn:[^\n]* -->\n\n### [^\n]+\n\n)(`{3,})text\n(.*?)\n\2\n\n'
                        r'(### Response\n\n)(`{3,})text\n(.*?)\n\5\n*', re.S)
CLIPPED = '\n[excerpt truncated]'


def clip_turn(section, room):
    """A turn whose request and response excerpts share room, so a long request cannot hide the response."""
    match = TURN_PARTS.fullmatch(section)
    if not match:
        return section
    top, f1, request, middle, f2, response = match.groups()
    shell = lambda a, b: top + f1 + 'text\n' + a + '\n' + f1 + '\n\n' + middle + f2 + 'text\n' + b + '\n' + f2 + '\n'
    space = room - len(shell('', ''))
    if len(request) + len(response) <= space or space < 4 * len(CLIPPED):
        return section
    first = min(len(request), max(space // 2, space - len(response)))
    clip = lambda value, size: value if len(value) <= size else value[:size - len(CLIPPED)] + CLIPPED
    return shell(clip(request, first), clip(response, space - first))


def recent_turns(text, room):
    """A session record's header and newest whole turns within room: continuity needs the latest turn."""
    starts = [found[0] for found in turns(text)]
    if len(text) <= room or not starts:
        return text
    head, first = text[:starts[0]], len(starts) - 1
    note = '[earlier turns omitted; read the file]\n\n' if first else ''
    while first > 1 and len(head) + len(note) + len(text) - starts[first - 1] <= room:
        first -= 1
    view = head + note + text[starts[first]:]
    if len(view) > room:  # Only the newest turn is left and it is too long on its own.
        view = head + note + clip_turn(text[starts[-1]:], room - len(head) - len(note))
    return view if len(view) < len(text) else text


def session_records(root, project=None):
    """Automatic records (one per session part, or legacy one per turn), oldest first.

    A record belongs to the project its header Project line names, so other projects' records never match.
    """
    root = Path(root).resolve()
    targets = project_targets(root, project) if project else None
    rows = []
    for path in (root / 'memory/sessions').glob('*-auto-*.md'):
        name = RECORD_NAME.fullmatch(path.name)
        relative = path.relative_to(root).as_posix()
        try:
            if not name or not scoped(root, relative, area='memory/sessions').is_file():
                continue
        except ValueError:
            continue  # Do not follow record symlinks outside the vault.
        with path.open(encoding='utf-8') as handle:
            head = handle.read(4096)
            found = owner(head)
            if RECORD_TITLE not in head or not found or (targets and found not in targets):
                continue
            text = head + handle.read()
        try:
            note = project or link_relative(root, found)
        except ValueError:
            continue
        meta = frontmatter(text)
        try:
            through = int(meta.get('compiled_through', 0))
        except ValueError:
            through = 0
        rows.append({'path': relative, 'project': note, 'created': meta.get('created', ''),
                     'session': meta.get('session'), 'status': meta.get('status', ''),
                     'turns': turn_count(text), 'compiled_through': through,
                     'order': (meta.get('created', ''), name[1], int(name[2] or 1))})
    return sorted(rows, key=lambda row: row['order'])


def pending(row):
    return row['status'] == 'recorded' or row['turns'] > row['compiled_through']


def pending_records(root, project, exclude_session=None):
    return [row for row in session_records(root, project)
            if pending(row) and not (exclude_session and row['session'] == exclude_session)]


def batch_row(root, row):
    through = row['compiled_through']
    if through < row['turns']:
        start = through + 1
    elif row['session'] and through == row['turns']:
        start = through + 1  # Only sections after the last compiled turn (subagent results) are new.
    else:
        start = 1
    return {'path': row['path'], 'link': '[[' + link_target(root, row['path']) + ']]', 'from_turn': start,
            'through_turn': row['turns'], 'created': row['created']}


def run_note(run):
    return 'memory/runs/' + run + '.md'


def run_state(root, run, project):
    """A closed run's state for compile, or ValueError; the path is built only from a validated run id."""
    path = internal(root, '.brain/runs/' + run + '.json') if isinstance(run, str) and RUN_ID.fullmatch(run) else None
    data = load_json(path, None) if path and path.is_file() else None
    if not isinstance(data, dict) or data.get('run') != run or data.get('project') != project or data.get('status') != 'closed':
        raise ValueError('Run is missing or not a closed run of this project: ' + str(run)[:80])
    return path, data


def uncompiled_runs(root, project):
    """Closed run boards of this project not yet compiled, oldest first; compile treats them like records."""
    root = Path(root).resolve()
    rows = []
    try:
        boards = internal_files(root, '.brain/runs', '*.json')
    except ValueError:
        boards = []  # A symlinked .brain/runs is never read; board commands refuse it.
    for path in boards:
        try:
            _, data = run_state(root, path.stem, project)
        except (ValueError, OSError):
            continue
        if not data.get('compiled'):
            rows.append({'run': path.stem, 'path': run_note(path.stem), 'link': '[[' + link_target(root, run_note(path.stem)) + ']]',
                         'title': data.get('title', ''), 'closed': str(data.get('closed_day') or '')})
    return sorted(rows, key=lambda row: (row['closed'], row['run']))


def sync_run_notes(root, names):
    """Bring each run board note in line with its JSON state (the caller holds the lock), so compile never lists a
    board whose note a failed write left stale: the agent reads the note, and --done stamps the state."""
    try:
        from . import runs  # runs imports brain, so it is loaded only when needed.
    except ImportError:
        import runs
    for run in names:
        runs.sync_note(root, runs.load(root, run))


def compile_batch(root, project):
    return internal(root, '.brain/automation/compile-' + digest(project.encode())[:24] + '.json')


def session_queued(root, session):
    """True while hook writes of this session wait in the spool; read-only."""
    try:
        files = internal_files(root, SPOOL)
    except ValueError:
        return False
    return any((name := SPOOL_NAME.fullmatch(path.name)) and name[2] == session for path in files)


def queued_writes(root, project):
    """This project's hook writes waiting in the spool for a lock holder; read-only (nothing is applied)."""
    try:
        files = internal_files(root, SPOOL)
    except ValueError:
        return 0
    count = 0
    for path in files:
        try:
            item = json.loads(path.read_text(encoding='utf-8')) if SPOOL_NAME.fullmatch(path.name) else None
        except (OSError, ValueError):
            continue
        count += isinstance(item, dict) and item.get('project') == project
    return count


def compile_status(root, project):
    """Read-only and lock-free: it applies no queued writes, and reports them as queued."""
    rows, boards = pending_records(root, project), uncompiled_runs(root, project)
    return {'pending_total': len(rows) + len(boards), 'oldest': rows[0]['created'] if rows else None,
            'records': [batch_row(root, row) for row in rows], **({'runs': boards} if boards else {}),
            'queued': queued_writes(root, project)}


def self_marker(root, session):
    """Where compile --done of a session's own batch leaves the turn count it covered (see records.self_compiled)."""
    return internal(root, '.brain/automation/self-compiled-' + session + '.json')


def turn_file(root, session):
    """The identity of a session's current turn (the ident its Stop item carries), replaced by each UserPromptSubmit;
    it binds a self-compile marker to the one turn that listed the batch."""
    return internal(root, '.brain/automation/turn-' + session + '.json')


def compile_list(root, project, limit=COMPILE_LIMIT, session=None):
    """List a compile batch; with session, only that session's own record parts (an agent compiling its own work)."""
    root = Path(root).resolve()
    if limit < 1:
        raise ValueError('Compile limit must be at least 1')
    if session is not None and not (isinstance(session, str) and SESSION_KEY.fullmatch(session)):
        raise ValueError('Session must be the 24-character key the session start context names')
    project_path = scoped(root, project, area='memory/projects')
    if not project_path.is_file():
        raise ValueError('Project note does not exist')
    with writer(root):
        ensure_sections(project_path)
        rows = pending_records(root, project)
        boards = uncompiled_runs(root, project)  # Closed run boards are batch items too (key absent when none).
        if session:
            rows, boards = [row for row in rows if row['session'] == session], []
        batch_path = compile_batch(root, project)
        # An own batch is listed even when empty: the turn running now is not recorded yet, and --done covers it.
        if not rows and not boards and not session:
            if batch_path.exists():
                batch_path.unlink()
            return {'records': [], 'pending_total': 0}
        # Hashes are taken under the lock, so they match the turn counts that were just read.
        batch = []
        for row in rows[:limit]:
            data = (root / row['path']).read_bytes()
            body = data.decode('utf-8')[body_start(data.decode('utf-8')):]
            batch.append(dict(batch_row(root, row), sha256=digest(data), body_length=len(body),
                              body_sha256=digest(body.encode())))
        pending, boards = len(rows) + len(boards), boards[:limit]
        sync_run_notes(root, [entry['run'] for entry in boards])
        token = uuid.uuid4().hex
        save_json(batch_path, {'token': token, 'project': project, 'created': timestamp(), 'records': batch,
                               'runs': boards, **({'session': session, 'turn': load_json(turn_file(root, session), {})
                                                   .get('token')} if session else {})})
    return {'token': token, 'records': [{k: v for k, v in entry.items() if not k.startswith(('sha256', 'body_'))}
                                        for entry in batch],
            'pending_total': pending, **({'runs': boards} if boards else {}), **({'session': session} if session else {}),
            'targets': {'project': project, 'profile': 'memory/profile.md', 'decisions': 'memory/decisions/',
                        'topics': 'wiki/topics/'},
            'instructions': COMPILE_STEPS}


def compile_done(root, project, token, updated=(), skipped=(), when=None):
    root = Path(root).resolve()
    when = when or day()
    date.fromisoformat(when)
    if not updated and not skipped:
        raise ValueError('Pass at least one --updated note or --skip record')
    with writer(root):
        batch_path = compile_batch(root, project)
        batch = load_json(batch_path, {})
        if not token or batch.get('token') != token or batch.get('project') != project:
            raise ValueError('Compile token does not match the current batch; run compile again')
        records = {entry['path']: entry for entry in batch['records']}
        boards = {entry['path']: entry for entry in batch.get('runs', [])}
        links = {link_target(root, name): name for name in [*records, *boards]}
        cited = set()
        for relative in updated:
            note = scoped(root, relative)
            if (note.suffix != '.md' or not any(note.is_relative_to(root / d) for d in ('wiki', 'memory'))
                    or note.is_relative_to(root / 'memory/sessions')):
                raise ValueError('Updated notes must be curated wiki/ or memory/ Markdown notes outside '
                                 'memory/sessions/: ' + relative)
            if not note.is_file():
                raise ValueError('Updated note does not exist: ' + relative)
            # Navigation pointers (Latest automatic record: ...) link records but do not cite them.
            body = drop_body_lines(note.read_text(encoding='utf-8'), POINTERS)
            found = {links[t] for t in link_targets(body) if t in links}
            if not found and links:
                raise ValueError('Updated note does not cite a record of this batch: ' + relative)
            cited |= found
        skipped = [run_note(n) if RUN_ID.fullmatch(n) else n for n in skipped]  # A run may be skipped by its id.
        for name in skipped:
            if name not in records and name not in boards:
                raise ValueError('Skipped record is not in this batch: ' + name)
        uncited = sorted((set(records) | set(boards)) - cited - set(skipped))
        if uncited:
            raise ValueError('Cite each batch record in an updated note or --skip it: ' + ', '.join(uncited))
        issues = note_issues(root, updated)[0]
        if issues:
            raise ValueError('Structural validation failed: ' + '; '.join(issues[:5]))
        # Prepare every change first, so a failed check leaves records and notes untouched.
        writes, session, last = [], batch.get('session'), None
        for name, entry in records.items():
            path = scoped(root, name, area='memory/sessions')
            if not path.is_file():
                raise ValueError('Record disappeared since listing; run compile again: ' + name)
            data = path.read_bytes()
            text = data.decode('utf-8')
            # Appended turns and frontmatter changes are expected; any edit to the listed body is not.
            body, size = text[body_start(text):], entry.get('body_length', -1)
            if size < 0 or digest(body[:size].encode()) != entry.get('body_sha256'):
                raise ValueError('Record changed since listing (not only appended turns); run compile again: ' + name)
            # Sections appended after listing (turns or subagent results) stay pending: compiled_through is the
            # turn count seen at listing time, and a grown body keeps status recorded.
            newer = len(body) > size
            fields = {'compiled_through': entry['through_turn'], 'compiled': when}
            if not newer:
                fields['status'] = 'compiled'
            writes.append((path, set_fields(text, **fields)))
            last = (name, entry['through_turn'], newer)  # Listing order is oldest first, so this ends as the newest part.
        if session and last is None:
            # Nothing pending (an already compiled session, or no record yet): the marker names the newest part.
            own = [row for row in session_records(root, project) if row['session'] == session]
            last = (own[-1]['path'], own[-1]['turns'], pending(own[-1])) if own else None
        # A queued older Stop of this session would be applied after --done and take the marker meant for the
        # turn running now, so a session with queued writes gets none.
        # The marker names the turn that listed the batch: a later prompt replaces the turn token, so a marker
        # written late (a slow --done) can never be taken by that later turn.
        if session and batch.get('turn') and not (last and last[2]) and not session_queued(root, session):
            # The turn running now is recorded only at its Stop; the marker lets that one turn count as compiled.
            # A record that grew since listing gets no marker, so the new sections stay pending.
            marker = {'project': project, 'record': last and last[0], 'through': last[1] if last else 0, 'at': time.time(),
                      'turn': batch['turn']}
            writes.append((self_marker(root, session), json.dumps(marker) + '\n'))
        for entry in boards.values():  # A compiled run is stamped in its own state, never listed again.
            path, data = run_state(root, entry['run'], project)
            writes.append((path, json.dumps(dict(data, compiled=when), ensure_ascii=False, indent=2) + '\n'))
        project_path = scoped(root, project, area='memory/projects')
        note = set_body_line(project_path.read_text(encoding='utf-8'), 'Last compiled:', 'Last compiled: ' + when)
        writes.append((project_path, stamp(note, when)))
        for path, text in writes:
            atomic_write(path, text)
        compiled = [name for name in [*records, *boards] if name in cited]
        if session and not records:
            compiled = []
        skips = [name for name in [*records, *boards] if name not in cited]
        append_log(root, f'Compiled automatic records for [[{link_target(root, project)}|{project_path.stem}]]: '
                         f'{len(compiled)} compiled, {len(skips)} skipped; updated ' +
                         (', '.join(f'[[{link_target(root, n)}|{Path(n).stem}]]' for n in dict.fromkeys(updated)) or 'no notes'), when)
        batch_path.unlink()
    return {'compiled': compiled, 'skipped': skips,
            'pending_total': len(pending_records(root, project)) + len(uncompiled_runs(root, project))}


def indexed(root, text, relative, label):
    """index.md text that links relative, unchanged when it already does."""
    target = link_target(root, relative)  # Resolves to this exact file, even for a dotted name such as api.v2.md.
    if target in link_targets(text):  # A link quoted in a code example does not count.
        return text
    return text.rstrip() + '\n\n- [[' + target + '|' + label + ']]\n'


def add_index(root, relative, label):
    path = root / 'index.md'
    text = path.read_text(encoding='utf-8')
    updated = indexed(root, text, relative, label)
    if updated != text:
        atomic_write(path, updated)


def logged(root, message, when):
    """log.md text with one more runtime line."""
    path = root / 'log.md'
    text = path.read_text(encoding='utf-8') if path.exists() else '# Change log\n'
    return text.rstrip() + f'\n\n## [{when}] runtime | {message}\n'


def append_log(root, message, when):
    atomic_write(root / 'log.md', logged(root, message, when))


def ends_in_fence(text):
    """True when text ends inside a fenced code block, so anything appended after it would be code."""
    return not without_examples(text.rstrip('\n') + '\n.').endswith('.')


def sources_sections(text):
    """(start, end) of each "## Sources" section outside code; it ends at the next # or ## heading."""
    found, start = [], None
    for offset, raw, plain in plain_lines(text):
        if raw.startswith('#') and plain.startswith('#') and re.match(r'#{1,2} ', raw):
            if start is not None:
                found.append((start, offset))
                start = None
            if SOURCES_HEADING.fullmatch(plain.casefold()):
                start = offset
    return found + ([(start, len(text))] if start is not None else [])


def file_answer(root, title, text, sources=(), slug=None, update=False, expected_sha=None, when=None):
    """File a reusable answer as wiki/topics/SLUG.md citing its sources, or append an update to that page.

    Everything is validated, including check_vault on the new page, index.md, and log.md as they would be
    written, before the first write; a refusal leaves the vault unchanged.
    """
    root = Path(root).resolve()
    when = when or day()
    date.fromisoformat(when)
    if update and not expected_sha:
        raise ValueError('--update requires --expected-sha (the sha256 of the page you read)')
    if expected_sha and not update:
        raise ValueError('--expected-sha is only valid with --update')
    title = ' '.join(str(title or '').split())
    if not title and not (update and slug):
        raise ValueError('A title is required' + (' (or --slug with --update)' if update else ''))
    if len(title) > 200 or re.search(r'[\[\]|`\x00-\x1f\x7f]', title):
        raise ValueError('Title must be one line of at most 200 characters without [ ] | or `')
    if excerpt(title) != title:
        raise ValueError('Title looks like it contains a credential')
    slug = slug if slug is not None else re.sub(r'[^a-z0-9]+', '-', title.casefold()).strip('-')[:64].strip('-')
    if not ANSWER_SLUG.fullmatch(slug):
        raise ValueError('Slug must be 1-64 characters from a-z 0-9 - starting with a letter or digit; pass --slug')
    relative = f'wiki/topics/{slug}.md'
    page = scoped(root, relative, area='wiki/topics')
    if page != root / relative:
        raise ValueError('Answer page path must not pass through a symlink: ' + relative)
    if not text.strip():
        raise ValueError('Answer text is empty')
    if len(text) > ANSWER_LIMIT:
        raise ValueError(f'Answer text exceeds {ANSWER_LIMIT} characters; split it into several pages')
    body = excerpt(text, 3 * ANSWER_LIMIT).strip('\n').rstrip()  # Redaction never truncates text this short.
    if sources_sections(body):
        raise ValueError('Text cannot contain its own "## Sources" heading; pass each source with --source')
    if ends_in_fence(body):
        raise ValueError('Close every code fence in the text')
    plain = without_examples(body)
    if plain.rfind('<!--') > plain.rfind('-->'):
        raise ValueError('Close every HTML comment in the text')
    if not sources and not update:
        raise ValueError('At least one --source is required')
    links = {}
    for name in sources:
        path = scoped(root, name)
        found = path.relative_to(root).as_posix()
        if found.split('/', 1)[0] not in ('raw', 'wiki', 'memory') or not path.is_file():
            raise ValueError('Each source must be an existing file under raw/, wiki/, or memory/: ' + str(name)[:200])
        if found == relative:
            raise ValueError('An answer page cannot cite itself')
        links[found] = evidence_link(root, found)
    with writer(root):
        if not update:
            if os.path.lexists(page):
                raise ValueError(f'Answer page already exists: {relative}; read it, then use --update --expected-sha')
            out = (f'---\ncreated: {when}\nupdated: {when}\nstatus: active\nkind: answer\n---\n# {title}\n\n{body}\n\n'
                   '## Sources\n\n' + ''.join(f'- {link}\n' for link in links.values()))
            message = f'Filed answer [[{link_target(root, relative)}|{title}]] citing {len(links)} source(s)'
        else:
            if not page.is_file():
                raise ValueError('Answer page does not exist: ' + relative)
            data = page.read_bytes()
            if digest(data) != expected_sha:
                raise ValueError('Answer page changed since you read it; reread before updating: ' + relative)
            old = data.decode('utf-8')
            spans = sources_sections(old)
            if len(spans) > 1:
                raise ValueError('Answer page has several "## Sources" sections; merge them first: ' + relative)
            if ends_in_fence(old):
                raise ValueError('Answer page ends inside a code fence; close it first: ' + relative)
            start, end = spans[0] if spans else (len(old), len(old))
            cited = linked_files(root, old[start:end])[0]  # Files, so [[x.md]] and [[x]] count as one source.
            lines = ''.join(f'- {link}\n' for name, link in links.items() if name not in cited)
            if lines and spans:  # New sources go after the section's last non-blank line.
                kept = old[start:end].rstrip()
                rest = old[start + len(kept):end].lstrip(' \t')
                rest = rest[1:] if rest.startswith('\n') else rest  # The blank line before the next heading stays.
                old = old[:start] + kept + ('\n' if '\n' in kept else '\n\n') + lines + rest + old[end:]
            out = set_fields(old, updated=when).rstrip('\n') + f'\n\n## Update — {when}\n\n{body}\n'
            out += '\n## Sources\n\n' + lines if lines and not spans else ''
            message = f'Updated answer [[{link_target(root, relative)}|{slug}]]'
        index = indexed(root, (root / 'index.md').read_text(encoding='utf-8'), relative, title or slug)
        log = logged(root, message, when)
        overlay = {relative: out, 'index.md': index, 'log.md': log}
        after = check(root, overlay)[0]
        if after:
            before = set(check(root)[0])
            issues = [i for i in after if i.startswith(relative + ':') or i not in before]
            if issues:
                raise ValueError('Structural validation failed: ' + '; '.join(issues[:5]))
        atomic_write(page, out)
        if index != (root / 'index.md').read_text(encoding='utf-8'):
            atomic_write(root / 'index.md', index)
        atomic_write(root / 'log.md', log)
        sha = digest(page.read_bytes())  # Read under the lock: exactly the page this call wrote.
    return {'path': relative, 'sha256': sha}


def checkpoint(root, project, summary, next_step, evidence, *, checkpoint_id=None, when=None, complete=False):
    root = Path(root).resolve()
    when = when or day()
    date.fromisoformat(when)
    project_path = scoped(root, project, area='memory/projects')
    if not project_path.is_file():
        raise ValueError('Project note does not exist')
    if not evidence:
        raise ValueError('Checkpoint evidence is required')
    for name in evidence:
        if not scoped(root, name).is_file():
            raise ValueError('Missing evidence: ' + name)
    links = [evidence_link(root, scoped(root, name).relative_to(root).as_posix()) for name in evidence]
    checkpoint_id = checkpoint_id or uuid.uuid4().hex[:12]
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', checkpoint_id):
        raise ValueError('Checkpoint ID must be a plain slug')
    relative = f'memory/sessions/{when}-{project_path.stem}-{checkpoint_id}.md'
    body = f'''---
created: {when}
updated: {when}
status: {'completed' if complete else 'active'}
---
# {'Handoff' if complete else 'Checkpoint'} — {project_path.stem}

Project: [[{link_target(root, project)}|Project state]].

## Saved progress

{summary}

## Next concrete step

{next_step}

## Evidence

'''
    body += ''.join(f'- {link}\n' for link in links)
    body += '\nThis record covers saved progress only. Do not infer that the next step is complete.\n'
    with writer(root):
        target = scoped(root, relative)
        if target.exists() and target.read_text(encoding='utf-8') != body:
            raise ValueError('Checkpoint ID already exists with different content')
        atomic_write(target, body)
        text = project_path.read_text(encoding='utf-8')
        field = 'Latest handoff' if complete else 'Latest checkpoint'
        pointer = f'{field}: [[{link_target(root, relative)}|{when} {checkpoint_id}]].'
        if complete:  # Pointer lines are found outside code only, so examples in the note stay as written.
            text = drop_body_lines(text, 'Latest checkpoint:')
        text = stamp(set_body_line(text, field + ':', pointer), when)
        atomic_write(project_path, text.rstrip() + '\n')
        add_index(root, relative, f'{project_path.stem} {checkpoint_id}')
        log_path = root / 'log.md'
        log_text = log_path.read_text(encoding='utf-8') if log_path.exists() else ''
        if '[[' + link_target(root, relative) + '|' not in log_text:
            append_log(root, f'Saved {field.lower()} [[{link_target(root, relative)}|{checkpoint_id}]]', when)
    return {'path': relative, 'state': 'completed' if complete else 'active'}


def brief_files(root, project=None):
    """(relative, text) for each existing file the brief draws from, in brief order.

    The project note is read once, so its pointers and its shown text come from the same snapshot.
    """
    root = Path(root).resolve()
    paths = ['memory/profile.md'] + ([project] if project else [])
    files = {}
    for relative in paths:
        path = scoped(root, relative)
        if relative not in files and path.is_file():
            files[relative] = path.read_text(encoding='utf-8')
    # Project-maintained pointers, rather than directory timestamps, define recent context; pointer-like lines in
    # code examples or frontmatter are not pointers.
    for _, _, plain in body_lines(files.get(project, ''), ('Latest checkpoint:', 'Latest handoff:', 'Latest automatic record:')):
        target = pointer_target(plain)
        relative = link_relative(root, target) if target else None
        if relative and relative not in files and scoped(root, relative).is_file():
            files[relative] = scoped(root, relative).read_text(encoding='utf-8')
    return list(files.items())


def brief_paths(root, project=None):
    return [relative for relative, _ in brief_files(root, project)]


def brief(root, project=None, limit=6000):
    return brief_with_paths(root, project, limit)[0]


def brief_with_paths(root, project=None, limit=6000):
    """The brief and the vault-relative files it shows (wholly or truncated), from one read pass."""
    root = Path(root).resolve()
    if limit < 500:
        raise ValueError('Context limit must be at least 500 characters')
    out = 'Shared memory: read index.md for deeper evidence. Save meaningful progress with second-brain run checkpoint.'
    files = brief_files(root, project)
    shown_paths = []

    def omitted(rest, room):
        """The most specific complete omission marker that fits in room, or None."""
        if not rest:
            return '' if room >= 0 else None
        for note in (f'\n[omitted by brief limit: {", ".join(rest)}; read these files]',
                     f'\n[omitted by brief limit: {len(rest)} more files linked from {project or "index.md"}; read them]',
                     '\n[brief truncated; read the project note]'):
            if len(note) <= room:
                return note
        return None

    def truncated(relative, count, total):
        return f'\n[truncated: {relative} shows {count} of {total} chars; read the file]'
    # Invariant: after each accepted file, a complete omission marker for the rest still fits.
    if omitted([r for r, _ in files], limit - len(out)) is None:
        out = ''
    for number, (relative, text) in enumerate(files):
        head = '\n\nFILE: ' + relative + '\n'
        later = [r for r, _ in files[number + 1:]]
        cap = 1100 if relative == 'memory/profile.md' else 1800
        view = recent_turns(text, cap)  # A session record shows its header and latest turns.
        shown = min(len(view), cap)
        tail = truncated(relative, shown, len(text)) if shown < len(text) else ''
        chunk = head + view[:shown] + tail
        rest = omitted(later, limit - len(out) - len(chunk))
        if rest is not None:
            out += chunk
            shown_paths.append(relative)
            continue
        for _ in range(2):  # The count's digits affect the marker length.
            used = len(out) + len(head) + len(truncated(relative, shown, len(text)))
            rest = omitted(later, limit - used - 1)
            shown = max(0, min(shown, limit - used - len(rest or ''))) if rest is not None else 0
        if shown > 0:
            return out + head + view[:shown] + truncated(relative, shown, len(text)) + rest, shown_paths + [relative]
        return out + omitted([r for r, _ in files[number:]], limit - len(out)), shown_paths
    return out, shown_paths


def query_terms(text, stop=STOP_WORDS, cap=QUERY_TERMS):
    """Distinct casefolded query words in first-seen order, without stop words, at most cap (None: all). Hook
    matching uses the defaults; search passes no stop words and no cap, so every word typed counts."""
    terms = list(dict.fromkeys(w for w in WORD.findall(text.casefold()) if w not in stop))
    return terms if cap is None else terms[:cap]


def word_at(term, low):
    """Offset of the first word of low that equals term or, from 5 characters on, starts with it; else -1.

    Words are runs of letters and digits (str.isalnum, the same as [^\\W_]), so "use" never matches "user";
    str.find keeps the scan fast.
    """
    size, exact = len(term), len(term) < 5
    at = low.find(term)
    while at >= 0:
        if (not at or not low[at - 1].isalnum()) and not (exact and low[at + size:at + size + 1].isalnum()):
            return at
        # No later hit inside this word starts a word, so resume after it: the scan stays linear.
        at = low.find(term, WORD_RUN.match(low, at).end())
    return -1


def outline(text):
    """(first "# " title, "## " headings joined by newlines) outside fenced code, casefolded.

    Fences follow without_examples, as in entries(); a "# comment" inside a code block is not a title.
    """
    title, heads, fence = None, [], None
    for line in text.splitlines():
        bare = line.lstrip(' ')
        indent = len(line) - len(bare) <= 3
        if fence:
            if indent and bare.startswith(fence) and re.fullmatch(r' {0,3}' + re.escape(fence[0]) + '{' + str(len(fence)) + r',}\s*', line):
                fence = None
        elif indent and bare[:3] in ('```', '~~~'):
            fence = re.match(r'`{3,}|~{3,}', bare)[0]
        elif line.startswith('# ') and title is None:
            title = line[2:]
        elif line.startswith('## '):
            heads.append(line[3:])
    return (title or '').casefold(), '\n'.join(heads).casefold()


def rank(relative, text, terms):
    """(complete, score, offset) of a note for query terms, or None when no term matches; search and prompt
    matching share it. A matched term scores 3 in the path or # title, else 2 in a ## heading, else 1 in the body
    (frontmatter excluded). Curated notes count x1.5 and automatic records x0.75, so curated knowledge wins ties.
    complete is True when every term matched; rank_key puts those first. offset is the first body hit in text.
    """
    start = body_start(text)
    low, path = text[start:].casefold(), relative.casefold()
    found, in_path = {}, set()
    for term in terms:
        at = word_at(term, low)
        if word_at(term, path) >= 0:
            in_path.add(term)
        if at >= 0 or term in in_path:
            found[term] = at
    if not found:
        return None
    heads, score = None, 0
    for term in found:
        if term in in_path:
            score += 3
            continue
        if heads is None:  # Only a term on a heading-like line needs the fence-aware outline.
            lines = '\n'.join(HEADING_LINES.findall(low))
            heads = outline(text[start:]) if any(word_at(t, lines) >= 0 for t in found) else ('', '')
        score += 3 if word_at(term, heads[0]) >= 0 else 2 if word_at(term, heads[1]) >= 0 else 1
    automatic = frontmatter(text).get('status') == 'recorded' or (
        relative.startswith('memory/sessions/') and bool(RECORD_NAME.fullmatch(Path(relative).name)))
    hits = [at for at in found.values() if at >= 0]  # A term only in the path has no body offset.
    return len(found) == len(terms), round(score * (0.75 if automatic else 1.5), 2), start + min(hits, default=0)


def rank_key(relative, ranked):
    """Sort key: notes matching every term first, then by score, then by path."""
    return not ranked[0], -ranked[1], relative


def search(root, query, limit=8):
    """Notes in wiki/ and memory/ matching any query word, best first (rank); each row carries its score. Every
    word of 3+ letters or digits counts: no stop words are dropped and there is no term cap."""
    root = Path(root).resolve()
    terms = query_terms(query, (), None)
    rows = []
    for folder in ('wiki', 'memory'):
        for path in sorted((root / folder).rglob('*.md')):
            relative = path.relative_to(root).as_posix()
            try:
                if not terms or not scoped(root, relative).is_file():
                    continue
            except ValueError:
                continue  # Do not follow note symlinks outside the vault.
            text = path.read_text(encoding='utf-8')
            ranked = rank(relative, text, terms)
            if ranked:
                pos = ranked[2]
                rows.append((rank_key(relative, ranked), {
                    'path': relative, 'status': frontmatter(text).get('status', 'unknown'), 'score': ranked[1],
                    'excerpt': text[max(0, pos - 70):pos + 250]}))
    return [row for _, row in sorted(rows, key=lambda r: r[0])][:limit]


REVIEW = 'Review candidate, not proof of error: '


def is_stub(body):
    """A standard project section that still holds only its placeholder (navigation lines aside)."""
    lines = [l.strip() for l in body.splitlines() if l.strip() and not l.startswith(POINTERS + ('Last compiled:',))]
    return lines == ['None recorded yet.']


def content_findings(root, texts, edges):
    """Meaning-level review candidates. texts holds every memory/ and wiki/ note doctor read; edges are check_vault's
    resolved links (wikilinks and Markdown links outside code), which orphan_note uses for inbound links."""
    findings, waiting, recorded = [], {}, set()
    for row in session_records(root):
        recorded.add(row['project'])
        if pending(row):
            waiting.setdefault(row['project'], []).append((row['created'], row['path']))
    projects = set()
    try:
        boards = internal_files(root, '.brain/runs', '*.json')
    except ValueError:
        boards = []
    for path in boards:
        try:
            data = load_json(path, None)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get('project'), str):
            projects.add(data['project'])
    for project in sorted(projects):  # Closed, uncompiled run boards are compile batch items too.
        for row in uncompiled_runs(root, project):
            waiting.setdefault(project, []).append((row['closed'], row['path']))
    for project, rows in waiting.items():
        findings.append({'kind': 'pending_compile', 'project': project, 'count': len(rows),
                         'oldest': min(when for when, _ in rows), 'paths': [name for _, name in rows],
                         'detail': REVIEW + 'automatic records or closed run boards not yet compiled into curated '
                                   'notes; run compile in that project.'})
    for project in sorted(recorded):
        found = sections(texts.get(project, ''))
        if all(name.casefold() in found and is_stub(found[name.casefold()]) for name in PROJECT_SECTIONS):
            findings.append({'kind': 'empty_project_note', 'paths': [project],
                             'detail': REVIEW + 'every standard section still says "None recorded yet." although the '
                                       'project has automatic records; compile them or fill in the sections.'})
    try:
        vault = root.parent if link_prefix(root) else root
    except (ValueError, OSError):
        vault = root  # The structural check reports the broken marker.
    inbound = {}  # Any checked note may link in, root files such as README.md included.
    for source, targets in edges.items():
        for target in targets:
            if target != source and target.is_relative_to(root) and source.is_relative_to(root):
                inbound.setdefault(target.relative_to(root).as_posix(), set()).add(source.relative_to(root).as_posix())
    for name in sorted(texts):
        if name.startswith('wiki/') or name.startswith('memory/decisions/'):
            # Evidence: raw sources, records, run boards, or other notes; index/log and project navigation are not.
            found, outside = linked_files(root, texts[name], POINTERS + ('Project:',), vault)
            evidence = [t for t in found - {name, 'index.md', 'log.md'}
                        if t.startswith(('raw/', 'wiki/', 'memory/')) and not t.startswith('memory/projects/')]
            if not evidence and not outside:
                findings.append({'kind': 'unsupported_note', 'paths': [name],
                                 'detail': REVIEW + 'no wikilink to evidence (raw/, memory/sessions/, memory/runs/, or '
                                           'another wiki/ or memory/ note); cite the sources it rests on.'})
        if (not name.startswith(('memory/sessions/', 'memory/runs/', 'memory/projects/')) and name != 'memory/profile.md'
                and 'index.md' in inbound.get(name, ()) and not inbound[name] - {'index.md', 'log.md'}):
            findings.append({'kind': 'orphan_note', 'paths': [name],
                             'detail': REVIEW + 'only index.md links here (log.md aside); link it from the related '
                                       'notes or the project note so it is found in context.'})
    return findings


def doctor(root, as_of=None, stale_days=90):
    root = Path(root).resolve()
    today = date.fromisoformat(as_of or day())
    edges = {}
    issues, notes, links = check(root, edges=edges)
    findings = [{'kind': 'structural', 'detail': e} for e in issues]
    groups, claims, texts = {}, {}, {}
    for folder in ('memory', 'wiki'):
        for path in sorted((root / folder).rglob('*.md')):
            name = path.relative_to(root).as_posix()
            try:
                if not scoped(root, name).is_file():
                    continue
            except ValueError:
                continue  # Do not follow note symlinks outside the vault.
            text = texts[name] = path.read_text(encoding='utf-8')
            meta = frontmatter(text)
            if meta.get('last_verified'):
                try:
                    if (today - date.fromisoformat(meta['last_verified'])).days > stale_days:
                        findings.append({'kind': 'stale_candidate', 'paths': [name], 'detail': 'Verification date exceeds review threshold; not proof the claim is false.'})
                except ValueError:
                    pass  # Structural checker reports invalid dates.
            body = re.sub(r'\A---\n.*?\n---\n', '', text, flags=re.S)
            body = re.sub(r'^# .*\n', '', body, count=1)
            normalized = ' '.join(body.split()).casefold()
            if len(normalized) >= 80:
                groups.setdefault(digest(normalized.encode()), []).append(name)
            if meta.get('claim_key') and meta.get('claim_value') and meta.get('status') != 'superseded':
                claims.setdefault(meta['claim_key'], []).append((name, meta['claim_value']))
    findings += content_findings(root, texts, edges)
    try:
        queued = [p.name for p in internal_files(root, SPOOL) if SPOOL_NAME.fullmatch(p.name)]
        failed = [p.name for p in internal_files(root, SPOOL + '/failed')]
    except ValueError as exc:
        queued, failed = [], []
        findings.append({'kind': 'structural', 'detail': str(exc)})
    if queued:
        findings.append({'kind': 'spool_backlog', 'count': len(queued), 'oldest': queued[0],
                         'detail': 'Hook writes waiting for the vault lock. The next SessionStart, UserPromptSubmit, Stop, or '
                                   'SubagentStop hook that gets the lock applies them, as do run compile (listing and '
                                   '--done) and board open, add, claim, post, release, and close; compile --status and '
                                   'board show report them as queued.'})
    if failed:
        findings.append({'kind': 'spool_failed', 'count': len(failed), 'paths': [SPOOL + '/failed/' + n for n in failed],
                         'detail': 'Hook writes that could not be applied for 7 days (for example, the project was unbound); '
                                   'inspect them and delete them when no longer needed.'})
    for paths in groups.values():
        if len(paths) > 1:
            findings.append({'kind': 'duplicate_candidate', 'paths': paths, 'detail': 'Equivalent normalized bodies; review provenance before merging.'})
    for key, values in claims.items():
        if len({value for _, value in values}) > 1:
            findings.append({'kind': 'conflicting_claims', 'claim_key': key, 'evidence': values,
                             'detail': 'Multiple explicit values; semantic resolution requires review.'})
    return {'as_of': today.isoformat(), 'findings': findings, 'coverage': coverage(root),
            'notes': notes, 'links': links,
            'review_checklist': ['Read recent handoffs and promote reusable findings.',
                                 'Check unsupported claims against cited raw sources.',
                                 'Cite evidence in unsupported_note candidates and link orphan_note candidates from '
                                 'related notes.',
                                 'Review meaning-level contradictions not expressible as claim_key/value.',
                                 'Check superseded decisions and avoid silently choosing a winner.']}


def consolidate(root, source, target, text, evidence, expected_sha, when=None):
    root = Path(root).resolve()
    when = when or day()
    date.fromisoformat(when)
    src = scoped(root, source, area='memory/sessions')
    dst = scoped(root, target)
    if not src.is_file() or not dst.is_file() or dst.suffix != '.md' or not any(dst.is_relative_to(root / d) for d in ('wiki', 'memory')):
        raise ValueError('Use an existing session source and a canonical memory/wiki note')
    if source == target or not text.strip() or not evidence:
        raise ValueError('A distinct target, reviewed text, and evidence are required')
    for name in evidence:
        if not scoped(root, name).is_file():
            raise ValueError('Evidence does not exist: ' + name)
        evidence_link(root, scoped(root, name).relative_to(root).as_posix())  # Refuse unlinkable names before writing.
    operation = digest(json.dumps([source, target, text.strip(), sorted(evidence)]).encode())[:24]
    marker = '<!-- consolidation:' + operation + ' -->'
    with writer(root):
        previous = dst.read_text(encoding='utf-8')
        if marker in previous:
            return {'state': 'already_applied', 'operation': operation}
        if digest(dst.read_bytes()) != expected_sha:
            raise ValueError('Canonical note changed; reread it before consolidation')
        refs = []
        for name in dict.fromkeys([source] + evidence):
            refs.append(evidence_link(root, scoped(root, name).relative_to(root).as_posix()))
        updated = stamp(previous, when).rstrip() + f'\n\n{marker}\n## Consolidated finding — {when}\n\n{text.strip()}\n\nEvidence: ' + ', '.join(refs) + '.\n'
        atomic_write(dst, updated)
        add_index(root, target, dst.stem)
        append_log(root, f'Consolidated {source} into {target}; operation {operation}', when)
    return {'state': 'applied', 'operation': operation, 'target_sha256': digest(dst.read_bytes())}


def snapshot(root, output):
    with writer(root):
        return _snapshot(root, output)


def _snapshot(root, output):
    root = Path(root).resolve()
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('Snapshot already exists')
    manifest = {'schema_version': VERSION, 'created': timestamp(), 'files': {}}
    payload = {}
    for p in sorted(root.rglob('*')):
        rel = p.relative_to(root)
        if any(x in {'.git', '.brain', '__pycache__'} for x in rel.parts) and not KEPT_STATE.fullmatch(rel.as_posix()):
            continue
        if p.name.startswith('.env') or p.suffix in {'.pem', '.key', '.pyc'} or p.name in {'CLAUDE.local.md', 'settings.local.json'}:
            continue
        if '.obsidian' in rel.parts and (p.name.startswith('workspace') or 'cache' in rel.parts):
            continue
        if p.is_symlink():
            raise ValueError('Snapshot does not follow symlinks: ' + str(rel))
        if not p.is_file() or p.resolve() == output:
            continue
        data = p.read_bytes()
        payload[rel.as_posix()] = data
        manifest['files'][rel.as_posix()] = {'sha256': digest(data), 'mode': p.stat().st_mode & 0o777}
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive output creation prevents replacing an earlier recovery point.
    fd, temporary = tempfile.mkstemp(prefix='.brain-snapshot-', dir=output.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            with zipfile.ZipFile(handle, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('manifest.json', json.dumps(manifest))
                for name, data in payload.items():
                    archive.writestr('vault/' + name, data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, output)  # Atomic publication, without replacing a prior snapshot.
    finally:
        os.unlink(temporary)
    return {'snapshot': str(output), 'files': len(payload), 'sha256': digest(output.read_bytes())}


def restore(archive_path, destination):
    destination = Path(destination).absolute()
    if destination.exists():
        raise ValueError('Restore requires a new destination; existing files are never overwritten')
    contents = {}
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Duplicate archive members')
        manifest = json.loads(archive.read('manifest.json'))
        if manifest.get('schema_version') != VERSION:
            raise ValueError('Unsupported snapshot schema')
        expected = {'manifest.json'} | {'vault/' + name for name in manifest['files']}
        if set(names) != expected:
            raise ValueError('Archive members do not match manifest')
        for name, info in manifest['files'].items():
            rel = Path(name)
            if (rel.is_absolute() or '..' in rel.parts or not rel.parts or rel.parts[0] == '.git'
                    or rel.parts[0] == '.brain' and not KEPT_STATE.fullmatch(name)):
                raise ValueError('Unsafe archive path')
            data = archive.read('vault/' + name)
            if digest(data) != info['sha256']:
                raise ValueError('Snapshot checksum mismatch: ' + name)
            if rel.parts[0] == '.brain' and not isinstance(json.loads(data), dict):
                raise ValueError('Invalid run state in snapshot: ' + name)
            contents[name] = data
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.brain-restore-', dir=destination.parent))
    try:
        for name, data in contents.items():
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(manifest['files'][name]['mode'] & 0o777)
        if destination.exists():
            raise ValueError('Restore destination appeared during operation')
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {'restored': str(destination), 'files': len(contents)}


HELP = {
    'scan': 'Register files under raw/ in the source ledger',
    'coverage': 'Report which raw sources are processed',
    'begin': 'Start processing a raw source; returns a token',
    'finish': 'Finish processing with attributed --output notes, or record --error',
    'checkpoint': 'Save project progress with evidence and a next step (--complete: a final handoff)',
    'brief': 'Print the session brief: profile, project note, latest checkpoint or handoff, latest record',
    'compile': 'List automatic records and closed run boards to fold into curated notes; --done TOKEN marks them compiled, --status only reports',
    'search': 'Ranked search of wiki/ and memory/ notes',
    'doctor': 'Structural check plus review candidates (pending_compile, content findings, spool)',
    'consolidate': 'Append a reviewed finding from a session note to a canonical note',
    'file-answer': 'File a reusable answer as wiki/topics/SLUG.md citing --source files (--update appends)',
    'snapshot': 'Write a zip snapshot of the vault',
    'restore': 'Restore a snapshot into a new directory',
}


def main(argv=None, prog=None):
    parser = argparse.ArgumentParser(prog=prog, description=__doc__, allow_abbrev=False,
                                     epilog='Through second-brain run, --root and --project come from the selected vault and '
                                            'bound project. It also routes board, a shared run board (run board --help).')
    # Routed calls get --root and --project from the router; listing them would invite the flags it rejects.
    routed = prog == 'second-brain run'
    bound = {'help': argparse.SUPPRESS} if routed else {}
    parser.add_argument('--root', type=Path, help=argparse.SUPPRESS if routed else 'Brain root (second-brain run supplies it); required except for restore')
    sub = parser.add_subparsers(dest='command', required=True)
    add = lambda name: sub.add_parser(name, help=HELP[name], allow_abbrev=False)
    for name in ('scan', 'coverage'):
        add(name)
    p = add('begin'); p.add_argument('source')
    p = add('finish'); p.add_argument('source'); p.add_argument('--token', required=True); p.add_argument('--output', action='append'); p.add_argument('--error')
    p = add('checkpoint'); p.add_argument('--project', **bound, required=True); p.add_argument('--summary', required=True); p.add_argument('--next', required=True); p.add_argument('--evidence', action='append', required=True); p.add_argument('--id'); p.add_argument('--date'); p.add_argument('--complete', action='store_true')
    p = add('brief'); p.add_argument('--project', **bound); p.add_argument('--limit', type=int, default=6000)
    p = add('compile'); p.add_argument('--project', **bound, required=True); p.add_argument('--limit', type=int, default=COMPILE_LIMIT); p.add_argument('--status', action='store_true'); p.add_argument('--done', metavar='TOKEN'); p.add_argument('--updated', action='append', default=[], metavar='NOTE'); p.add_argument('--skip', action='append', default=[], metavar='RECORD'); p.add_argument('--date'); p.add_argument('--session', metavar='KEY', help="List only this session's own records")
    p = add('search'); p.add_argument('query'); p.add_argument('--limit', type=int, default=8)
    p = add('doctor'); p.add_argument('--as-of'); p.add_argument('--stale-days', type=int, default=90)
    p = add('consolidate'); p.add_argument('--source', required=True); p.add_argument('--target', required=True); p.add_argument('--text-file', type=Path, required=True); p.add_argument('--evidence', action='append', required=True); p.add_argument('--expected-sha', required=True); p.add_argument('--date')
    p = add('file-answer'); p.add_argument('--title'); p.add_argument('--text-file', type=Path, required=True); p.add_argument('--source', action='append', default=[], metavar='PATH'); p.add_argument('--slug'); p.add_argument('--update', action='store_true'); p.add_argument('--expected-sha'); p.add_argument('--date')
    p = add('snapshot'); p.add_argument('--output', type=Path, required=True)
    p = add('restore'); p.add_argument('archive', type=Path); p.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.root is None and args.command != 'restore':
        parser.error('--root is required for ' + args.command)
    root = args.root.resolve() if args.root else None
    try:
        if args.command == 'scan': result = scan(root)
        elif args.command == 'coverage': result = coverage(root)
        elif args.command == 'begin': result = begin(root, args.source)
        elif args.command == 'finish': result = finish(root, args.source, args.token, args.output, args.error)
        elif args.command == 'checkpoint': result = checkpoint(root, args.project, args.summary, args.next, args.evidence, checkpoint_id=args.id, when=args.date, complete=args.complete)
        elif args.command == 'brief': print(brief(root, args.project, args.limit)); return 0
        elif args.command == 'compile':
            if args.status and (args.done is not None or args.updated or args.skip or args.session):
                raise ValueError('--status cannot be combined with --done, --updated, --skip, or --session')
            if args.status: result = compile_status(root, args.project)
            elif args.done is not None:
                if args.session: raise ValueError('--session applies to the listing; --done uses its TOKEN')
                result = compile_done(root, args.project, args.done, args.updated, args.skip, args.date)
            elif args.updated or args.skip: raise ValueError('--updated and --skip require --done TOKEN')
            else: result = compile_list(root, args.project, args.limit, args.session)
        elif args.command == 'search': result = search(root, args.query, args.limit)
        elif args.command == 'doctor': result = doctor(root, args.as_of, args.stale_days)
        elif args.command == 'consolidate': result = consolidate(root, args.source, args.target, args.text_file.read_text(encoding='utf-8'), args.evidence, args.expected_sha, args.date)
        elif args.command == 'file-answer':
            with args.text_file.open(encoding='utf-8') as handle:
                text = handle.read(ANSWER_LIMIT + 1)  # Enough to refuse an oversized answer without reading it all.
            result = file_answer(root, args.title, text, args.source, args.slug, args.update, args.expected_sha, args.date)
        elif args.command == 'snapshot': result = snapshot(root, args.output)
        else: result = restore(args.archive, args.destination)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, zipfile.BadZipFile) as exc:
        print(json.dumps({'error': str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
