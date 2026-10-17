"""End to end through the PACKAGED plugin: every hook and command runs as a subprocess of the built package's
scripts, against a throwaway vault, config, and HOME. Also a vault left by plugin 0.3.0 keeps working."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
AGENT_ID = 'aa264b85e4d489e92'  # Payload shapes follow the probed Claude Code hooks (agent ids, prompt_id).


class PackagedCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='second-brain-e2e-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / 'home').mkdir()
        self.project = self.base / 'project'
        self.project.mkdir()
        self.config = self.base / 'config/config.json'
        built = subprocess.run([sys.executable, '-B', str(REPO / 'scripts/package_plugin.py'), '--output',
                                str(self.base / 'dist')], capture_output=True, text=True, cwd=self.base)
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        self.plugin = Path(json.loads(built.stdout)['plugin'])
        self.hooks = json.loads((self.plugin / 'hooks/hooks.json').read_text())['hooks']
        # Nothing from the caller's setup leaks in: no registry, vault, run, client, or module path settings.
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('SECOND_BRAIN_', 'XDG_', 'CODEX_', 'CLAUDE', 'PYTHONPATH'))}
        self.env = dict(env, HOME=str(self.base / 'home'), SECOND_BRAIN_CONFIG=str(self.config),
                        CLAUDE_PLUGIN_ROOT=str(self.plugin), PYTHONDONTWRITEBYTECODE='1')

    def run_script(self, script, *args, env=None, cwd=None, check=True):
        proc = subprocess.run([sys.executable, '-B', str(self.plugin / script), *map(str, args)], capture_output=True,
                              text=True, env=env or self.env, cwd=cwd or self.project)
        if check:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def cli(self, *args, skill='memory', env=None):
        """The bundled command as an agent runs it: the packaged skill's entry point."""
        proc = self.run_script(f'skills/{skill}/scripts/second_brain.py', '--cwd', self.project, 'run', *args, env=env)
        return json.loads(proc.stdout)

    def hook(self, kind, session, env=None, **extra):
        """One hook exactly as the client runs it: the packaged hooks.json command through a shell."""
        command = self.hooks[kind][0]['hooks'][0]['command']
        event = {'hook_event_name': kind, 'cwd': str(self.project), 'session_id': session, **extra}
        proc = subprocess.run(command, shell=True, input=json.dumps(event), capture_output=True, text=True,
                              env=env or self.env, cwd=self.project, timeout=10)
        self.assertEqual((proc.returncode, proc.stderr), (0, ''), proc.stdout)
        result = json.loads(proc.stdout)
        self.assertNotIn('systemMessage', result)
        return result

    def context(self, result):
        return result.get('hookSpecificOutput', {}).get('additionalContext', '')

    def turn(self, session, prompt, response, env=None, **ids):
        prompted = self.hook('UserPromptSubmit', session, env, prompt=prompt, **ids)
        self.hook('Stop', session, env, last_assistant_message=response, **ids)
        return prompted

    def init(self):
        proc = self.run_script('skills/init/scripts/second_brain.py', '--cwd', self.project, 'init', '--new',
                               self.base / 'vault')
        info = json.loads(proc.stdout)
        self.root, self.project_note = Path(info['root']), info['project']
        self.note = self.root / self.project_note
        return info

    def check_vault(self, root=None):
        proc = self.run_script('scripts/check_vault.py', root or self.root, check=False)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertTrue(proc.stdout.startswith('PASS'), proc.stdout)

    def record_of(self, marker):
        found = [p for p in (self.root / 'memory/sessions').glob('*-auto-*.md') if marker in p.read_text()]
        self.assertEqual(len(found), 1, marker)
        return found[0].relative_to(self.root).as_posix()

    def link(self, relative):
        return '[[' + (relative[:-3] if relative.endswith('.md') else relative) + ']]'

    def fill(self, section, text):
        body = self.note.read_text()
        self.assertIn(f'## {section}\n\nNone recorded yet.', body)
        self.note.write_text(body.replace(f'## {section}\n\nNone recorded yet.', f'## {section}\n\n{text}', 1))


class PackagedFlowTests(PackagedCase):
    def test_the_whole_flow_runs_from_the_built_package(self):
        info = self.init()
        self.assertEqual(info['state'], 'ready')
        # A session: brief at start, prompts, client notices, and a subagent.
        start = self.context(self.hook('SessionStart', 'lead', source='startup'))
        self.assertIn('second-brain automatic memory is active', start)
        self.assertIn('Bundled command: python3 -B ' + shlex.quote(str(self.plugin / 'scripts/second_brain.py')), start)
        self.assertIn('"brief": ', start)
        self.turn('lead', 'We will store the offline catalogue in SQLite.', 'Agreed: SQLite stores the catalogue.',
                  prompt_id='l1')
        for number, notice in enumerate(['<task-notification>catalogue build finished</task-notification>',
                                         '<agent-message from="helper">catalogue lint passed</agent-message>'], 2):
            self.assertEqual(self.turn('lead', notice, f'Noted notice {number}.', prompt_id=f'l{number}'), {})
        brief = self.context(self.hook('SubagentStart', 'lead', agent_id=AGENT_ID, agent_type='general-purpose'))
        self.assertIn('PROJECT NOTE ' + self.project_note, brief)
        self.hook('SubagentStop', 'lead', agent_id=AGENT_ID, agent_type='general-purpose',
                  agent_transcript_path=str(self.base / 'agent.jsonl'), last_assistant_message='SUBAGENT_SCHEMA_FOUND')
        lead = self.record_of('Agreed: SQLite')
        text = (self.root / lead).read_text()
        self.assertEqual((text.count('### Request'), text.count('### System notification')), (1, 2))
        self.assertEqual(len(re.findall(r'^## Turn \d', text, re.M)), 3)
        self.assertIn('## Subagent general-purpose (' + AGENT_ID[:12] + ')', text)
        prompt = self.context(self.hook('UserPromptSubmit', 'lead', prompt='catalogue sqlite', prompt_id='l4'))
        self.assertNotIn(lead, prompt)  # A session never gets its own record back.
        self.check_vault()

        # A run board shared by two CLI workers (SECOND_BRAIN_RUN), driven by a supervisor.
        run = self.cli('board', 'open', '--title', 'Ship CSV export', '--goal', 'Export reports as CSV')['run']
        self.assertEqual(self.cli('board', 'add', '--title', 'Write the exporter')['task']['id'], 't1')
        self.assertEqual(self.cli('board', 'add', '--title', 'Document it', '--depends', 't1')['task']['id'], 't2')
        worker = dict(self.env, SECOND_BRAIN_RUN=run)
        for name, task, answer in (('w1', 't1', 'EXPORTER_WRITTEN'), ('w2', 't2', 'EXPORT_DOCUMENTED')):
            context = self.context(self.hook('SessionStart', name, worker, source='startup'))
            self.assertIn(f'Run board {run}', context)
            self.assertIn('"run_board": ', context)
            self.cli('board', 'claim', task, '--agent', name, env=worker)
            self.turn(name, f'Claim {task} and do it.', answer, worker, prompt_id=name + '-1')
            record = self.record_of(answer)
            self.assertIn('run: ' + run, (self.root / record).read_text())
            posted = self.cli('board', 'post', task, '--agent', name, '--status', 'done', '--note', answer,
                              '--evidence', record, env=worker)
            self.assertEqual(posted['task']['status'], 'done')
        self.assertEqual(self.cli('board', 'show', env=worker)['run'], run)  # Workers resolve the run by env.
        board = self.cli('board', 'show')
        self.assertEqual([t['status'] for t in board['tasks']], ['done', 'done'])
        self.assertEqual(sum('joined' in e['text'] for e in board['activity']), 2)
        self.assertEqual(self.cli('board', 'close', '--summary', 'CSV export shipped.')['status'], 'closed')
        self.assertIn('Latest run: [[memory/runs/' + run + '|', self.note.read_text())
        self.check_vault()

        # A new session is nudged to compile; the agent compiles with the bundled compile skill.
        nudge = self.context(self.hook('SessionStart', 'next', source='startup'))
        self.assertIn('Compile pending: 3 automatic record(s) from other sessions and 1 closed run board(s)', nudge)
        listing = self.cli('compile', skill='compile')
        self.assertEqual(sorted(r['path'] for r in listing['records']),
                         sorted([lead, self.record_of('EXPORTER_WRITTEN'), self.record_of('EXPORT_DOCUMENTED')]))
        self.assertEqual([r['run'] for r in listing['runs']], [run])
        self.assertEqual(listing['pending_total'], 4)
        links = {r['path']: r['link'] for r in listing['records'] + listing['runs']}
        self.fill('Decisions', f'- The user chose SQLite for the offline catalogue. Evidence: {links[lead]}.')
        self.fill('Status', 'CSV export shipped by two workers (agent claims). Evidence: ' +
                  ', '.join(link for path, link in links.items() if path != lead) + '.')
        done = self.cli('compile', '--done', listing['token'], '--updated', self.project_note, skill='compile')
        self.assertEqual((len(done['compiled']), done['skipped'], done['pending_total']), (4, [], 0))
        self.assertEqual(self.cli('compile', '--status', skill='compile')['pending_total'], 0)
        self.assertNotIn('Compile pending', self.context(self.hook('SessionStart', 'after', source='startup')))

        # File a reusable answer citing the record, link it, and check the vault.
        answer = self.base / 'answer.md'
        answer.write_text('SQLite keeps the catalogue available offline and needs no server.\n')
        filed = self.cli('file-answer', '--title', 'Why SQLite for the offline catalogue', '--text-file', answer,
                         '--source', lead)
        self.assertEqual(filed['path'], 'wiki/topics/why-sqlite-for-the-offline-catalogue.md')
        self.fill('Next steps', f'- Reuse {self.link(filed["path"])} for storage questions.')
        doctor = self.cli('doctor')
        kinds = {f['kind'] for f in doctor['findings']}
        self.assertEqual(kinds & {'structural', 'pending_compile', 'spool_backlog', 'spool_failed', 'orphan_note',
                                  'unsupported_note', 'empty_project_note'}, set(), doctor['findings'])
        self.check_vault()
        found = self.cli('search', 'offline catalogue sqlite')
        self.assertEqual(found[0]['path'], filed['path'])

        # Snapshot and restore round trip.
        snapshot = self.cli('snapshot', '--output', self.base / 'backup.zip')
        restored = Path(self.cli('restore', self.base / 'backup.zip', '--destination', self.base / 'restored')['restored'])
        self.assertEqual(snapshot['files'], len([p for p in restored.rglob('*') if p.is_file()]))
        notes = lambda root: {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*.md')}
        self.assertEqual(notes(restored), notes(self.root))
        self.assertTrue((restored / '.brain/runs' / (run + '.json')).is_file())
        self.check_vault(restored)


LEGACY_KEYS = ('c' * 24, 'd' * 24)


class LegacyVaultTests(PackagedCase):
    def legacy_vault(self):
        """Rewrite a fresh vault into the layout plugin 0.3.0 left (generated with that release's code): one
        record per turn, a project note without sections, and an index.md entry and log.md line per record."""
        self.init()
        stem = Path(self.project_note).stem
        names = []
        for number, (key, day) in enumerate(zip(LEGACY_KEYS, ('2026-01-02', '2026-01-03')), 1):
            name = f'memory/sessions/{day}-auto-{key}.md'
            names.append(name)
            (self.root / name).write_text(
                f'---\ncreated: {day}\nupdated: {day}\nstatus: recorded\n---\n# Automatic conversation record\n\n'
                f'Project: [[memory/projects/{stem}|Project state]].\n\n'
                'Source: native UserPromptSubmit and Stop events from the local agent session.\n'
                'This is an attributed excerpt, not an independently verified handoff. Assistant statements are claims; '
                'proposals are not accepted decisions. Excerpts are bounded and recognizable credentials are redacted.\n\n'
                f'## Recorded assistant response\n\n```text\nLEGACY_ANSWER_{number}\n```\n\n'
                f'## Recorded user request\n\n```text\nlegacy prompt {number}\n```\n')
        self.note.write_text(
            f'---\ncreated: 2026-01-01\nupdated: 2026-01-03\nstatus: active\n---\n# Project — {stem}\n\n'
            'No project decisions or progress recorded yet.\n\n'
            'Source: explicit local project binding during setup. This is a navigation record.\n\n'
            f'Latest automatic record: [[{names[1][:-3]}|2026-01-03 conversation]].\n')
        index = (self.root / 'index.md').read_text().rstrip()
        log = (self.root / 'log.md').read_text().rstrip()
        for name in names:
            index += f'\n\n- [[{name[:-3]}|Automatic conversation record]]'
            log += f'\n\n## [{name[16:26]}] runtime | Saved automatic record [[{name[:-3]}|conversation]]'
        (self.root / 'index.md').write_text(index + '\n')
        (self.root / 'log.md').write_text(log + '\n')
        # 0.3.0 session state (a pending prompt, no turn_source) for the session that resumes after the upgrade.
        key = hashlib.sha256((self.project_note + '\0' + 'upgraded').encode()).hexdigest()[:32]
        state = self.root / '.brain/automation' / f'session-{key}.json'
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({'project': self.project_note, 'prompt': 'legacy prompt', 'turn': 'f' * 32}))
        self.check_vault()
        return names

    def test_a_vault_from_plugin_0_3_0_keeps_working(self):
        names = self.legacy_vault()
        original = self.note.read_text()
        brief = self.run_script('skills/memory/scripts/second_brain.py', '--cwd', self.project, 'run', 'brief').stdout
        self.assertIn('No project decisions or progress recorded yet.', brief)
        self.assertIn('LEGACY_ANSWER_2', brief)  # The latest record pointer still leads the brief.
        start = self.context(self.hook('SessionStart', 'upgraded', source='startup'))
        self.assertIn('Compile pending: 2 automatic record(s) from other sessions are not yet compiled '
                      '(oldest 2026-01-02)', start)
        self.assertIn('LEGACY_ANSWER_2', start)
        listing = self.cli('compile', skill='compile')
        self.assertEqual([(r['path'], r['from_turn'], r['through_turn']) for r in listing['records']],
                         [(names[0], 1, 1), (names[1], 1, 1)])
        text = self.note.read_text()
        self.assertTrue(text.startswith(original))  # The legacy note keeps its text; missing sections are appended.
        self.assertEqual(re.findall(r'^## (.+)$', text, re.M), ['Goal', 'Decisions', 'Status', 'Open questions', 'Next steps'])
        self.fill('Decisions', 'Legacy work was recorded. Evidence: ' + ', '.join(r['link'] for r in listing['records']) + '.')
        done = self.cli('compile', '--done', listing['token'], '--updated', self.project_note, skill='compile')
        self.assertEqual((sorted(done['compiled']), done['pending_total']), (names, 0))
        for name in names:
            self.assertEqual(re.search(r'^status: (\w+)', (self.root / name).read_text(), re.M)[1], 'compiled')
        self.check_vault()
        # New turns write a per-session record beside the legacy ones; the legacy index entries stay.
        self.turn('upgraded', 'First request after the upgrade.', 'UPGRADED_ANSWER', prompt_id='u1')
        record = self.record_of('UPGRADED_ANSWER')
        self.assertIn('Latest automatic record: [[' + record[:-3] + '|', self.note.read_text())
        index = (self.root / 'index.md').read_text()
        self.assertTrue(all(f'[[{name[:-3]}|Automatic conversation record]]' in index for name in names))
        self.assertNotIn(record[:-3], index)  # New records are listed in the project's records list instead.
        self.assertIn(names[0], [row['path'] for row in self.cli('search', 'legacy answer', '--limit', '20')])
        self.assertEqual([f for f in self.cli('doctor')['findings'] if f['kind'] == 'structural'], [])
        self.check_vault()


if __name__ == '__main__':
    unittest.main()
