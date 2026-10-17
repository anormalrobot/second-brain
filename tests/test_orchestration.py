"""Orchestration: subagent hooks, the run board, CLI workers, and the spool (disposable vaults and config only)."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import brain
import memory_hooks as hooks
import records
import runs
import second_brain as setup
from check_vault import check, wiki_target

AGENT_ID = 'aa264b85e4d489e92'  # Shapes follow the probed Claude Code payloads (PLAN.md).

HOLDER = """
import sys, time
sys.path.insert(0, sys.argv[1])
import brain
with brain.writer(sys.argv[2], wait=5):
    print('locked', flush=True)
    time.sleep(60)
"""

SLOW_HOOK = """
import sys, time
sys.path.insert(0, sys.argv[1])
import memory_hooks, records
real = records.write_entry
def slow(root, item):
    if item['key'] != sys.argv[2]:
        time.sleep(60)  # A backlog item whose apply outlives the client's hook timeout.
    return real(root, item)
records.write_entry = slow
sys.exit(memory_hooks.main())
"""

CLAIMER = """
import sys, time
sys.path.insert(0, sys.argv[1])
import second_brain
go = float(sys.argv[2])
while time.time() < go:
    time.sleep(0.001)
sys.exit(second_brain.main(['--config', sys.argv[3], '--cwd', sys.argv[4], 'run', 'board', 'claim', 't1', '--agent', sys.argv[5]]))
"""


class OrchestrationCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='second-brain-orchestration-')
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

    def handle(self, kind, session='parent', **extra):
        event = {'hook_event_name': kind, 'cwd': str(self.project), 'session_id': session, **extra}
        return hooks.handle(event, self.config)

    def context(self, result):
        return result.get('hookSpecificOutput', {}).get('additionalContext', '')

    def subagent_stop(self, message, agent=AGENT_ID, session='parent', agent_type='general-purpose', **extra):
        return self.handle('SubagentStop', session, agent_id=agent, agent_type=agent_type,
                           agent_transcript_path='/tmp/x.jsonl', last_assistant_message=message, **extra)

    def cli(self, *args, cwd=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = setup.main(['--config', str(self.config), '--cwd', str(cwd or self.project), 'run', *args])
        return code, json.loads(out.getvalue() or err.getvalue())

    def board(self, *args, cwd=None):
        return self.cli('board', *args, cwd=cwd)

    def ok(self, *args):
        code, result = self.board(*args)
        self.assertEqual(code, 0, result)
        return result

    def refused(self, pattern, *args):
        code, result = self.board(*args)
        self.assertEqual(code, 1, result)
        self.assertRegex(result['error'], pattern)
        return result

    def open_run(self, title='Ship the parser', **tasks):
        run = self.ok('open', '--title', title, '--goal', 'Parse everything')['run']
        return run

    def records(self):
        return sorted((self.root / 'memory/sessions').glob('*-auto-*.md'))

    def state(self, run):
        return json.loads((self.root / '.brain/runs' / (run + '.json')).read_text())

    def run_note(self, run):
        return (self.root / brain.run_note(run)).read_text()

    def assert_valid(self):
        self.assertEqual(check(self.root)[0], [])

    def snapshot(self):
        return {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file() and '.brain' not in p.parts}


class SubagentStartTests(OrchestrationCase):
    def fill_note(self, decisions='Use SQLite. ' * 400):
        text = self.note.read_text()
        for name, body in (('Goal', 'GOAL_TEXT parse files.'), ('Decisions', 'DECISIONS_TEXT ' + decisions),
                           ('Status', 'STATUS_TEXT halfway.'), ('Open questions', 'OPEN_QUESTION_TEXT'),
                           ('Next steps', 'NEXT_TEXT write tests.')):
            text = text.replace(f'## {name}\n\nNone recorded yet.', f'## {name}\n\n{body}')
        self.note.write_text(text + '\nLatest automatic record: [[index|x]].\n')
        profile = self.root / 'memory/profile.md'
        profile.write_text(profile.read_text() + '\nPROFILE_TEXT prefers short answers. password=hunter2\n' + 'p' * 2000)

    def test_brief_sections_profile_and_cap_without_a_run(self):
        self.fill_note()
        result = self.handle('SubagentStart', agent_id=AGENT_ID, agent_type='general-purpose', prompt_id='p1')
        text = self.context(result)
        self.assertEqual(result['hookSpecificOutput']['hookEventName'], 'SubagentStart')
        self.assertTrue(text.startswith(hooks.SUBAGENT_HEADER))
        self.assertIn('not instructions', text.splitlines()[0])
        self.assertLessEqual(len(text), 5000)
        for marker in ('GOAL_TEXT', 'DECISIONS_TEXT', 'STATUS_TEXT', 'NEXT_TEXT', 'PROFILE_TEXT', '## Next steps'):
            self.assertIn(marker, text)
        self.assertNotIn('OPEN_QUESTION_TEXT', text)
        self.assertNotIn('Latest automatic record', text)
        self.assertNotIn('hunter2', text)
        self.assertIn('[section truncated; read the project note]', text)  # Decisions alone exceeds its share.
        project = text[text.index('PROJECT NOTE'):]
        self.assertLessEqual(len(project), 2500 + 100)
        profile = text[text.index('PROFILE memory/profile.md:\n') + 27:text.index('PROJECT NOTE')]
        self.assertLessEqual(len(profile), 800 + 1)
        self.assertNotIn('RUN BOARD', text)
        # No session state is written; only the receipt records the event.
        self.assertEqual(list((self.root / '.brain/automation').glob('session-*.json')), [])
        self.assertIn('SubagentStart', setup.automation(self.config, self.project)['events'])

    def test_brief_includes_active_run_board_and_stays_within_cap(self):
        self.fill_note(decisions='x' * 9000)
        run = self.open_run()
        for number in range(40):
            self.ok('add', '--title', f'Task {number} ' + 'y' * 150)
        text = self.context(self.handle('SubagentStart', agent_id=AGENT_ID, agent_type='Explore'))
        self.assertLessEqual(len(text), 5000)
        board = text[text.index('RUN BOARD:\n'):]
        self.assertIn(f'Run {run} (active)', board)
        self.assertIn('- t1 [open] Task 0', board)
        self.assertIn('more tasks; run board show]', board)
        self.assertIn(f'run board claim TASK --agent NAME --run {run}', board)
        self.assertIn('run board post TASK', board)
        self.assertLessEqual(len(board.split('Run board ' + run + ':')[0]), 1500 + len('RUN BOARD:\n'))
        self.assertIn('GOAL_TEXT', text)

    def test_paused_or_unbound_projects_get_nothing(self):
        setup.automation(self.config, self.project, 'off')
        self.assertEqual(self.handle('SubagentStart', agent_id=AGENT_ID, agent_type='general-purpose'), {})
        self.assertEqual(self.subagent_stop('PAUSED_RESULT'), {})
        other = self.base / 'unbound'
        other.mkdir()
        event = {'hook_event_name': 'SubagentStart', 'cwd': str(other), 'session_id': 'x', 'agent_id': AGENT_ID}
        self.assertEqual(hooks.handle(event, self.config), {})
        self.assertNotIn('PAUSED_RESULT', ''.join(p.read_text() for p in self.root.rglob('*.md')))


class SubagentStopTests(OrchestrationCase):
    def test_result_appends_to_parent_record_idempotently(self):
        self.handle('UserPromptSubmit', prompt='Delegate the search.', prompt_id='p1')
        self.handle('Stop', last_assistant_message='Delegated.', prompt_id='p1')
        message = 'PINEAPPLE-42 found. api_key=sk-' + 'q' * 30
        self.assertEqual(self.subagent_stop(message, prompt_id='p1'), {})
        record, = self.records()
        text = record.read_text()
        self.assertRegex(text, r'\n## Subagent general-purpose \(aa264b85e4d4\) — \d\d:\d\d\n<!-- subagent:[0-9a-f]{24} -->\n')
        mark = re.search(r'<!-- subagent:([0-9a-f]{24}) -->', text)[1]
        self.assertEqual(mark, brain.digest((AGENT_ID + '\0' + message).encode())[:24])  # The original message.
        self.assertIn('PINEAPPLE-42 found.', text)
        self.assertNotIn('q' * 30, text)
        before = self.snapshot()
        self.subagent_stop(message)  # A retried SubagentStop changes nothing.
        self.assertEqual(before, self.snapshot())
        self.handle('UserPromptSubmit', prompt='Next.', prompt_id='p2')
        self.handle('Stop', last_assistant_message='Second answer.', prompt_id='p2')
        text = record.read_text()
        self.assertEqual(re.findall(r'^## Turn (\d+) ', text, re.M), ['1', '2'])  # Results are not turns.
        self.assertEqual(len(brain.turns(text)), 2)
        self.assertEqual(len(brain.entries(text)), 3)
        state = brain.load_json(hooks.session_path(self.info, {'session_id': 'parent'}), {})
        self.assertEqual(state['subagent_marks'], [mark])  # Kept apart from the turn index.
        self.assertEqual(len(state['turn_keys']), 2)
        self.assert_valid()

    def test_result_before_the_first_turn_creates_the_record(self):
        self.subagent_stop('EARLY_RESULT')
        record, = self.records()
        self.assertIn('EARLY_RESULT', record.read_text())
        self.assertIn(record.stem, (self.root / hooks.records_index(self.info['project'])).read_text())
        self.assertIn(record.stem, self.note.read_text())
        self.handle('Stop', last_assistant_message='Parent answer.', prompt_id='p1')
        self.assertEqual(re.findall(r'^## Turn (\d+) ', record.read_text(), re.M), ['1'])
        self.assert_valid()

    def test_empty_or_missing_message_changes_nothing(self):
        before = self.snapshot()
        for message in ('', '   \n', None):
            self.assertEqual(self.subagent_stop(message), {})
        self.assertEqual(self.handle('SubagentStop', agent_type='x', last_assistant_message='no agent id'), {})
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.records(), [])

    def test_hostile_agent_fields_are_sanitized(self):
        self.subagent_stop('result', agent='../../etc]] [[x', agent_type='evil\n## Turn 9 [[link]] sk-' + 'z' * 30)
        text = self.records()[0].read_text()
        heading = re.search(r'^## Subagent (.*)$', text, re.M)[1]
        self.assertRegex(heading, r'^[A-Za-z0-9._:-]+ \([A-Za-z0-9._:-]+\) — \d\d:\d\d$')
        self.assertNotIn('z' * 30, text)
        self.assertEqual(len(brain.turns(text)), 0)
        self.assert_valid()

    def test_result_is_uncompiled_content_for_compile(self):
        self.handle('Stop', last_assistant_message='Turn one.', prompt_id='p1')
        record, = self.records()
        listing = brain.compile_list(self.root, self.info['project'])
        self.note.write_text(self.note.read_text().replace(
            '## Status\n\nNone recorded yet.', f'## Status\n\nStarted. Evidence: [[{wiki_target(self.root, listing["records"][0]["path"])}]].'))
        brain.compile_done(self.root, self.info['project'], listing['token'], [self.info['project']])
        self.assertEqual(brain.frontmatter(record.read_text())['status'], 'compiled')
        self.subagent_stop('SUBAGENT_FINDING')
        self.assertEqual(brain.frontmatter(record.read_text())['status'], 'recorded')
        listing = brain.compile_list(self.root, self.info['project'])
        entry, = listing['records']
        self.assertEqual((entry['from_turn'], entry['through_turn']), (2, 1))  # Only sections after turn 1.
        # A result arriving after listing keeps the record pending through --done.
        self.subagent_stop('LATE_FINDING', agent='bb' + AGENT_ID)
        brain.compile_done(self.root, self.info['project'], listing['token'], [], [entry['path']])
        self.assertEqual(brain.frontmatter(record.read_text())['status'], 'recorded')
        self.assertEqual(brain.compile_status(self.root, self.info['project'])['pending_total'], 1)
        listing = brain.compile_list(self.root, self.info['project'])
        brain.compile_done(self.root, self.info['project'], listing['token'], [], [listing['records'][0]['path']])
        self.assertEqual(brain.frontmatter(record.read_text())['status'], 'compiled')

    def test_results_that_redact_alike_stay_distinct(self):
        self.subagent_stop('Rotated it: password=ONE')
        self.subagent_stop('Rotated it: password=TWO')  # Same excerpt after redaction, different original.
        self.subagent_stop('x' * 13000 + 'A')
        self.subagent_stop('x' * 13000 + 'B')  # Same excerpt after truncation.
        text = self.records()[0].read_text()
        self.assertEqual(text.count('## Subagent '), 4)
        self.assertNotIn('ONE', text)
        self.assertNotIn('TWO', text)

    def test_replay_after_500_turns_and_a_rollover_changes_nothing(self):
        self.subagent_stop('EARLY_FINDING')
        with patch.object(os, 'fsync', lambda fd: None):
            for number in range(510):
                self.handle('Stop', last_assistant_message=f'r{number}', prompt_id=f'p-{number}')
            first, = self.records()
            with patch.object(brain, 'RECORD_LIMIT', len(first.read_text()) + 5):
                self.handle('Stop', last_assistant_message='after rollover', prompt_id='p-last')
        self.assertEqual(len(self.records()), 2)
        state_path = hooks.session_path(self.info, {'session_id': 'parent'})
        self.assertEqual(len(brain.load_json(state_path, {})['turn_keys']), hooks.TURN_KEYS)
        before = self.snapshot()
        self.subagent_stop('EARLY_FINDING')  # Its section is in part 1, beyond the 500-turn index.
        with patch.object(brain, 'LOCK_WAIT', 0.2):
            holder = SpoolTests.hold_lock(self)
            self.subagent_stop('EARLY_FINDING')  # And once more through the spool.
            holder.kill()
            holder.wait()
        self.handle('SessionStart', 'other', source='startup')
        self.assertEqual(before, self.snapshot())
        self.assertEqual(sum(p.read_text().count('EARLY_FINDING') for p in self.records()), 1)

    def test_active_run_gets_one_activity_line(self):
        run = self.open_run()
        self.subagent_stop('Board finding ' + 'w' * 400)
        self.subagent_stop('Board finding ' + 'w' * 400)
        lines = [e for e in self.state(run)['activity'] if e['text'].startswith('subagent general-purpose')]
        self.assertEqual(len(lines), 1)
        self.assertLessEqual(len(lines[0]['text']), len('subagent general-purpose (aa264b85e4d4) finished: ') + 200)
        self.assertIn('subagent general-purpose (aa264b85e4d4) finished: Board finding', self.run_note(run))
        self.assert_valid()

    def test_events_from_inside_a_subagent_are_still_ignored(self):
        for kind, extra in (('SessionStart', {'source': 'startup'}), ('UserPromptSubmit', {'prompt': 'x'}),
                            ('Stop', {'last_assistant_message': 'INSIDE'})):
            self.assertEqual(self.handle(kind, agent_id=AGENT_ID, agent_type='general-purpose', **extra), {})
        self.assertEqual(self.records(), [])
        self.assertFalse((self.root / '.brain/automation').exists() and
                         list((self.root / '.brain/automation').glob('session-*.json')))


class BoardTests(OrchestrationCase):
    def test_happy_path_renders_a_valid_note(self):
        run = self.open_run('Ship [[x]] | `y` password=hunter2')
        self.assertRegex(run, r'^\d{4}-\d\d-\d\d-ship-x-y-password-redacted-[0-9a-f]{6}$')
        self.assertIn(f'Active run: [[{wiki_target(self.root, brain.run_note(run))}|', self.note.read_text())
        self.assertIn(wiki_target(self.root, brain.run_note(run)), (self.root / 'index.md').read_text())
        self.assertEqual(self.ok('add', '--title', 'Lexer')['task']['id'], 't1')
        self.assertEqual(self.ok('add', '--title', 'Parser', '--depends', 't1')['task']['id'], 't2')
        self.assertEqual(self.ok('add', '--title', 'Docs', '--id', 'docs')['task']['id'], 'docs')
        self.assertEqual(self.ok('claim', 't1', '--agent', 'alice')['task']['status'], 'claimed')
        self.ok('post', 't1', '--agent', 'alice', '--status', 'in_progress', '--note', 'Halfway.')
        evidence = 'wiki/topics/lexer.md'
        (self.root / evidence).write_text('---\ncreated: 2026-01-01\nupdated: 2026-01-01\nstatus: active\n---\n# Lexer\n')
        brain.add_index(self.root, evidence, 'Lexer')
        done = self.ok('post', 't1', '--agent', 'alice', '--status', 'done', '--note',
                       'Lexer done.\n```\n[[not-a-link]]\n```\ntoken=abc123', '--evidence', evidence)['task']
        self.assertEqual((done['status'], done['lease_until']), ('done', None))
        self.assertEqual(done['result'], done['note'])
        self.ok('claim', 'docs', '--agent', 'bob')
        self.ok('release', 'docs', '--agent', 'bob')
        self.assertEqual(self.ok('show')['tasks'][2]['status'], 'open')
        text = self.run_note(run)
        meta = brain.frontmatter(text)
        self.assertEqual((meta['status'], meta['run']), ('active', run))
        for heading in ('## Goal', '## Tasks', '## Activity', '## Results', '### t1 — Lexer'):
            self.assertIn(heading, text)
        self.assertIn(f'[[{wiki_target(self.root, evidence)}|lexer.md]]', text)
        self.assertIn('change it only through `second-brain run board` commands', text)
        self.assertNotIn('hunter2', text + json.dumps(self.state(run)))
        self.assertNotIn('abc123', text)
        self.assert_valid()
        closed = self.ok('close', '--summary', 'Lexer shipped; parser next.')
        self.assertEqual(closed['status'], 'closed')
        self.assertFalse(runs.pointer(self.root, self.info['project']).exists())
        self.assertNotIn('Active run:', self.note.read_text())
        self.assertIn(f'Latest run: [[{wiki_target(self.root, brain.run_note(run))}|', self.note.read_text())
        self.assertIn('Closed run [[', (self.root / 'log.md').read_text())
        self.assertEqual(brain.frontmatter(self.run_note(run))['status'], 'closed')
        self.assertIn('## Summary', self.run_note(run))
        self.refused('No active run', 'show')
        self.refused('Run is closed', 'add', '--title', 'late', '--run', run)
        self.assert_valid()

    def test_claim_conflict_lease_expiry_and_dependencies(self):
        run = self.open_run()
        self.ok('add', '--title', 'First')
        self.ok('add', '--title', 'Second', '--depends', 't1')
        self.ok('claim', 't1', '--agent', 'alice')
        self.refused('claimed by alice', 'claim', 't1', '--agent', 'bob')
        renewed = self.ok('claim', 't1', '--agent', 'alice', '--lease-minutes', '5')['task']  # Same agent refreshes.
        self.assertEqual(renewed['owner'], 'alice')
        self.refused('waits for unfinished dependencies: t1', 'claim', 't2', '--agent', 'bob')
        # Expire alice's lease: another agent may take the task over.
        state = self.state(run)
        state['tasks'][0]['lease_until'] = '2020-01-01T00:00:00+00:00'
        brain.save_json(self.root / '.brain/runs' / (run + '.json'), state)
        self.assertEqual(self.ok('claim', 't1', '--agent', 'bob')['task']['owner'], 'bob')
        self.refused('claimed by bob', 'post', 't1', '--agent', 'alice', '--status', 'done', '--note', 'mine')
        self.ok('post', 't1', '--agent', 'bob', '--status', 'done', '--note', 'Done by bob.')
        self.refused('is done', 'claim', 't1', '--agent', 'carol')
        self.assertEqual(self.ok('claim', 't2', '--agent', 'carol')['task']['owner'], 'carol')
        self.refused('Only the current claimant', 'release', 't2', '--agent', 'bob')

    def test_only_the_claimant_posts_even_after_its_lease_expires(self):
        run = self.open_run()
        self.ok('add', '--title', 'Contested')
        self.ok('claim', 't1', '--agent', 'alice')
        state = self.state(run)
        state['tasks'][0]['lease_until'] = '2020-01-01T00:00:00+00:00'
        brain.save_json(self.root / '.brain/runs' / (run + '.json'), state)
        self.refused('claimed by alice; only the claimant posts', 'post', 't1', '--agent', 'bob', '--status', 'done', '--note', 'mine')
        self.assertEqual(self.state(run)['tasks'][0]['owner'], 'alice')
        self.ok('claim', 't1', '--agent', 'bob')  # Taking over an expired lease needs an explicit claim.
        failed = self.ok('post', 't1', '--agent', 'bob', '--status', 'failed', '--note', 'Could not finish.')['task']
        self.assertEqual((failed['status'], failed['owner'], failed['lease_until']), ('failed', None, None))
        retry = self.ok('post', 't1', '--agent', 'carol', '--status', 'in_progress', '--note', 'Retrying.')['task']
        self.assertEqual((retry['owner'], retry['status']), ('carol', 'in_progress'))  # Unclaimed: claimed implicitly.
        self.refused('claimed by carol', 'post', 't1', '--agent', 'bob', '--status', 'done', '--note', 'late')

    def test_evidence_links_resolve_to_the_exact_file(self):
        dotted = 'wiki/topics/api.v2.md'
        (self.root / dotted).write_text('---\ncreated: 2026-01-01\nupdated: 2026-01-01\nstatus: active\n---\n# API v2\n')
        brain.add_index(self.root, dotted, 'API v2')
        (self.root / 'raw/build-log').write_text('log')
        (self.root / 'raw/report.v1.pdf').write_bytes(b'%PDF')
        self.assert_valid()
        run = self.open_run()
        self.ok('add', '--title', 'Document v2')
        self.refused('file extension', 'post', 't1', '--agent', 'a', '--status', 'done', '--note', 'x', '--evidence', 'raw/build-log')
        self.ok('post', 't1', '--agent', 'a', '--status', 'done', '--note', 'Documented.', '--evidence', dotted,
                '--evidence', 'raw/report.v1.pdf')
        self.assertIn('[[wiki/topics/api.v2.md|api.v2.md]]', self.run_note(run))
        self.assert_valid()
        with self.assertRaisesRegex(ValueError, 'file extension'):
            brain.checkpoint(self.root, self.info['project'], 'Saved.', 'Next.', ['raw/build-log'])
        saved = brain.checkpoint(self.root, self.info['project'], 'Saved.', 'Next.', [dotted, 'raw/report.v1.pdf'])
        self.assertIn('[[wiki/topics/api.v2.md|api.v2.md]]', (self.root / saved['path']).read_text())
        self.assert_valid()
        self.handle('Stop', last_assistant_message='Decided on v2.', prompt_id='p1')
        source = self.records()[0].relative_to(self.root).as_posix()
        target = self.root / dotted
        with self.assertRaisesRegex(ValueError, 'file extension'):
            brain.consolidate(self.root, source, dotted, 'Use v2.', ['raw/build-log'], brain.digest(target.read_bytes()))
        brain.consolidate(self.root, source, dotted, 'Use v2.', [dotted], brain.digest(target.read_bytes()))
        self.assert_valid()
        listing = brain.compile_list(self.root, self.info['project'])
        target.write_text(target.read_text() + f'\nEvidence: [[{wiki_target(self.root, source)}]].\n')
        brain.compile_done(self.root, self.info['project'], listing['token'], [dotted])
        self.assertIn('[[wiki/topics/api.v2.md|api.v2]]', (self.root / 'log.md').read_text())
        self.assert_valid()
        self.assertEqual(brain.evidence_link(self.root, dotted), '[[wiki/topics/api.v2.md|api.v2.md]]')
        self.assertEqual(brain.evidence_link(self.root, 'raw/report.v1.pdf'), '[[raw/report.v1.pdf|report.v1.pdf]]')
        self.assertEqual(brain.evidence_link(self.root, 'wiki/topics/plain.md'), '[[wiki/topics/plain|plain.md]]')

    def test_post_rules_evidence_and_names(self):
        self.open_run()
        self.ok('add', '--title', 'Unclaimed')
        posted = self.ok('post', 't1', '--agent', 'dave', '--status', 'blocked', '--note', 'Waiting on review.')['task']
        self.assertEqual((posted['owner'], posted['status']), ('dave', 'blocked'))  # Claimed implicitly.
        self.refused('Evidence does not exist', 'post', 't1', '--agent', 'dave', '--status', 'in_progress', '--note', 'x',
                  '--evidence', 'wiki/topics/missing.md')
        for bad in ('../outside.md', '/etc/passwd', '.brain/write.flock'):
            code, result = self.board('post', 't1', '--agent', 'dave', '--status', 'in_progress', '--note', 'x', '--evidence', bad)
            self.assertEqual(code, 1, bad)
        self.refused('Agent name', 'claim', 't1', '--agent', 'bad name')
        self.refused('Agent name', 'claim', 't1', '--agent', 'sk-' + 'a' * 30)
        self.refused('Agent name', 'claim', 't1', '--agent', 'x' * 65)
        self.refused('A note is required', 'post', 't1', '--agent', 'dave', '--status', 'done', '--note', '  ')
        self.refused('Unknown task', 'claim', 'nope', '--agent', 'dave')
        self.refused('Task id already exists', 'add', '--title', 'dup', '--id', 't1')
        self.refused('Unknown dependency', 'add', '--title', 'dep', '--depends', 'zzz')
        self.refused('still active', 'open', '--title', 'Another')

    def test_run_resolution_order(self):
        old = self.open_run('Old run')
        self.ok('close')
        new = self.open_run('New run')
        self.assertEqual(self.ok('show')['run'], new)  # The project's active run.
        self.assertEqual(self.ok('show', '--run', old)['run'], old)
        with patch.dict(os.environ, {'SECOND_BRAIN_RUN': old}):
            self.assertEqual(self.ok('show')['run'], old)  # env beats the active run.
            self.assertEqual(self.ok('show', '--run', new)['run'], new)  # --run beats env.
        with patch.dict(os.environ, {'SECOND_BRAIN_RUN': '2020-01-01-missing-abcdef'}):
            self.refused('Unknown run', 'show')
        self.refused('Invalid run id', 'show', '--run', '../../escape')
        other = self.base / 'other'
        other.mkdir()
        setup.initialize(self.config, other)
        code, result = self.board('show', '--run', new, cwd=other)
        self.assertRegex(result['error'], 'another project')

    def test_another_projects_board_never_matches_prompts(self):
        other = self.base / 'other'
        other.mkdir()
        setup.initialize(self.config, other)
        code, result = self.board('open', '--title', 'Other ZEPHYR migration', cwd=other)
        self.assertEqual(code, 0, result)
        mine = self.open_run('My ZEPHYR migration')
        self.handle('SessionStart', 'reader', source='startup')
        text = self.context(self.handle('UserPromptSubmit', 'reader', prompt='zephyr migration status'))
        paths = [n['path'] for n in json.loads(text[len(hooks.PROMPT_HEADER):])['matching_notes']]
        self.assertIn(brain.run_note(mine), paths)
        self.assertNotIn(brain.run_note(result['run']), paths)

    def test_unbound_project_is_refused(self):
        unbound = self.base / 'unbound'
        unbound.mkdir()
        code, result = self.board('open', '--title', 'x', cwd=unbound)
        self.assertEqual(code, 1)
        self.assertIn('Bind this project', result['error'])

    def test_closed_runs_compile_like_records_even_without_records(self):
        run = self.open_run()
        self.assertEqual(self.cli('compile')[1], {'records': [], 'pending_total': 0})  # Still active: nothing yet.
        self.ok('close', '--summary', 'Parser shipped.')
        nudge = self.context(self.handle('SessionStart', 'next', source='startup'))
        self.assertIn('Compile pending: 0 automatic record(s) from other sessions and 1 closed run board(s)', nudge)
        code, listing = self.cli('compile')
        self.assertEqual((code, listing['records'], listing['pending_total']), (0, [], 1))
        entry, = listing['runs']
        self.assertEqual((entry['run'], entry['path']), (run, brain.run_note(run)))
        self.assertEqual(entry['link'], f'[[{wiki_target(self.root, brain.run_note(run))}]]')
        self.assertEqual(brain.compile_status(self.root, self.info['project'])['pending_total'], 1)
        code, refused = self.cli('compile', '--done', listing['token'], '--updated', self.info['project'])
        self.assertIn('does not cite', refused['error'])  # The run must be cited or skipped.
        self.note.write_text(self.note.read_text().replace(
            '## Status\n\nNone recorded yet.', f'## Status\n\nParser shipped (agent claim). Evidence: {entry["link"]}.'))
        code, done = self.cli('compile', '--done', listing['token'], '--updated', self.info['project'])
        self.assertEqual((code, done['compiled'], done['pending_total']), (0, [brain.run_note(run)], 0))
        self.assertEqual(self.state(run)['compiled'], brain.day())
        self.assertEqual(self.cli('compile')[1], {'records': [], 'pending_total': 0})
        self.assertNotIn('Compile pending', self.context(self.handle('SessionStart', 'again', source='startup')))
        other = self.open_run('Second run')  # A run can also be skipped by its id.
        self.ok('close')
        listing = self.cli('compile')[1]
        code, done = self.cli('compile', '--done', listing['token'], '--skip', other)
        self.assertEqual((code, done['skipped'], done['pending_total']), (0, [brain.run_note(other)], 0))
        self.assertEqual(self.state(other)['compiled'], brain.day())
        self.assert_valid()


class RunStateTests(OrchestrationCase):
    def test_snapshot_and_restore_keep_run_state(self):
        closed = self.open_run('Closed run')
        self.ok('add', '--title', 'Ship')
        self.ok('claim', 't1', '--agent', 'alice')
        self.ok('post', 't1', '--agent', 'alice', '--status', 'done', '--note', 'Shipped.')
        self.ok('close', '--summary', 'Done.')
        active = self.open_run('Active run')
        self.ok('add', '--title', 'Follow up')
        self.ok('claim', 't1', '--agent', 'bob')
        runs.spool(self.root, {'kind': 'join', 'project': self.info['project'], 'key': 'f' * 24, 'run': active})
        self.handle('SessionStart', 'transient', source='startup')  # Session state and a receipt exist too.
        runs.spool(self.root, {'kind': 'join', 'project': self.info['project'], 'key': 'e' * 24, 'run': active})
        archive = self.base / 'snap.zip'
        brain.snapshot(self.root, archive)
        import zipfile
        names = zipfile.ZipFile(archive).namelist()
        kept = sorted(n for n in names if '/.brain/' in n)
        self.assertEqual(kept, sorted(['vault/.brain/runs/' + closed + '.json', 'vault/.brain/runs/' + active + '.json',
                                       'vault/' + runs.pointer(self.root, self.info['project']).relative_to(self.root).as_posix()]))
        restored = Path(brain.restore(archive, self.base / 'restored')['restored'])
        self.assertEqual(runs.show(restored, self.info['project'], closed)['tasks'][0]['result'], 'Shipped.')
        self.assertEqual(runs.active(restored, self.info['project'])['run'], active)
        self.assertEqual(runs.active(restored, self.info['project'])['tasks'][0]['owner'], 'bob')
        self.assertEqual([r['run'] for r in brain.uncompiled_runs(restored, self.info['project'])], [closed])
        self.assertFalse((restored / brain.SPOOL).exists())
        self.assertEqual(check(restored)[0], [])

    def test_restore_rejects_other_brain_paths(self):
        import zipfile
        for bad in ('.brain/automation/session-' + 'a' * 32 + '.json', '.brain/runs/../x.json', '.brain/write.flock'):
            archive = self.base / ('bad-' + str(abs(hash(bad))) + '.zip')
            data = b'{}'
            with zipfile.ZipFile(archive, 'w') as out:
                out.writestr('manifest.json', json.dumps({'schema_version': 1, 'files': {bad: {'sha256': brain.digest(data), 'mode': 0o644}}}))
                out.writestr('vault/' + bad, data)
            with self.subTest(bad), self.assertRaisesRegex(ValueError, 'Unsafe archive path'):
                brain.restore(archive, self.base / ('out-' + archive.stem))

    def test_activity_is_capped_and_hook_lines_stay_idempotent(self):
        run = self.open_run()
        key = 'a' * 24
        with brain.writer(self.root):
            self.assertTrue(runs.join(self.root, run, self.info['project'], key))
            data = runs.load(self.root, run)
            for number in range(2000 + 50):  # runs.ACTIVITY_KEEP lines are kept.
                runs.log(data, 'board', f'line {number}')
            runs.save(self.root, data)
            self.assertFalse(runs.join(self.root, run, self.info['project'], key))  # Its line was dropped, not its id.
        data = self.state(run)
        self.assertEqual((len(data['activity']), data.get('activity_dropped')), (2000, 52))
        self.assertIn('earliest entries no longer kept', self.run_note(run))
        self.assert_valid()


@unittest.skipUnless(os.name == 'posix', 'process tests use POSIX locks')
class ConcurrencyTests(OrchestrationCase):
    def test_eight_processes_claim_one_task_exactly_once(self):
        run = self.open_run()
        self.ok('add', '--title', 'Contested')
        go = time.time() + 1.5
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        procs = [subprocess.Popen([sys.executable, '-B', '-c', CLAIMER, str(SCRIPTS), str(go), str(self.config),
                                   str(self.project), f'agent{n}'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, env=env) for n in range(8)]
        results = [(proc.wait(timeout=60), proc.stdout.read(), proc.stderr.read()) for proc in procs]
        for proc in procs:
            proc.stdout.close()
            proc.stderr.close()
        winners = [json.loads(out)['task']['owner'] for code, out, _ in results if code == 0]
        self.assertEqual(len(winners), 1, results)
        for code, out, err in results:
            if code:
                self.assertIn('claimed by ' + winners[0], json.loads(out)['error'])
        self.assertEqual(self.state(run)['tasks'][0]['owner'], winners[0])
        self.assertEqual(sum('claimed t1' in e['text'] for e in self.state(run)['activity']), 1)


class WorkerTests(OrchestrationCase):
    def test_worker_session_joins_board_and_records_its_run(self):
        run = self.open_run()
        self.ok('add', '--title', 'Worker task')
        with patch.dict(os.environ, {'SECOND_BRAIN_RUN': run}):
            text = self.context(self.handle('SessionStart', 'worker', source='startup'))
            self.assertIn(f'Run board {run}:', text)
            self.assertIn(f'run board claim TASK --agent NAME --run {run}', text)
            data = json.loads(text[text.index('{'):])
            self.assertEqual(data['run'], run)
            self.assertIn('- t1 [open] Worker task', data['run_board'])
            self.handle('SessionStart', 'worker', source='resume')
            self.handle('UserPromptSubmit', 'worker', prompt='Do t1.', prompt_id='w1')
            self.handle('Stop', 'worker', last_assistant_message='t1 done.', prompt_id='w1')
        record, = self.records()
        text = record.read_text()
        self.assertEqual(brain.frontmatter(text)['run'], run)
        self.assertIn(f'Run: [[{wiki_target(self.root, brain.run_note(run))}|Run board]].', text)
        key = hooks.session_key(self.info, {'session_id': 'worker'})
        joined = [e for e in self.state(run)['activity'] if e['text'] == f'session {key} joined']
        self.assertEqual(len(joined), 1)
        self.assertIn(f'session {key} joined', self.run_note(run))
        self.assert_valid()

    def test_unknown_run_warns_and_continues(self):
        with patch.dict(os.environ, {'SECOND_BRAIN_RUN': '2020-01-01-gone-abcdef'}):
            text = self.context(self.handle('SessionStart', 'worker', source='startup'))
            self.handle('Stop', 'worker', last_assistant_message='Still recorded.')
        self.assertIn('SECOND_BRAIN_RUN=2020-01-01-gone-abcdef does not name a run of this project', text)
        self.assertIn('Shared memory: read index.md', text)
        record, = self.records()
        self.assertNotIn('run', brain.frontmatter(record.read_text()))
        with patch.dict(os.environ, {'SECOND_BRAIN_RUN': '../bad id'}):
            self.assertIn('SECOND_BRAIN_RUN=(invalid id)', self.context(self.handle('SessionStart', 'w2', source='startup')))

    def test_unknown_or_closed_env_run_never_falls_back_to_the_active_board(self):
        closed = self.open_run('Closed run')
        self.ok('close')
        active = self.open_run('Active run')
        for name in ('2020-01-01-gone-abcdef', closed):
            with self.subTest(name), patch.dict(os.environ, {'SECOND_BRAIN_RUN': name}):
                session = 'w-' + name[-6:]
                before = len(self.state(active)['activity'])
                start = self.context(self.handle('SessionStart', session, source='startup'))
                self.assertIn('continuing without a run board', start)
                self.assertNotIn('run_board', start)
                brief = self.context(self.handle('SubagentStart', session, agent_id=AGENT_ID, agent_type='Explore'))
                self.assertNotIn('RUN BOARD', brief)
                self.assertIn('continuing without a run board', brief)
                self.subagent_stop('NO_BOARD_RESULT', session=session)
                self.handle('Stop', session, last_assistant_message='worker answer', prompt_id='q1')
                self.assertEqual(len(self.state(active)['activity']), before)  # Nothing reached the active board.
                key = hooks.session_key(self.info, {'session_id': session})
                record, = [p for p in self.records() if key in p.name]
                self.assertNotIn('run', brain.frontmatter(record.read_text()))
        self.assertEqual(self.state(closed)['status'], 'closed')
        self.assertEqual(len([e for e in self.state(closed)['activity'] if 'joined' in e['text']]), 0)
        self.subagent_stop('ACTIVE_BOARD_RESULT', session='plain')  # Without the env, the active board applies.
        self.assertTrue(any('ACTIVE_BOARD_RESULT' in e['text'] for e in self.state(active)['activity']))

    def test_launch_run_dry_run(self):
        run = self.open_run()
        out = io.StringIO()
        with redirect_stdout(out):
            code = setup.main(['--config', str(self.config), '--cwd', str(self.project), 'launch', '--dry-run',
                               '--run', run, 'codex', 'exec', 'claim and do t1'])
        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result['env']['SECOND_BRAIN_RUN'], run)
        self.assertEqual(result['env']['SECOND_BRAIN_CONFIG'], str(self.config.resolve()))
        self.assertEqual(result['command'][:2], ['codex', '--add-dir'])
        self.assertEqual(result['command'][-2:], ['exec', 'claim and do t1'])
        err = io.StringIO()
        with redirect_stderr(err):
            code = setup.main(['--config', str(self.config), '--cwd', str(self.project), 'launch', '--dry-run',
                               '--run', '2020-01-01-gone-abcdef', 'claude', '-p', 'x'])
        self.assertEqual(code, 1)
        self.assertIn('Unknown run', err.getvalue())


@unittest.skipUnless(os.name == 'posix', 'process tests use POSIX locks')
class SpoolTests(OrchestrationCase):
    def hold_lock(self):
        proc = subprocess.Popen([sys.executable, '-B', '-c', HOLDER, str(SCRIPTS), str(self.root)],
                                stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.addCleanup(proc.stdout.close)
        self.assertEqual(proc.stdout.readline().strip(), 'locked')
        return proc

    def spooled(self):
        folder = self.root / brain.SPOOL
        return sorted(p for p in folder.iterdir() if brain.SPOOL_NAME.fullmatch(p.name)) if folder.is_dir() else []

    def hook_process(self, event):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        return subprocess.Popen([sys.executable, '-B', str(SCRIPTS / 'memory_hooks.py')], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)

    def test_stops_under_contention_spool_then_drain_exactly_once(self):
        sessions = [f'worker-{n}' for n in range(8)]
        for session in sessions:
            self.handle('UserPromptSubmit', session, prompt=f'Request {session}', prompt_id=f'{session}-p')
        holder = self.hold_lock()
        procs = []
        for session in sessions:
            event = {'hook_event_name': 'Stop', 'cwd': str(self.project), 'session_id': session, 'prompt_id': f'{session}-p',
                     'last_assistant_message': f'Answer {session} password=hunter2', 'stop_hook_active': False}
            proc = self.hook_process(event)
            procs.append((proc, json.dumps(event)))
        for proc, data in procs:  # Feed every hook before reading any, so all eight wait on the lock at once.
            proc.stdin.write(data)
            proc.stdin.close()
        started = time.monotonic()
        outputs = [(proc.wait(timeout=60), proc.stdout.read(), proc.stderr.read()) for proc, _ in procs]
        self.assertLess(time.monotonic() - started, 4 * brain.LOCK_WAIT)  # Parallel waits, each bounded.
        for (proc, _), (code, out, err) in zip(procs, outputs):
            proc.stdout.close()
            proc.stderr.close()
            self.assertEqual((code, json.loads(out), err), (0, {}, ''))
        self.assertEqual(len(self.spooled()), 8)
        self.assertEqual(self.records(), [])
        self.assertNotIn('hunter2', ''.join(p.read_text() for p in self.spooled()))  # Spooled already redacted.
        kinds = [f['kind'] for f in brain.doctor(self.root)['findings']]
        self.assertIn('spool_backlog', kinds)
        holder.kill()
        holder.wait()
        replay = self.spooled()[0].read_text()
        self.handle('SessionStart', 'later', source='startup')  # Any lock holder drains the spool.
        self.assertEqual(self.spooled(), [])
        self.assertEqual(len(self.records()), 8)
        for session in sessions:
            name = hooks.session_key(self.info, {'session_id': session})
            record, = [p for p in self.records() if name in p.name]
            text = record.read_text()
            self.assertEqual(len(brain.turns(text)), 1)
            self.assertIn(f'Request {session}', text)
            self.assertIn(f'Answer {session}', text)
        before = self.snapshot()
        replayed = self.root / brain.SPOOL / f'{time.time_ns():020d}-{json.loads(replay)["key"]}-{"0" * 32}.json'
        replayed.write_text(replay)  # The same item applied twice.
        self.assertEqual(self.spooled(), [replayed])
        self.handle('SessionStart', 'later', source='resume')
        self.assertEqual(self.spooled(), [])
        self.assertEqual(before, self.snapshot())
        self.assertNotIn('spool_backlog', [f['kind'] for f in brain.doctor(self.root)['findings']])
        self.assert_valid()

    def test_prompt_and_session_start_deliver_context_under_contention(self):
        run = self.open_run()
        holder = self.hold_lock()
        with patch.object(brain, 'LOCK_WAIT', 0.2), patch.dict(os.environ, {'SECOND_BRAIN_RUN': run}):
            start = self.handle('SessionStart', 'busy', source='startup')
            self.assertIn('Shared memory: read index.md', self.context(start))
            self.assertIn(f'Run board {run}:', self.context(start))
            self.handle('UserPromptSubmit', 'busy', prompt='Busy request', prompt_id='b1')
            brief = self.context(self.handle('SubagentStart', 'busy', agent_id=AGENT_ID, agent_type='Explore'))
            self.assertIn('RUN BOARD:', brief)
            self.subagent_stop('BUSY_RESULT', session='busy')
            self.handle('Stop', 'busy', last_assistant_message='Busy answer', prompt_id='b1')
        state = brain.load_json(hooks.session_path(self.info, {'session_id': 'busy'}), {})
        self.assertTrue(state['primed'])
        self.assertEqual(state['prompt'], 'Busy request')  # Written directly; the session file is its own.
        self.assertEqual((state['run'], len(self.spooled())), (run, 3))  # The join, the result, and the turn.
        self.assertEqual(setup.automation(self.config, self.project)['events'], {})  # Receipts need the lock.
        holder.kill()
        holder.wait()
        self.handle('SessionStart', 'next', source='startup')
        record, = self.records()
        text = record.read_text()
        self.assertLess(text.index('BUSY_RESULT'), text.index('Busy answer'))
        self.assertEqual(brain.frontmatter(text)['run'], run)
        key = hooks.session_key(self.info, {'session_id': 'busy'})
        self.assertEqual(sum(e['text'] == f'session {key} joined' for e in self.state(run)['activity']), 1)
        self.assertEqual(sum(e['text'].startswith('subagent Explore') or e['text'].startswith('subagent general')
                             for e in self.state(run)['activity']), 1)
        self.assert_valid()

    def backlog_item(self, number, **extra):
        item = {'kind': 'turn', 'project': self.info['project'], 'state': 'session-' + 'a' * 32 + '.json',
                'time': '10:00', 'run': None, 'label': 'Request', 'prompt': 'p', 'ident': '',
                'key': f'{number:024x}', 'answer': brain.digest(str(number).encode())[:24], 'response': f'backlog {number}'}
        return dict(item, **extra)

    def drain(self, **extra):
        with brain.writer(self.root):
            return runs.drain(self.root, setup.automatic_projects(setup.registry(self.config), self.info['vault_id']), **extra)

    def test_a_sessions_queued_items_keep_their_order(self):
        with patch.object(brain, 'LOCK_WAIT', 0.2):
            holder = self.hold_lock()
            self.handle('UserPromptSubmit', prompt='first', prompt_id='p1')
            self.handle('Stop', last_assistant_message='first answer', prompt_id='p1')
            self.handle('UserPromptSubmit', prompt='second', prompt_id='p2')
            holder.kill()
            holder.wait()
        self.assertEqual(len(self.spooled()), 1)
        self.handle('Stop', last_assistant_message='second answer', prompt_id='p2')  # Queued behind the first.
        self.assertEqual(self.spooled(), [])
        record, = self.records()
        text = record.read_text()
        self.assertLess(text.index('first answer'), text.index('second answer'))
        self.assertEqual(re.findall(r'^## Turn (\d+) ', text, re.M), ['1', '2'])

    def test_drain_attempts_at_most_twenty_and_keeps_failed_items_in_place(self):
        for number in range(25):  # Each fails inside apply (its session state name is invalid).
            runs.spool(self.root, self.backlog_item(number, state='bad.json'))
        runs.spool(self.root, self.backlog_item(99))
        before = [p.name for p in self.spooled()]
        attempts, real = [], runs.apply
        with patch.object(runs, 'apply', lambda root, item: attempts.append(item['key']) or real(root, item)):
            self.assertFalse(self.drain())
        self.assertEqual(len(attempts), 20)
        self.assertEqual([p.name for p in self.spooled()], before)  # Names, and so the order, never change.
        tried = [json.loads(p.read_text()) for p in self.spooled()]
        self.assertEqual([t.get('attempts') for t in tried[:21]], [1] * 20 + [None])
        self.assertIn('Invalid session state name', tried[0]['error'])

    def test_unbound_items_wait_without_attempts_and_expire_to_failed(self):
        for number in range(25):  # Not attempts: their project is not bound to this vault.
            runs.spool(self.root, self.backlog_item(number, project='memory/projects/unbound.md'))
        runs.spool(self.root, self.backlog_item(99))
        self.assertTrue(self.drain())
        self.assertEqual(len(self.spooled()), 25)
        self.assertTrue(any('backlog 99' in p.read_text() for p in self.records()))
        # Unreadable items and items older than 7 days move to failed/; doctor reports them.
        name = self.spooled()[0].name
        item = json.loads(self.spooled()[0].read_text())
        brain.save_json(self.spooled()[0], dict(item, spooled_ns=time.time_ns() - 8 * 86400 * 10 ** 9))
        corrupt = self.root / brain.SPOOL / f'{time.time_ns() - 86400 * 10 ** 9:020d}-{"c" * 24}-{"c" * 32}.json'
        corrupt.write_text('{not json')
        self.drain()
        self.assertTrue((self.root / brain.SPOOL / 'failed' / name).is_file())
        self.assertTrue((self.root / brain.SPOOL / 'failed' / corrupt.name).is_file())
        failed, = [f for f in brain.doctor(self.root)['findings'] if f['kind'] == 'spool_failed']
        self.assertEqual(failed['count'], 2)

    def test_a_transient_failure_never_reorders_a_session(self):
        session = hooks.session_path(self.info, {'session_id': 'ordered'}).name
        key = hooks.session_key(self.info, {'session_id': 'ordered'})
        for number, text in ((1, 'FIRST_TURN'), (2, 'SECOND_TURN')):
            runs.spool(self.root, self.backlog_item(number, key=key, state=session, response=text))
        runs.spool(self.root, self.backlog_item(7, response='OTHER_SESSION'))
        real = records.write_entry
        def flaky(root, item):
            if item['response'] == 'FIRST_TURN':
                raise OSError('disk briefly full')
            return real(root, item)
        with patch.object(records, 'write_entry', flaky):
            self.drain()
        self.assertTrue(any('OTHER_SESSION' in p.read_text() for p in self.records()))  # Others progress...
        self.assertFalse(any('SECOND_TURN' in p.read_text() for p in self.records()))  # ...this session waits.
        first = json.loads(self.spooled()[0].read_text())
        self.assertEqual((first['response'], first['attempts']), ('FIRST_TURN', 1))
        self.drain()
        self.assertEqual(self.spooled(), [])
        record, = [p for p in self.records() if key in p.name]
        text = record.read_text()
        self.assertLess(text.index('FIRST_TURN'), text.index('SECOND_TURN'))
        self.assertEqual(re.findall(r'^## Turn (\d+) ', text, re.M), ['1', '2'])

    def test_a_retry_completes_navigation_after_a_fault_between_any_writes(self):
        # The first record of a project writes, in order: record, project pointer, log.md, records index, the
        # project's records link, index.md, then session state. A fault after the record must not leave a gap.
        real = brain.atomic_write
        for where, nth in (('project pointer', 1), ('log', 1), ('records index', 1), ('project records link', 2),
                           ('index.md', 1), ('state', 1)):
            for kind in ('turn', 'subagent'):
                with self.subTest(where=where, kind=kind):
                    folder = self.base / re.sub(r'\W', '-', f'{where}-{kind}')
                    folder.mkdir()
                    info = setup.initialize(self.config, folder)
                    project, index = self.root / info['project'], self.root / hooks.records_index(info['project'])
                    state_path = hooks.session_path(info, {'session_id': 's'})
                    target = {'project pointer': project, 'records index': index, 'log': self.root / 'log.md',
                              'project records link': project, 'index.md': self.root / 'index.md'}.get(where, state_path)
                    event = {'session_id': 's', 'prompt_id': 'f1', 'agent_id': AGENT_ID, 'agent_type': 'Explore'}
                    item = (hooks.turn_item(info, event, state_path, {}, 'ANSWER ' + where) if kind == 'turn' else
                            hooks.subagent_item(info, event, state_path, where, 'ANSWER ' + where))
                    runs.spool(self.root, item)
                    seen = []

                    def fault(path, text):
                        if Path(path) == target:
                            seen.append(path)
                            if len(seen) == nth:
                                raise OSError('injected fault at ' + where)
                        return real(path, text)
                    with patch.object(brain, 'atomic_write', fault):
                        self.drain()
                    self.assertEqual((len(seen), len(self.spooled())), (nth, 1))  # It fired; the item stays queued.
                    self.drain()  # The retry finds its entry and completes what the fault left out.
                    self.assertEqual(self.spooled(), [])
                    key = hooks.session_key(info, {'session_id': 's'})
                    record, = [p for p in self.records() if key in p.name]
                    rel = record.relative_to(self.root).as_posix()
                    self.assertEqual(record.read_text().count('ANSWER ' + where), 1)
                    note = project.read_text()
                    self.assertIn(f'Latest automatic record: [[{wiki_target(self.root, rel)}|', note)
                    self.assertIn(f'Automatic records: [[{wiki_target(self.root, hooks.records_index(info["project"]))}|', note)
                    self.assertIn(f'[[{wiki_target(self.root, rel)}|', index.read_text())
                    self.assertIn(wiki_target(self.root, hooks.records_index(info['project'])), (self.root / 'index.md').read_text())
                    self.assertEqual(brain.load_json(state_path, {})['record'], rel)
                    self.assertIn(f'Created automatic records index [[{wiki_target(self.root, hooks.records_index(info["project"]))}|',
                                  (self.root / 'log.md').read_text())
                    self.assert_valid()

    def test_a_retry_does_not_take_the_pointer_from_a_newer_record(self):
        state_path = hooks.session_path(self.info, {'session_id': 'older'})
        item = hooks.turn_item(self.info, {'session_id': 'older', 'prompt_id': 'o1'}, state_path, {}, 'OLDER_ANSWER')
        runs.spool(self.root, item)
        real = brain.atomic_write
        project = self.root / self.info['project']
        with patch.object(brain, 'atomic_write', lambda path, text: (_ for _ in ()).throw(OSError('fault'))
                          if Path(path) == project else real(path, text)):
            self.drain()  # The record is written; the pointer write fails.
        time.sleep(0.02)
        self.handle('Stop', 'newer', last_assistant_message='NEWER_ANSWER', prompt_id='n1')  # Drains the retry too.
        self.assertEqual(self.spooled(), [])
        newer = next(p for p in self.records() if 'NEWER_ANSWER' in p.read_text())
        self.assertIn(f'Latest automatic record: [[{wiki_target(self.root, newer.relative_to(self.root).as_posix())}|',
                      project.read_text())
        self.assert_valid()

    def test_drain_checks_its_deadline_before_every_apply(self):
        for number in range(10):
            runs.spool(self.root, self.backlog_item(number))
        attempts, real = [], runs.apply

        def slow(root, item):
            attempts.append(item['key'])
            time.sleep(0.15)
            return real(root, item)
        with patch.object(runs, 'apply', slow):
            started = time.monotonic()
            self.assertFalse(self.drain(seconds=0.4))
        self.assertLess(time.monotonic() - started, 0.4 + 0.15 + 0.2)
        self.assertLessEqual(len(attempts), 3)
        self.assertEqual(len(self.spooled()), 10 - len(attempts))

    def test_own_item_is_persisted_before_a_slow_backlog(self):
        own = hooks.session_key(self.info, {'session_id': 'mine'})
        self.handle('UserPromptSubmit', 'mine', prompt='mine', prompt_id='m1')
        runs.spool(self.root, self.backlog_item(1))  # Another session's queued item whose apply hangs.
        event = {'hook_event_name': 'Stop', 'cwd': str(self.project), 'session_id': 'mine', 'prompt_id': 'm1',
                 'last_assistant_message': 'OWN_RESULT'}
        proc = subprocess.Popen([sys.executable, '-B', '-c', SLOW_HOOK, str(SCRIPTS), own], stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
                                env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        proc.stdin.write(json.dumps(event))
        proc.stdin.close()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not any('OWN_RESULT' in p.read_text() for p in self.records()):
            time.sleep(0.05)
        self.assertIsNone(proc.poll())  # Still stuck in the backlog...
        proc.kill()  # ...when the client's timeout kills it.
        proc.wait()
        self.assertTrue(any('OWN_RESULT' in p.read_text() for p in self.records()))
        self.assertEqual(len(self.spooled()), 1)

    def test_partial_failure_is_completed_exactly_once(self):
        run = self.open_run()
        with patch.object(brain, 'LOCK_WAIT', 0.2):
            holder = self.hold_lock()
            self.subagent_stop('PARTIAL_RESULT')
            holder.kill()
            holder.wait()
        with patch.object(runs, 'note_activity', side_effect=OSError('disk full')):
            self.assertTrue(self.drain())
        self.assertEqual(len(self.spooled()), 1)  # Record written, activity failed: the item stays queued.
        self.assertEqual(self.records()[0].read_text().count('PARTIAL_RESULT'), 1)
        self.drain()
        self.assertEqual(self.spooled(), [])
        self.assertEqual(self.records()[0].read_text().count('PARTIAL_RESULT'), 1)
        self.assertEqual(sum('PARTIAL_RESULT' in e['text'] for e in self.state(run)['activity']), 1)

    def test_direct_state_write_loses_at_most_its_own_prompt(self):
        state_path = hooks.session_path(self.info, {'session_id': 'race'})
        self.handle('UserPromptSubmit', 'race', prompt='first', prompt_id='r1')
        holder = self.hold_lock()
        real_save = brain.save_json
        # 1. A lock holder records turn r1 between the fallback's state read and its direct write.
        def concurrent_turn(path, data):
            if Path(path) == state_path and data.get('prompt') == 'second':
                with patch.object(brain, 'save_json', real_save):
                    item = hooks.turn_item(self.info, {'session_id': 'race', 'prompt_id': 'r1'}, state_path,
                                           brain.load_json(state_path, {}), 'first answer')
                    runs.apply(self.root, item)  # The holder process applies it under its lock.
            return real_save(path, data)
        with patch.object(brain, 'LOCK_WAIT', 0.2), patch.object(brain, 'save_json', concurrent_turn):
            self.handle('UserPromptSubmit', 'race', prompt='second', prompt_id='r2')
        state = brain.load_json(state_path, {})
        self.assertEqual(state['prompt'], 'second')  # The fallback's write won; the holder's index entry is lost...
        self.assertNotIn('turn_keys', state)
        holder.kill()
        holder.wait()
        record, = self.records()
        self.assertIn('first answer', record.read_text())  # ...but never the recorded turn.
        self.handle('Stop', 'race', last_assistant_message='first answer', prompt_id='r1')  # A retry is still caught.
        self.handle('Stop', 'race', last_assistant_message='second answer', prompt_id='r2')
        text = record.read_text()
        self.assertEqual(re.findall(r'^## Turn (\d+) ', text, re.M), ['1', '2'])
        self.assertIn('second', text.split('## Turn 2')[1])
        # 2. The other order: the holder's stale state write replaces the fallback's prompt.
        holder = self.hold_lock()
        real_remember = records.remember_record
        def stale_remember(path, relative, keys=None, marks=None):
            stale = brain.load_json(path, {})
            with patch.object(brain, 'LOCK_WAIT', 0.2):
                self.handle('UserPromptSubmit', 'race', prompt='third', prompt_id='r3')  # Direct write lands here.
            real_save(path, stale)
            return real_remember(path, relative, keys, marks)
        item = hooks.turn_item(self.info, {'session_id': 'race', 'prompt_id': 'x9'}, state_path,
                               brain.load_json(state_path, {}), 'background answer')
        with patch.object(records, 'remember_record', stale_remember):
            runs.apply(self.root, item)
        holder.kill()
        holder.wait()
        self.assertNotEqual(brain.load_json(state_path, {}).get('prompt'), 'third')  # Only that prompt is lost.
        self.handle('Stop', 'race', last_assistant_message='third answer', prompt_id='r3')
        text = record.read_text()
        self.assertEqual(re.findall(r'^## Turn (\d+) ', text, re.M), ['1', '2', '3', '4'])
        self.assertIn('background answer', text)
        self.assertIn('[prompt not captured for this turn]', text.split('## Turn 4')[1])
        self.assertIn('third answer', text.split('## Turn 4')[1])

    def test_a_board_note_left_stale_by_a_failed_write_is_repaired_on_retry(self):
        run = self.open_run()
        note = (self.root / brain.run_note(run)).resolve()
        state_path = hooks.session_path(self.info, {'session_id': 'stale'})
        items = {'join': {'kind': 'join', 'project': self.info['project'], 'key': 'b' * 24, 'run': run},
                 'subagent': hooks.subagent_item(self.info, {'session_id': 'stale', 'agent_id': AGENT_ID, 'agent_type': 'Explore'},
                                                 state_path, 'STALE_RESULT', 'STALE_RESULT')}
        lines = {'join': f'session {"b" * 24} joined', 'subagent': 'finished: STALE_RESULT'}
        real = brain.atomic_write
        for kind, item in items.items():
            with self.subTest(kind):
                runs.spool(self.root, item)
                failures = []

                def fail_note(path, text):  # Run JSON saved, then the Markdown write fails once.
                    if Path(path).resolve() == note and not failures:
                        failures.append(path)
                        raise OSError('disk full while rendering')
                    return real(path, text)
                with patch.object(brain, 'atomic_write', fail_note):
                    self.drain()
                self.assertEqual((len(failures), len(self.spooled())), (1, 1))
                self.assertEqual(sum(lines[kind] in e['text'] for e in self.state(run)['activity']), 1)
                self.assertNotIn(lines[kind], self.run_note(run))  # The note is stale until the retry.
                self.drain()
                self.assertEqual(self.spooled(), [])
                self.assertEqual(self.run_note(run).count(lines[kind]), 1)
                self.assertEqual(sum(lines[kind] in e['text'] for e in self.state(run)['activity']), 1)
        # A board command that refuses still brings the note in line: close's note write fails, the retry is refused.
        with patch.object(brain, 'atomic_write', lambda path, text: (_ for _ in ()).throw(OSError('disk full'))
                          if Path(path).resolve() == note else real(path, text)):
            self.assertEqual(self.board('close')[0], 1)
        self.assertEqual(brain.frontmatter(self.run_note(run))['status'], 'active')
        self.refused('Run is closed', 'add', '--title', 'late', '--run', run)
        self.assertEqual(brain.frontmatter(self.run_note(run))['status'], 'closed')

    def test_a_replay_after_compile_never_moves_the_pointer_back(self):
        first, second = sorted(('alpha', 'beta'), key=lambda n: hooks.session_key(self.info, {'session_id': n}))
        self.handle('Stop', first, last_assistant_message='ANSWER_A', prompt_id='a1')
        time.sleep(0.02)
        self.handle('Stop', second, last_assistant_message='ANSWER_B', prompt_id='b1')
        newer = next(p for p in self.records() if 'ANSWER_B' in p.read_text())
        pointer = f'Latest automatic record: [[{wiki_target(self.root, newer.relative_to(self.root).as_posix())}|'
        self.assertIn(pointer, self.note.read_text())
        time.sleep(0.02)
        listing = brain.compile_list(self.root, self.info['project'], limit=1)  # Lists A only, then rewrites it.
        older, = listing['records']
        self.assertNotEqual(older['path'], newer.relative_to(self.root).as_posix())
        brain.compile_done(self.root, self.info['project'], listing['token'], [], [older['path']])
        self.handle('Stop', first, last_assistant_message='ANSWER_A', prompt_id='a1')  # A replay of A's Stop.
        self.assertIn(pointer, self.note.read_text())
        self.assert_valid()

    def test_subagent_start_does_not_drain(self):
        with patch.object(brain, 'LOCK_WAIT', 0.2):
            holder = self.hold_lock()
            self.handle('Stop', last_assistant_message='QUEUED', prompt_id='p1')
            holder.kill()
            holder.wait()
        self.assertIn('PROJECT NOTE', self.context(self.handle('SubagentStart', agent_id=AGENT_ID, agent_type='x')))
        self.assertEqual(len(self.spooled()), 1)
        self.assertIn('SubagentStart', setup.automation(self.config, self.project)['events'])

    def test_pause_discards_this_projects_spooled_items(self):
        with patch.object(brain, 'LOCK_WAIT', 0.2):
            holder = self.hold_lock()
            self.handle('Stop', last_assistant_message='PRIVATE_ANSWER', prompt_id='p1')
            holder.kill()
            holder.wait()
        self.assertEqual(len(self.spooled()), 1)
        setup.automation(self.config, self.project, 'off')
        self.assertEqual(self.spooled(), [])
        setup.automation(self.config, self.project, 'on')
        self.handle('SessionStart', source='startup')
        self.assertNotIn('PRIVATE_ANSWER', ''.join(p.read_text() for p in self.root.rglob('*.md')))

    def test_board_commands_drain_first_and_packaged_hooks_include_subagents(self):
        with patch.object(brain, 'LOCK_WAIT', 0.2):
            holder = self.hold_lock()
            self.subagent_stop('DRAINED_BY_BOARD')
            holder.kill()
            holder.wait()
        self.assertEqual(len(self.spooled()), 1)
        self.open_run()
        self.assertEqual(self.spooled(), [])
        self.assertIn('DRAINED_BY_BOARD', self.records()[0].read_text())
        manifest = json.loads((SCRIPTS.parent / 'hooks/hooks.json').read_text())['hooks']
        self.assertEqual(manifest['SubagentStart'], manifest['Stop'])
        self.assertEqual(manifest['SubagentStop'], manifest['Stop'])
        for name in ('scripts/redact.py', 'scripts/records.py', 'scripts/runs.py'):
            self.assertIn(name, setup.RUNTIME_FILES)


if __name__ == '__main__':
    unittest.main()
