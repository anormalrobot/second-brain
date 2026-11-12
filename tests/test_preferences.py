"""0.6.6: the user's general preferences (profile) versus a project's own preferences (the project note's
## Preferences), which reach the main session in full at start and after compaction, but not subagents."""
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
from check_vault import check

META = '---\ncreated: 2026-01-01\nupdated: 2026-01-01\nstatus: active\n---\n'
PLUGIN = Path(__file__).resolve().parents[1]


class PreferencesCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='second-brain-prefs-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / 'proj_alpha-2'
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

    def handle(self, kind, session='prefs-session', **extra):
        return hooks.handle({'hook_event_name': kind, 'cwd': str(self.project), 'session_id': session, **extra},
                            self.config)

    def context(self, result):
        return result['hookSpecificOutput']['additionalContext']

    def fill(self, **bodies):
        text = self.note.read_text()
        for name, body in bodies.items():
            heading = name.replace('_', ' ').capitalize()
            text = text.replace(f'## {heading}\n\nNone recorded yet.', f'## {heading}\n\n{body}')
        self.note.write_text(text)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = setup.main(['--config', str(self.config), '--cwd', str(self.project), 'run', *args])
        return code, json.loads(out.getvalue() or err.getvalue())

    def assert_valid(self):
        self.assertEqual(check(self.root)[0], [])


class ScopeRuleTests(PreferencesCase):
    def test_session_start_nudge_uses_the_scope_rule(self):
        self.handle('UserPromptSubmit', session='other', prompt='zzqx')
        self.handle('Stop', session='other', last_assistant_message='Done.')
        text = self.context(self.handle('SessionStart', source='startup'))
        self.assertIn('Compile pending:', text)
        self.assertNotIn('only for preferences the user stated', text)
        self.assertIn("project note's ## Preferences", text)

    def test_previous_agents_template_is_refreshed(self):
        # The AGENTS.md that plugin 0.6.3-0.6.5 generated is a known template, so migrate refreshes it.
        self.assertIn('6a6447c6a0d851218aaec1dd431373ae96b6cb747ea5ddabc35f08d84b66cc87', brain.OLD_AGENTS)
        self.assertNotIn(brain.digest(brain.AGENTS_TEMPLATE.encode()), brain.OLD_AGENTS)


class SectionTests(PreferencesCase):
    def test_new_project_note_has_preferences_after_goal(self):
        self.assertEqual(brain.PROJECT_SECTIONS[:2], ('Goal', 'Preferences'))
        self.assertIn('## Goal\n\nNone recorded yet.\n\n## Preferences\n\nNone recorded yet.\n\n## Decisions', self.note.read_text())
        self.assert_valid()

    def test_compile_listing_inserts_preferences_after_goal_only(self):
        old = (META + '# Project — old\n\n## Goal\n\nShip it.\n\n```markdown\n## Preferences\n```\n\n'
               '## Status\n\nHalfway.\n')
        self.note.write_text(old)
        brain.compile_list(self.root, self.info['project'])
        text = self.note.read_text()
        self.assertTrue(text.startswith(old.split('## Status')[0].rstrip('\n')))
        self.assertIn('```\n\n## Preferences\n\nNone recorded yet.\n\n## Status\n\nHalfway.\n', text)
        self.assertEqual(text.count('## Preferences\n\nNone recorded yet.'), 1)
        for heading in ('Decisions', 'Open questions', 'Next steps'):
            self.assertIn(f'\n## {heading}\n\nNone recorded yet.\n', text)
        brain.compile_list(self.root, self.info['project'])
        self.assertEqual(self.note.read_text(), text)

    def test_note_without_goal_gets_preferences_appended(self):
        old = META + '# Project — old\n\n## Status\n\nHalfway.\n'
        self.note.write_text(old)
        brain.compile_list(self.root, self.info['project'])
        self.assertTrue(self.note.read_text().startswith(old))

    def test_migrate_adds_the_heading_once_and_moves_nothing(self):
        other = self.root / 'memory/projects/other.md'
        old = (META + '# Project — other\n\n## Goal\n\nGOAL_TEXT\n\n## Decisions\n\nUse PostgreSQL (user).\n\n'
               'Latest checkpoint: [[log|x]].\n')
        other.write_text(old)
        with brain.writer(self.root):
            brain.rebuild_index(self.root)  # Only the heading is left to migrate.
        current = self.note.read_text()
        code, plan = self.cli('migrate')
        self.assertEqual(code, 0, plan)
        self.assertEqual(plan['preferences'], ['memory/projects/other.md'])
        self.assertEqual(plan['changes'], 1)
        self.assertEqual(other.read_text(), old)  # A dry run writes nothing.
        code, done = self.cli('migrate', '--apply')
        self.assertTrue(done['applied'], done)
        text = other.read_text()
        self.assertEqual(text, old.replace('GOAL_TEXT\n\n', 'GOAL_TEXT\n\n## Preferences\n\nNone recorded yet.\n\n'))
        self.assertEqual(self.note.read_text(), current)
        self.assertIn('Preferences heading added to 1 project note', (self.root / 'log.md').read_text())
        code, again = self.cli('migrate')
        self.assertEqual((again['changes'], again['preferences']), (0, []))


if __name__ == '__main__':
    unittest.main()
