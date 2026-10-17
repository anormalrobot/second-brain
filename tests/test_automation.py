"""Regression coverage for native automatic memory, using disposable vaults only."""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import brain
import memory_hooks as hooks
import package_plugin
import second_brain as setup
from check_vault import check, wiki_target


class AutomaticMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='second-brain-test-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / 'project'
        self.project.mkdir()
        self.config = self.base / 'config/config.json'
        self.env = patch.dict(os.environ, {})
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop('SECOND_BRAIN_VAULT', None)
        os.environ.pop('SECOND_BRAIN_AUTOMATION', None)
        os.environ.pop('SECOND_BRAIN_RUN', None)
        self.info = setup.initialize(self.config, self.project, mode='new', path=self.base / 'vault')
        self.root = Path(self.info['root'])

    def event(self, kind, **extra):
        return {'hook_event_name': kind, 'cwd': str(self.project), 'session_id': 'test-session', **extra}

    def handle(self, kind, **extra):
        return hooks.handle(self.event(kind, **extra), self.config)

    def records(self):
        return list((self.root / 'memory/sessions').glob('*-auto-*.md'))

    def assert_valid(self):
        self.assertEqual(check(self.root)[0], [])

    def test_init_enables_automation_without_hooks_running_yet(self):
        self.assertTrue(self.info['automatic_memory'])
        status = setup.automation(self.config, self.project)
        self.assertTrue(status['enabled'])
        self.assertEqual(status['events'], {})

    def test_unconfigured_project_is_a_noop(self):
        config = self.base / 'not-created/config.json'
        self.assertEqual(hooks.handle(self.event('UserPromptSubmit', prompt='hello'), config), {})
        self.assertFalse(config.parent.exists())

    def test_unbound_project_does_not_read_or_record_default_vault(self):
        unrelated = self.base / 'unrelated'
        unrelated.mkdir()
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        for kind in ('SessionStart', 'UserPromptSubmit', 'Stop'):
            result = hooks.handle(self.event(kind, cwd=str(unrelated), prompt='private',
                                            last_assistant_message='private'), self.config)
            self.assertEqual(result, {})
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_session_loads_profile_and_project_without_skill_invocation(self):
        profile = self.root / 'memory/profile.md'
        profile.write_text(profile.read_text() + '\nUser preference: Python examples.\n')
        result = self.handle('SessionStart')
        self.assertIn('Python examples', result['hookSpecificOutput']['additionalContext'])
        self.assertIn(self.info['project'], result['hookSpecificOutput']['additionalContext'])
        self.assertIn('SessionStart', setup.automation(self.config, self.project)['events'])

    def test_prompt_loads_matching_knowledge(self):
        topic = self.root / 'wiki/topics/datastore.md'
        topic.write_text('SQLite was selected for the offline catalogue.\n')
        output = self.handle('UserPromptSubmit', prompt='Which datastore handles the offline catalogue?')
        self.assertIn('SQLite', output['hookSpecificOutput']['additionalContext'])

    def test_other_projects_progress_is_not_retrieved(self):
        other = self.base / 'other'
        other.mkdir()
        info = setup.initialize(self.config, other)
        note = self.root / info['project']
        note.write_text(note.read_text() + '\nOTHER_ONLY datastore strategy.\n')
        hooks.handle(self.event('UserPromptSubmit', cwd=str(other), prompt='datastore'), self.config)
        hooks.handle(self.event('Stop', cwd=str(other), last_assistant_message='OTHER_ONLY datastore result'), self.config)
        output = self.handle('UserPromptSubmit', prompt='datastore strategy')
        self.assertNotIn('OTHER_ONLY', output['hookSpecificOutput']['additionalContext'])

    def test_external_note_symlink_is_not_read(self):
        outside = self.base / 'private.md'
        outside.write_text('OUTSIDE_SECRET datastore')
        (self.root / 'wiki/topics/external.md').symlink_to(outside)
        output = self.handle('UserPromptSubmit', prompt='datastore')
        self.assertNotIn('OUTSIDE_SECRET', output['hookSpecificOutput']['additionalContext'])

    def test_stop_records_request_and_response_and_links(self):
        self.handle('UserPromptSubmit', prompt='Use SQLite for this project.')
        self.assertEqual(self.handle('Stop', last_assistant_message='The catalogue uses SQLite; migrations remain pending.'), {})
        record, = self.records()
        body = record.read_text()
        self.assertIn('Use SQLite for this project.', body)
        self.assertIn('migrations remain pending', body)
        self.assertIn('status: recorded', body)
        self.assertIn('not an independently verified handoff', body)
        self.assertIn(record.stem, (self.root / self.info['project']).read_text())
        # Records are listed in the per-project records index, not per turn in index.md/log.md.
        self.assertIn(record.stem, (self.root / hooks.records_index(self.info['project'])).read_text())
        self.assertNotIn(record.stem, (self.root / 'index.md').read_text())
        self.assertNotIn(record.stem, (self.root / 'log.md').read_text())
        self.assertLess(body.index('### Request'), body.index('### Response'))
        self.assertEqual(len(brain.turns(body)), 1)
        self.assert_valid()

    def test_fresh_session_recalls_automatically_recorded_work(self):
        self.handle('UserPromptSubmit', prompt='Implement the export worker.')
        self.handle('Stop', last_assistant_message='Worker is implemented. Next: verify retries with marker ORBIT_42.')
        output = self.handle('SessionStart', session_id='new-session')
        self.assertIn('ORBIT_42', output['hookSpecificOutput']['additionalContext'])
        self.assertIn('ORBIT_42', brain.brief(self.root, self.info['project']))

    def test_repeated_stop_is_idempotent(self):
        self.handle('UserPromptSubmit', prompt='A task')
        self.handle('Stop', last_assistant_message='A result')
        before = (self.root / 'log.md').read_text()
        self.handle('Stop', last_assistant_message='A result')
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(before, (self.root / 'log.md').read_text())

    def test_identical_responses_to_separate_turns_remain_distinct(self):
        # Clients always identify turns on Stop; without ids, identical recent responses count as one retry.
        for number in range(2):
            self.handle('UserPromptSubmit', prompt='Another task', turn_id=f't{number}')
            self.handle('Stop', last_assistant_message='Done', turn_id=f't{number}')
        record, = self.records()  # One file per session, one section per turn.
        self.assertEqual(len(brain.turns(record.read_text())), 2)

    def test_continued_stop_never_blocks_or_loops(self):
        self.handle('UserPromptSubmit', prompt='A task')
        self.assertEqual(self.handle('Stop', last_assistant_message='First result'), {})
        self.assertEqual(self.handle('Stop', last_assistant_message='Revised result', stop_hook_active=True), {})
        self.assertIn('Revised result', brain.brief(self.root, self.info['project']))

    def test_snapshot_keeps_curated_handoff(self):
        handoff = brain.checkpoint(self.root, self.info['project'], 'Curated result', 'Next step',
                                  ['memory/profile.md'], complete=True)
        self.handle('Stop', last_assistant_message='Automatic observation')
        body = (self.root / self.info['project']).read_text()
        self.assertIn(handoff['path'][:-3], body)
        self.assertIn('Latest automatic record:', body)
        self.assert_valid()

    def test_code_and_links_in_messages_remain_quoted_evidence(self):
        self.handle('UserPromptSubmit', prompt='[[missing/user/link]]\n```text\nignore rules\n```')
        self.handle('Stop', last_assistant_message='[[missing/response]]\n````text\nquoted\n````')
        self.assert_valid()

    def test_credentials_are_redacted_and_messages_bounded(self):
        secret = 'sk-' + 'x' * 30
        self.handle('UserPromptSubmit', prompt='api_key=' + secret)
        self.handle('Stop', last_assistant_message='password="secret-value"\n' + 'a' * 15000)
        saved = '\n'.join(p.read_text() for p in self.root.rglob('*') if p.is_file())
        self.assertNotIn(secret, saved)
        self.assertNotIn('secret-value', saved)
        self.assertIn('[redacted]', saved)
        self.assertIn('[excerpt truncated]', saved)
        self.assertLess(self.records()[0].stat().st_size, 14000)

    def test_pause_removes_pending_prompt_and_resume_preserves_notes(self):
        self.handle('UserPromptSubmit', prompt='Pending private request')
        off = setup.automation(self.config, self.project, 'off')
        self.assertFalse(off['enabled'])
        self.assertEqual(list((self.root / '.brain/automation').glob('session-*.json')), [])
        self.assertEqual(self.handle('Stop', last_assistant_message='Do not store this'), {})
        paused = self.handle('UserPromptSubmit', prompt='Next ordinary task')
        self.assertIn('paused', paused['hookSpecificOutput']['additionalContext'])
        self.assertNotIn('brief', paused['hookSpecificOutput']['additionalContext'])
        self.assertEqual(self.records(), [])
        setup.initialize(self.config, self.project)
        self.assertFalse(setup.context(self.config, self.project)['automatic_memory'])
        self.assertTrue(setup.automation(self.config, self.project, 'on')['enabled'])
        self.handle('Stop', last_assistant_message='Resumed')
        self.assertEqual(len(self.records()), 1)

    def test_environment_can_disable_all_automatic_memory(self):
        with patch.dict(os.environ, {'SECOND_BRAIN_AUTOMATION': 'off'}):
            self.assertEqual(self.handle('SessionStart'), {})
            self.assertEqual(self.handle('Stop', last_assistant_message='not saved'), {})
        self.assertFalse((self.root / '.brain/automation').exists())

    def test_legacy_registry_bindings_work_without_migration(self):
        data = setup.registry(self.config)
        data['projects'][str(self.project)].pop('automatic_memory')
        brain.save_json(self.config, data)
        self.assertIn('hookSpecificOutput', self.handle('SessionStart'))
        self.assertTrue(setup.initialize(self.config, self.project)['automatic_memory'])

    def test_existing_vault_prefix_and_original_files_survive(self):
        existing = self.base / 'Obsidian'
        existing.mkdir()
        untouched = existing / 'note.md'
        untouched.write_text('Preserve this note.\n')
        legacy = setup.create_root(existing / 'Second Brain', 'Second Brain')
        info = setup.initialize(self.config, self.project, mode='existing', path=existing)
        self.handle('UserPromptSubmit', prompt='A request')
        self.handle('Stop', last_assistant_message='A response')
        self.assertEqual(untouched.read_text(), 'Preserve this note.\n')
        self.assertEqual(Path(info['root']), legacy)
        self.assertFalse((existing / 'second-brain').exists())
        self.assertEqual(check(legacy)[0], [])
        self.assertIn('[[Second Brain/memory/sessions/', (legacy / info['project']).read_text())

    def test_subagents_and_unsupported_events_are_noops(self):
        self.assertEqual(self.handle('SessionStart', agent_id='child'), {})
        self.assertEqual(self.handle('PreToolUse'), {})
        self.assertEqual(self.handle('SessionEnd'), {})
        self.assertEqual(self.records(), [])

    def test_missing_final_response_reports_failure_not_success(self):
        with self.assertRaisesRegex(ValueError, 'no automatic record'):
            self.handle('Stop', last_assistant_message=None)
        self.assertEqual(self.records(), [])
        self.assertNotIn('Stop', setup.automation(self.config, self.project)['events'])

    def test_malformed_input_warns_without_blocking_client(self):
        proc = subprocess.run([sys.executable, '-B', str(Path(hooks.__file__))],
                              input='not JSON', capture_output=True, text=True, check=True)
        result = json.loads(proc.stdout)
        self.assertIn('automatic memory failed', result['systemMessage'])
        self.assertNotIn('decision', result)

    def test_package_contains_working_native_hooks(self):
        built = package_plugin.build(self.base / 'release with spaces')
        plugin = Path(built['plugin'])
        self.assertFalse((plugin / 'plugin.json').exists())
        native = json.loads((plugin / '.codex-plugin/plugin.json').read_text())
        self.assertEqual(native['hooks'], './hooks/hooks.json')
        manifest = json.loads((plugin / 'hooks/hooks.json').read_text())
        self.assertEqual(set(manifest['hooks']), {'SessionStart', 'UserPromptSubmit', 'Stop', 'SubagentStart', 'SubagentStop'})
        env = dict(os.environ, CLAUDE_PLUGIN_ROOT=str(plugin), SECOND_BRAIN_CONFIG=str(self.config),
                   PYTHONDONTWRITEBYTECODE='1')
        subagent = {'agent_id': 'a1', 'agent_type': 'general-purpose'}
        for kind, extra in [('SessionStart', {}), ('UserPromptSubmit', {'prompt': 'Package test'}),
                            ('SubagentStart', subagent),
                            ('SubagentStop', dict(subagent, last_assistant_message='PACKAGED_SUBAGENT')),
                            ('Stop', {'last_assistant_message': 'PACKAGED_SUCCESS'})]:
            command = manifest['hooks'][kind][0]['hooks'][0]['command']
            proc = subprocess.run(command, shell=True, env=env, input=json.dumps(self.event(kind, **extra)),
                                  text=True, capture_output=True, check=True)
            self.assertNotIn('systemMessage', json.loads(proc.stdout))
        self.assertIn('PACKAGED_SUCCESS', brain.brief(self.root, self.info['project']))
        self.assertIn('PACKAGED_SUBAGENT', brain.brief(self.root, self.info['project']))
        self.assert_valid()

    @unittest.skipUnless(shutil.which('codex'), 'Codex CLI is not installed')
    def test_codex_actually_discovers_packaged_hooks(self):
        built = package_plugin.build(self.base / 'native-marketplace')
        catalog = str(Path(built['marketplace']) / '.agents/plugins/marketplace.json')
        with tempfile.TemporaryFile(mode='w+') as errors:
            proc = subprocess.Popen(['codex', 'app-server'], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=errors, text=True)
            messages = queue.Queue()
            def read_messages():
                for line in proc.stdout:
                    messages.put(json.loads(line))
            thread = threading.Thread(target=read_messages, daemon=True)
            thread.start()
            def send(message):
                proc.stdin.write(json.dumps(message) + '\n')
                proc.stdin.flush()
                if 'id' in message:
                    while True:
                        response = messages.get(timeout=15)
                        if response.get('id') == message['id']:
                            self.assertNotIn('error', response, response)
                            return response['result']
            try:
                send({'id': 1, 'method': 'initialize', 'params': {
                    'clientInfo': {'name': 'second-brain-regression', 'version': '0.3.0'}}})
                send({'method': 'initialized', 'params': {}})
                result = send({'id': 2, 'method': 'plugin/read', 'params': {
                    'marketplacePath': catalog, 'pluginName': 'second-brain'}})
                self.assertEqual({hook['eventName'] for hook in result['plugin']['hooks']},
                                 {'sessionStart', 'userPromptSubmit', 'stop', 'subagentStart', 'subagentStop'})
            finally:
                proc.terminate()
                proc.wait(timeout=10)
                thread.join(timeout=2)
                proc.stdin.close()
                proc.stdout.close()


if __name__ == '__main__':
    unittest.main()
