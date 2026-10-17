"""Final-pass regressions: code-aware navigation lines, synthetic prompts, dotted project notes, an idempotent
board close, and package consistency (disposable vaults and an explicit config only)."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import brain
import memory_hooks as hooks
import package_plugin
import runs
import second_brain as setup
from check_vault import check, without_examples

EXAMPLE = ('```text\n'
           'Latest automatic record: [[memory/sessions/example-record|example]].\n'
           'Latest checkpoint: [[memory/sessions/example-note|example]].\n'
           'Latest handoff: [[memory/sessions/example-note|example]].\n'
           'Automatic records: [[memory/sessions/records/example|example]].\n'
           'Latest run: [[memory/runs/example|example]].\n'
           'updated: 1999-01-01\n'
           '```\n'
           '~~~~\n'
           'Latest checkpoint: [[memory/sessions/example-note|tilde example]].\n'
           '~~~~\n')


class FinalCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='second-brain-final-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / 'project'
        self.project.mkdir()
        self.config = self.base / 'config/config.json'
        env = patch.dict(os.environ, {'SECOND_BRAIN_CONFIG': str(self.config)})
        env.start()
        self.addCleanup(env.stop)
        for name in ('SECOND_BRAIN_VAULT', 'SECOND_BRAIN_AUTOMATION', 'SECOND_BRAIN_RUN'):
            os.environ.pop(name, None)
        self.info = setup.initialize(self.config, self.project, mode='new', path=self.base / 'vault')
        self.root = Path(self.info['root'])
        self.note = self.root / self.info['project']

    def handle(self, kind, session='s1', cwd=None, **extra):
        event = {'hook_event_name': kind, 'cwd': str(cwd or self.project), 'session_id': session, **extra}
        return hooks.handle(event, self.config)

    def turn(self, prompt, response, session='s1', cwd=None, **ids):
        self.handle('UserPromptSubmit', session, cwd, prompt=prompt, **ids)
        self.handle('Stop', session, cwd, last_assistant_message=response, **ids)

    def cli(self, *args, cwd=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = setup.main(['--config', str(self.config), '--cwd', str(cwd or self.project), 'run', *args])
        return code, json.loads(out.getvalue() or err.getvalue())

    def ok(self, *args, cwd=None):
        code, result = self.cli(*args, cwd=cwd)
        self.assertEqual(code, 0, result)
        return result

    def records(self):
        return sorted((self.root / 'memory/sessions').glob('*-auto-*.md'))

    def context(self, result):
        return result.get('hookSpecificOutput', {}).get('additionalContext', '')

    def assert_valid(self):
        self.assertEqual(check(self.root)[0], [])

    def real_lines(self, text, prefix):
        """Lines starting with prefix outside code, by an independent reading (check_vault's own code filter)."""
        return [l for l in without_examples(text).splitlines() if l.startswith(prefix)]

    def with_example(self, frontmatter=True):
        """The project note with the navigation-line examples above its sections."""
        text = self.note.read_text()
        head, rest = text.split('# Project', 1)
        if not frontmatter:
            head = '---\ncreated: 2026-01-01\nstatus: active\n---\n'
        self.note.write_text(head + '# Project' + rest.replace('\n## Goal', '\nNavigation line format:\n\n' + EXAMPLE + '\n## Goal', 1))
        (self.root / 'memory/sessions/example-note.md').write_text(
            '---\ncreated: 2026-01-01\nupdated: 2026-01-01\nstatus: active\n---\n# Example\n\nEXAMPLE_ONLY_TEXT\n')
        brain.add_index(self.root, 'memory/sessions/example-note.md', 'example')


    def queued_stop(self, session, prompt, response):
        """A turn whose Stop finds the vault lock busy, so its write waits in the spool."""
        self.handle('UserPromptSubmit', session, prompt=prompt, prompt_id=session + '-1')

        @contextmanager
        def busy(root, wait=None):
            yield False
        with patch.object(brain, 'try_writer', busy):
            self.handle('Stop', session, last_assistant_message=response, prompt_id=session + '-1')
        self.assertEqual(len(runs.spool_names(self.root)), 1)

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
            result = action()
        return opened, result


class CodeAwareNavigationTests(FinalCase):
    def test_stop_hook_rewrites_only_real_pointer_lines_and_frontmatter_updated(self):
        self.with_example(frontmatter=False)  # No updated field: the only updated: line is the code example.
        self.turn('first request', 'FIRST_ANSWER', prompt_id='p1')
        self.turn('second request', 'SECOND_ANSWER', session='s2', prompt_id='p2')
        text = self.note.read_text()
        self.assertEqual(text.count(EXAMPLE), 1)  # Byte for byte: no example line was rewritten.
        newest = next(r for r in self.records() if 'SECOND_ANSWER' in r.read_text())
        pointer, = self.real_lines(text, 'Latest automatic record:')
        self.assertIn(brain.link_target(self.root, newest.relative_to(self.root).as_posix()), pointer)
        self.assertEqual(len(self.real_lines(text, 'Automatic records:')), 1)
        self.assertEqual(brain.frontmatter(text)['updated'], brain.day())
        self.assertNotIn('EXAMPLE_ONLY_TEXT', brain.brief(self.root, self.info['project']))
        self.assert_valid()
        # A replayed Stop restores a missing pointer even when a code example names an existing record.
        target = brain.link_target(self.root, newest.relative_to(self.root).as_posix())
        quoted = f'```text\nLatest automatic record: [[{target}|quoted]].\n```\n'
        self.note.write_text(quoted + text.replace(pointer + '\n', ''))
        self.handle('Stop', 's2', last_assistant_message='SECOND_ANSWER', prompt_id='p2')
        text = self.note.read_text()
        self.assertEqual(text.count(quoted), 1)
        self.assertEqual(len(self.real_lines(text, 'Latest automatic record:')), 1)
        self.assertEqual(len(re.findall(r'^## Turn ', newest.read_text(), re.M)), 1)  # The replay added no turn.

    def test_brief_follows_only_pointer_lines_outside_code(self):
        self.with_example()
        brief = brain.brief(self.root, self.info['project'])
        self.assertNotIn('EXAMPLE_ONLY_TEXT', brief)
        self.assertNotIn('memory/sessions/example-note.md', brain.brief_paths(self.root, self.info['project']))
        self.note.write_text(self.note.read_text() + '\nLatest checkpoint: [[memory/sessions/example-note|real]].\n')
        self.assertIn('EXAMPLE_ONLY_TEXT', brain.brief(self.root, self.info['project']))

    def test_checkpoint_and_handoff_leave_code_examples_alone(self):
        self.with_example()
        evidence = 'memory/profile.md'
        self.ok('checkpoint', '--summary', 'CHECKPOINT_SUMMARY', '--next', 'Ship it.', '--evidence', evidence, '--id', 'c1')
        text = self.note.read_text()
        self.assertEqual(text.count(EXAMPLE), 1)
        self.assertEqual(len(self.real_lines(text, 'Latest checkpoint:')), 1)
        self.assertIn('CHECKPOINT_SUMMARY', brain.brief(self.root, self.info['project']))
        self.ok('checkpoint', '--summary', 'HANDOFF_SUMMARY', '--next', 'Review.', '--evidence', evidence, '--id', 'h1',
                '--complete')
        text = self.note.read_text()
        self.assertEqual(text.count(EXAMPLE), 1)
        self.assertEqual(self.real_lines(text, 'Latest checkpoint:'), [])
        self.assertEqual(len(self.real_lines(text, 'Latest handoff:')), 1)
        brief = brain.brief(self.root, self.info['project'])
        self.assertIn('HANDOFF_SUMMARY', brief)
        self.assertNotIn('EXAMPLE_ONLY_TEXT', brief)
        self.assert_valid()

    def test_consolidate_sets_updated_only_in_frontmatter(self):
        topic = self.root / 'wiki/topics/format.md'
        example = '```yaml\nupdated: 1999-01-01\n```\n'
        topic.write_text('---\ncreated: 2026-01-01\nstatus: active\n---\n# Format\n\n' + example)
        brain.add_index(self.root, 'wiki/topics/format.md', 'format')
        self.turn('which format', 'YAML front matter.')
        source = self.records()[0].relative_to(self.root).as_posix()
        text_file = self.base / 'finding.md'
        text_file.write_text('Front matter is YAML.\n')
        sha = brain.digest(topic.read_bytes())
        brain.consolidate(self.root, source, 'wiki/topics/format.md', text_file.read_text(), [source], sha)
        text = topic.read_text()
        self.assertEqual(text.count(example), 1)
        self.assertEqual(brain.frontmatter(text)['updated'], brain.day())
        self.assert_valid()

    def test_subagent_brief_keeps_code_examples_that_look_like_navigation(self):
        text = self.note.read_text().replace(
            '## Decisions\n\nNone recorded yet.',
            '## Decisions\n\nRun lines look like this:\n\n```text\nLatest run: [[memory/runs/x|KEEP_THIS_EXAMPLE]].\n```')
        self.note.write_text(text)
        self.turn('hello', 'Hi.')  # Appends a real Latest automatic record: line after the last section.
        brief = self.context(self.handle('SubagentStart', agent_id='a1', agent_type='general-purpose'))
        self.assertIn('KEEP_THIS_EXAMPLE', brief)
        self.assertNotIn('Latest automatic record:', brief)

    def test_index_entries_count_only_links_outside_code(self):
        index = self.root / 'index.md'
        index.write_text(index.read_text() + '\nExample entry:\n\n```text\n- [[memory/projects/quoted|quoted]]\n```\n')
        quoted = self.base / 'quoted'
        quoted.mkdir()
        setup.bind(self.config, quoted, project='quoted')
        self.assertEqual(len(re.findall(r'^- \[\[memory/projects/quoted\|', index.read_text(), re.M)), 2)
        self.assert_valid()

    def test_a_project_line_quoted_in_another_projects_record_does_not_scope_it(self):
        other = self.base / 'other'
        other.mkdir()
        setup.initialize(self.config, other)
        target = brain.link_target(self.root, self.info['project'])
        self.turn('quote it', f'Project: [[{target}|Project state]]. zebracorn appears here.', cwd=other)
        self.turn('mine', 'zebracorn in my own project.', session='mine-earlier')
        self.turn('later', 'Unrelated.', session='mine-latest')  # The brief now shows this record, not the earlier one.
        self.handle('SessionStart', 'reader', source='startup')
        notes = json.loads(self.context(self.handle('UserPromptSubmit', 'reader', prompt='zebracorn please'))
                           .split('\n', 1)[1])['matching_notes']
        paths = [n['path'] for n in notes]
        theirs = [r.relative_to(self.root).as_posix() for r in self.records() if 'quote it' in r.read_text()]
        mine = [r.relative_to(self.root).as_posix() for r in self.records() if 'my own project' in r.read_text()]
        self.assertNotIn(theirs[0], paths)
        self.assertIn(mine[0], paths)


class SyntheticPromptTests(FinalCase):
    def test_agent_messages_and_task_notifications_are_system_notifications(self):
        self.handle('SessionStart', source='startup')
        (self.root / 'wiki/topics/lint.md').write_text('---\ncreated: 2026-01-01\nupdated: 2026-01-01\nstatus: active\n'
                                                       '---\n# Lint\n\nlint results passed\n')
        for number, prompt in enumerate(['<agent-message from="worker-1">lint results passed</agent-message>',
                                         '\n <task-notification>lint results passed</task-notification>'], 1):
            self.assertEqual(self.handle('UserPromptSubmit', prompt=prompt, prompt_id=f'n{number}'), {})
            self.handle('Stop', last_assistant_message=f'Acknowledged {number}.', prompt_id=f'n{number}')
        self.assertIn('wiki/topics/lint.md', self.context(self.handle('UserPromptSubmit', prompt='lint results', prompt_id='u1')))
        self.handle('Stop', last_assistant_message='Real answer.', prompt_id='u1')
        record, = self.records()
        text = record.read_text()
        self.assertEqual(text.count('### System notification'), 2)
        self.assertEqual(text.count('### Request'), 1)
        self.assertIn('<agent-message from=', text)


class DottedProjectNoteTests(FinalCase):
    def test_a_dotted_project_note_links_resolve_everywhere(self):
        dotted = self.base / 'dotted'
        dotted.mkdir()
        note = self.root / 'memory/projects/my.app.md'
        note.write_text('---\ncreated: 2026-01-01\nupdated: 2026-01-01\nstatus: active\n---\n# Project — my.app\n'
                        + brain.section_stubs(brain.PROJECT_SECTIONS))
        info = setup.bind(self.config, dotted, note='memory/projects/my.app.md')
        project = info['project']
        self.assertEqual(project, 'memory/projects/my.app.md')
        self.handle('SessionStart', 'd1', dotted, source='startup')
        self.turn('dotted request', 'DOTTED_ANSWER kiwifruit', 'd1', dotted, prompt_id='d1')
        self.handle('SubagentStop', 'd1', dotted, agent_id='a1', agent_type='general-purpose', last_assistant_message='SUB')
        self.assert_valid()
        record = self.records()[0].relative_to(self.root).as_posix()
        self.assertEqual([r['path'] for r in brain.session_records(self.root, project)], [record])
        self.assertEqual({r['project'] for r in brain.session_records(self.root)}, {project})
        self.assertIn('DOTTED_ANSWER', brain.brief(self.root, project))
        self.ok('checkpoint', '--summary', 'DOTTED_CHECKPOINT', '--next', 'n', '--evidence', record, '--id', 'c1', cwd=dotted)
        self.assertIn('DOTTED_CHECKPOINT', brain.brief(self.root, project))
        self.ok('checkpoint', '--summary', 'DOTTED_HANDOFF', '--next', 'n', '--evidence', record, '--id', 'h1',
                '--complete', cwd=dotted)
        self.assertIn('DOTTED_HANDOFF', brain.brief(self.root, project))
        self.assert_valid()
        run = self.ok('board', 'open', '--title', 'Dotted run', cwd=dotted)['run']
        self.ok('board', 'close', cwd=dotted)
        self.assert_valid()
        listing = self.ok('compile', cwd=dotted)
        self.assertEqual([r['path'] for r in listing['records']], [record])
        self.assertEqual([r['run'] for r in listing['runs']], [run])
        links = ' '.join(r['link'] for r in listing['records'] + listing['runs'])
        note.write_text(note.read_text().replace('## Status\n\nNone recorded yet.', f'## Status\n\nStarted. Evidence: {links}.'))
        done = self.ok('compile', '--done', listing['token'], '--updated', project, cwd=dotted)
        self.assertEqual(done['pending_total'], 0)
        self.assert_valid()
        self.handle('SessionStart', 'd2', dotted, source='startup')
        self.turn('later', 'Later answer.', 'd3', dotted)
        self.handle('SessionStart', 'reader', dotted, source='startup')
        found = self.context(self.handle('UserPromptSubmit', 'reader', dotted, prompt='kiwifruit'))
        self.assertIn(record, found)  # Another session's record of this project is in scope.
        self.assertEqual([f for f in brain.doctor(self.root)['findings'] if f['kind'] == 'structural'], [])


class IdempotentCloseTests(FinalCase):
    def interrupted_close(self, run):
        """A close whose run note write fails right after the JSON save: pointer, run line and log are left undone."""
        real = runs.sync_note

        def fail(root, data):
            if data['status'] == 'closed':
                raise OSError('disk full')
            return real(root, data)
        with patch.object(runs, 'sync_note', fail):
            code, result = self.cli('board', 'close', '--summary', 'FIRST_SUMMARY')
        self.assertEqual(code, 1, result)
        self.assertEqual(json.loads((self.root / '.brain/runs' / (run + '.json')).read_text())['status'], 'closed')

    def closes(self):
        return (self.root / 'log.md').read_text().count('Closed run [[')

    def test_close_after_an_interrupted_close_completes_once(self):
        run = self.ok('board', 'open', '--title', 'Interrupted')['run']
        self.ok('board', 'add', '--title', 'one')
        self.interrupted_close(run)
        self.assertTrue(runs.pointer(self.root, self.info['project']).exists())
        self.assertEqual(len(self.real_lines(self.note.read_text(), 'Active run:')), 1)
        self.assertEqual(self.closes(), 0)
        result = self.ok('board', 'close', '--summary', 'IGNORED_SECOND_SUMMARY')
        self.assertEqual((result['run'], result['status'], result['already_closed']), (run, 'closed', True))
        self.assertFalse(runs.pointer(self.root, self.info['project']).exists())
        text = self.note.read_text()
        self.assertEqual(self.real_lines(text, 'Active run:'), [])
        line, = self.real_lines(text, 'Latest run:')
        self.assertIn(brain.link_target(self.root, brain.run_note(run)) + '|', line)
        self.assertEqual(self.closes(), 1)
        self.assertEqual(runs.show(self.root, self.info['project'], run)['summary'], 'FIRST_SUMMARY')
        self.assert_valid()
        files = lambda: {p: (p.read_bytes(), p.stat().st_ino) for p in self.root.rglob('*')
                         if p.is_file() and p.name != 'write.flock'}  # The inode shows a rewrite with equal bytes.
        before = files()
        self.ok('board', 'close', '--run', run)
        self.assertEqual(files(), before)

    def test_a_repeated_close_never_takes_over_a_newer_runs_line_or_pointer(self):
        first = self.ok('board', 'open', '--title', 'First')['run']
        self.interrupted_close(first)
        second = self.ok('board', 'open', '--title', 'Second')['run']  # The first is closed in its state.
        self.ok('board', 'close', '--run', first)
        self.assertEqual(runs.active(self.root, self.info['project'])['run'], second)
        line, = self.real_lines(self.note.read_text(), 'Active run:')
        self.assertIn(brain.link_target(self.root, brain.run_note(second)) + '|', line)
        self.assertEqual(self.real_lines(self.note.read_text(), 'Latest run:'), [])
        self.assertEqual(self.closes(), 1)
        self.ok('board', 'close')
        line, = self.real_lines(self.note.read_text(), 'Latest run:')
        self.assertIn(brain.link_target(self.root, brain.run_note(second)) + '|', line)
        self.assertEqual(self.closes(), 2)
        self.assert_valid()


class FixRound1Tests(FinalCase):
    """Final review fix round 1 (.orchestration/reviews/phase-final-review.md and the tester's queue finding)."""

    def test_session_start_builds_its_brief_and_nudge_after_draining(self):
        self.queued_stop('earlier', 'Use SQLite?', 'QUEUED_ANSWER: yes, SQLite.')
        result = self.handle('SessionStart', 'next', source='startup')
        context = self.context(result)
        self.assertEqual(runs.spool_names(self.root), [])
        record = self.records()[0].relative_to(self.root).as_posix()
        self.assertIn('Compile pending: 1 automatic record(s) from other sessions', context)
        self.assertIn('QUEUED_ANSWER', context)  # The brief follows the pointer the drained record set.
        state = brain.load_json(hooks.session_path(self.info, {'session_id': 'next'}), {})
        self.assertIn(record, state['brief_paths'])
        self.assertTrue(state['primed'])

    def test_compile_listing_repairs_a_stale_run_note_before_it_is_read(self):
        run = self.ok('board', 'open', '--title', 'Stale note')['run']
        real = runs.sync_note
        with patch.object(runs, 'sync_note', lambda root, data: (_ for _ in ()).throw(OSError('disk full'))
                          if data['status'] == 'closed' else real(root, data)):
            self.assertEqual(self.cli('board', 'close', '--summary', 'CLOSING_SUMMARY')[0], 1)
        note = self.root / brain.run_note(run)
        self.assertEqual(brain.frontmatter(note.read_text())['status'], 'active')
        listing = self.ok('compile')
        self.assertEqual([r['run'] for r in listing['runs']], [run])
        self.assertEqual(brain.frontmatter(note.read_text())['status'], 'closed')
        self.assertIn('CLOSING_SUMMARY', note.read_text())

    def test_internal_state_is_never_read_or_written_outside_the_vault(self):
        outside = self.base / 'outside'
        outside.mkdir()
        for folder in ('.brain/runs', '.brain/automation'):
            with self.subTest(folder=folder):
                target = outside / Path(folder).name
                target.mkdir()
                link = self.root / folder
                if link.exists():
                    link.rename(self.base / ('saved-' + link.name))
                link.symlink_to(target, target_is_directory=True)
                code, result = self.cli('board', 'open', '--title', 'Escape')
                self.assertEqual(code, 1, result)
                self.assertRegex(result['error'], 'must not be a symlink')
                if folder == '.brain/automation':
                    with self.assertRaisesRegex(ValueError, 'must not be a symlink'):
                        self.handle('SessionStart', 'escape', source='startup')
                    self.assertEqual(self.cli('compile')[0], 1)
                self.assertEqual(list(target.iterdir()), [])
                link.unlink()
                saved = self.base / ('saved-' + link.name)
                if saved.exists():
                    saved.rename(link)
        run = self.ok('board', 'open', '--title', 'Real')['run']  # A run file that is a symlink is not read either.
        state = self.root / '.brain/runs' / (run + '.json')
        copy = outside / 'copy.json'
        original = state.read_text()
        copy.write_text(original)
        state.unlink()
        state.symlink_to(copy)
        code, result = self.cli('board', 'show', '--run', run)
        self.assertEqual(code, 1, result)
        self.assertRegex(result['error'], 'must not be a symlink')
        self.assertEqual(self.cli('board', 'add', '--title', 'x', '--run', run)[0], 1)
        self.assertEqual(copy.read_text(), original)
        whole = self.base / 'brain-elsewhere'  # .brain itself a symlink: even the lock is refused.
        (self.root / '.brain').rename(whole)
        (self.root / '.brain').symlink_to(whole, target_is_directory=True)
        before = sorted(p.name for p in whole.rglob('*'))
        self.assertEqual(self.cli('board', 'open', '--title', 'Escape')[0], 1)
        with self.assertRaisesRegex(ValueError, 'must not be a symlink'):
            brain.compile_list(self.root, self.info['project'])
        self.assertEqual(sorted(p.name for p in whole.rglob('*')), before)

    def test_check_vault_reports_escaping_symlinks_without_opening_them(self):
        secret = self.base / 'outside-secret.md'
        secret.write_text('---\ncreated: 2026-01-01\nupdated: 2026-01-01\nstatus: active\n---\nOUTSIDE_SECRET\n')
        (self.root / 'wiki/topics/leak.md').symlink_to(secret)
        opened, (issues, _, _) = self.opened_during(lambda: check(self.root))
        self.assertIn('wiki/topics/leak.md: symlink escapes vault', issues)
        self.assertNotIn(secret.resolve(), opened)
        opened, report = self.opened_during(lambda: brain.doctor(self.root))
        self.assertIn('wiki/topics/leak.md: symlink escapes vault', [f['detail'] for f in report['findings']])
        self.assertNotIn(secret.resolve(), opened)
        (self.root / 'wiki/topics/leak.md').unlink()
        agents = self.root / 'AGENTS.md'
        moved = self.base / 'AGENTS-outside.md'
        agents.rename(moved)
        agents.symlink_to(moved)
        opened, (issues, _, _) = self.opened_during(lambda: check(self.root))
        self.assertIn('AGENTS.md: symlink escapes vault', issues)
        self.assertNotIn(moved.resolve(), opened)

    def test_a_plain_close_retry_completes_after_a_project_note_or_log_failure(self):
        for name in ('project note', 'log'):
            with self.subTest(failure=name):
                run = self.ok('board', 'open', '--title', 'Close ' + name)['run']
                target = (self.note if name == 'project note' else self.root / 'log.md').resolve()
                real, failures = brain.atomic_write, []

                def fail_once(path, text):
                    if Path(path).resolve() == target and not failures:
                        failures.append(path)
                        raise OSError('disk full')
                    return real(path, text)
                with patch.object(brain, 'atomic_write', fail_once):
                    self.assertEqual(self.cli('board', 'close')[0], 1)
                self.assertEqual(len(failures), 1)
                self.assertTrue(runs.pointer(self.root, self.info['project']).exists())
                result = self.ok('board', 'close')
                self.assertEqual((result['run'], result.get('already_closed')), (run, True))
                self.assertFalse(runs.pointer(self.root, self.info['project']).exists())
                link = brain.link_target(self.root, brain.run_note(run)) + '|'
                line, = self.real_lines(self.note.read_text(), 'Latest run:')
                self.assertIn(link, line)
                self.assertEqual((self.root / 'log.md').read_text().count('Closed run [[' + link), 1)
                self.assert_valid()

    def test_a_legacy_dotted_owner_link_is_attributed_to_its_note(self):
        dotted = self.base / 'dotted'
        dotted.mkdir()
        note = self.root / 'memory/projects/my.app.md'
        note.write_text('---\ncreated: 2026-01-01\nupdated: 2026-01-01\nstatus: active\n---\n# Project — my.app\n'
                        + brain.section_stubs(brain.PROJECT_SECTIONS))
        project = setup.bind(self.config, dotted, note='memory/projects/my.app.md')['project']
        legacy = 'memory/sessions/2026-01-02-auto-' + 'e' * 24 + '.md'
        (self.root / legacy).write_text(  # Written by plugin 0.3.0, which dropped .md from every link.
            '---\ncreated: 2026-01-02\nupdated: 2026-01-02\nstatus: recorded\n---\n# Automatic conversation record\n\n'
            'Project: [[memory/projects/my.app|Project state]].\n\n## Recorded assistant response\n\n```text\nOld.\n```\n')
        self.turn('new', 'New answer.', 'd1', dotted)
        self.assertEqual({r['project'] for r in brain.session_records(self.root)}, {project})
        self.assertEqual(sorted(r['path'] for r in brain.session_records(self.root, project))[0], legacy)
        pending = [f for f in brain.doctor(self.root)['findings'] if f['kind'] == 'pending_compile']
        self.assertEqual([(f['project'], f['count']) for f in pending], [(project, 2)])

    def test_status_and_show_report_queued_writes_without_applying_them(self):
        run = self.ok('board', 'open', '--title', 'Queue')['run']
        self.queued_stop('worker', 'Work.', 'QUEUED_WORK')
        self.assertEqual(self.ok('compile', '--status')['queued'], 1)
        self.assertEqual(self.ok('board', 'show')['queued'], 1)
        self.handle('SubagentStart', 'lead', agent_id='a1', agent_type='general-purpose')
        self.assertEqual(len(runs.spool_names(self.root)), 1)  # None of these apply queued writes.
        self.assertEqual((self.ok('compile', '--status')['queued'], self.ok('board', 'show', '--run', run)['queued']), (1, 1))
        self.handle('UserPromptSubmit', 'other', prompt='hello', prompt_id='o1')  # A prompt hook applies them.
        self.assertEqual(runs.spool_names(self.root), [])
        self.assertEqual(self.ok('compile', '--status')['queued'], 0)
        self.assertIn('QUEUED_WORK', self.records()[0].read_text())


class FixRound2Tests(FinalCase):
    """Final re-review fix round 2 (.orchestration/reviews/phase-finalr-review.md, tests/phase-finalr-test.md)."""

    def test_an_escaping_marker_symlink_is_reported_and_never_opened(self):
        marker = self.root / '.second-brain.json'
        outside = self.base / 'outside-marker.json'
        marker.rename(outside)
        marker.symlink_to(outside)
        opened, (issues, _, _) = self.opened_during(lambda: check(self.root))
        self.assertEqual(issues, ['.second-brain.json: symlink escapes vault'])
        self.assertNotIn(outside.resolve(), opened)
        for name, action in (('identity', lambda: setup.identity(self.root)),
                             ('context', lambda: setup.context(self.config, self.project)),
                             ('link_target', lambda: brain.link_target(self.root, 'memory/profile.md'))):
            with self.subTest(caller=name):
                def refused():
                    with self.assertRaisesRegex(ValueError, 'symlink escapes vault'):
                        action()
                opened, _ = self.opened_during(refused)
                self.assertNotIn(outside.resolve(), opened)
        opened, report = self.opened_during(lambda: brain.doctor(self.root))
        self.assertIn('.second-brain.json: symlink escapes vault', [f['detail'] for f in report['findings']])
        self.assertNotIn(outside.resolve(), opened)
        code, result = self.cli('doctor')  # Routed commands resolve the vault through identity(), which refuses.
        self.assertEqual(code, 1)
        self.assertIn('symlink escapes vault', result['error'])

    def test_check_vault_resolves_a_legacy_dotted_link_like_the_runtime(self):
        dotted = self.base / 'dotted'
        dotted.mkdir()
        note = self.root / 'memory/projects/my.app.md'
        note.write_text('---\ncreated: 2026-01-01\nupdated: 2026-01-01\nstatus: active\n---\n# Project — my.app\n')
        setup.bind(self.config, dotted, note='memory/projects/my.app.md')
        legacy = 'memory/sessions/2026-01-02-auto-' + 'e' * 24 + '.md'
        (self.root / legacy).write_text(
            '---\ncreated: 2026-01-02\nupdated: 2026-01-02\nstatus: recorded\n---\n# Automatic conversation record\n\n'
            'Project: [[memory/projects/my.app|Project state]].\n')
        brain.add_index(self.root, legacy, 'Automatic conversation record')
        self.assert_valid()
        edges = {}
        check(self.root, edges=edges)
        self.assertIn(note.resolve(), edges[(self.root / legacy).resolve()])
        self.assertEqual(brain.linked_files(self.root, (self.root / legacy).read_text())[0], {'memory/projects/my.app.md'})
        (self.root / legacy).write_text((self.root / legacy).read_text() + 'See [[memory/projects/none.app]].\n')
        self.assertEqual(check(self.root)[0], [legacy + ':9: missing target: memory/projects/none.app'])

    def test_the_md_fallback_applies_only_to_targets_with_their_own_extension(self):
        self.test_check_vault_resolves_a_legacy_dotted_link_like_the_runtime()  # The dotted fallback still resolves.
        legacy = 'memory/sessions/2026-01-02-auto-' + 'e' * 24 + '.md'
        (self.root / legacy).write_text((self.root / legacy).read_text().replace('[[memory/projects/none.app]]', ''))
        self.assert_valid()
        (self.root / 'memory/projects/missing.md.md').write_text('---\ncreated: 2026-01-01\nupdated: 2026-01-01\n'
                                                                'status: active\n---\n# Doubled\n')
        brain.add_index(self.root, 'memory/projects/missing.md.md', 'doubled')
        text = (self.root / legacy).read_text() + 'See [[memory/projects/missing]].\n'
        (self.root / legacy).write_text(text)
        self.assertEqual(check(self.root)[0], [legacy + ':10: missing target: memory/projects/missing'])
        self.assertEqual(brain.linked_files(self.root, text)[0], {'memory/projects/my.app.md'})  # The runtime agrees.

    def test_the_spool_backlog_hint_names_exactly_what_applies_the_queue(self):
        self.queued_stop('worker', 'Work.', 'QUEUED_WORK')
        backlog, = [f for f in brain.doctor(self.root)['findings'] if f['kind'] == 'spool_backlog']
        for name in ('SessionStart', 'UserPromptSubmit', 'Stop', 'SubagentStop', 'run compile', '--done',
                     'board open, add, claim, post, release, and close', 'compile --status and board show'):
            self.assertIn(name, backlog['detail'])
        old = re.compile(r'next hook or (?:board|compile)|board/compile command|compile/board command', re.I)
        for path in [*(REPO / 'scripts').glob('*.py'), *(REPO / 'skills').rglob('*.md'), REPO / 'README.md']:
            self.assertIsNone(old.search(path.read_text()), path)


class PackageConsistencyTests(unittest.TestCase):
    def test_runtime_files_are_exactly_the_shipped_modules_skills_and_hooks(self):
        shipped = {p.relative_to(REPO).as_posix() for p in [*(REPO / 'scripts').glob('*.py'), *(REPO / 'skills').rglob('*'),
                                                             REPO / 'hooks/hooks.json']
                   if p.is_file() and '__pycache__' not in p.parts}
        shipped -= {'scripts/package_plugin.py'}  # The build tool itself is not runtime.
        self.assertEqual(set(setup.RUNTIME_FILES), shipped)
        with tempfile.TemporaryDirectory(prefix='second-brain-package-') as tmp:
            built = package_plugin.build(Path(tmp) / 'dist')
            plugin = Path(built['plugin'])
            for name in setup.RUNTIME_FILES:
                self.assertEqual((plugin / name).read_bytes(), (REPO / name).read_bytes(), name)
            manifest = json.loads((Path(built['marketplace']) / 'package-manifest.json').read_text())
            self.assertEqual(manifest['version'], json.loads((REPO / 'packaging/plugin.json').read_text())['version'])
            self.assertTrue(all('plugins/second-brain/' + n in manifest['files'] for n in setup.RUNTIME_FILES))

    def test_ci_runs_every_test_file_on_the_oldest_and_newest_python(self):
        workflow = (REPO / '.github/workflows/tests.yml').read_text()
        self.assertRegex(workflow, r"python-version: \['3\.9', '3\.13'\]")
        self.assertIn('python3 -B -m unittest discover -s tests', workflow)
        self.assertTrue(all(re.fullmatch(r'test_[a-z0-9_]+\.py', p.name) for p in (REPO / 'tests').glob('*.py')))


class SkillRenameTests(unittest.TestCase):
    def test_install_replaces_our_old_second_brain_skill_links_with_memory(self):
        with tempfile.TemporaryDirectory(prefix='second-brain-install-') as tmp:
            home, data = Path(tmp) / 'home', Path(tmp) / 'data'
            ours = home / '.claude/skills/second-brain'
            foreign = home / '.agents/skills/second-brain'
            for link, target in ((ours, data / 'current/skills/second-brain'), (foreign, Path(tmp) / 'elsewhere')):
                link.parent.mkdir(parents=True)
                link.symlink_to(target, target_is_directory=True)
            result = setup.install(home=home, data_dir=data)
            self.assertFalse(os.path.lexists(ours))  # A link the old install made is removed.
            self.assertEqual(os.readlink(foreign), str(Path(tmp) / 'elsewhere'))  # Anything else stays.
            for client in ('.claude', '.agents'):
                skill = home / client / 'skills/memory'
                self.assertIn(str(skill), result['skills'])
                self.assertIn('name: memory\n', (skill / 'SKILL.md').read_text())
            self.assertFalse((data / 'current/skills/second-brain').exists())


if __name__ == '__main__':
    unittest.main()
