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
    from .check_vault import check, without_examples, wiki_target, wiki_relative
except ImportError:
    from check_vault import check, without_examples, wiki_target, wiki_relative

VERSION = 1
LEDGER = 'inbox/source-ledger.json'
LOCK_WAIT = 2.5  # Seconds; hooks time out after 10 s, so contention waits only briefly.


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


@contextmanager
def writer(root, wait=None):
    # An OS advisory lock needs no staleness rules. Legacy .brain/write.lock directories
    # left by plugin <= 0.3.0 are ignored, and this file is never deleted.
    path = Path(root) / '.brain' / 'write.flock'
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + (LOCK_WAIT if wait is None else wait)
        while not lock_file(fd, True):
            if time.monotonic() >= deadline:
                raise ValueError(f'Another writer is active ({describe_lock(path)}). '
                                 f'Retry shortly; the lock is released when that process exits: {path}')
            time.sleep(0.05)
        try:
            # Owner details only inform the error message above; the kernel lock is authoritative.
            owner = json.dumps({'pid': os.getpid(), 'hostname': socket.gethostname(), 'started': timestamp()})
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, owner.encode())
        except OSError:
            pass
        try:
            yield
        finally:
            lock_file(fd, False)
    finally:
        os.close(fd)


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
            raw_links = {wiki_target(root, p) for p in entry['paths']}
            for relative in outputs:
                path = scoped(root, relative)
                if path.suffix != '.md' or not any(path.is_relative_to(root / d) for d in ('wiki', 'memory')):
                    raise ValueError('Output must be a wiki or memory Markdown note')
                body = without_examples(path.read_text(encoding='utf-8'))
                targets = {m.replace('\\|', '|').split('|', 1)[0].split('#', 1)[0]
                           for m in re.findall(r'\[\[([^\]]+)\]\]', body)}
                if not (targets & raw_links):
                    raise ValueError('Each output must link directly to a current raw source path')
                hashes[relative] = digest(path.read_bytes())
            issues, notes, links = check(root)
            # Unrelated broken notes elsewhere must not block ingestion; global issues still do.
            names = {str(scoped(root, r).relative_to(root)) for r in outputs}
            issues = [i for i in issues if not re.match(r'.+?\.md:', i) or re.match(r'(.+?\.md):', i)[1] in names]
            if issues:
                raise ValueError('Structural validation failed: ' + '; '.join(issues[:5]))
            entry.update(state='processed', outputs=hashes, error=None,
                         receipt={'checked_at': timestamp(), 'notes': notes, 'links': links,
                                  'scope': 'file structure and direct source links; not factual verification'})
        entry['attempts'][-1].update(outcome=entry['state'], ended=timestamp())
        entry.pop('token', None)
        save_json(root / LEDGER, ledger)
        return {'id': entry['id'], 'state': entry['state']}


def frontmatter(text):
    match = re.match(r'\A---\n(.*?)\n---(?:\n|$)', text, re.S)
    return dict(re.findall(r'^([a-z_]+):[ \t]*([^\n]*)$', match[1], re.M)) if match else {}


def add_index(root, relative, label):
    path = root / 'index.md'
    text = path.read_text(encoding='utf-8')
    target = wiki_target(root, relative)
    if '[[' + target + '|' not in text and '[[' + target + ']]' not in text:
        atomic_write(path, text.rstrip() + '\n\n- [[' + target + '|' + label + ']]\n')


def append_log(root, message, when):
    path = root / 'log.md'
    text = path.read_text(encoding='utf-8') if path.exists() else '# Change log\n'
    atomic_write(path, text.rstrip() + f'\n\n## [{when}] runtime | {message}\n')


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

Project: [[{wiki_target(root, project)}|Project state]].

## Saved progress

{summary}

## Next concrete step

{next_step}

## Evidence

'''
    for name in evidence:
        target = wiki_target(root, name)
        body += f'- [[{target}|{Path(name).name}]]\n'
    body += '\nThis record covers saved progress only. Do not infer that the next step is complete.\n'
    with writer(root):
        target = scoped(root, relative)
        if target.exists() and target.read_text(encoding='utf-8') != body:
            raise ValueError('Checkpoint ID already exists with different content')
        atomic_write(target, body)
        text = project_path.read_text(encoding='utf-8')
        field = 'Latest handoff' if complete else 'Latest checkpoint'
        pointer = f'{field}: [[{wiki_target(root, relative)}|{when} {checkpoint_id}]].'
        if complete:
            text = re.sub(r'^Latest checkpoint:.*\n?', '', text, flags=re.M)
        text = re.sub(r'^' + field + r':.*$', lambda _: pointer, text, flags=re.M) if re.search(r'^' + field + ':', text, re.M) else text.rstrip() + '\n\n' + pointer + '\n'
        text = re.sub(r'^updated:.*$', 'updated: ' + when, text, count=1, flags=re.M)
        atomic_write(project_path, text.rstrip() + '\n')
        add_index(root, relative, f'{project_path.stem} {checkpoint_id}')
        log_path = root / 'log.md'
        log_text = log_path.read_text(encoding='utf-8') if log_path.exists() else ''
        if '[[' + wiki_target(root, relative) + '|' not in log_text:
            append_log(root, f'Saved {field.lower()} [[{wiki_target(root, relative)}|{checkpoint_id}]]', when)
    return {'path': relative, 'state': 'completed' if complete else 'active'}


def brief(root, project=None, limit=6000):
    root = Path(root).resolve()
    if limit < 500:
        raise ValueError('Context limit must be at least 500 characters')
    paths = ['memory/profile.md'] + ([project] if project else [])
    project_path = scoped(root, project) if project else None
    if project_path and project_path.exists():
        # Project-maintained pointers, rather than directory timestamps, define recent context.
        for line in project_path.read_text(encoding='utf-8').splitlines():
            if line.startswith(('Latest checkpoint:', 'Latest handoff:', 'Latest automatic record:')):
                match = re.search(r'\[\[([^|\]]+)', line)
                if match:
                    paths.append(wiki_relative(root, match[1]) + '.md')
    out = 'Shared memory: read index.md for deeper evidence. Save meaningful progress with second-brain run checkpoint.'
    files = [(r, scoped(root, r)) for r in dict.fromkeys(paths)]
    files = [(r, p.read_text(encoding='utf-8')) for r, p in files if p.is_file()]

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
        shown = min(len(text), 1100 if relative == 'memory/profile.md' else 1800)
        tail = truncated(relative, shown, len(text)) if shown < len(text) else ''
        chunk = head + text[:shown] + tail
        rest = omitted(later, limit - len(out) - len(chunk))
        if rest is not None:
            out += chunk
            continue
        for _ in range(2):  # The count's digits affect the marker length.
            used = len(out) + len(head) + len(truncated(relative, shown, len(text)))
            rest = omitted(later, limit - used - 1)
            shown = max(0, min(shown, limit - used - len(rest or ''))) if rest is not None else 0
        if shown > 0:
            return out + head + text[:shown] + truncated(relative, shown, len(text)) + rest
        return out + omitted([r for r, _ in files[number:]], limit - len(out))
    return out


def search(root, query, limit=8):
    terms = query.casefold().split()
    rows = []
    for folder in ('wiki', 'memory'):
        for path in sorted((Path(root) / folder).rglob('*.md')):
            text = path.read_text(encoding='utf-8')
            low = text.casefold()
            if terms and all(term in low for term in terms):
                pos = min(low.find(term) for term in terms)
                rows.append({'path': path.relative_to(root).as_posix(),
                             'status': frontmatter(text).get('status', 'unknown'),
                             'excerpt': text[max(0, pos - 70):pos + 250]})
    return rows[:limit]


def doctor(root, as_of=None, stale_days=90):
    root = Path(root).resolve()
    today = date.fromisoformat(as_of or day())
    issues, notes, links = check(root)
    findings = [{'kind': 'structural', 'detail': e} for e in issues]
    groups, claims = {}, {}
    for folder in ('memory', 'wiki'):
        for path in sorted((root / folder).rglob('*.md')):
            text = path.read_text(encoding='utf-8')
            meta = frontmatter(text)
            name = path.relative_to(root).as_posix()
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
            refs.append('[[' + wiki_target(root, name) + '|' + Path(name).name + ']]')
        updated = re.sub(r'^updated:.*$', 'updated: ' + when, previous, count=1, flags=re.M)
        updated = updated.rstrip() + f'\n\n{marker}\n## Consolidated finding — {when}\n\n{text.strip()}\n\nEvidence: ' + ', '.join(refs) + '.\n'
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
        if any(x in {'.git', '.brain', '__pycache__'} for x in rel.parts):
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
            if rel.is_absolute() or '..' in rel.parts or not rel.parts or rel.parts[0] in ('.git', '.brain'):
                raise ValueError('Unsafe archive path')
            data = archive.read('vault/' + name)
            if digest(data) != info['sha256']:
                raise ValueError('Snapshot checksum mismatch: ' + name)
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--root', type=Path, help='Brain root; required except for restore')
    sub = parser.add_subparsers(dest='command', required=True)
    add = lambda name: sub.add_parser(name, allow_abbrev=False)
    for name in ('scan', 'coverage'):
        add(name)
    p = add('begin'); p.add_argument('source')
    p = add('finish'); p.add_argument('source'); p.add_argument('--token', required=True); p.add_argument('--output', action='append'); p.add_argument('--error')
    p = add('checkpoint'); p.add_argument('--project', required=True); p.add_argument('--summary', required=True); p.add_argument('--next', required=True); p.add_argument('--evidence', action='append', required=True); p.add_argument('--id'); p.add_argument('--date'); p.add_argument('--complete', action='store_true')
    p = add('brief'); p.add_argument('--project'); p.add_argument('--limit', type=int, default=6000)
    p = add('search'); p.add_argument('query'); p.add_argument('--limit', type=int, default=8)
    p = add('doctor'); p.add_argument('--as-of'); p.add_argument('--stale-days', type=int, default=90)
    p = add('consolidate'); p.add_argument('--source', required=True); p.add_argument('--target', required=True); p.add_argument('--text-file', type=Path, required=True); p.add_argument('--evidence', action='append', required=True); p.add_argument('--expected-sha', required=True); p.add_argument('--date')
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
        elif args.command == 'search': result = search(root, args.query, args.limit)
        elif args.command == 'doctor': result = doctor(root, args.as_of, args.stale_days)
        elif args.command == 'consolidate': result = consolidate(root, args.source, args.target, args.text_file.read_text(encoding='utf-8'), args.evidence, args.expected_sha, args.date)
        elif args.command == 'snapshot': result = snapshot(root, args.output)
        else: result = restore(args.archive, args.destination)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, zipfile.BadZipFile) as exc:
        print(json.dumps({'error': str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
