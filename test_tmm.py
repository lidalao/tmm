import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from tmm import Failure, Manager, fetch_token, save_state, valid_token


class DockerFixture:
    def __init__(self):
        self.container = None
        self.tokens = []
        self.fail_token = None
        self.offline = False

    def __call__(self, argv, **kwargs):
        code, output = 0, ''
        if self.offline:
            code = 1
        elif argv[1:3] == ['container', 'inspect']:
            if self.container is None:
                code = 1
            else:
                output = json.dumps([self.container])
        elif argv[1:3] == ['container', 'ls']:
            output = 'tm' if self.container else ''
        elif argv[1] == 'compose':
            if argv[-1] != 'tm' or '--no-deps' not in argv:
                raise AssertionError('must target tm only')
            env = kwargs['env']
            token = env['TM_TOKEN']
            self.tokens.append(token)
            # Model a create that succeeds but whose process immediately exits.
            self.container = {
                'Config': {'Labels': {'com.docker.compose.project': 'tm-stack', 'com.docker.compose.service': 'tm'},
                           'Cmd': ['start', 'accept', '--token', token, '--device-name', env['TM_DEVICE_NAME']]},
                'State': {'Running': token != self.fail_token}}
        else:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, code, output, 'sensitive output must not reach logs')


class TmmTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'applied.json'
        self.docker = DockerFixture()
        self.env = {'TM_TOKEN_URL': 'https://example.test/token.txt', 'TM_START_GRACE': '0'}
        self.manager = Manager(self.env, self.path, run=self.docker, sleep=lambda _: None)

    def check(self, token):
        self.manager.check(lambda _: token)

    def test_first_start_unchanged_rotation_and_restart_persistence(self):
        self.check('first')
        self.assertEqual(json.loads(self.path.read_text())['token'], 'first')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.check('first')
        self.assertEqual(self.docker.tokens, ['first'])
        self.check('next')
        self.assertEqual(self.docker.tokens, ['first', 'next'])
        self.assertEqual(json.loads(self.path.read_text())['token'], 'next')
        self.docker.container = None
        restarted = Manager(self.env, self.path, run=self.docker, sleep=lambda _: None)
        restarted.restore()
        self.assertEqual(self.docker.tokens[-1], 'next')

    def test_failed_rotation_restores_old_token_without_saving_new_one(self):
        self.check('old')
        self.docker.fail_token = 'bad'
        before = self.path.read_bytes()
        with self.assertRaises(Failure), self.assertLogs('tmm') as logs:
            self.check('bad')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.docker.tokens, ['old', 'bad', 'old'])
        self.assertTrue(self.docker.container['State']['Running'])
        self.assertNotIn('sensitive output', str(logs.output))

    def test_fetch_failure_never_changes_container_or_state(self):
        self.check('old')
        before = self.path.read_bytes()
        def failed(_):
            raise Failure('download failed')
        with self.assertRaises(Failure):
            self.manager.check(failed)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.docker.tokens, ['old'])

    def test_colliding_unmanaged_container_and_daemon_failure_are_not_missing(self):
        self.check('old')
        self.docker.container['Config']['Labels'] = {}
        with self.assertRaisesRegex(Failure, '不属于'):
            self.check('new')
        self.assertEqual(self.docker.tokens, ['old'])
        self.docker.offline = True
        with self.assertRaisesRegex(Failure, '无法连接'):
            self.check('new')
        self.assertEqual(self.docker.tokens, ['old'])

    def test_crash_after_container_create_reconciles_and_corrupt_state_refused(self):
        self.check('old')
        self.manager.apply({'token': 'next', 'settings': self.manager.settings}, force=True)
        self.check('next')
        self.assertEqual(json.loads(self.path.read_text())['token'], 'next')
        self.path.write_text('{')
        before = len(self.docker.tokens)
        with self.assertRaisesRegex(Failure, '损坏'):
            self.check('another')
        self.assertEqual(len(self.docker.tokens), before)

    def test_failed_state_write_restores_old_configuration(self):
        self.check('old')
        before = self.path.read_bytes()
        with patch('tmm.save_state', side_effect=OSError('disk full')):
            with self.assertRaises(Failure):
                self.check('new')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.docker.tokens[-1], 'old')

    def test_fetch_validates_body_status_and_https(self):
        response = contextlib.closing(io.BytesIO(b'\xef\xbb\xbftoken-123=\r\n'))
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = response
            self.assertEqual(fetch_token(self.env['TM_TOKEN_URL']), 'token-123=')
        for value in ('', 'a\nb', '<html>', '{"token":"x"}', 'x' * 4097, 'a\x00b'):
            with self.subTest(value=value[:20]), self.assertRaises(Failure):
                valid_token(value)
        with self.assertRaises(Failure):
            fetch_token('http://example.test/token')
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = urllib.error.HTTPError('url', 404, 'secret', {}, None)
            with self.assertRaises(Failure) as error:
                fetch_token(self.env['TM_TOKEN_URL'])
            self.assertNotIn('secret', str(error.exception))


if __name__ == '__main__':
    unittest.main()
