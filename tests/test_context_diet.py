"""Context diet: the full brief loads at SessionStart; later prompts get only new matching notes."""
import json
import os
import re
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import brain
import memory_hooks as hooks
import second_brain as setup

BRIEF_MARK = 'Shared memory: read index.md'
INSTRUCTIONS_MARK = 'Bundled command:'


class ContextDietTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='second-brain-diet-')
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

    def handle(self, kind, session='diet-session', **extra):
        event = {'hook_event_name': kind, 'cwd': str(self.project), 'session_id': session, **extra}
        return hooks.handle(event, self.config)

    def context(self, result):
        return result.get('hookSpecificOutput', {}).get('additionalContext')

    def notes(self, result):
        text = self.context(result)
        self.assertTrue(text.startswith(hooks.PROMPT_HEADER))
        return json.loads(text[len(hooks.PROMPT_HEADER):])['matching_notes']

    def topic(self, name, body):
        (self.root / 'wiki/topics' / name).write_text(body)

    def test_prompt_after_session_start_omits_brief_and_instructions(self):
        start = self.context(self.handle('SessionStart', source='startup'))
        self.assertIn(BRIEF_MARK, start)
        self.assertIn(INSTRUCTIONS_MARK, start)
        self.topic('datastore.md', 'SQLite was selected for the offline catalogue.\n')
        text = self.context(self.handle('UserPromptSubmit', prompt='Which datastore for the offline catalogue?'))
        self.assertIn('SQLite', text)
        self.assertNotIn(BRIEF_MARK, text)
        self.assertNotIn(INSTRUCTIONS_MARK, text)
        self.assertIn('not instructions', text)

    def test_no_match_returns_nothing_but_saves_prompt(self):
        self.handle('SessionStart')
        self.assertEqual(self.handle('UserPromptSubmit', prompt='zzqx'), {})
        self.assertIn('UserPromptSubmit', setup.automation(self.config, self.project)['events'])
        self.handle('Stop', last_assistant_message='Answered.')
        record, = (self.root / 'memory/sessions').glob('*-auto-*.md')
        self.assertIn('zzqx', record.read_text())

    def test_matches_exclude_brief_project_index_and_own_records(self):
        profile = self.root / 'memory/profile.md'
        profile.write_text(profile.read_text() + '\nzebracorn preference.\n')
        project = self.root / self.info['project']
        project.write_text(project.read_text() + '\nzebracorn project state.\n')
        checkpoint = brain.checkpoint(self.root, self.info['project'], 'zebracorn checkpoint', 'next',
                                      ['memory/profile.md'])['path']
        self.handle('SessionStart')
        state = brain.load_json(hooks.session_path(self.info, {'session_id': 'diet-session'}), {})
        self.assertTrue(state['primed'])
        self.assertIn(checkpoint, state['brief_paths'])
        # Another session records after this one was primed, so its record is not in this brief.
        other = 'zebracorn-other-session'
        self.handle('UserPromptSubmit', session=other, prompt='zebracorn plan')
        self.handle('Stop', session=other, last_assistant_message='zebracorn from another session')
        other_record = brain.load_json(hooks.session_path(self.info, {'session_id': other}), {})['records'][0]
        self.handle('UserPromptSubmit', prompt='zebracorn task')
        self.handle('Stop', last_assistant_message='zebracorn own answer')
        own = brain.load_json(hooks.session_path(self.info, {'session_id': 'diet-session'}), {})['records']
        self.assertEqual(len(own), 1)
        names = [n['path'] for n in self.notes(self.handle('UserPromptSubmit', prompt='zebracorn again'))]
        for path in ['memory/profile.md', self.info['project'], checkpoint,
                     hooks.records_index(self.info['project']), *own]:
            self.assertNotIn(path, names)
        self.assertIn(other_record, names)
        # The prompt overwrite preserved this session's records list.
        state = brain.load_json(hooks.session_path(self.info, {'session_id': 'diet-session'}), {})
        self.assertEqual(state['records'], own)
        self.assertEqual(state['prompt'], 'zebracorn again')

    def test_unprimed_session_falls_back_once(self):
        first = self.context(self.handle('UserPromptSubmit', prompt='zzqx first'))
        self.assertIn(BRIEF_MARK, first)
        self.assertIn(INSTRUCTIONS_MARK, first)
        self.assertEqual(self.handle('UserPromptSubmit', prompt='zzqx second'), {})

    def test_compact_session_start_resends_brief(self):
        self.handle('SessionStart', source='startup')
        self.handle('UserPromptSubmit', prompt='zzqx')
        text = self.context(self.handle('SessionStart', source='compact'))
        self.assertIn(BRIEF_MARK, text)
        self.assertIn(INSTRUCTIONS_MARK, text)
        # A mid-turn compaction keeps the pending prompt for Stop.
        self.handle('Stop', last_assistant_message='Done after compaction.')
        record, = (self.root / 'memory/sessions').glob('*-auto-*.md')
        self.assertIn('zzqx', record.read_text())

    def test_payload_is_bounded_with_many_large_notes(self):
        self.handle('SessionStart')
        for number in range(12):
            self.topic(f'big{number}.md', ('quasar "quoted"\\ line\n' * 2000))
        text = self.context(self.handle('UserPromptSubmit', prompt='quasar quoted line'))
        self.assertLessEqual(len(text), hooks.NOTES_BUDGET + len(hooks.PROMPT_HEADER))
        self.assertTrue(self.notes(self.handle('UserPromptSubmit', prompt='quasar')))

    def test_fit_notes_respects_budget_with_escapes(self):
        notes = [{'path': f'wiki/topics/n{i}.md', 'excerpt': '"\\\n\t' * 400} for i in range(5)]
        fitted = hooks.fit_notes(notes)
        self.assertTrue(fitted)
        self.assertLessEqual(len(json.dumps({'matching_notes': fitted}, ensure_ascii=False)), 3000)
        self.assertEqual(len(notes[0]['excerpt']), 1600)  # Input is not mutated.

    def test_wrapped_payload_exact_boundary(self):
        # The bare list serializes to 2990 chars, so only budgeting the wrapped object trims it.
        note = {'path': 'wiki/topics/edge.md', 'excerpt': ''}
        note['excerpt'] = 'e' * (2990 - len(json.dumps([note])))
        self.assertEqual(len(json.dumps([note])), 2990)
        self.assertGreater(len(json.dumps({'matching_notes': [note]})), 3000)
        self.handle('SessionStart')
        with patch.object(hooks, 'matching_notes', return_value=[note]):
            text = self.context(self.handle('UserPromptSubmit', prompt='edge'))
        self.assertTrue(text.startswith(hooks.PROMPT_HEADER))
        self.assertLessEqual(len(text) - len(hooks.PROMPT_HEADER), 3000)
        self.assertGreaterEqual(len(text) - len(hooks.PROMPT_HEADER), 2900)

    def test_stop_during_prompt_scan_does_not_return_own_record(self):
        self.handle('SessionStart')
        self.handle('UserPromptSubmit', prompt='first')
        real = hooks.matching_notes

        def stop_then_scan(*args, **kwargs):
            # The state was read before this scan; a Stop now writes a record the exclusions do not know.
            self.handle('Stop', last_assistant_message='nebulite racing answer')
            return real(*args, **kwargs)
        with patch.object(hooks, 'matching_notes', stop_then_scan):
            result = self.handle('UserPromptSubmit', prompt='nebulite racing')
        own = brain.load_json(hooks.session_path(self.info, {'session_id': 'diet-session'}), {})['records']
        self.assertEqual(len(own), 1)
        self.assertEqual(result, {})  # The only match was this session's own new record.

    def test_brief_paths_match_rendered_files_when_pointer_moves(self):
        first, second = (brain.checkpoint(self.root, self.info['project'], f'step {n}', 'next',
                                          ['memory/profile.md'])['path'] for n in (1, 2))
        project = self.root / self.info['project']
        project.write_text(project.read_text().replace(second[:-3], first[:-3]))
        real, moved = brain.wiki_relative, []

        def move_pointer(*args):
            if not moved:  # Another session moves the pointer while the brief is being built.
                moved.append(True)
                project.write_text(project.read_text().replace(first[:-3], second[:-3]))
            return real(*args)
        with patch.object(brain, 'wiki_relative', move_pointer):
            text = self.context(self.handle('SessionStart'))
        self.assertTrue(moved)
        shown = re.findall(r'\nFILE: (\S+)\n', json.loads(text.rsplit('\n', 1)[1])['brief'])
        state = brain.load_json(hooks.session_path(self.info, {'session_id': 'diet-session'}), {})
        self.assertEqual(state['brief_paths'], shown)
        self.assertIn(first, shown)


if __name__ == '__main__':
    unittest.main()
