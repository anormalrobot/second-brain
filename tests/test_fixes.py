"""Regression coverage for locking, redaction, records, guards, and vault operations (temp dirs only)."""
from contextlib import redirect_stderr
from datetime import datetime
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import brain
import check_vault
import memory_hooks as hooks
import second_brain as setup
from check_vault import check, wiki_target


def meta(body, status='active'):
    stamp = brain.day()
    return f'---\ncreated: {stamp}\nupdated: {stamp}\nstatus: {status}\n---\n' + body


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='second-brain-fixes-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        env = patch.dict(os.environ, {'SECOND_BRAIN_CONFIG': str(self.base / 'config/config.json')})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('SECOND_BRAIN_VAULT', None)
        os.environ.pop('SECOND_BRAIN_AUTOMATION', None)
        os.environ.pop('SECOND_BRAIN_RUN', None)


class VaultCase(TempCase):
    def setUp(self):
        super().setUp()
        self.project = self.base / 'project'
        self.project.mkdir()
        self.config = self.base / 'config/config.json'
        self.info = setup.initialize(self.config, self.project, mode='new', path=self.base / 'vault')
        self.root = Path(self.info['root'])

    def event(self, kind, cwd=None, **extra):
        return {'hook_event_name': kind, 'cwd': str(cwd or self.project), 'session_id': 'fix-session', **extra}

    def turn(self, prompt, response, cwd=None, session='fix-session'):
        hooks.handle(self.event('UserPromptSubmit', cwd, prompt=prompt, session_id=session), self.config)
        hooks.handle(self.event('Stop', cwd, last_assistant_message=response, session_id=session), self.config)

    def assert_valid(self, root=None):
        self.assertEqual(check(root or self.root)[0], [])


HOLDER = """
import sys, time
sys.path.insert(0, sys.argv[1])
import brain
with brain.writer(sys.argv[2], wait=float(sys.argv[3])):
    print('locked', flush=True)
    time.sleep(float(sys.argv[4]))
"""

WORKER = """
import os, sys, time
sys.path.insert(0, sys.argv[1])
import brain
for _ in range(int(sys.argv[4])):
    with brain.writer(sys.argv[2], wait=30):
        with open(sys.argv[3], 'a') as log:
            log.write('B %d\\n' % os.getpid()); log.flush()
            time.sleep(0.005)
            log.write('E %d\\n' % os.getpid()); log.flush()
"""


@unittest.skipUnless(os.name == 'posix', 'subprocess lock tests use POSIX signals')
class LockTests(TempCase):
    scripts = str(Path(brain.__file__).resolve().parent)

    def holder(self, hold=30.0):
        proc = subprocess.Popen([sys.executable, '-B', '-c', HOLDER, self.scripts, str(self.base), '5', str(hold)],
                                stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.addCleanup(proc.stdout.close)
        self.assertEqual(proc.stdout.readline().strip(), 'locked')
        return proc

    def test_release_lets_the_next_writer_in_and_keeps_lock_file(self):
        with brain.writer(self.base):
            owner = json.loads((self.base / '.brain/write.flock').read_text())
            self.assertEqual(owner['pid'], os.getpid())
            self.assertEqual(owner['hostname'], socket.gethostname())
        with brain.writer(self.base, wait=0):
            pass
        self.assertTrue((self.base / '.brain/write.flock').is_file())

    def test_killed_holder_does_not_block_next_writer(self):
        proc = self.holder()
        proc.kill()
        proc.wait()
        with brain.writer(self.base, wait=1):
            pass

    def test_live_holder_causes_bounded_wait_and_informative_error(self):
        proc = self.holder()
        started = time.monotonic()
        with self.assertRaises(ValueError) as caught:
            with brain.writer(self.base, wait=0.3):
                pass
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertLess(elapsed, 2)
        self.assertIn('Another writer is active', str(caught.exception))
        self.assertIn('pid ' + str(proc.pid), str(caught.exception))
        with patch.object(brain, 'LOCK_WAIT', 0.1), self.assertRaises(ValueError):
            with brain.writer(self.base):
                pass

    def test_retry_succeeds_when_holder_releases_mid_wait(self):
        self.holder(hold=0.4)
        with brain.writer(self.base):  # Default wait (LOCK_WAIT) outlasts the holder.
            pass

    def test_threads_in_one_process_are_serialized(self):
        inside, results = [], []

        def contend():
            try:
                with brain.writer(self.base, wait=5):
                    inside.append(1)
                    self.assertEqual(len(inside), 1)
                    time.sleep(0.02)
                    inside.pop()
                results.append('ok')
            except Exception as exc:  # Surface assertion failures from threads.
                results.append(exc)
        threads = [threading.Thread(target=contend) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results, ['ok'] * 4)

    def test_contending_processes_never_overlap(self):
        log = self.base / 'sections.log'
        procs = [subprocess.Popen([sys.executable, '-B', '-c', WORKER, self.scripts, str(self.base), str(log), '10'])
                 for _ in range(4)]
        self.assertEqual([proc.wait(timeout=60) for proc in procs], [0] * 4)
        lines = log.read_text().splitlines()
        self.assertEqual(len(lines), 80)
        for begin, end in zip(lines[::2], lines[1::2]):
            self.assertEqual((begin[0], end[0]), ('B', 'E'))
            self.assertEqual(begin[2:], end[2:])

    def test_legacy_lock_directory_is_ignored(self):
        legacy = self.base / '.brain/write.lock'
        legacy.mkdir(parents=True)
        (legacy / 'owner.json').write_text(json.dumps({'pid': os.getpid(), 'started': 'old'}))
        with brain.writer(self.base, wait=0):
            pass
        self.assertTrue(legacy.is_dir())


class LocalDateAndCliTests(TempCase):
    @unittest.skipUnless(hasattr(time, 'tzset'), 'time.tzset is unavailable')
    def test_day_uses_system_local_date(self):
        self.addCleanup(time.tzset)
        for zone in ('UTC', 'Pacific/Kiritimati', 'Etc/GMT+12'):
            with patch.dict(os.environ, {'TZ': zone}):
                time.tzset()
                self.assertEqual(brain.day(), datetime.now().astimezone().date().isoformat())
        self.assertNotIn('zoneinfo', Path(brain.__file__).read_text())

    def run_main(self, main, argv):
        with redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as caught:
            main(argv)
        return caught.exception.code, err.getvalue()

    def test_brain_cli_requires_root_except_restore(self):
        for argv in (['scan'], ['coverage'], ['doctor'], ['brief'], ['search', 'x']):
            code, err = self.run_main(brain.main, argv)
            self.assertEqual(code, 2)
            self.assertIn('--root is required', err)
        code, _ = self.run_main(brain.main, ['--root', str(self.base), 'checkpoint', '--summary', 's',
                                             '--next', 'n', '--evidence', 'x'])
        self.assertEqual(code, 2)  # --project has no repository default.
        with patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(brain.main(['restore', str(self.base / 'missing.zip'),
                                         '--destination', str(self.base / 'out')]), 1)
        self.assertNotIn('--root', out.getvalue())  # Restore runs without a root.

    def test_brief_has_no_default_project(self):
        self.assertIsNone(brain.brief.__defaults__[0])

    def test_check_vault_requires_root_argument(self):
        with patch.object(sys, 'argv', ['check_vault.py']), redirect_stderr(io.StringIO()):
            self.assertEqual(check_vault.main(), 2)

    def test_parsers_reject_abbreviations(self):
        code, _ = self.run_main(brain.main, ['--roo', str(self.base), 'scan'])
        self.assertEqual(code, 2)
        code, _ = self.run_main(brain.main, ['--root', str(self.base), 'brief', '--proj', 'x'])
        self.assertEqual(code, 2)
        code, _ = self.run_main(setup.main, ['--conf', str(self.base / 'c.json'), 'status'])
        self.assertEqual(code, 2)
        code, _ = self.run_main(setup.main, ['automation', '--hel'])
        self.assertEqual(code, 2)


class RedactionTests(unittest.TestCase):
    def assert_redacted(self, text, secret, keep=()):
        result = hooks.excerpt(text)
        self.assertNotIn(secret, result)
        self.assertIn('[redacted', result)
        for part in keep:
            self.assertIn(part, result)

    def test_quoted_json_and_yaml_keys(self):
        self.assert_redacted('{"api_key": "abc123xyz", "name": "kept"}', 'abc123xyz', ['"api_key"', 'kept'])
        self.assert_redacted("'password': 'two words'", 'two words', ["'password'"])
        self.assert_redacted('client_secret: s3cr3tvalue', 's3cr3tvalue')

    def test_authorization_headers(self):
        self.assert_redacted('Authorization: Bearer abc.def.ghi', 'abc.def', ['Authorization: Bearer'])
        self.assert_redacted('curl -H "Authorization: Basic dXNlcjpwYXNz" url', 'dXNlcjpwYXNz', ['Basic'])
        self.assert_redacted('use bearer Zm9vYmFyYmF6cXV4MTIzNDU2', 'Zm9vYmFyYmF6cXV4MTIzNDU2')

    def test_url_userinfo_keeps_scheme_user_and_host(self):
        result = hooks.excerpt('postgres://admin:hunter2@db.example.com:5432/app')
        self.assertNotIn('hunter2', result)
        self.assertIn('postgres://admin:', result)
        self.assertIn('@db.example.com:5432/app', result)

    def test_provider_tokens(self):
        for secret in ('xoxb-1234567890-abcdefghijkl', 'xoxp-1234567890-abcdefghijkl',
                       'AIza' + 'Sy' + 'A1b2C3d4E5' * 3 + 'x_-',
                       'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U'):
            self.assert_redacted('value ' + secret + ' end', secret, ['value', 'end'])

    def test_broader_key_names(self):
        for key in ('token', 'auth_token', 'refresh_token', 'client_secret', 'private_key', 'passwd', 'pwd',
                    'GITHUB_TOKEN', 'access-token', 'api_key', 'secret', 'password'):
            self.assert_redacted(key + '=Value9876', 'Value9876', [key])
        self.assert_redacted('?refresh_token=r1secret&page=2', 'r1secret', ['&page=2'])

    def test_quoted_values_with_escapes_and_newlines(self):
        self.assert_redacted('{"password":"abc\\"restsecret", "name": "kept"}', 'restsecret', ['kept'])
        self.assert_redacted("pwd: 'it''s restsecret'\nother: kept", 'restsecret', ['other: kept'])
        result = hooks.excerpt('"private_key": "first-line-secret\nsecond-line-secret"\nafter')
        self.assertNotIn('first-line-secret', result)
        self.assertNotIn('second-line-secret', result)
        self.assertIn('after', result)

    def test_long_multiline_quoted_values_and_linear_time(self):
        for size in (4010, 50000):
            text = '"private_key": "' + 'A' * size + '\nSECOND_LINE_SECRET"\nafter'
            result = hooks.excerpt(text, 10 ** 6)
            self.assertNotIn('SECOND_LINE_SECRET', result)
            self.assertIn('after', result)
        for adversarial in ('password: "x\n' * 20000, 'password: "' + '\\a' * 100000, "pwd: '" + "''" * 100000,
                            'a-' * 100000, 'a.' * 100000 + '://', 'x://' * 50000):
            self.assertGreaterEqual(len(adversarial), 200000)
            started = time.perf_counter()
            hooks.excerpt(adversarial, 10 ** 7)
            self.assertLess(time.perf_counter() - started, 1)
        # Unterminated quoted values fail closed to the end of the redaction window.
        self.assertEqual(hooks.excerpt('password: "open\nnext line'), 'password: "[redacted]"')

    def test_window_bounds_time_for_huge_inputs(self):
        for adversarial in ('password: "x"\n' * (4500000 // 14), '-----BEGIN RSA PRIVATE KEY-----\nabc\n' * 30000,
                            'password: "x\n' * 20000, 'password: "' + '\\a' * 100000, "pwd: '" + "''" * 100000,
                            'a-' * 100000, 'a.' * 100000 + '://', 'x://' * 50000, 'a.' * 24000):
            started = time.perf_counter()
            result = hooks.excerpt(adversarial)
            self.assertLess(time.perf_counter() - started, 0.5)
            self.assertLessEqual(len(result), hooks.MESSAGE_LIMIT + len('\n[excerpt truncated]'))

    def test_provider_token_scan_is_linear(self):
        for prefix in ('sk-', 'sk-proj-', 'ghp_', 'gho_', 'github_pat_', 'AKIA', 'xoxb-', 'AIza'):
            n = 4 * 200000 // len(prefix) + 1  # Fills the whole redaction window.
            for text in (prefix * n + '_', (prefix + 'x') * n):
                started = time.perf_counter()
                hooks.excerpt(text, limit=200000)
                self.assertLess(time.perf_counter() - started, 0.5, prefix)

    def test_provider_token_semantics_match_previous_regex(self):
        previous = re.compile(r'\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|'
                              r'github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16}|xox[abprs]-[A-Za-z0-9-]{10,}|'
                              r'AIza[A-Za-z0-9_-]{35})(?![A-Za-z0-9_-])')
        cases = ['sk-' + 'a' * 20, 'sk-' + 'a' * 19, 'x sk-proj-' + 'a' * 30 + ' y', 'ghp_' + 'a' * 20,
                 'ghp_' + 'a' * 20 + '_b', 'ghp_' + 'a' * 20 + '-b', 'github_pat_' + 'a_' * 10,
                 'github_pat_' + 'a' * 20 + '-', 'AKIA' + 'A' * 16, 'AKIA' + 'A' * 17, 'AKIA' + 'a' * 16,
                 'xoxb-' + 'a-' * 5, 'xoxb-' + 'a' * 10 + '_', 'AIza' + 'a' * 35, 'AIza' + 'a' * 36,
                 'pre-sk-' + 'a' * 20, 'pre_sk-' + 'a' * 20, '\u00e9sk-' + 'a' * 20, 'xoxs-ab-cd-ef-gh, ghs_short']
        for case in cases:
            self.assertEqual(hooks.WORD_RUN.sub(hooks.redact_token_run, case),
                             previous.sub('[redacted credential]', case), case)

    def test_jwt_scan_is_linear(self):
        for text in ('eyJ-' * 48000, 'eyJab.' * 31000 + '\n', 'eyJab.c.' * 23000 + '\n'):
            started = time.perf_counter()
            hooks.excerpt(text, limit=48000)
            self.assertLess(time.perf_counter() - started, 0.5)
        self.assertEqual(hooks.excerpt('a.b x-eyJabc.def.ghi y_eyJabc.def.ghi 1.2.3'),
                         'a.b x-[redacted credential] y_eyJabc.def.ghi 1.2.3')

    def test_window_edge_discards_partial_last_line(self):
        window = 4 * hooks.MESSAGE_LIMIT
        # Earlier content collapses during redaction, pulling later lines into the stored limit.
        reviewer = 'password="' + 'x' * 47965 + '"\nhttps://user:TAILSECRET@host'
        self.assertNotIn('TAILSECRET', hooks.excerpt(reviewer))
        tester = 'https://alice:' + 'URLSECRET' * 7000 + '@example.com/'
        self.assertEqual(hooks.excerpt(tester), '\n[excerpt truncated]')
        self.assertEqual(hooks.excerpt('y' * 100000), '\n[excerpt truncated]')
        tokens = ('sk-proj-' + 'S' * 60, 'ghp_' + 'G' * 60, 'AIza' + 'Q' * 35,
                  'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.' + 'J' * 43)
        for token in tokens:
            for prefix in ('', 'password="' + 'x' * 30000 + '"\n',
                           '-----BEGIN PRIVATE KEY-----\n' + 'k' * 30000 + '\n-----END PRIVATE KEY-----\n'):
                lead = prefix + 'z' * (window - len(prefix) - len(token) // 2 - 1) + ' '
                result = hooks.excerpt(lead + token + ' tail\nafter')
                self.assertNotIn(token[:10], result)
                self.assertNotIn(token[-10:], result)
                self.assertTrue(result.endswith('\n[excerpt truncated]'))

    def test_hook_does_not_store_url_secret_crossing_the_window(self):
        tmp = tempfile.TemporaryDirectory(prefix='second-brain-edge-')
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        (base / 'project').mkdir()
        config = base / 'config/config.json'
        with patch.dict(os.environ, {'SECOND_BRAIN_CONFIG': str(config)}):
            info = setup.initialize(config, base / 'project', mode='new', path=base / 'vault')
            payload = 'https://alice:' + 'URLSECRET' * 7000 + '@example.com/'
            event = {'cwd': str(base / 'project'), 'session_id': 'edge'}
            hooks.handle({**event, 'hook_event_name': 'UserPromptSubmit', 'prompt': payload}, config)
            hooks.handle({**event, 'hook_event_name': 'Stop', 'last_assistant_message': payload}, config)
        saved = '\n'.join(p.read_text(errors='replace') for p in Path(info['root']).rglob('*') if p.is_file())
        self.assertNotIn('URLSECRET', saved)

    def test_long_url_password_keeps_structure(self):
        result = hooks.excerpt('postgres://admin:' + 'P' * 2000 + '@db.example.com/app')
        self.assertEqual(result, 'postgres://admin:[redacted]@db.example.com/app')

    def test_placeholder_prefix_does_not_bypass_redaction(self):
        for text in ('{"password": "[redacted]TAILSECRET"}', 'password=[redacted]TAILSECRET',
                     "token: '[redacted credential]TAILSECRET'", 'Authorization: Bearer [redacted]TAILSECRET'):
            self.assertNotIn('TAILSECRET', hooks.excerpt(text), text)
        self.assertEqual(hooks.excerpt('password: "[redacted]"'), 'password: "[redacted]"')

    def test_secret_straddling_the_window_edge_fails_closed(self):
        text = 'a' * 11000 + '\npassword: "' + 'S' * 20000 + '\n' + 'S' * 29000 + '\nTAIL_SECRET"\nafter'
        result = hooks.excerpt(text)
        self.assertNotIn('S' * 10, result)
        self.assertNotIn('TAIL_SECRET', result)
        self.assertTrue(result.endswith('password: "[redacted]"\n[excerpt truncated]'))
        pem = 'x\n-----BEGIN PRIVATE KEY-----\n' + 'K' * 60000 + '\n-----END PRIVATE KEY-----\nafter'
        self.assertEqual(hooks.excerpt(pem), 'x\n[redacted private key]\n[excerpt truncated]')

    def test_normal_excerpt_behavior_is_unchanged(self):
        secret = 'sk-' + 'x' * 30
        text = 'api_key=' + secret + '\npassword="secret-value"\n' + 'a' * 12000
        result = hooks.excerpt(text)
        self.assertTrue(result.startswith('api_key=[redacted credential]\npassword="[redacted]"\naaa'))
        self.assertTrue(result.endswith('\n[excerpt truncated]'))
        self.assertEqual(len(result), hooks.MESSAGE_LIMIT + len('\n[excerpt truncated]'))
        pem = 'k: -----BEGIN EC PRIVATE KEY-----\nabc\n-----END EC PRIVATE KEY----- done'
        self.assertEqual(hooks.excerpt(pem), 'k: [redacted private key] done')
        self.assertEqual(hooks.excerpt('short text'), 'short text')

    def test_short_segment_jwt(self):
        token = 'eyJhbGciOiJIUzI1NiJ9.e30._zoCskUqiKud3jm5DU6ztAsqTCVK0C07Wr_3hDRIqFU'
        self.assert_redacted('jwt ' + token, token)
        self.assertEqual(hooks.excerpt('version 1.2.3 and e30.x'), 'version 1.2.3 and e30.x')

    def test_prose_is_not_redacted(self):
        prose = ('The token is valid, the password policy applies, max_tokens=4096 stays, '
                 'and the bearer of news reads https://example.com/path.')
        self.assertEqual(hooks.excerpt(prose), prose)

    def test_existing_patterns_still_apply_once(self):
        secret = 'sk-' + 'x' * 30
        self.assertEqual(hooks.excerpt('api_key=' + secret), 'api_key=[redacted credential]')


class RecordsIndexTests(VaultCase):
    def test_records_index_replaces_index_and_log_flooding(self):
        index_before = (self.root / 'index.md').read_text()
        log_before = (self.root / 'log.md').read_text()
        for number in range(3):  # Records are per session, so three sessions make three records.
            self.turn(f'Task {number}', f'Result {number}', session=f'session-{number}')
        self.turn('Task 3', 'Result 3', session='session-2')
        records = sorted((self.root / 'memory/sessions').glob('*-auto-*.md'))
        self.assertEqual(len(records), 3)
        relative = hooks.records_index(self.info['project'])
        listing = (self.root / relative).read_text()
        index, log = (self.root / 'index.md').read_text(), (self.root / 'log.md').read_text()
        for record in records:
            self.assertIn(record.stem, listing)
            self.assertNotIn(record.stem, index)
            self.assertNotIn(record.stem, log)
        self.assertEqual(index.count(wiki_target(self.root, relative)), 1)
        self.assertEqual(len(index.splitlines()) - len(index_before.splitlines()), 2)
        self.assertLessEqual(log.count('\n## ') - log_before.count('\n## '), 1)
        project = (self.root / self.info['project']).read_text()
        self.assertEqual(project.count(wiki_target(self.root, relative)), 1)
        self.assertEqual(brain.frontmatter(listing)['status'], 'active')
        self.assert_valid()
        self.assertIn('Result 3', brain.brief(self.root, self.info['project']))

    def test_existing_index_entries_are_preserved(self):
        index = self.root / 'index.md'
        index.write_text(index.read_text() + '\n- [[memory/profile|User kept line]]\n')
        before = index.read_text()
        self.turn('Task', 'Result')
        self.assertTrue(index.read_text().startswith(before.rstrip()))

    def test_records_index_does_not_leak_into_other_projects(self):
        other = self.base / 'other'
        other.mkdir()
        info = setup.initialize(self.config, other)
        self.turn('zebracorn question', 'zebracorn answer', cwd=other)
        self.turn('ordinary', 'ordinary answer')
        names = [n['path'] for n in hooks.matching_notes(self.root, self.info['project'], 'automatic records conversation zebracorn')]
        self.assertNotIn(hooks.records_index(info['project']), names)
        self.assertFalse(any('zebracorn' in (self.root / n).read_text() for n in names))
        self.assertNotIn(hooks.records_index(self.info['project']), names)
        self.assert_valid()


class NestedRecordsTests(VaultCase):
    def test_nested_project_notes_get_distinct_records_lists(self):
        names = ('memory/projects/a/b.md', 'memory/projects/a--b.md')
        self.assertNotEqual(hooks.records_index(names[0]), hooks.records_index(names[1]))
        for number, name in enumerate(names):
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text(meta(f'# Project {number}\n'))
            folder = self.base / f'nested-{number}'
            folder.mkdir()
            setup.bind(self.config, folder, note=name)
            self.turn(f'request {number}', f'unique-answer-{number}', cwd=folder)
        for number, name in enumerate(names):
            listing = (self.root / hooks.records_index(name)).read_text()
            self.assertEqual(listing.count('conversation]]'), 1)
            self.assertIn(wiki_target(self.root, name) + '|', listing)
            found = [n['path'] for n in hooks.matching_notes(self.root, name, 'automatic records conversation unique answer')]
            self.assertNotIn(hooks.records_index(names[1 - number]), found)
        self.assert_valid()


class BriefTests(VaultCase):
    def test_cap_marker_names_truncated_file(self):
        profile = self.root / 'memory/profile.md'
        profile.write_text(meta('# Profile\n\n' + 'p' * 2400 + '\n'))
        size = len(profile.read_text())
        text = brain.brief(self.root, self.info['project'])
        self.assertIn(f'[truncated: memory/profile.md shows 1100 of {size} chars; read the file]', text)
        self.assertLessEqual(len(text), 6000)

    def test_untruncated_brief_has_no_marker(self):
        self.assertNotIn('[truncated', brain.brief(self.root, self.info['project']))

    def test_overall_limit_holds_with_marker(self):
        (self.root / 'memory/profile.md').write_text(meta('# Profile\n\n' + 'p' * 900 + '\n'))
        project = self.root / self.info['project']
        project.write_text(project.read_text() + 'q' * 3000 + '\n')
        for limit in (500, 700, 1300, 2500):
            text = brain.brief(self.root, self.info['project'], limit=limit)
            self.assertLessEqual(len(text), limit)
            self.assertIn('[truncated: ', text)
        text = brain.brief(self.root, self.info['project'], limit=700)
        self.assertIn('memory/profile.md shows', text)
        self.assertIn('omitted by brief limit: ' + self.info['project'], text)


class BriefInvariantTests(VaultCase):
    def check_invariants(self, limit, project=None):
        text = brain.brief(self.root, project or self.info['project'], limit=limit)
        self.assertLessEqual(len(text), limit)
        markers = re.findall(r'\[(?:truncated|omitted|brief truncated)[^\]]*\]', text)
        self.assertEqual(len(markers), text.count('[truncated') + text.count('[omitted') + text.count('[brief truncated'),
                         (limit, text[-200:]))
        named = [m for m in markers if m.startswith('[omitted') and m.endswith('; read these files]')]
        omitted = sum((m[len('[omitted by brief limit: '):-len('; read these files]')].split(', ') for m in named), [])
        # Compact markers stand in for names that do not fit the limit.
        compact = any(m.endswith('; read them]') or m.startswith('[brief truncated') for m in markers)
        for relative, body in self.files.items():
            full = '\nFILE: ' + relative + '\n' + body in text
            cut = re.search(re.escape('[truncated: ' + relative + ' shows ') + r'\d+ of ' + str(len(body)) +
                            r' chars; read the file\]', text)
            self.assertTrue(full or cut or relative in omitted or compact, (limit, relative, text[-300:]))

    def test_markers_are_complete_and_limit_holds(self):
        project = self.info['project']
        checkpoint = 'memory/sessions/brief-checkpoint.md'
        for sizes in ((350, 900, 0), (100, 100, 100), (1050, 1700, 1700), (2400, 3000, 50), (450, 40, 2000)):
            self.files = {'memory/profile.md': meta('# Profile\n' + 'p' * sizes[0] + '\n'),
                          project: meta('# Project\n' + 'q' * sizes[1] + '\n')}
            if sizes[2]:
                self.files[project] += f'Latest checkpoint: [[{checkpoint[:-3]}|cp]].\n'
                self.files[checkpoint] = meta('# Checkpoint\n' + 'c' * sizes[2] + '\n')
            for relative, body in self.files.items():
                (self.root / relative).write_text(body)
            if not sizes[2] and (self.root / checkpoint).exists():
                (self.root / checkpoint).unlink()
            for limit in range(500, 3001, 7):
                self.check_invariants(limit)

    def test_long_valid_paths_still_respect_limit(self):
        project = 'memory/projects/' + 'a' * 80 + '/' + 'n' * 120 + '/' + 'b' * 80 + '.md'
        pointers = ['memory/sessions/2026-10-16-' + 'x' * 80 + '-' + 'c' * 32 + '.md',
                    'memory/sessions/2026-10-16-' + 'y' * 80 + '-' + 'd' * 32 + '.md',
                    'memory/sessions/2026-10-16-auto-' + 'e' * 120 + '.md']
        fields = ('Latest checkpoint:', 'Latest handoff:', 'Latest automatic record:')
        for sizes in ((300, 900, 50), (1100, 50, 1700), (20, 20, 20), (2000, 2000, 2000)):
            body = meta('# Project\n' + 'q' * sizes[1] + '\n') + ''.join(
                f'{field} [[{name[:-3]}|entry]].\n' for field, name in zip(fields, pointers))
            self.files = {'memory/profile.md': meta('p' * sizes[0]), project: body}
            self.files.update({name: meta('c' * sizes[2]) for name in pointers})
            for relative, text in self.files.items():
                (self.root / relative).parent.mkdir(parents=True, exist_ok=True)
                (self.root / relative).write_text(text)
            for limit in range(500, 3001, 11):
                self.check_invariants(limit, project)

    def test_reviewer_and_tester_repros(self):
        self.files = {'memory/profile.md': meta('p' * 350), self.info['project']: meta('q' * 900)}
        for relative, body in self.files.items():
            (self.root / relative).write_text(body)
        self.check_invariants(500)
        self.files['memory/profile.md'] = meta('p' * 1080)
        (self.root / 'memory/profile.md').write_text(self.files['memory/profile.md'])
        for limit in range(1150, 1300):
            self.check_invariants(limit)


class GuardTests(VaultCase):
    def test_root_and_project_prefixes_are_rejected(self):
        for argv in (['scan', '--roo', '/tmp'], ['brief', '--proj=x'], ['scan', '--root=/x'], ['brief', '--pro', 'x']):
            with self.assertRaisesRegex(ValueError, 'Routed operations'):
                setup.run_operation(self.config, self.project, None, argv)

    def test_session_state_pruning_keeps_current_session(self):
        automation = self.root / '.brain/automation'
        automation.mkdir(parents=True, exist_ok=True)
        old, recent = automation / 'session-old.json', automation / 'session-recent.json'
        for path in (old, recent):
            path.write_text('{}')
        current = hooks.session_path(self.info, self.event('SessionStart'))
        current.write_text(json.dumps({'project': self.info['project'], 'prompt': 'pending', 'turn': 't'}))
        stale = time.time() - 8 * 86400
        for path in (old, current):
            os.utime(path, (stale, stale))
        hooks.handle(self.event('SessionStart'), self.config)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(current.exists())
        hooks.handle(self.event('Stop', last_assistant_message='Done'), self.config)
        hooks.handle(self.event('Stop', last_assistant_message='Done'), self.config)
        self.assertEqual(len(list((self.root / 'memory/sessions').glob('*-auto-*.md'))), 1)

    def test_home_and_filesystem_root_are_refused(self):
        home = self.base / 'home'
        (home / 'code').mkdir(parents=True)
        with patch.object(Path, 'home', return_value=home):
            for path in (home, Path('/')):
                with self.assertRaisesRegex(ValueError, 'capture every project'):
                    setup.project_root(path)
            with self.assertRaisesRegex(ValueError, 'capture every project'):
                setup.initialize(self.config, home)
            self.assertNotIn(str(home), setup.registry(self.config)['projects'])
            if shutil.which('git'):
                subprocess.run(['git', 'init', '-q', str(home)], check=True)
                # A dotfiles repository in home must not turn home into the project.
                self.assertEqual(setup.project_root(home / 'code'), (home / 'code').resolve())

    def test_finish_ignores_unrelated_broken_notes(self):
        (self.root / 'raw/article.txt').write_text('Original source.\n')
        brain.scan(self.root)
        lease = brain.begin(self.root, 'raw/article.txt')
        output = self.root / 'wiki/sources/article.md'
        output.write_text(meta('# Article\n\nSummary of [[raw/article.txt|the source]].\n'))
        index = self.root / 'index.md'
        index.write_text(index.read_text() + '\n- [[wiki/sources/article]]\n')
        (self.root / 'wiki/topics/broken.md').write_text('# Broken\n\n[[missing/target]]\n')
        self.assertTrue(check(self.root)[0])
        result = brain.finish(self.root, 'raw/article.txt', lease['token'], ['wiki/sources/article.md'])
        self.assertEqual(result['state'], 'processed')

    def test_finish_still_requires_reachable_output(self):
        (self.root / 'raw/article.txt').write_text('Original source.\n')
        brain.scan(self.root)
        lease = brain.begin(self.root, 'raw/article.txt')
        (self.root / 'wiki/sources/orphan.md').write_text(meta('# Orphan\n\n[[raw/article.txt]]\n'))
        with self.assertRaisesRegex(ValueError, 'orphan.md: unreachable'):
            brain.finish(self.root, 'raw/article.txt', lease['token'], ['wiki/sources/orphan.md'])


class StaleAliasTests(VaultCase):
    def test_recreated_vault_replaces_stale_alias_without_rebinding(self):
        second = self.base / 'second'
        second.mkdir()
        old_id = setup.initialize(self.config, second)['vault_id']
        shutil.rmtree(self.root)
        setup.create_root(self.root)  # Recreated at the same path with a new identity.
        with self.assertRaisesRegex(ValueError, 'init --existing ' + str(self.root)):
            setup.context(self.config, self.project)
        info = setup.initialize(self.config, self.project, mode='existing', path=self.root)
        data = setup.registry(self.config)
        self.assertEqual([v['root'] for v in data['vaults'].values()], [str(self.root)])
        self.assertEqual(data['default'], info['vault'])
        self.assertNotEqual(info['vault_id'], old_id)
        self.assertTrue((self.root / info['project']).is_file())
        self.assertEqual(data['projects'][str(second.resolve())]['vault_id'], old_id)
        with self.assertRaisesRegex(ValueError, 'unregistered vault.*init --existing'):
            setup.context(self.config, second)


class GeneratedAliasStaleTests(VaultCase):
    def test_recreated_vault_under_generated_alias(self):
        other_root = self.base / 'vault-two'
        second = self.base / 'second'
        second.mkdir()
        first = setup.initialize(self.config, second, mode='new', path=other_root)
        self.assertTrue(first['vault'].startswith('vault-'))
        shutil.rmtree(other_root)
        setup.create_root(other_root)
        info = setup.initialize(self.config, second, mode='existing', path=other_root)
        data = setup.registry(self.config)
        self.assertEqual(info['vault'], first['vault'])
        self.assertNotEqual(info['vault_id'], first['vault_id'])
        self.assertEqual(data['vaults']['main'], {'id': self.info['vault_id'], 'root': str(self.root)})
        self.assertEqual(data['projects'][str(self.project.resolve())]['vault_id'], self.info['vault_id'])
        self.assertEqual(setup.context(self.config, self.project)['vault'], 'main')


class RoundTripTests(VaultCase):
    def test_snapshot_and_restore(self):
        self.turn('Snapshot task', 'Snapshot result')
        archive = self.base / 'snap/brain.zip'
        made = brain.snapshot(self.root, archive)
        self.assertGreater(made['files'], 5)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            brain.snapshot(self.root, archive)
        destination = self.base / 'restored'
        with redirect_stderr(io.StringIO()), patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(brain.main(['restore', str(archive), '--destination', str(destination)]), 0)
        self.assertEqual(json.loads(out.getvalue())['files'], made['files'])
        for path in self.root.rglob('*.md'):
            self.assertEqual(path.read_bytes(), (destination / path.relative_to(self.root)).read_bytes())
        self.assertFalse((destination / '.brain').exists())
        self.assert_valid(destination)
        with self.assertRaisesRegex(ValueError, 'new destination'):
            brain.restore(archive, destination)

    def test_scan_begin_finish_ingestion(self):
        (self.root / 'raw/notes.txt').write_text('Raw evidence.\n')
        state = brain.scan(self.root)
        self.assertEqual([r['state'] for r in state['sources']], ['pending'])
        self.assertFalse(state['complete'])
        lease = brain.begin(self.root, 'raw/notes.txt')
        with self.assertRaisesRegex(ValueError, 'Processing lease'):
            brain.begin(self.root, 'raw/notes.txt')
        (self.root / 'wiki/sources/notes.md').write_text(meta('# Notes\n\nFrom [[raw/notes.txt]].\n'))
        index = self.root / 'index.md'
        index.write_text(index.read_text() + '\n- [[wiki/sources/notes]]\n')
        self.assertEqual(brain.finish(self.root, 'raw/notes.txt', lease['token'], ['wiki/sources/notes.md'])['state'], 'processed')
        self.assertTrue(brain.coverage(self.root)['complete'])
        self.assertEqual(brain.begin(self.root, 'raw/notes.txt')['state'], 'already_processed')
        (self.root / 'wiki/sources/notes.md').write_text(meta('# Notes\n\nEdited [[raw/notes.txt]].\n'))
        self.assertEqual(brain.coverage(self.root)['sources'][0]['state'], 'needs_review')

    def test_checkpoint_and_consolidate(self):
        saved = brain.checkpoint(self.root, self.info['project'], 'Chose SQLite.', 'Add migrations.', ['memory/profile.md'])
        target = self.root / 'memory/profile.md'
        sha = brain.digest(target.read_bytes())
        args = (self.root, saved['path'], 'memory/profile.md', 'User prefers SQLite.', [saved['path']], sha)
        applied = brain.consolidate(*args)
        self.assertEqual(applied['state'], 'applied')
        self.assertIn('User prefers SQLite.', target.read_text())
        self.assertEqual(brain.consolidate(*args)['state'], 'already_applied')
        with self.assertRaisesRegex(ValueError, 'changed'):
            brain.consolidate(self.root, saved['path'], 'memory/profile.md', 'Other text.', [saved['path']], sha)
        self.assert_valid()

    def test_doctor_reports_review_candidates(self):
        body = '# Same\n\n' + 'Identical body text for duplicate detection in the doctor. ' * 3 + '\n'
        index = self.root / 'index.md'
        for name in ('a', 'b'):
            (self.root / f'wiki/topics/{name}.md').write_text(meta(body).replace('status: active', 'status: active\nclaim_key: db\nclaim_value: ' + name))
            index.write_text(index.read_text() + f'\n- [[wiki/topics/{name}]]\n')
        report = brain.doctor(self.root)
        kinds = {f['kind'] for f in report['findings']}
        self.assertEqual(report['as_of'], brain.day())
        self.assertIn('duplicate_candidate', kinds)
        self.assertIn('conflicting_claims', kinds)
        self.assertNotIn('structural', kinds)
        with redirect_stderr(io.StringIO()), patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(brain.main(['--root', str(self.root), 'doctor']), 0)
        self.assertIn('duplicate_candidate', out.getvalue())


if __name__ == '__main__':
    unittest.main()
