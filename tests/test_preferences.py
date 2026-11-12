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
    def test_guidance_separates_general_and_project_preferences(self):
        texts = {'compile steps': ' '.join(brain.COMPILE_STEPS), 'AGENTS template': brain.AGENTS_TEMPLATE,
                 'compile skill': (PLUGIN / 'skills/compile/SKILL.md').read_text(),
                 'memory skill': (PLUGIN / 'skills/memory/SKILL.md').read_text(),
                 'README': (PLUGIN / 'README.md').read_text()}
        for name, text in texts.items():
            flat = ' '.join(text.split())
            self.assertFalse('only for preferences the user stated' in flat, name)
            self.assertTrue('every project' in flat and '## Preferences' in flat, name)

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


PREFS = '- Release: tag vX.Y.Z after the gate passes (user).\n' + ''.join(
    f'- PREF_RULE_{n:03d}: run the review pipeline step {n} before merging.\n' for n in range(60))


class BriefTests(PreferencesCase):
    def test_preferences_lead_the_brief_whole_and_are_not_repeated(self):
        self.assertGreater(len(PREFS), 3000)
        self.fill(goal='GOAL_TEXT', preferences=PREFS.strip(), status='STATUS_TEXT')
        text = brain.brief(self.root, self.info['project'])
        block = text.index('PROJECT PREFERENCES')
        self.assertLess(block, text.index('FILE: memory/profile.md'))
        self.assertIn(PREFS.strip(), text)
        self.assertEqual(text.count('PREF_RULE_059'), 1)
        self.assertIn('STATUS_TEXT', text)
        self.assertIn('## Preferences\n\n' + brain.PREFERENCES_SHOWN, text)
        self.assertLessEqual(len(text), 6000 + brain.PREFERENCES_LIMIT)
        self.fill(decisions='d' * 5000)  # A note cut by section keeps the pointer whole too.
        self.note.write_text(self.note.read_text().replace('STATUS_TEXT', 'STATUS_TEXT ' + 's' * 5000))
        self.assertIn('## Preferences\n\n' + brain.PREFERENCES_SHOWN, brain.brief(self.root, self.info['project']))

    def test_stub_preferences_add_nothing(self):
        self.assertNotIn('PROJECT PREFERENCES', brain.brief(self.root, self.info['project']))

    def test_oversized_preferences_are_cut_with_a_marker(self):
        self.fill(preferences=PREFS * 3)
        text = brain.brief(self.root, self.info['project'])
        block = text[text.index('PROJECT PREFERENCES'):text.index('FILE: ')]
        self.assertLessEqual(len(block), brain.PREFERENCES_LIMIT)
        self.assertRegex(block, r'\[truncated: ## Preferences shows \d+ of \d+ chars; read the project note\]')

    def test_project_note_keeps_status_and_next_steps_and_shortens_decisions(self):
        self.fill(goal='GOAL_TEXT', decisions='DECISION_HISTORY ' + 'd' * 6000, status='STATUS_TEXT current.',
                  open_questions='OPEN_TEXT', next_steps='NEXT_TEXT')
        text = brain.brief(self.root, self.info['project'], limit=6000)
        self.assertLessEqual(len(text), 6000)
        shown = text[text.index('FILE: ' + self.info['project']):]
        for marker in ('STATUS_TEXT current.', 'OPEN_TEXT', 'NEXT_TEXT', 'GOAL_TEXT', 'DECISION_HISTORY'):
            self.assertIn(marker, shown)
        self.assertIn('[section truncated; read the project note]', shown)
        self.assertIn('## Preferences\n\nNone recorded yet.', shown)  # A stub costs less than a marker.
        self.assertRegex(shown, r'\[truncated: ' + re.escape(self.info['project']) + r' shows \d+ of \d+ chars')

    def test_long_status_is_preferred_over_goal_and_decisions(self):
        self.fill(goal='GOAL_TEXT ' + 'g' * 900, decisions='DECISION_TEXT ' + 'd' * 900,
                  status='STATUS_HEAD ' + 's' * 400, next_steps='NEXT_TEXT ' + 'n' * 400)
        text = brain.brief(self.root, self.info['project'], limit=8000)
        shown = text[text.index('FILE: ' + self.info['project']):]
        self.assertIn('s' * 400, shown)
        self.assertIn('n' * 400, shown)
        self.assertNotIn('d' * 900, shown)  # The decision history is shortened first.

    def test_session_start_and_compact_carry_preferences_but_subagents_do_not(self):
        self.fill(goal='GOAL_TEXT', preferences=PREFS.strip(), status='STATUS_TEXT')
        for source in ('startup', 'compact'):
            text = self.context(self.handle('SessionStart', source=source))
            brief = json.loads(text[text.index('{"vault"'):])['brief']
            self.assertIn(PREFS.strip(), brief, source)
            self.assertIn('Preferences', text.split('{"vault"')[0])  # The instructions point at them.
        sub = self.context(self.handle('SubagentStart', agent_id='agent-1', agent_type='general-purpose'))
        self.assertIn('GOAL_TEXT', sub)
        self.assertNotIn('PREF_RULE', sub)
        self.assertNotIn('Preferences', sub)

    def test_limits_and_markers_hold_with_sections_and_preferences(self):
        sizes = ((40, 300, 3000, 200, 100), (900, 4000, 50, 2500, 2500), (0, 0, 9000, 10, 10), (7000, 10, 10, 10, 10))
        for prefs, goal, decisions, status, next_steps in sizes:
            self.note.write_text(META + '# Project\n\nIntro.\n' + brain.section_stubs(brain.PROJECT_SECTIONS))
            self.fill(**{k: v for k, v in dict(preferences='P' * prefs, goal='g' * goal, decisions='d' * decisions,
                                                  status='s' * status, next_steps='n' * next_steps).items() if v})
            total = len(self.note.read_text())
            for limit in range(500, 4001, 61):
                text = brain.brief(self.root, self.info['project'], limit=limit)
                files = text[text.index('FILE: ') - 2:] if 'FILE: ' in text else ''
                block = text[:len(text) - len(files)]
                self.assertLessEqual(len(block), len(brain.BRIEF_HEADER) + brain.PREFERENCES_LIMIT)
                self.assertLessEqual(len(text) - len(block) + len(brain.BRIEF_HEADER) * bool(files), limit)
                markers = re.findall(r'\[(?:truncated|omitted|brief truncated)[^\]]*\]', text)
                self.assertEqual(len(markers), text.count('[truncated') + text.count('[omitted') +
                                 text.count('[brief truncated'))
                if ('FILE: ' + self.info['project']) in text:
                    self.assertTrue(re.search(r'shows \d+ of ' + str(total) + r' chars', text) or
                                    self.note.read_text() in text, (limit, text[-300:]))


class DoctorTests(PreferencesCase):
    def findings(self, kind):
        code, report = self.cli('doctor')
        self.assertEqual(code, 0, report)
        return [f for f in report['findings'] if f['kind'] == kind]

    def test_profile_size(self):
        self.assertEqual(self.findings('profile_size'), [])
        profile = self.root / 'memory/profile.md'
        profile.write_text(profile.read_text() + 'x' * brain.PROFILE_BUDGET)
        found, = self.findings('profile_size')
        self.assertEqual(found['paths'], ['memory/profile.md'])
        self.assertTrue(found['detail'].startswith(brain.REVIEW))

    def test_profile_lines_naming_a_bound_project(self):
        profile = self.root / 'memory/profile.md'
        stem = Path(self.info['project']).stem
        lines = ['# Profile', '', 'Answer in English.', 'The project uses general tooling.',
                 'In proj alpha-2 CI runs on GitHub Actions.', f'Note: {stem} release steps.',
                 f'Run scripts from {self.project}/scripts.', 'Profile memory note; project notes.']
        profile.write_text(META + '\n'.join(lines) + '\n')
        found, = self.findings('profile_project_specific')
        offset = META.count('\n')
        self.assertEqual(found['lines'], [offset + 5, offset + 6, offset + 7])
        self.assertTrue(found['detail'].startswith(brain.REVIEW))
        # Without the registry (brain doctor --root), note slugs still match.
        names = brain.doctor(self.root)['findings']
        self.assertIn(offset + 6, next(f for f in names if f['kind'] == 'profile_project_specific')['lines'])

    def test_preferences_oversized(self):
        self.assertEqual(self.findings('preferences_oversized'), [])
        self.fill(preferences='p ' * brain.PREFERENCES_LIMIT)
        found, = self.findings('preferences_oversized')
        self.assertEqual(found['paths'], [self.info['project']])


if __name__ == '__main__':
    unittest.main()
