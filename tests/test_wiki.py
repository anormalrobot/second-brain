"""Wiki usefulness: filed answers, doctor content checks, and the ranked scorer (disposable vaults only)."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import tracemalloc
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import brain
import memory_hooks as hooks
import runs
import second_brain as setup
from check_vault import check, wiki_target

META = '---\ncreated: 2026-01-01\nupdated: 2026-01-01\nstatus: active\n---\n'
CONTENT = {'pending_compile', 'empty_project_note', 'unsupported_note', 'orphan_note'}


class WikiCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='second-brain-wiki-')
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
        (self.root / 'raw/paper.pdf').write_bytes(b'%PDF evidence')
        (self.root / 'raw/notes.md').write_text('raw markdown source\n')
        self.write('wiki/topics/sqlite.md', META + '# SQLite\n\nEvidence: [[raw/paper.pdf|paper]].\n', index=True)

    def write(self, relative, text, index=False):
        (self.root / relative).parent.mkdir(parents=True, exist_ok=True)
        (self.root / relative).write_text(text)
        if index:
            brain.add_index(self.root, relative, Path(relative).stem)

    def text_file(self, text):
        path = self.base / ('answer-' + str(len(list(self.base.glob('answer-*')))) + '.md')
        path.write_text(text)
        return path

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = setup.main(['--config', str(self.config), '--cwd', str(self.project), 'run', *args])
        return code, json.loads(out.getvalue() or err.getvalue())

    def answer(self, *sources, title='Why SQLite for offline sync', text='SQLite keeps the catalogue offline.', **extra):
        args = ['file-answer', '--title', title, '--text-file', str(self.text_file(text))]
        for name in sources:
            args += ['--source', name]
        for key, value in extra.items():
            args += ['--' + key.replace('_', '-')] + ([] if value is True else [value])
        return self.cli(*args)

    def files(self):
        """Every vault file's bytes; the lock file's owner line is rewritten on each acquire, so it is left out."""
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in sorted(self.root.rglob('*'))
                if p.is_file() and p.name != 'write.flock'}

    def refused(self, pattern, *sources, **extra):
        before = self.files()
        code, result = self.answer(*sources, **extra)
        self.assertEqual(code, 1, result)
        self.assertRegex(result['error'], pattern)
        self.assertEqual(self.files(), before)  # A refusal leaves the vault byte-identical.
        return result

    def assert_valid(self):
        self.assertEqual(check(self.root)[0], [])

    def handle(self, kind, session='wiki-session', **extra):
        event = {'hook_event_name': kind, 'cwd': str(self.project), 'session_id': session, **extra}
        return hooks.handle(event, self.config)

    def turn(self, prompt, response, session='wiki-session'):
        self.handle('UserPromptSubmit', session, prompt=prompt)
        self.handle('Stop', session, last_assistant_message=response)

    def kinds(self, report=None):
        return [f for f in (report or brain.doctor(self.root))['findings'] if f['kind'] in CONTENT]


class FileAnswerTests(WikiCase):
    def test_file_answer_creates_a_valid_cited_page_with_index_and_log(self):
        code, result = self.answer('raw/paper.pdf', 'raw/notes.md', 'wiki/topics/sqlite.md', 'memory/profile.md',
                                   'raw/paper.pdf', date='2026-03-04')
        self.assertEqual(code, 0, result)
        self.assertEqual(set(result), {'path', 'sha256'})
        self.assertEqual(result['path'], 'wiki/topics/why-sqlite-for-offline-sync.md')
        page = self.root / result['path']
        self.assertEqual(brain.digest(page.read_bytes()), result['sha256'])
        text = page.read_text()
        self.assertEqual(brain.frontmatter(text), {'created': '2026-03-04', 'updated': '2026-03-04', 'status': 'active',
                                                   'kind': 'answer'})
        self.assertIn('\n# Why SQLite for offline sync\n\nSQLite keeps the catalogue offline.\n\n## Sources\n\n', text)
        sources = text.split('## Sources\n\n', 1)[1]
        self.assertEqual(sources, '- [[raw/paper.pdf|paper.pdf]]\n- [[raw/notes|notes.md]]\n'
                                  '- [[wiki/topics/sqlite|sqlite.md]]\n- [[memory/profile|profile.md]]\n')
        self.assertIn('[[wiki/topics/why-sqlite-for-offline-sync|Why SQLite for offline sync]]',
                      (self.root / 'index.md').read_text())
        self.assertIn('## [2026-03-04] runtime | Filed answer [[wiki/topics/why-sqlite-for-offline-sync|Why SQLite for '
                      'offline sync]] citing 4 source(s)', (self.root / 'log.md').read_text())
        self.assert_valid()

    def test_links_honor_the_link_prefix_and_dotted_names(self):
        vault = self.base / 'obsidian'
        vault.mkdir()
        other = self.base / 'other'
        other.mkdir()
        info = setup.initialize(self.config, other, mode='existing', path=vault)
        root = Path(info['root'])
        self.assertEqual(info['link_prefix'], 'second-brain')
        (root / 'raw/api.v2.md').write_text('dotted\n')
        (root / 'raw/data.json').write_text('{}\n')
        result = brain.file_answer(root, 'API versions', 'v2 is current.', ['raw/api.v2.md', 'raw/data.json'])
        text = (root / result['path']).read_text()
        self.assertIn('- [[second-brain/raw/api.v2.md|api.v2.md]]\n- [[second-brain/raw/data.json|data.json]]\n', text)
        self.assertIn('[[second-brain/wiki/topics/api-versions|API versions]]', (root / 'index.md').read_text())
        self.assertEqual(check(root)[0], [])

    def test_an_existing_page_is_refused(self):
        code, result = self.answer('raw/paper.pdf')
        self.assertEqual(code, 0, result)
        self.refused('already exists.*--update --expected-sha', 'raw/notes.md', text='Different text.')
        (self.root / 'wiki/topics/dangling.md').symlink_to(self.root / 'wiki/topics/missing.md')
        self.refused('symlink', 'raw/paper.pdf', title='Dangling')
        (self.root / 'wiki/topics/escaping.md').symlink_to(self.base / 'missing.md')
        self.refused('outside the permitted', 'raw/paper.pdf', title='Escaping')

    def test_missing_and_out_of_area_sources_are_rejected(self):
        (self.root / 'inbox/x.md').write_text('inbox\n')
        (self.root / 'raw/README').write_text('no extension\n')
        (self.root / 'raw/a#b.md').write_text('anchor in name\n')
        (self.base / 'outside.md').write_text('outside the vault\n')
        (self.root / 'raw/escape.md').symlink_to(self.base / 'outside.md')
        for source, pattern in (('raw/missing.pdf', 'existing file under raw/'), ('index.md', 'under raw/, wiki/'),
                                ('AGENTS.md', 'under raw/, wiki/'), ('inbox/x.md', 'under raw/, wiki/'),
                                ('raw', 'existing file'), ('../outside.md', 'parent traversal'),
                                (str(self.base / 'outside.md'), 'parent traversal'), ('.brain/write.flock', 'Hidden'),
                                ('raw/escape.md', 'outside the permitted'), ('raw/README', 'extension'),
                                ('raw/a#b.md', 'cannot be linked')):
            with self.subTest(source=source):
                self.refused(pattern, 'raw/paper.pdf', source)
        self.refused('At least one --source')
        self.assertFalse((self.root / 'wiki/topics/why-sqlite-for-offline-sync.md').exists())

    def test_update_with_the_right_sha_appends_and_dedupes_sources(self):
        code, first = self.answer('raw/paper.pdf', 'memory/profile.md', date='2026-03-04',
                                  text='First answer.\n\n```markdown\n## Sources\n- [[raw/quoted]]\n```')
        self.assertEqual(code, 0, first)
        page = self.root / first['path']
        # A hand-written source with .md spelled out is the same file as memory/profile.
        page.write_text(page.read_text().replace('- [[memory/profile|profile.md]]\n', '- [[memory/profile.md]]\n'))
        sha = brain.digest(page.read_bytes())
        code, second = self.answer('raw/paper.pdf', 'memory/profile.md', 'raw/notes.md', 'raw/notes.md',
                                   text='Second finding.', update=True, expected_sha=sha, date='2026-03-09')
        self.assertEqual(code, 0, second)
        self.assertEqual(second['path'], first['path'])
        text = page.read_text()
        self.assertEqual(brain.digest(page.read_bytes()), second['sha256'])
        self.assertEqual(brain.frontmatter(text)['created'], '2026-03-04')
        self.assertEqual(brain.frontmatter(text)['updated'], '2026-03-09')
        section = text[text.index('\n## Sources\n\n- '):text.index('## Update')]
        self.assertEqual(section, '\n## Sources\n\n- [[raw/paper.pdf|paper.pdf]]\n- [[memory/profile.md]]\n'
                                  '- [[raw/notes|notes.md]]\n\n')
        self.assertTrue(text.endswith('\n## Update — 2026-03-09\n\nSecond finding.\n'))
        self.assertIn('```markdown\n## Sources\n- [[raw/quoted]]\n```', text)  # Quoted code is left alone.
        self.assertIn('runtime | Updated answer [[wiki/topics/why-sqlite-for-offline-sync|', (self.root / 'log.md').read_text())
        self.assertEqual((self.root / 'index.md').read_text().count('wiki/topics/why-sqlite-for-offline-sync'), 1)
        self.assert_valid()
        # A third update with only known sources changes the section not at all.
        code, third = self.answer('raw/notes.md', text='Third.', update=True, expected_sha=second['sha256'],
                                  date='2026-03-10')
        self.assertEqual(code, 0, third)
        self.assertIn(section + '## Update — 2026-03-09', page.read_text())
        self.assertTrue(page.read_text().endswith('\n## Update — 2026-03-09\n\nSecond finding.\n\n## Update — 2026-03-10\n\nThird.\n'))
        self.assert_valid()

    def test_update_of_a_page_without_sources_adds_the_section(self):
        self.write('wiki/topics/hand-written.md', META + '# Hand written\n\nSee [[wiki/topics/sqlite]].\n', index=True)
        sha = brain.digest((self.root / 'wiki/topics/hand-written.md').read_bytes())
        result = brain.file_answer(self.root, None, 'Added.', ['raw/paper.pdf'], slug='hand-written', update=True,
                                   expected_sha=sha, when='2026-02-01')
        self.assertTrue((self.root / result['path']).read_text().endswith(
            '\n## Update — 2026-02-01\n\nAdded.\n\n## Sources\n\n- [[raw/paper.pdf|paper.pdf]]\n'))
        self.assert_valid()

    def test_a_wrong_or_missing_sha_is_refused(self):
        code, first = self.answer('raw/paper.pdf')
        self.assertEqual(code, 0, first)
        self.refused('reread before updating', 'raw/notes.md', update=True, expected_sha='0' * 64)
        (self.root / first['path']).write_text((self.root / first['path']).read_text() + '\nEdited by hand.\n')
        self.refused('reread before updating', 'raw/notes.md', update=True, expected_sha=first['sha256'])
        self.refused('--update requires --expected-sha', 'raw/notes.md', update=True)
        self.refused('only valid with --update', 'raw/notes.md', expected_sha=first['sha256'])
        self.refused('does not exist', 'raw/notes.md', title='Never filed', update=True, expected_sha=first['sha256'])

    def test_redaction_is_applied_to_the_text(self):
        secret = 'sk-' + 'a1B2' * 8
        pem = '-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----'
        code, result = self.answer('raw/paper.pdf', text=f'Use password=hunter2-real and token {secret}.\n\n{pem}\n')
        self.assertEqual(code, 0, result)
        text = (self.root / result['path']).read_text()
        for value in ('hunter2-real', secret, 'MIIEvQIBADANBgkqhkiG9w0BAQEFAASC'):
            self.assertNotIn(value, text)
        self.assertIn('password=[redacted]', text)
        self.assertIn('[redacted credential]', text)
        self.assertIn('[redacted private key]', text)
        self.refused('credential', 'raw/paper.pdf', title='Key ' + secret)

    def test_text_title_and_slug_are_validated_before_any_write(self):
        self.refused('own "## Sources" heading', 'raw/paper.pdf', text='Answer.\n\n## Sources\n\n- fake\n')
        self.refused('Close every code fence', 'raw/paper.pdf', text='Answer.\n\n```bash\nunclosed\n')
        self.refused('Close every HTML comment', 'raw/paper.pdf', text='Answer. <!-- hides the sources')
        self.refused('Structural validation failed.*missing target', 'raw/paper.pdf', text='See [[wiki/topics/nowhere]].')
        self.refused('empty', 'raw/paper.pdf', text='\n  \n')
        self.refused('exceeds', 'raw/paper.pdf', text='x' * (brain.ANSWER_LIMIT + 1))
        self.refused('Title must be one line', 'raw/paper.pdf', title='Bad [[link]] title')
        self.refused('Title must be one line', 'raw/paper.pdf', title='Pipe | title')
        self.refused('Slug must be', 'raw/paper.pdf', title='Ünïcödé', slug='Upper')
        self.refused('Slug must be', 'raw/paper.pdf', title='!!!')
        self.refused('Slug must be', 'raw/paper.pdf', slug='a' * 65)
        self.refused('Slug must be', 'raw/paper.pdf', slug='../escape')
        self.refused('month|isoformat', 'raw/paper.pdf', date='2026-13-01')
        code, result = self.answer('raw/paper.pdf', title='  Multi   word\ttitle ', slug='custom-slug',
                                   text='See [[wiki/topics/sqlite]] and `[[not/a/link]]`.')
        self.assertEqual(code, 0, result)
        self.assertEqual(result['path'], 'wiki/topics/custom-slug.md')
        self.assertIn('\n# Multi word title\n', (self.root / result['path']).read_text())
        self.assert_valid()

    def test_symlinked_paths_are_refused(self):
        code, first = self.answer('raw/paper.pdf')
        self.assertEqual(code, 0, first)
        (self.root / 'wiki/topics/alias.md').symlink_to(self.root / first['path'])
        self.refused('symlink', 'raw/paper.pdf', title='Alias', update=True, expected_sha=first['sha256'])
        topics = self.root / 'wiki/topics'
        moved = self.base / 'topics-outside'
        topics.rename(moved)
        topics.symlink_to(moved, target_is_directory=True)
        self.refused('outside the permitted', 'raw/paper.pdf', title='Elsewhere')

    def test_a_failure_while_writing_happens_only_after_validation(self):
        # Every check runs before the first write: a refusal from check_vault leaves no partial page behind.
        with patch.object(brain, 'atomic_write', side_effect=AssertionError('no write expected')):
            with self.assertRaisesRegex(ValueError, 'Structural validation failed'):
                brain.file_answer(self.root, 'Broken', 'See [[nowhere]].', ['raw/paper.pdf'])
        self.assertFalse((self.root / 'wiki/topics/broken.md').exists())


class DoctorContentTests(WikiCase):
    def test_a_clean_vault_has_no_findings(self):
        fresh = self.base / 'fresh'
        fresh.mkdir()
        info = setup.initialize(self.config, fresh, mode='new', path=self.base / 'fresh-vault')
        self.assertEqual(brain.doctor(Path(info['root']))['findings'], [])

    def test_a_healthy_vault_with_compiled_records_and_linked_answers_has_none(self):
        self.turn('We chose SQLite for the offline catalogue.', 'Noted: SQLite.')
        listing = brain.compile_list(self.root, self.info['project'])
        record = listing['records'][0]
        note = self.root / self.info['project']
        answer = brain.file_answer(self.root, 'Offline storage', 'SQLite, as in [[wiki/topics/sqlite]].', [record['path']])
        note.write_text(note.read_text().replace(
            '## Decisions\n\nNone recorded yet.',
            f'## Decisions\n\nSQLite was chosen by the user. Evidence: {record["link"]}. '
            f'See [[{wiki_target(self.root, answer["path"])}|Offline storage]].'))
        brain.compile_done(self.root, self.info['project'], listing['token'], [self.info['project']])
        self.assertEqual(self.kinds(), [])
        self.assert_valid()

    def test_each_content_kind_is_reported_on_a_crafted_vault(self):
        project = self.info['project']
        self.turn('Remember the cache layout.', 'Saved.')
        # Another project has only a closed, uncompiled run board: it is still pending.
        other = self.base / 'other'
        other.mkdir()
        second = setup.initialize(self.config, other)['project']
        run = runs.open_run(self.root, second, 'Ship export')['run']
        runs.close_run(self.root, second, 'Done.')
        self.write('wiki/topics/claim.md', META + '# Claim\n\nTrust me. `[[raw/paper.pdf]]` in code is no citation.\n',
                   index=True)
        self.write('memory/decisions/choice.md', META + f'# Choice\n\nProject: [[{project[:-3]}|Project state]].\n\n'
                   f'Automatic records: [[{hooks.records_index(project)[:-3]}|All automatic records]].\n\n'
                   'See [[index]] and [[log]].\n', index=True)  # Navigation links are not evidence.
        self.write('wiki/topics/lonely.md', META + '# Lonely\n\nFrom [[raw/notes.md|notes]].\n', index=True)
        self.write('wiki/topics/linked.md', META + '# Linked\n\nSee [[wiki/topics/lonely]] [[raw/paper.pdf]].\n', index=True)
        self.write('wiki/topics/hub.md', META + '# Hub\n\n[[wiki/topics/linked]] [[wiki/topics/claim]] '
                   '[[memory/decisions/choice]] [[wiki/topics/sqlite]].\n', index=True)
        brain.append_log(self.root, 'Touched [[wiki/topics/hub|hub]]', '2026-01-02')  # log.md links are not inbound.
        findings = self.kinds()
        got = {(f['kind'], tuple(f['paths'])) for f in findings if f['kind'] != 'pending_compile'}
        self.assertEqual(got, {('empty_project_note', (project,)), ('unsupported_note', ('wiki/topics/claim.md',)),
                               ('unsupported_note', ('memory/decisions/choice.md',)),
                               ('orphan_note', ('wiki/topics/hub.md',))})
        pending = {f['project']: f for f in findings if f['kind'] == 'pending_compile'}
        self.assertEqual(set(pending), {project, second})
        self.assertEqual(pending[second]['count'], 1)
        self.assertEqual(pending[second]['paths'], [brain.run_note(run)])
        self.assertEqual(pending[second]['oldest'], brain.day())
        self.assertEqual(pending[project]['count'], 1)
        self.assertTrue(pending[project]['paths'][0].startswith('memory/sessions/'))
        for finding in findings:
            self.assertTrue({'kind', 'paths', 'detail'} <= set(finding))
            self.assertIn('not proof of error', finding['detail'])
        # Filling one section, citing evidence, and linking the hub clear those findings.
        note = self.root / project
        note.write_text(note.read_text().replace('## Status\n\nNone recorded yet.', '## Status\n\nCache layout saved.'))
        self.write('wiki/topics/claim.md', META + f'# Claim\n\nSee [[{hooks.records_index(project)[:-3]}]].\n')
        self.write('memory/decisions/choice.md', META + '# Choice\n\nFrom [[memory/profile]].\n')
        note.write_text(note.read_text() + '\nTopics: [[wiki/topics/hub]].\n')
        self.assertEqual({f['kind'] for f in self.kinds()}, {'pending_compile'})
        with redirect_stderr(io.StringIO()), patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(brain.main(['--root', str(self.root), 'doctor']), 0)
        self.assertIn('pending_compile', out.getvalue())

    def test_a_project_note_with_navigation_lines_only_is_still_empty(self):
        self.turn('Hello.', 'Hi.')
        note = (self.root / self.info['project']).read_text()
        self.assertIn('Latest automatic record:', note.split('## Next steps', 1)[1])  # Inside the last section.
        self.assertEqual([f['kind'] for f in self.kinds() if f['kind'] == 'empty_project_note'], ['empty_project_note'])
        # Without any record, a fresh note is not a candidate.
        for path in (self.root / 'memory/sessions').glob('*-auto-*.md'):
            path.unlink()
        self.assertEqual([f for f in self.kinds() if f['kind'] == 'empty_project_note'], [])


class RankingTests(WikiCase):
    def setUp(self):
        super().setUp()
        (self.root / 'wiki/topics/sqlite.md').unlink()

    def paths(self, query, limit=8):
        return [row['path'] for row in brain.search(self.root, query, limit)]

    def test_title_and_path_hits_outrank_body_only_hits(self):
        self.write('wiki/topics/b-body.md', META + '# Notes\n\nKafka retention is seven days.\n')
        self.write('wiki/topics/a-title.md', META + '# Kafka retention\n\nSeven days.\n')
        self.write('wiki/topics/kafka-retention.md', META + '# Other\n\nSeven days.\n')
        self.write('wiki/topics/c-heading.md', META + '# Notes\n\n## Kafka retention\n\nSeven days.\n')
        self.write('wiki/topics/d-code.md', META + '# Notes\n\n```bash\n# kafka retention\n## kafka retention\n```\n')
        rows = {row['path']: row['score'] for row in brain.search(self.root, 'kafka retention')}
        self.assertEqual(rows, {'wiki/topics/a-title.md': 9.0, 'wiki/topics/kafka-retention.md': 9.0,
                                'wiki/topics/c-heading.md': 6.0, 'wiki/topics/b-body.md': 3.0,
                                'wiki/topics/d-code.md': 3.0})  # Headings inside code are body text.
        self.assertEqual(self.paths('kafka retention'), ['wiki/topics/a-title.md', 'wiki/topics/kafka-retention.md',
                                                         'wiki/topics/c-heading.md', 'wiki/topics/b-body.md',
                                                         'wiki/topics/d-code.md'])

    def test_all_terms_outrank_partial_matches_and_any_term_matches_are_returned(self):
        self.write('wiki/topics/partial.md', META + '# Kafka\n\n## Kafka\n\nKafka only.\n')
        self.write('wiki/topics/zz-both.md', META + '# Notes\n\nkafka and retention in the body.\n')
        rows = brain.search(self.root, 'kafka retention')
        self.assertEqual([r['path'] for r in rows], ['wiki/topics/zz-both.md', 'wiki/topics/partial.md'])
        self.assertGreater(rows[1]['score'], rows[0]['score'])  # Completeness wins over a higher score.
        self.assertEqual(self.paths('kafka', limit=1), ['wiki/topics/partial.md'])

    def test_curated_notes_outrank_records_with_equal_hits(self):
        record = 'memory/sessions/2026-01-01-auto-' + 'a' * 24 + '.md'
        self.write(record, '---\ncreated: 2026-01-01\nupdated: 2026-01-01\nstatus: recorded\n---\n# Log\n\nzebrafish\n')
        self.write('memory/sessions/2026-01-01-auto-' + 'b' * 24 + '.md',
                   '---\ncreated: 2026-01-01\nupdated: 2026-01-01\nstatus: compiled\n---\n# Log\n\nzebrafish\n')
        self.write('wiki/topics/zz-curated.md', META + '# Fish\n\nzebrafish\n')
        rows = brain.search(self.root, 'zebrafish')
        self.assertEqual([(r['path'], r['score']) for r in rows],
                         [('wiki/topics/zz-curated.md', 1.5), (record, 0.75),
                          ('memory/sessions/2026-01-01-auto-' + 'b' * 24 + '.md', 0.75)])

    def test_short_terms_match_whole_words_and_long_terms_match_prefixes(self):
        self.write('wiki/topics/user.md', META + '# People\n\nThe user rapidly reuses apis.\n')
        self.write('wiki/topics/api.md', META + '# Notes\n\nCall the API (v2).\n')
        self.write('wiki/topics/deploy.md', META.replace('status', 'claim_key: zanzibar\nstatus') +
                   '# Notes\n\nDeployment pipeline; testing daily.\n')
        self.assertEqual(self.paths('use'), [])  # Only stop words: still searched, but "use" is not "user".
        self.assertEqual(self.paths('api'), ['wiki/topics/api.md'])
        self.assertEqual(self.paths('deploy'), ['wiki/topics/deploy.md'])
        self.assertEqual(self.paths('test'), [])  # Under 5 characters: whole words only.
        self.assertEqual(self.paths('ployment'), [])  # A prefix, not a substring.
        self.assertEqual(self.paths('rapid'), ['wiki/topics/user.md'])
        self.assertEqual(self.paths('zanzibar'), [])  # Frontmatter is not body text.
        for term, word in (('use', 'user'), ('api', 'rapidly'), ('test', 'testing'), ('deploy', 'deployment')):
            self.assertEqual(brain.word_at(term, word) >= 0, len(term) >= 5, term)

    def test_search_rows_carry_scores_and_the_cli_keeps_limit(self):
        for number in range(4):
            self.write(f'wiki/topics/t{number}.md', META + f'# T{number}\n\nharbour\n')
        code, rows = self.cli('search', 'harbour', '--limit', '2')
        self.assertEqual(code, 0)
        self.assertEqual([(r['path'], r['score'], r['status']) for r in rows],
                         [('wiki/topics/t0.md', 1.5, 'active'), ('wiki/topics/t1.md', 1.5, 'active')])
        self.assertIn('harbour', rows[0]['excerpt'])

    def test_matching_notes_uses_the_shared_scorer_and_keeps_scope(self):
        project = self.info['project']
        self.write('wiki/topics/rapid.md', META + '# Rapid\n\nrapid capital apis\n')
        self.write('wiki/topics/zz-gateway.md', META + '# Gateway\n\nThe api gateway.\n')
        self.write('wiki/topics/body.md', META + '# Notes\n\nThe gateway API is internal.\n')
        with patch.object(brain, 'rank', wraps=brain.rank) as scorer:
            found = [n['path'] for n in hooks.matching_notes(self.root, project, 'Which api gateway?')]
        self.assertTrue(scorer.called)
        self.assertEqual(found, ['wiki/topics/zz-gateway.md', 'wiki/topics/body.md'])
        self.assertEqual([n['path'] for n in hooks.matching_notes(self.root, project, 'Which api gateway?',
                                                                  exclude={'wiki/topics/zz-gateway.md'})],
                         ['wiki/topics/body.md'])
        # Another project's records and records list never match.
        other = self.base / 'other'
        other.mkdir()
        info = setup.initialize(self.config, other)
        hooks.handle({'hook_event_name': 'UserPromptSubmit', 'cwd': str(other), 'session_id': 'o',
                      'prompt': 'gateway secrets'}, self.config)
        hooks.handle({'hook_event_name': 'Stop', 'cwd': str(other), 'session_id': 'o',
                      'last_assistant_message': 'gateway answer'}, self.config)
        found = [n['path'] for n in hooks.matching_notes(self.root, project, 'gateway answer')]
        self.assertFalse([p for p in found if p.startswith('memory/sessions/')], found)
        self.assertNotIn(hooks.records_index(info['project']), found)

    def test_prompt_notes_stay_within_the_budget(self):
        for number in range(6):
            self.write(f'wiki/topics/big{number}.md', META + f'# Lighthouse {number}\n\n' + 'lighthouse keeper ' * 400 + '\n')
        self.handle('SessionStart', source='startup')
        text = self.handle('UserPromptSubmit', prompt='Tell me about the lighthouse keeper')['hookSpecificOutput']['additionalContext']
        self.assertTrue(text.startswith(hooks.PROMPT_HEADER))
        payload = text[len(hooks.PROMPT_HEADER):]
        self.assertLessEqual(len(payload), hooks.NOTES_BUDGET)
        self.assertEqual([n['path'] for n in json.loads(payload)['matching_notes']][:1], ['wiki/topics/big0.md'])


class ReviewFixTests(WikiCase):
    """Fix round 1: each test fails on the reviewed phase-4 code."""

    def test_the_returned_sha_is_the_page_this_call_wrote(self):
        page, real, extra = self.root / 'wiki/topics/race.md', brain.release, 'Another writer appended this.\n'

        def release(fd):  # Another writer updates the page the moment the lock is free.
            real(fd)
            if page.is_file() and not page.read_text().endswith(extra):
                page.write_text(page.read_text() + extra)
        with patch.object(brain, 'release', release):
            result = brain.file_answer(self.root, 'Race', 'Mine.', ['raw/paper.pdf'])
        text = page.read_text()
        self.assertTrue(text.endswith(extra))
        self.assertEqual(result['sha256'], brain.digest(text[:-len(extra)].encode()))
        with self.assertRaisesRegex(ValueError, 'reread before updating'):  # So the guard still sees that update.
            brain.file_answer(self.root, None, 'Next.', [], slug='race', update=True, expected_sha=result['sha256'])

    def test_word_matching_stays_linear_on_long_repetitive_words(self):
        started = time.perf_counter()
        self.assertEqual(brain.word_at('a' * 10000, 'b' + 'a' * 60000), -1)
        self.assertEqual(brain.word_at('aaa', 'a' * 60000 + ' aaa'), 60001)
        self.assertEqual(brain.word_at('aaaaa', 'b' + 'a' * 60000 + ' aaaaaa'), 60002)
        self.assertLess(time.perf_counter() - started, 0.1)
        for number in range(25):
            self.write(f'wiki/topics/long{number}.md', META + f'# Long {number}\n\nb' + 'a' * 59000 + '\n')
        self.handle('SessionStart', source='startup')
        started = time.perf_counter()
        self.assertEqual(self.handle('UserPromptSubmit', prompt='a' * 10000), {})
        self.assertLess(time.perf_counter() - started, 3)  # The hook timeout is 10 s.

    def test_prompt_matching_keeps_only_the_best_candidates_in_memory(self):
        for number in range(150):
            self.write(f'wiki/topics/harbour{number:03}.md', META + f'# Note {number}\n\n' + 'harbour ' * 6000 + '\n')
        tracemalloc.start()
        try:
            found = hooks.matching_notes(self.root, self.info['project'], 'harbour')
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual([n['path'] for n in found], [f'wiki/topics/harbour{n:03}.md' for n in range(3)])
        self.assertLess(peak, 2_500_000)  # 150 retained 48 KB texts would need over 7 MB.

    def test_cli_search_keeps_every_word_typed(self):
        (self.root / 'wiki/topics/sqlite.md').unlink()
        self.write('wiki/topics/only-word.md', META + '# Notes\n\nWe keep using it.\n')
        self.write('wiki/topics/only-term.md', META + '# Notes\n\nThe api.\n')
        self.write('wiki/topics/both.md', META + '# Notes\n\nThe api we are using.\n')
        code, rows = self.cli('search', 'api using')
        self.assertEqual(code, 0)
        self.assertEqual([r['path'] for r in rows], ['wiki/topics/both.md', 'wiki/topics/only-term.md',
                                                     'wiki/topics/only-word.md'])
        words = [f'zq{n}' for n in range(70)]
        self.write('wiki/topics/a-first.md', META + '# Notes\n\n' + ' '.join(words[:64]) + '\n')
        self.write('wiki/topics/b-all.md', META + '# Notes\n\n' + ' '.join(words) + '\n')
        self.write('wiki/topics/c-last.md', META + '# Notes\n\n' + words[-1] + '\n')
        rows = brain.search(self.root, ' '.join(words))
        self.assertEqual([(r['path'], r['score']) for r in rows],
                         [('wiki/topics/b-all.md', 105.0), ('wiki/topics/a-first.md', 96.0), ('wiki/topics/c-last.md', 1.5)])
        self.assertEqual(len(brain.query_terms(' '.join(words))), brain.QUERY_TERMS)  # Hook matching stays capped.

    def test_orphan_detection_follows_markdown_links(self):
        self.write('wiki/topics/demo.md', META + '# Demo\n\nFrom [[raw/paper.pdf]].\n', index=True)
        note = self.root / self.info['project']
        note.write_text(note.read_text() + '\nSee [Demo](../../wiki/topics/demo.md).\n')
        self.write('wiki/topics/md-only.md', META + '# Markdown only\n\nFrom [[raw/notes.md]].\n')
        index = self.root / 'index.md'
        index.write_text(index.read_text() + '\n- [Markdown only](wiki/topics/md-only.md)\n')
        self.assert_valid()
        orphans = {f['paths'][0] for f in self.kinds() if f['kind'] == 'orphan_note'}
        self.assertNotIn('wiki/topics/demo.md', orphans)
        self.assertIn('wiki/topics/md-only.md', orphans)


if __name__ == '__main__':
    unittest.main()
