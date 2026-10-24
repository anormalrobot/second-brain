"""Per-session records and the compile loop (disposable vaults and an explicit config only)."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import brain
import memory_hooks as hooks
import second_brain as setup
from check_vault import check, wiki_target

NUDGE = 'Compile pending:'


class CompileCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='second-brain-compile-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / 'project'
        self.project.mkdir()
        self.config = self.base / 'config/config.json'
        env = patch.dict(os.environ, {'SECOND_BRAIN_CONFIG': str(self.config)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('SECOND_BRAIN_VAULT', None)
        os.environ.pop('SECOND_BRAIN_AUTOMATION', None)
        os.environ.pop('SECOND_BRAIN_RUN', None)
        self.info = setup.initialize(self.config, self.project, mode='new', path=self.base / 'vault')
        self.root = Path(self.info['root'])
        self.note = self.root / self.info['project']

    def handle(self, kind, session='s1', cwd=None, **extra):
        event = {'hook_event_name': kind, 'cwd': str(cwd or self.project), 'session_id': session, **extra}
        return hooks.handle(event, self.config)

    def turn(self, prompt, response, session='s1', cwd=None, **ids):
        self.handle('UserPromptSubmit', session, cwd, prompt=prompt, **ids)
        self.handle('Stop', session, cwd, last_assistant_message=response, **ids)

    def records(self):
        return sorted((self.root / 'memory/sessions').glob('*-auto-*.md'))

    def rel(self, path):
        return path.relative_to(self.root).as_posix()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = setup.main(['--config', str(self.config), '--cwd', str(self.project), 'run', *args])
        return code, json.loads(out.getvalue() or err.getvalue())

    def cite(self, *records, section='Decisions', text='SQLite was chosen by the user.'):
        links = ', '.join(f'[[{wiki_target(self.root, r)}]]' for r in records)
        body = self.note.read_text()
        body = re.sub(r'(## ' + section + r'\n\n)None recorded yet\.', lambda m: m[1] + f'{text} Evidence: {links}.', body)
        self.note.write_text(body)

    def legacy(self, name='2026-01-02-auto-' + 'a' * 24 + '.md', project=None, cwd=None):
        project = project or self.info['project']
        relative = 'memory/sessions/' + name
        (self.root / relative).write_text(
            '---\ncreated: 2026-01-02\nupdated: 2026-01-02\nstatus: recorded\n---\n# Automatic conversation record\n\n'
            f'Project: [[{wiki_target(self.root, project)}|Project state]].\n\nSource: legacy turn.\n\n'
            '## Recorded user request\n\n```text\nLEGACY_REQUEST\n```\n\n'
            '## Recorded assistant response\n\n```text\nLEGACY_RESPONSE\n```\n')
        with brain.writer(self.root):
            hooks.add_record(self.root, project, relative, '2026-01-02', '2026-01-02 conversation')
        return relative

    def assert_valid(self):
        self.assertEqual(check(self.root)[0], [])

    def opened_during(self, action):
        """Resolved paths opened through pathlib while action runs."""
        opened, real_open, real_read = [], Path.open, Path.read_text

        def record_open(path, *args, **kwargs):
            opened.append(path.resolve())
            return real_open(path, *args, **kwargs)

        def record_read(path, *args, **kwargs):
            opened.append(path.resolve())
            return real_read(path, *args, **kwargs)
        with patch.object(Path, 'open', record_open), patch.object(Path, 'read_text', record_read):
            action()
        return opened


class SessionRecordTests(CompileCase):
    def test_one_file_per_session_with_turn_sections_and_idempotent_retry(self):
        for number in range(3):
            self.turn(f'Request {number}', f'Response {number}')
        record, = self.records()
        text = record.read_text()
        self.assertEqual(len(brain.turns(text)), 3)
        self.assertEqual([m for m in re.findall(r'^## Turn (\d+) — \d\d:\d\d$', text, re.M)], ['1', '2', '3'])
        meta = brain.frontmatter(text)
        self.assertEqual((meta['status'], meta['compiled_through']), ('recorded', '0'))
        self.assertEqual(meta['session'], hooks.session_key(self.info, {'session_id': 's1'}))
        self.assertLess(text.index('Request 0'), text.index('Request 2'))
        index = (self.root / hooks.records_index(self.info['project'])).read_text()
        self.assertEqual(index.count('conversation]]'), 1)
        before = {p: p.read_text() for p in self.root.rglob('*.md')}
        self.handle('Stop', last_assistant_message='Response 2')  # Retry of the same turn.
        self.assertEqual(before, {p: p.read_text() for p in self.root.rglob('*.md')})
        self.assertIn(self.rel(record)[:-3], self.note.read_text())
        self.assert_valid()

    def test_two_sessions_two_files_and_part_rollover(self):
        self.turn('first session', 'answer one', session='a')
        self.turn('second session', 'answer two', session='b')
        self.assertEqual(len(self.records()), 2)
        with patch.object(brain, 'RECORD_LIMIT', 2500):
            for number in range(4):
                self.turn(f'long {number} ' + 'x' * 600, f'reply {number} ' + 'y' * 600, session='c')
        parts = [p for p in self.records() if hooks.session_key(self.info, {'session_id': 'c'}) in p.name]
        self.assertGreater(len(parts), 1)
        self.assertTrue(any(p.name.endswith('-2.md') for p in parts))
        for part in parts:
            text = part.read_text()
            self.assertLessEqual(len(text), 2500)
            self.assertEqual(brain.frontmatter(text)['compiled_through'], '0')
        newest = max(parts, key=lambda p: int(brain.RECORD_NAME.fullmatch(p.name)[2] or 1))
        self.assertIn('reply 3', newest.read_text())
        self.assertIn('(part ', newest.read_text())
        self.assertIn(wiki_target(self.root, self.rel(newest)), self.note.read_text())
        index = (self.root / hooks.records_index(self.info['project'])).read_text()
        self.assertEqual(len(re.findall(r'conversation(?: part \d+)?\]\]', index)), 2 + len(parts))
        state = brain.load_json(hooks.session_path(self.info, {'session_id': 'c'}), {})
        self.assertEqual(state['record'], self.rel(newest))
        self.assertEqual(sorted(state['records']), sorted(self.rel(p) for p in parts))
        self.assert_valid()

    def test_record_keeps_its_date_and_survives_lost_state(self):
        with patch.object(brain, 'day', return_value='2026-03-01'):
            self.turn('before midnight', 'one')
        with patch.object(brain, 'day', return_value='2026-03-02'):
            self.turn('after midnight', 'two')
            hooks.session_path(self.info, {'session_id': 's1'}).unlink()
            self.turn('state lost', 'three')
        record, = self.records()
        self.assertTrue(record.name.startswith('2026-03-01-auto-'))
        meta = brain.frontmatter(record.read_text())
        self.assertEqual((meta['created'], meta['updated']), ('2026-03-01', '2026-03-02'))
        self.assertEqual(len(brain.turns(record.read_text())), 3)

    def test_quoted_marker_inside_a_response_is_not_a_turn(self):
        self.turn('quote', '## Turn 9 — 00:00\n<!-- turn:' + 'b' * 24 + ' -->\n'
                  '## Turn 10 — 00:00\n<!-- turn:I=;R=' + 'b' * 24 + ' -->\n')
        record, = self.records()
        self.assertEqual(len(brain.turns(record.read_text())), 1)

    def test_brief_shows_latest_turn_of_latest_record(self):
        for number in range(3):
            self.turn(f'TURN_REQUEST_{number} ' + 'r' * 900, f'TURN_RESPONSE_{number} ' + 's' * 900)
        text = brain.brief(self.root, self.info['project'], limit=8000)
        self.assertIn('TURN_REQUEST_2', text)
        self.assertNotIn('TURN_REQUEST_0', text)
        self.assertIn('[earlier turns omitted; read the file]', text)
        self.assertIn('Automatic conversation record', text)
        record, = self.records()
        self.assertIn(f'[truncated: {self.rel(record)} shows ', text)
        start = self.handle('SessionStart', session='fresh')['hookSpecificOutput']['additionalContext']
        self.assertIn('TURN_REQUEST_2', start)

    def test_task_notification_is_recorded_as_notification_without_context(self):
        self.handle('SessionStart', source='startup')
        (self.root / 'wiki/topics/build.md').write_text('---\ncreated: 2026-01-01\nupdated: 2026-01-01\n'
                                                       'status: active\n---\n# Build\n\nbackground build task\n')
        self.assertEqual(self.handle('UserPromptSubmit', prompt='  <task-notification>background build task '
                                     'finished</task-notification>', prompt_id='p-1'), {})
        self.handle('Stop', last_assistant_message='Build finished cleanly.', prompt_id='p-1')
        self.turn('real user request about the build', 'Answer.', prompt_id='p-2')
        record, = self.records()
        text = record.read_text()
        self.assertEqual(text.count('### System notification'), 1)
        self.assertEqual(text.count('### Request'), 1)
        self.assertLess(text.index('### System notification'), text.index('real user request'))
        self.assertIn('<task-notification>', text)

    def test_prompt_id_identifies_turns(self):
        self.handle('UserPromptSubmit', prompt='same', prompt_id='p-1')
        self.handle('Stop', last_assistant_message='same answer', prompt_id='p-1')
        self.handle('Stop', last_assistant_message='same answer', prompt_id='p-1')
        self.handle('UserPromptSubmit', prompt='same', prompt_id='p-2')
        self.handle('Stop', last_assistant_message='same answer', prompt_id='p-2')
        state = brain.load_json(hooks.session_path(self.info, {'session_id': 's1'}), {})
        self.assertEqual(state['turn'], 'p-2')
        record, = self.records()
        self.assertEqual(len(brain.turns(record.read_text())), 2)


class CompileTests(CompileCase):
    def test_list_cite_done_status(self):
        self.turn('Use SQLite for storage.', 'Agreed; SQLite it is.', session='a')
        self.turn('Thanks', 'You are welcome.', session='b')
        code, listing = self.run_cli('compile')
        self.assertEqual(code, 0)
        records = [r['path'] for r in listing['records']]
        self.assertEqual(len(records), 2)
        self.assertEqual(listing['pending_total'], 2)
        self.assertEqual(listing['targets']['project'], self.info['project'])
        self.assertTrue(listing['instructions'])
        self.assertEqual({(r['from_turn'], r['through_turn']) for r in listing['records']}, {(1, 1)})
        self.assertEqual(listing['records'][0]['link'], '[[' + wiki_target(self.root, records[0]) + ']]')
        batch = brain.load_json(brain.compile_batch(self.root, self.info['project']), {})
        self.assertEqual(batch['token'], listing['token'])
        self.assertTrue(all(len(r['sha256']) == 64 for r in batch['records']))
        sqlite = next(r for r in records if 'Use SQLite' in (self.root / r).read_text())
        trivial = next(r for r in records if r != sqlite)
        self.cite(sqlite)
        code, done = self.run_cli('compile', '--done', listing['token'], '--updated', self.info['project'],
                                  '--skip', trivial)
        self.assertEqual(code, 0, done)
        self.assertEqual((done['compiled'], done['skipped'], done['pending_total']), ([sqlite], [trivial], 0))
        for record in records:
            meta = brain.frontmatter((self.root / record).read_text())
            self.assertEqual((meta['status'], meta['compiled_through'], meta['compiled']), ('compiled', '1', brain.day()))
        self.assertEqual(self.run_cli('compile', '--status')[1], {'pending_total': 0, 'oldest': None, 'records': [], 'queued': 0})
        self.assertIn('Last compiled: ' + brain.day(), self.note.read_text())
        self.assertIn('Compiled automatic records for', (self.root / 'log.md').read_text())
        self.assertFalse(brain.compile_batch(self.root, self.info['project']).exists())
        self.assertEqual(self.run_cli('compile')[1], {'records': [], 'pending_total': 0})
        self.assert_valid()
        # A new turn makes a compiled record pending again from the next turn.
        self.turn('Also add an index.', 'Index added.', session='a')
        status = brain.compile_status(self.root, self.info['project'])
        self.assertEqual(status['pending_total'], 1)
        self.assertEqual((status['records'][0]['from_turn'], status['records'][0]['through_turn']), (2, 2))
        self.assertEqual(brain.frontmatter((self.root / sqlite).read_text())['status'], 'recorded')

    def test_done_rejections(self):
        self.turn('Decide the queue.', 'Use Redis.', session='a')
        listing = brain.compile_list(self.root, self.info['project'])
        record = listing['records'][0]['path']
        token = listing['token']
        bad = self.root / 'memory/sessions/notes.md'
        bad.write_text(f'---\ncreated: 2026-01-01\nupdated: 2026-01-01\nstatus: active\n---\n[[{wiki_target(self.root, record)}]]\n')
        outside = self.root / 'inbox/notes.md'
        outside.write_text(bad.read_text())
        sessions = {p: p.read_text() for p in self.records()}
        cases = [
            ('does not match', dict(token='wrong', updated=[self.info['project']], skipped=[record])),
            ('does not cite', dict(token=token, updated=['memory/profile.md'])),
            ('outside memory/sessions', dict(token=token, updated=[self.rel(bad)])),
            ('curated wiki', dict(token=token, updated=[self.rel(outside)])),
            ('permitted vault area|parent traversal', dict(token=token, updated=['../outside.md'])),
            ('does not exist', dict(token=token, updated=['memory/decisions/missing.md'])),
            ('at least one', dict(token=token)),
            ('not in this batch', dict(token=token, skipped=['memory/sessions/other.md'])),
        ]
        for pattern, kwargs in cases:
            with self.subTest(pattern):
                with self.assertRaisesRegex(ValueError, pattern):
                    brain.compile_done(self.root, self.info['project'], **kwargs)
        self.assertEqual(sessions, {p: p.read_text() for p in self.records()})  # Rejections change nothing.
        self.cite(record, section='Goal', text='Queue goal.')  # Cites the first record only.
        self.turn('Second session.', 'Other answer.', session='b')
        listing = brain.compile_list(self.root, self.info['project'])
        self.assertEqual(len(listing['records']), 2)
        with self.assertRaisesRegex(ValueError, 'Cite each batch record'):
            brain.compile_done(self.root, self.info['project'], listing['token'], [self.info['project']])
        with self.assertRaisesRegex(ValueError, 'does not match'):
            brain.compile_done(self.root, self.info['project'], token, [self.info['project']], [listing['records'][1]['path']])
        self.assertEqual(brain.compile_status(self.root, self.info['project'])['pending_total'], 2)
        code, result = self.run_cli('compile', '--updated', self.info['project'])
        self.assertEqual(code, 1)
        self.assertIn('require --done', result['error'])

    def test_structural_issue_in_updated_note_blocks_done(self):
        self.turn('Decide the queue.', 'Use Redis.', session='a')
        listing = brain.compile_list(self.root, self.info['project'])
        topic = self.root / 'wiki/topics/queues.md'
        topic.write_text(f'# Queues\n\nRedis was proposed. [[{wiki_target(self.root, listing["records"][0]["path"])}]]\n')
        with self.assertRaisesRegex(ValueError, 'Structural validation failed'):
            brain.compile_done(self.root, self.info['project'], listing['token'], ['wiki/topics/queues.md'])
        self.assertEqual(brain.compile_status(self.root, self.info['project'])['pending_total'], 1)

    def test_turn_appended_after_listing_stays_pending(self):
        self.turn('One', 'First.', session='a')
        self.turn('Two', 'Second.', session='a')
        listing = brain.compile_list(self.root, self.info['project'])
        entry, = listing['records']
        self.assertEqual((entry['from_turn'], entry['through_turn']), (1, 2))
        self.turn('Three', 'Third.', session='a')
        self.cite(entry['path'])
        done = brain.compile_done(self.root, self.info['project'], listing['token'], [self.info['project']])
        self.assertEqual(done['pending_total'], 1)
        meta = brain.frontmatter((self.root / entry['path']).read_text())
        self.assertEqual((meta['status'], meta['compiled_through']), ('recorded', '2'))
        again = brain.compile_list(self.root, self.info['project'])
        self.assertEqual((again['records'][0]['from_turn'], again['records'][0]['through_turn']), (3, 3))
        self.assert_valid()

    def test_other_projects_records_are_never_listed(self):
        other = self.base / 'other'
        other.mkdir()
        info = setup.initialize(self.config, other)
        self.turn('OTHER_PROJECT request', 'OTHER_PROJECT answer', session='x', cwd=other)
        self.legacy('2026-01-03-auto-' + 'c' * 24 + '.md', project=info['project'])
        self.assertEqual(brain.compile_list(self.root, self.info['project']), {'records': [], 'pending_total': 0})
        self.assertEqual(brain.compile_status(self.root, info['project'])['pending_total'], 2)
        self.turn('mine', 'mine answer', session='y')
        listing = brain.compile_list(self.root, self.info['project'])
        self.assertEqual(len(listing['records']), 1)
        self.assertNotIn('OTHER_PROJECT', (self.root / listing['records'][0]['path']).read_text())
        doctor = [f for f in brain.doctor(self.root)['findings'] if f['kind'] == 'pending_compile']
        self.assertEqual({(f['project'], f['count']) for f in doctor}, {(self.info['project'], 1), (info['project'], 2)})
        self.assertEqual({f['project']: f['oldest'] for f in doctor}[info['project']], '2026-01-02')

    def test_legacy_records_are_listed_and_compiled(self):
        legacy = self.legacy()
        self.turn('new style', 'new answer', session='a')
        listing = brain.compile_list(self.root, self.info['project'])
        self.assertEqual(listing['records'][0]['path'], legacy)  # Oldest first.
        self.assertEqual((listing['records'][0]['from_turn'], listing['records'][0]['through_turn']), (1, 1))
        self.cite(legacy)
        brain.compile_done(self.root, self.info['project'], listing['token'], [self.info['project']],
                           [listing['records'][1]['path']])
        meta = brain.frontmatter((self.root / legacy).read_text())
        self.assertEqual((meta['status'], meta['compiled_through']), ('compiled', '1'))
        self.assertIn('LEGACY_RESPONSE', (self.root / legacy).read_text())
        self.assertEqual(brain.compile_status(self.root, self.info['project'])['pending_total'], 0)
        self.assert_valid()

    def test_limit_and_new_listing_replaces_batch(self):
        for session in 'abc':
            self.turn(f'request {session}', f'answer {session}', session=session)
        first = brain.compile_list(self.root, self.info['project'], limit=2)
        self.assertEqual((len(first['records']), first['pending_total']), (2, 3))
        second = brain.compile_list(self.root, self.info['project'], limit=2)
        self.assertNotEqual(first['token'], second['token'])
        self.cite(*[r['path'] for r in second['records']])
        with self.assertRaisesRegex(ValueError, 'does not match'):
            brain.compile_done(self.root, self.info['project'], first['token'], [self.info['project']])
        done = brain.compile_done(self.root, self.info['project'], second['token'], [self.info['project']])
        self.assertEqual(done['pending_total'], 1)


class ProjectNoteTests(CompileCase):
    def test_new_project_note_has_sections(self):
        text = self.note.read_text()
        for heading in brain.PROJECT_SECTIONS:
            self.assertIn(f'\n## {heading}\n\nNone recorded yet.\n', text)
        self.assertIn('# Project — ', text)
        self.assertIn('Source: explicit local project binding during setup.', text)
        self.assert_valid()

    def test_listing_appends_missing_sections_without_changing_text(self):
        old = ('---\ncreated: 2026-01-01\nupdated: 2026-01-01\nstatus: active\n---\n# Project — old\n\n'
               'No project decisions or progress recorded yet.\n\n## Status\n\nShipping v1.\n')
        self.note.write_text(old)
        self.assertEqual(brain.compile_list(self.root, self.info['project']), {'records': [], 'pending_total': 0})
        text = self.note.read_text()
        self.assertTrue(text.startswith(old))
        self.assertEqual(text.count('## Status'), 1)
        for heading in ('Goal', 'Decisions', 'Open questions', 'Next steps'):
            self.assertIn(f'\n## {heading}\n\nNone recorded yet.\n', text)
        brain.compile_list(self.root, self.info['project'])
        self.assertEqual(self.note.read_text(), text)


class NudgeTests(CompileCase):
    def context(self, **extra):
        return self.handle('SessionStart', **extra)['hookSpecificOutput']['additionalContext']

    def test_nudge_only_for_other_sessions_and_not_on_compact(self):
        self.assertNotIn(NUDGE, self.context(session='new', source='startup'))
        self.turn('earlier work', 'done', session='old')
        self.assertNotIn(NUDGE, self.context(session='old', source='resume'))
        text = self.context(session='new', source='startup')
        self.assertIn(NUDGE + ' 1 automatic record(s)', text)
        self.assertIn('oldest ' + brain.day(), text)
        self.assertIn('run compile --done TOKEN', text)
        self.assertIn('Bundled command:', text)
        self.assertNotIn(NUDGE, self.context(session='new', source='compact'))
        self.assertIn(NUDGE, self.context(session='new'))  # Missing source counts as a fresh start.
        # A primed prompt never carries the nudge; the unprimed full-payload fallback stands in for SessionStart.
        (self.root / 'wiki/topics/work.md').write_text('---\ncreated: 2026-01-01\nupdated: 2026-01-01\n'
                                                      'status: active\n---\n# Work\n\nearlier work notes\n')
        primed = self.handle('UserPromptSubmit', session='new', prompt='earlier work')
        self.assertNotIn(NUDGE, primed['hookSpecificOutput']['additionalContext'])
        unprimed = self.handle('UserPromptSubmit', session='unprimed', prompt='earlier work')
        self.assertIn(NUDGE + ' 1 automatic record(s)', unprimed['hookSpecificOutput']['additionalContext'])
        again = self.handle('UserPromptSubmit', session='unprimed', prompt='earlier work')
        self.assertNotIn(NUDGE, again['hookSpecificOutput']['additionalContext'])
        listing = brain.compile_list(self.root, self.info['project'])
        self.cite(listing['records'][0]['path'])
        brain.compile_done(self.root, self.info['project'], listing['token'], [self.info['project']])
        self.assertNotIn(NUDGE, self.context(session='later', source='startup'))



class SelfCompileTests(CompileCase):
    """An agent that curates its own session marks it compiled, including the turn that ran compile --done."""

    def handle(self, kind, session='s1', cwd=None, **extra):
        # Like Claude Code, each prompt carries a prompt_id that its Stop repeats.
        current = self.__dict__.setdefault('prompts', {})
        if kind == 'UserPromptSubmit':
            current[session] = extra.setdefault('prompt_id', f'prompt-{len(self.__dict__.setdefault("ids", []))}')
            self.ids.append(current[session])
        elif kind == 'Stop' and session in current:
            extra.setdefault('prompt_id', current[session])
        return super().handle(kind, session, cwd, **extra)

    def key(self, session):
        text = self.handle('SessionStart', session, source='startup')['hookSpecificOutput']['additionalContext']
        return re.search(r"This session's record key: ([0-9a-f]{24})\.", text)[1]

    def meta(self, record):
        return brain.frontmatter((self.root / record).read_text())

    def self_compile(self, session, cite=True):
        code, listing = self.run_cli('compile', '--session', self.key(session))
        self.assertEqual(code, 0, listing)
        if cite and listing['records']:
            self.cite(*[r['path'] for r in listing['records']])
        code, done = self.run_cli('compile', '--done', listing['token'], '--updated', self.info['project'])
        self.assertEqual(code, 0, done)
        return listing, done

    def test_own_session_is_compiled_through_the_turn_that_ran_done(self):
        self.turn('earlier task', 'other session work', session='other')
        self.turn('Use SQLite for storage.', 'Agreed.', session='me')
        self.turn('Add an index.', 'Index added.', session='me')
        self.handle('UserPromptSubmit', 'me', prompt='Save this to memory.')
        listing, done = self.self_compile('me')
        record, = [r['path'] for r in listing['records']]  # Only this session's record, never another's.
        self.assertEqual((listing['session'], listing['records'][0]['through_turn']), (self.key('me'), 2))
        self.assertNotIn('runs', listing)
        self.assertEqual(done['compiled'], [record])
        self.assertEqual(self.meta(record)['compiled_through'], '2')
        self.handle('Stop', 'me', last_assistant_message='Saved; the notes are up to date.')
        meta = self.meta(record)
        self.assertEqual((meta['status'], meta['compiled_through'], meta['compiled']), ('compiled', '3', brain.day()))
        self.assertFalse(brain.self_marker(self.root, self.key('me')).exists())
        status = brain.compile_status(self.root, self.info['project'])
        self.assertEqual([r['path'] for r in status['records']], [self.rel(r) for r in self.records() if self.rel(r) != record])
        self.assertIn(NUDGE + ' 1 automatic record(s)', self.handle('SessionStart', 'new')['hookSpecificOutput']['additionalContext'])
        # The marker covered one turn only: the next turn is pending again.
        self.turn('One more thing.', 'Done.', session='me')
        self.assertEqual((self.meta(record)['status'], self.meta(record)['compiled_through']), ('recorded', '3'))
        self.assert_valid()

    def test_self_compile_in_consecutive_turns(self):
        self.turn('first', 'one', session='me')
        record = self.rel(self.records()[0])
        for number in (2, 3):  # The second listing is empty (all compiled) yet still covers its own turn.
            self.handle('UserPromptSubmit', 'me', prompt=f'task {number}')
            listing, _ = self.self_compile('me')
            self.handle('Stop', 'me', last_assistant_message=f'answer {number}')
            self.assertEqual((self.meta(record)['status'], self.meta(record)['compiled_through']), ('compiled', str(number)))
        self.assertEqual(listing['records'], [])

    def test_first_turn_self_compile_before_any_record_exists(self):
        self.handle('UserPromptSubmit', 'fresh', prompt='Decide the storage.')
        listing, done = self.self_compile('fresh')
        self.assertEqual((listing['records'], done['compiled']), ([], []))
        self.handle('Stop', 'fresh', last_assistant_message='Decided and saved.')
        record, = self.records()
        meta = self.meta(self.rel(record))
        self.assertEqual((meta['status'], meta['compiled_through']), ('compiled', '1'))
        self.assertEqual(brain.compile_status(self.root, self.info['project'])['pending_total'], 0)

    def test_marker_is_one_shot_fresh_and_dropped_by_later_sections(self):
        self.turn('first', 'one', session='me')
        record = self.rel(self.records()[0])
        # A subagent result arriving after --done was not folded in: the turn stays pending.
        self.handle('UserPromptSubmit', 'me', prompt='second')
        self.self_compile('me')
        self.handle('SubagentStop', 'me', agent_id='agent1', agent_type='Explore', last_assistant_message='Found it.')
        self.handle('Stop', 'me', last_assistant_message='two')
        self.assertEqual((self.meta(record)['status'], self.meta(record)['compiled_through']), ('recorded', '1'))
        # A marker past its age is not honoured, even by its own turn.
        self.handle('UserPromptSubmit', 'me', prompt='later')
        self.self_compile('me')
        marker = brain.self_marker(self.root, self.key('me'))
        data = json.loads(marker.read_text())
        marker.write_text(json.dumps(dict(data, at=data['at'] - brain.SELF_COMPILE_AGE - 1)))
        self.handle('Stop', 'me', last_assistant_message='three')
        self.assertEqual((self.meta(record)['status'], self.meta(record)['compiled_through']), ('recorded', '2'))
        self.assertFalse(marker.exists())
        # A turn recorded between listing and --done gets no marker, so it and the next turn stay pending.
        self.handle('UserPromptSubmit', 'me', prompt='list')
        listing = self.run_cli('compile', '--session', self.key('me'))[1]
        self.cite(record)
        self.turn('meanwhile', 'four', session='me')
        self.assertEqual(self.run_cli('compile', '--done', listing['token'], '--updated', self.info['project'])[0], 0)
        self.assertFalse(marker.exists())
        self.assertEqual((self.meta(record)['status'], self.meta(record)['compiled_through']), ('recorded', '3'))

    def test_abandoned_marker_never_marks_the_next_turn(self):
        self.turn('first', 'one', session='me')
        record = self.rel(self.records()[0])
        self.handle('UserPromptSubmit', 'me', prompt='curate')
        self.self_compile('me')  # This turn's Stop is never delivered.
        marker = brain.self_marker(self.root, self.key('me'))
        self.assertTrue(marker.exists())
        self.turn('next task, not curated', 'two', session='me')
        self.assertFalse(marker.exists())
        self.assertEqual((self.meta(record)['status'], self.meta(record)['compiled_through']), ('recorded', '1'))

    def test_no_marker_while_the_session_has_queued_writes(self):
        self.turn('first', 'one', session='me')
        key = self.key('me')
        spooled = brain.internal(self.root, brain.SPOOL) / f'{1:020d}-{key}-{"0" * 32}.json'
        spooled.parent.mkdir(parents=True, exist_ok=True)
        spooled.write_text('{}')
        other = brain.internal(self.root, brain.SPOOL) / f'{2:020d}-{"e" * 24}-{"0" * 32}.json'
        other.write_text('{}')
        listing = brain.compile_list(self.root, self.info['project'], session=key)
        self.cite(*[r['path'] for r in listing['records']])
        brain.compile_done(self.root, self.info['project'], listing['token'], [self.info['project']])
        self.assertFalse(brain.self_marker(self.root, key).exists())
        spooled.unlink()  # Another session's queued writes do not matter.
        listing = brain.compile_list(self.root, self.info['project'], session=key)
        brain.compile_done(self.root, self.info['project'], listing['token'], [self.info['project']])
        self.assertTrue(brain.self_marker(self.root, key).exists())

    def test_expired_markers_are_swept_and_automation_off_removes_markers(self):
        self.turn('first', 'one', session='me')
        self.self_compile('me')
        marker = brain.self_marker(self.root, self.key('me'))
        old = marker.stat().st_mtime - brain.SELF_COMPILE_AGE - 10
        os.utime(marker, (old, old))
        self.handle('SessionStart', 'someone-else', source='startup')
        self.assertFalse(marker.exists())
        self.self_compile('me')
        self.assertTrue(marker.exists())
        setup.automation(self.config, self.project, 'off')
        self.assertFalse(marker.exists())

    def test_a_late_marker_from_an_earlier_turn_is_never_taken(self):
        self.turn('first', 'one', session='me')
        record = self.rel(self.records()[0])
        self.handle('UserPromptSubmit', 'me', prompt='curate')
        listing = self.run_cli('compile', '--session', self.key('me'))[1]
        self.cite(record)
        # A slow --done of that turn finishes only after the next prompt (whose hook could not wait for the lock).
        self.handle('UserPromptSubmit', 'me', prompt='next task, not curated')
        self.assertEqual(self.run_cli('compile', '--done', listing['token'], '--updated', self.info['project'])[0], 0)
        self.handle('Stop', 'me', last_assistant_message='two')
        self.assertEqual((self.meta(record)['status'], self.meta(record)['compiled_through']), ('recorded', '1'))
        self.assertFalse(brain.self_marker(self.root, self.key('me')).exists())

    def test_rollover_keeps_the_self_compiled_turn(self):
        self.turn('first', 'one', session='me')
        self.handle('UserPromptSubmit', 'me', prompt='curate')
        self.self_compile('me')
        with patch.object(brain, 'RECORD_LIMIT', 10):  # The next turn starts part 2.
            self.handle('Stop', 'me', last_assistant_message='Saved.')
        first, second = sorted(self.records(), key=lambda p: (len(p.name), p.name))
        self.assertTrue(second.name.endswith('-2.md'))
        meta = self.meta(self.rel(second))
        self.assertEqual((meta['status'], meta['compiled_through']), ('compiled', '1'))
        self.assertEqual(brain.compile_status(self.root, self.info['project'])['pending_total'], 0)

    def test_a_delayed_earlier_stop_never_takes_the_current_turns_marker(self):
        self.turn('first', 'one', session='me')
        record = self.rel(self.records()[0])
        self.handle('UserPromptSubmit', 'me', prompt='turn A', prompt_id='A')  # A's Stop is delayed.
        self.handle('UserPromptSubmit', 'me', prompt='turn B, curates', prompt_id='B')
        self.self_compile('me')
        self.handle('Stop', 'me', last_assistant_message='A, not curated', prompt_id='A')
        self.assertEqual((self.meta(record)['status'], self.meta(record)['compiled_through']), ('recorded', '1'))

    def test_without_client_turn_ids_no_turn_is_marked(self):
        self.turn('first', 'one', session='me', prompt_id='x')
        record = self.rel(self.records()[0])
        CompileCase.handle(self, 'UserPromptSubmit', 'me', prompt='curate')  # A client that sends no ids.
        self.self_compile('me')
        self.assertFalse(brain.self_marker(self.root, self.key('me')).exists())
        CompileCase.handle(self, 'Stop', 'me', last_assistant_message='Saved.')
        self.assertEqual((self.meta(record)['status'], self.meta(record)['compiled_through']), ('recorded', '1'))

    def test_session_option_is_validated(self):
        self.turn('first', 'one', session='me')
        for args in (['compile', '--session', 'not-a-key'], ['compile', '--status', '--session', self.key('me')],
                     ['compile', '--done', 'x', '--session', self.key('me'), '--updated', self.info['project']]):
            code, result = self.run_cli(*args)
            self.assertNotEqual(code, 0, args)
        # An empty own batch still requires an updated note that exists and passes validation.
        listing = self.run_cli('compile', '--session', 'f' * 24)[1]
        self.assertEqual(listing['records'], [])
        self.assertNotEqual(self.run_cli('compile', '--done', listing['token'], '--updated', 'memory/missing.md')[0], 0)
        self.assertFalse(brain.self_marker(self.root, 'f' * 24).exists())

class FixRoundTests(CompileCase):
    """Regressions from the phase-2 review; each failed before its fix."""

    def total_turns(self):
        return sum(len(brain.turns(p.read_text())) for p in self.records())

    def test_delayed_retry_of_an_earlier_part_is_a_noop(self):
        self.handle('UserPromptSubmit', prompt='continue', prompt_id='first')
        first = dict(last_assistant_message='Original reply', prompt_id='first')
        self.handle('Stop', **first)
        with patch.object(brain, 'RECORD_LIMIT', 2500):
            for number in range(4):
                self.turn('continue', f'Later reply {number} ' + 'z' * 900, prompt_id=f'later-{number}')
            self.assertGreater(len(self.records()), 1)
            before = {p: p.read_text() for p in self.records()}
            self.handle('Stop', **first)  # Delayed retry; its section is in part 1.
        self.assertEqual(before, {p: p.read_text() for p in self.records()})
        self.assertEqual(self.total_turns(), 5)

    def test_stop_never_takes_another_turns_request_or_label(self):
        self.handle('UserPromptSubmit', prompt='REQUEST_ONE', prompt_id='p-1')
        self.handle('Stop', last_assistant_message='answer one', prompt_id='p-1')
        # UserPromptSubmit p-2 was missed; Stop p-2 must not reuse REQUEST_ONE.
        self.handle('Stop', last_assistant_message='answer two', prompt_id='p-2')
        self.handle('UserPromptSubmit', prompt='<task-notification>done</task-notification>', prompt_id='p-3')
        self.handle('Stop', last_assistant_message='answer four', prompt_id='p-4')
        record, = self.records()
        text = record.read_text()
        sections = [text[o:] for o, *_ in brain.turns(text)]
        self.assertIn('REQUEST_ONE', sections[0].split('### Response')[0])
        for section in sections[1:]:
            request = section.split('### Response')[0]
            self.assertIn('[prompt not captured for this turn]', request)
            self.assertNotIn('REQUEST_ONE', request)
            self.assertIn('### Request', request)
            self.assertNotIn('### System notification', request)
        # Generated fallback tokens never count as a mismatch: a prompt without ids pairs with any Stop id.
        self.handle('UserPromptSubmit', session='codex', prompt='CODEX_PROMPT')
        self.handle('Stop', session='codex', last_assistant_message='codex answer', turn_id='t-9')
        codex = next(p for p in self.records() if p != record)
        self.assertIn('CODEX_PROMPT', codex.read_text())

    def test_failed_done_leaves_every_record_untouched(self):
        self.turn('first', 'one', session='a')
        self.turn('second', 'two', session='b')
        listing = brain.compile_list(self.root, self.info['project'])
        first, second = (self.root / r['path'] for r in listing['records'])
        second.write_text(re.sub(r'\A---\n.*?\n---\n', '', second.read_text(), flags=re.S))
        before = {p: p.read_text() for p in self.root.rglob('*.md')}
        with self.assertRaises(ValueError):
            brain.compile_done(self.root, self.info['project'], listing['token'], [],
                               [self.rel(first), self.rel(second)])
        self.assertEqual(before, {p: p.read_text() for p in self.root.rglob('*.md')})
        self.assertTrue(brain.compile_batch(self.root, self.info['project']).exists())

    def test_edit_to_listed_turns_requires_a_new_listing(self):
        self.turn('Decide storage.', 'ORIGINAL_ANSWER', session='a')
        listing = brain.compile_list(self.root, self.info['project'])
        record = self.root / listing['records'][0]['path']
        record.write_text(record.read_text().replace('ORIGINAL_ANSWER', 'REPLACED_ANSWER'))
        self.cite(self.rel(record))
        with self.assertRaisesRegex(ValueError, 'changed since listing'):
            brain.compile_done(self.root, self.info['project'], listing['token'], [self.info['project']])
        self.assertEqual(brain.frontmatter(record.read_text())['status'], 'recorded')
        relisted = brain.compile_list(self.root, self.info['project'])
        done = brain.compile_done(self.root, self.info['project'], relisted['token'], [self.info['project']])
        self.assertEqual(done['pending_total'], 0)

    def test_last_compiled_updates_the_real_line_not_code_examples(self):
        example = '```text\nLast compiled: EXAMPLE\n```\n'
        self.note.write_text(self.note.read_text() + '\n' + example + '\nInline `Last compiled: CODE` too.\n')
        for real in (False, True):
            if real:  # A stale real line after the example must be the one updated.
                self.note.write_text(self.note.read_text().replace('\nLast compiled: ' + brain.day() + '\n',
                                                                   '\nLast compiled: 2020-01-01\n'))
                self.assertIn('Last compiled: 2020-01-01', self.note.read_text())
            self.turn(f'request {real}', f'answer {real}', session=f's-{real}')
            listing = brain.compile_list(self.root, self.info['project'])
            self.cite(listing['records'][0]['path'], section='Status' if real else 'Goal', text='Noted.')
            brain.compile_done(self.root, self.info['project'], listing['token'], [self.info['project']])
            text = self.note.read_text()
            self.assertIn(example, text)
            self.assertIn('`Last compiled: CODE`', text)
            self.assertEqual(re.findall(r'^Last compiled: (.*)$', text, re.M), ['EXAMPLE', brain.day()])
        self.assertNotIn('2020-01-01', text)
        self.assert_valid()

    def test_brief_keeps_latest_response_after_a_long_request(self):
        self.turn('early', 'early answer')
        self.turn('REQUEST_SENTINEL ' + 'q' * 3000, 'RESPONSE_SENTINEL ' + 'a' * 3000)
        record, = self.records()
        for limit in (6000, 8000):
            text = brain.brief(self.root, self.info['project'], limit=limit)
            self.assertLessEqual(len(text), limit)
            self.assertIn('REQUEST_SENTINEL', text)
            self.assertIn('RESPONSE_SENTINEL', text)
            self.assertIn('[excerpt truncated]', text)
            self.assertIn(f'[truncated: {self.rel(record)} shows ', text)
            self.assertIn(f' of {len(record.read_text())} chars; read the file]', text)
        shown = text.split('FILE: ' + self.rel(record) + '\n', 1)[1]
        self.assertLessEqual(len(shown.split('\n[truncated: ')[0]), 1800)
        self.assertEqual(shown.count('```'), 4)  # Both excerpts keep closed fences.

    def test_fenced_headings_do_not_count_as_sections(self):
        old = ('---\ncreated: 2026-01-01\nupdated: 2026-01-01\nstatus: active\n---\n# Project — old\n\n'
               'Template:\n\n```markdown\n' + ''.join(f'## {h}\n' for h in brain.PROJECT_SECTIONS) + '```\n')
        self.note.write_text(old)
        brain.compile_list(self.root, self.info['project'])
        text = self.note.read_text()
        self.assertTrue(text.startswith(old))
        for heading in brain.PROJECT_SECTIONS:
            self.assertIn(f'\n## {heading}\n\nNone recorded yet.\n', text[len(old):])



class FixRound2Tests(CompileCase):
    """Regressions from the phase-2 re-review; each failed before its fix."""

    def rolled_session(self, turns=4):
        with patch.object(brain, 'RECORD_LIMIT', 2500):
            for number in range(turns):
                self.turn('continue', f'reply {number} ' + 'z' * 900, prompt_id=f'p-{number}')
        parts = sorted(self.records(), key=lambda p: int(brain.RECORD_NAME.fullmatch(p.name)[2] or 1))
        self.assertGreater(len(parts), 2)
        return parts  # Oldest part first.

    def turn_total(self):
        return sum(len(brain.turns(p.read_text())) for p in self.records())

    def test_stop_with_or_without_its_id_is_one_turn(self):
        for session, ids in (('first-with-id', ({'prompt_id': 'p1'}, {})), ('first-without', ({}, {'prompt_id': 'p1'}))):
            with self.subTest(session):
                self.handle('UserPromptSubmit', session, prompt='same request', prompt_id='p1')
                for extra in ids:
                    self.handle('Stop', session, last_assistant_message='same answer', **extra)
                record, = [p for p in self.records() if hooks.session_key(self.info, {'session_id': session}) in p.name]
                self.assertEqual(len(brain.turns(record.read_text())), 1)

    def test_new_turn_reads_no_older_parts_and_lost_index_recovers(self):
        parts = self.rolled_session()
        older = {p.resolve() for p in parts[:-1]}
        with patch.object(brain, 'RECORD_LIMIT', 2500):
            opened = self.opened_during(lambda: self.turn('continue', 'short new reply', prompt_id='p-new'))
            self.assertFalse(older & set(opened))
            # A lost key index is rebuilt from the parts, so a delayed retry of part 1 still adds nothing.
            state_path = hooks.session_path(self.info, {'session_id': 's1'})
            state = brain.load_json(state_path, {})
            state.pop('turn_keys', None)
            brain.save_json(state_path, state)
            before = self.turn_total()
            self.handle('Stop', last_assistant_message='reply 0 ' + 'z' * 900, prompt_id='p-0')
            self.assertEqual(self.turn_total(), before)
        self.assertEqual(len(brain.load_json(state_path, {})['turn_keys']), before)

    def test_older_part_linked_outside_the_vault_is_never_read(self):
        parts = self.rolled_session()
        outside = self.base / 'archive.md'
        outside.write_text(parts[0].read_text())
        parts[0].unlink()
        parts[0].symlink_to(outside)
        state_path = hooks.session_path(self.info, {'session_id': 's1'})
        for keep_index in (True, False):  # Valid current state, then a state that must rebuild its index.
            with self.subTest(keep_index=keep_index):
                if not keep_index:
                    state = brain.load_json(state_path, {})
                    state.pop('turn_keys', None)
                    brain.save_json(state_path, state)
                opened = self.opened_during(lambda: self.turn('continue', f'after link {keep_index}',
                                                             prompt_id=f'q-{keep_index}'))
                self.assertNotIn(outside.resolve(), opened)
        self.assertNotIn('after link', outside.read_text())



class FixRound3Tests(CompileCase):
    """Turn identity: id hash I plus response hash R in each marker (phase-2 second re-review)."""

    def session_turns(self, session='s1'):
        name = hooks.session_key(self.info, {'session_id': session})
        return sum(len(brain.turns(p.read_text())) for p in self.records() if name in p.name)

    def test_markers_carry_id_and_response_hashes(self):
        self.turn('with id', 'answer', prompt_id='p-1')
        self.handle('UserPromptSubmit', prompt='without id')
        self.handle('Stop', last_assistant_message='other answer')
        record, = self.records()
        marks = re.findall(r'^<!-- turn:I=([0-9a-f]{24})?;R=([0-9a-f]{24}) -->$', record.read_text(), re.M)
        self.assertEqual(len(marks), 2)
        self.assertEqual(marks[0][0], brain.digest((hooks.session_key(self.info, {'session_id': 's1'}) + '\0p-1').encode())[:24])
        self.assertEqual(marks[1][0], '')
        state = brain.load_json(hooks.session_path(self.info, {'session_id': 's1'}), {})
        self.assertEqual(state['turn_keys'], [list(m) for m in marks])

    def test_failed_record_write_lets_the_retry_write_the_turn(self):
        self.turn('first', 'first answer', prompt_id='p-1')
        record, = self.records()
        real = brain.atomic_write

        def fail_record(path, text):
            if Path(path) == record:
                raise OSError('disk full')
            return real(path, text)
        self.handle('UserPromptSubmit', prompt='second', prompt_id='p-2')
        with patch.object(brain, 'atomic_write', fail_record):
            with self.assertRaises(OSError):
                self.handle('Stop', last_assistant_message='second answer', prompt_id='p-2')
        self.handle('Stop', last_assistant_message='second answer', prompt_id='p-2')
        self.assertEqual(len(brain.turns(record.read_text())), 2)
        self.assertIn('second answer', record.read_text())

    def test_retry_of_recent_turn_after_a_600_turn_part_rolls_over(self):
        with patch.object(os, 'fsync', lambda fd: None):
            for number in range(1, 601):
                self.handle('Stop', last_assistant_message=f'r{number}', prompt_id=f'p-{number}')
            record, = self.records()
            with patch.object(brain, 'RECORD_LIMIT', len(record.read_text()) + 5):
                self.handle('Stop', last_assistant_message='r601', prompt_id='p-601')
                self.assertEqual(len(self.records()), 2)
                for number in range(591, 601):  # Delayed retries of recent turns, now in the previous part.
                    self.handle('Stop', last_assistant_message=f'r{number}', prompt_id=f'p-{number}')
        self.assertEqual(self.session_turns(), 601)
        state = brain.load_json(hooks.session_path(self.info, {'session_id': 's1'}), {})
        self.assertEqual(len(state['turn_keys']), hooks.TURN_KEYS)
        ident = lambda n: brain.digest((hooks.session_key(self.info, {'session_id': 's1'}) + f'\0p-{n}').encode())[:24]
        self.assertEqual([pair[0] for pair in state['turn_keys'][-3:]], [ident(599), ident(600), ident(601)])

    def test_idless_prompt_then_stop_with_and_without_id_is_one_turn(self):
        for session, ids in (('id-first', ({'turn_id': 't1'}, {})), ('id-later', ({}, {'turn_id': 't1'}))):
            with self.subTest(session):
                self.handle('UserPromptSubmit', session, prompt='generated token request')
                for extra in ids:
                    self.handle('Stop', session, last_assistant_message='same answer', **extra)
                self.assertEqual(self.session_turns(session), 1)

    def test_lost_index_recovery_reads_at_most_the_newest_parts(self):
        with patch.object(brain, 'RECORD_LIMIT', 2500):
            for number in range(16):
                self.turn('continue', f'reply {number} ' + 'z' * 900, prompt_id=f'p-{number}')
            parts = sorted(self.records(), key=lambda p: int(brain.RECORD_NAME.fullmatch(p.name)[2] or 1))
            self.assertGreaterEqual(len(parts), 8)
            state_path = hooks.session_path(self.info, {'session_id': 's1'})
            state = brain.load_json(state_path, {})
            state.pop('turn_keys', None)
            brain.save_json(state_path, state)
            opened = set(self.opened_during(lambda: self.turn('continue', 'after loss', prompt_id='p-new')))
        bound = 5  # hooks.RECOVER_PARTS
        newest = {p.resolve() for p in parts[-bound:]}
        self.assertTrue(opened & newest)
        self.assertFalse({p.resolve() for p in parts[:-bound]} & opened)
        self.assertTrue(brain.load_json(state_path, {})['turn_keys'])

    def test_old_style_markers_stay_readable(self):
        self.turn('first', 'first answer', prompt_id='p-1')
        record, = self.records()
        legacy = '## Turn 9 — 00:00\n<!-- turn:' + 'c' * 24 + ' -->\n\n### Request\n\n```text\nold\n```\n\n### Response\n\n```text\nold answer\n```\n'
        record.write_text(record.read_text() + '\n' + legacy)
        self.assertEqual([t[1:] for t in brain.turns(record.read_text())][-1], ('c' * 24, ''))
        self.turn('second', 'second answer', prompt_id='p-2')
        self.assertEqual(len(brain.turns(record.read_text())), 3)


if __name__ == '__main__':
    unittest.main()
