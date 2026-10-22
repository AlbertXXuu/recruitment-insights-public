"""Persistent public registration policy; only temporary files and test listeners."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import Store
from recruitment_insights.web.routes import Routes
from scripts import invitation_console as console


ORIGIN = 'https://registration.example.test'
PASSWORD = 'Isolated-registration-fixture-2026!'
VERSIONS = {'python': '3.12.14', 'sqlite': '3.53.1'}


class InvitationRegistrationPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='invitation-registration-policy-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / 'web-policy.json'
        runtime = patch.object(console, 'RUNTIME_DIRECTORY', self.root)
        runtime.start()
        self.addCleanup(runtime.stop)
        # No connector construction may inspect a real runtime or credential file.
        tunnel = patch.object(console, 'InvitationTunnel')
        factory = tunnel.start()
        factory.return_value.running = False
        factory.return_value.stop.return_value = False
        self.addCleanup(tunnel.stop)

    def write_policy(self, allowed):
        self.config.write_text(json.dumps({'allow_candidate_registration': allowed}), encoding='utf-8')

    def controller(self, **kwargs):
        return console.InvitationController(self.root / 'test.sqlite3', ORIGIN, port=0, **kwargs)

    def test_absent_file_keeps_registration_closed_without_creating_files(self):
        self.assertFalse(console.load_registration_policy())
        self.assertFalse(self.controller().policy.registration_enabled)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_strict_true_and_false_are_read_again_on_each_console_opening(self):
        for allowed in (True, False, True):
            self.write_policy(allowed)
            before = self.config.read_bytes()
            self.assertIs(console.load_registration_policy(), allowed)
            self.assertEqual(self.config.read_bytes(), before)

    def test_explicit_true_can_register_only_an_unprivileged_candidate(self):
        self.write_policy(True)
        controller = self.controller(allow_candidate_registration=console.load_registration_policy())
        store = Store(controller.database)
        routes = Routes(store, controller.policy)
        response = routes.post('/api/auth/register', dict(
            username='web_candidate', password=PASSWORD, display_name='网页求职者',
            role='admin', org_id=999, can_manage_jobs=True, can_manage=True), '')
        self.assertEqual(response.status, 201)
        _, actor = store.accounts.login('web_candidate', PASSWORD)
        self.assertEqual(actor['role'], 'candidate')
        self.assertIsNone(actor['org_id'])
        self.assertFalse(actor['can_manage_jobs'])
        self.assertNotIn('password_hash', actor)
        html = routes.login_page().decode('utf-8')
        self.assertNotIn('data-registration="disabled"', html)
        self.assertNotIn('demo-accounts', html)

    def test_explicit_false_rejects_registration_without_creating_user(self):
        self.write_policy(False)
        controller = self.controller(allow_candidate_registration=console.load_registration_policy())
        store = Store(controller.database)
        routes = Routes(store, controller.policy)
        response = routes.post('/api/auth/register', dict(
            username='closed_candidate', password=PASSWORD, display_name='禁止创建'), '')
        self.assertEqual(response.status, 403)
        with store.uow() as tx:
            self.assertIsNone(tx.accounts.by_username('closed_candidate'))
        self.assertIn('data-registration="disabled"', routes.login_page().decode('utf-8'))

    def test_invalid_json_shapes_unknown_fields_and_non_boolean_values_are_rejected(self):
        for value in (None, [], 'true', {}, {'unknown': True},
                      {'allow_candidate_registration': True, 'unknown': False},
                      *({'allow_candidate_registration': bad} for bad in (None, 0, 1, 'true', [], {}))):
            with self.subTest(value=value):
                self.config.write_text(json.dumps(value), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, '普通端注册配置'):
                    console.load_registration_policy()
        for raw in (b'{broken', b'\xff\xfe\x00'):
            with self.subTest(raw=raw):
                self.config.write_bytes(raw)
                with self.assertRaisesRegex(ValueError, 'UTF-8 JSON'):
                    console.load_registration_policy()

    def test_byte_limit_accepts_exactly_4096_and_rejects_more(self):
        raw = b'{"allow_candidate_registration":true}'
        self.config.write_bytes(raw + b' ' * (4096 - len(raw)))
        self.assertTrue(console.load_registration_policy())
        self.config.write_bytes(self.config.read_bytes() + b' ')
        with self.assertRaisesRegex(ValueError, '4096'):
            console.load_registration_policy()

    def test_directory_and_shared_file_paths_are_rejected(self):
        self.config.mkdir()
        with self.assertRaisesRegex(ValueError, '普通文件'):
            console.load_registration_policy()
        self.config.rmdir()
        original = self.root / 'shared.json'
        original.write_text('{"allow_candidate_registration":true}', encoding='utf-8')
        os.link(original, self.config)
        with self.assertRaisesRegex(ValueError, '链接或共享文件'):
            console.load_registration_policy()

    def test_reject_links_runs_even_when_the_policy_file_is_absent(self):
        with patch.object(console, 'reject_links', side_effect=ValueError('不安全路径')) as reject:
            with self.assertRaisesRegex(ValueError, '不安全路径'):
                console.load_registration_policy()
        reject.assert_called_once_with(self.config)
        self.assertFalse(self.config.exists())

    def test_read_failure_has_a_clear_chinese_startup_error(self):
        self.write_policy(True)
        with patch.object(Path, 'open', side_effect=PermissionError('denied')):
            with self.assertRaisesRegex(ValueError, '无法读取普通端注册配置'):
                console.load_registration_policy()

    def test_main_passes_persisted_policy_to_controller_without_command_line_flags(self):
        self.write_policy(True)
        with patch.object(console, 'validate_runtime', return_value=VERSIONS), \
                patch.object(console.sys.stdin, 'isatty', return_value=True), \
                patch.object(console, 'InvitationController') as controller, \
                patch.object(console, 'ConsoleLock'), \
                patch.object(console, 'RotatingFileHandler'), \
                patch.object(console.logging, 'getLogger'), \
                patch.object(console, 'run_console') as menu:
            self.assertEqual(console.main(['--start-services']), 0)
        controller.assert_called_once_with(str(self.root / 'test.sqlite3'), console.TEST_ORIGIN, 8775,
                                           allow_candidate_registration=True)
        self.assertTrue(menu.call_args.kwargs['start_services'])

    def test_bad_configuration_fails_before_controller_lock_or_database(self):
        self.config.write_text('{"allow_candidate_registration":"true"}', encoding='utf-8')
        output = io.StringIO()
        with patch.object(console, 'validate_runtime', return_value=VERSIONS), \
                patch.object(console.sys.stdin, 'isatty', return_value=True), \
                patch.object(console, 'InvitationController') as controller, \
                patch.object(console, 'ConsoleLock') as lock, \
                patch.object(console, 'Store') as store, contextlib.redirect_stderr(output):
            self.assertEqual(console.main([]), 1)
        controller.assert_not_called()
        lock.assert_not_called()
        store.assert_not_called()
        self.assertIn('控制台未打开：普通端注册配置', output.getvalue())

    def test_runtime_probe_returns_before_reading_policy_or_constructing_controller(self):
        self.config.write_bytes(b'invalid configuration')
        with patch.object(console, 'validate_runtime', return_value=VERSIONS), \
                patch.object(console, 'load_registration_policy') as load, \
                patch.object(console, 'InvitationController') as controller, \
                patch.object(console, 'ConsoleLock') as lock, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(console.main(['--check-runtime']), 0)
        load.assert_not_called()
        controller.assert_not_called()
        lock.assert_not_called()

    def test_controller_source_restart_keeps_the_loaded_registration_policy(self):
        self.write_policy(True)
        controller = self.controller(allow_candidate_registration=console.load_registration_policy())
        store = Store(controller.database)
        store.accounts.create_user('console_admin', PASSWORD, '隔离管理员', role='admin')
        self.addCleanup(controller.stop)
        with patch.object(console, 'validate_runtime'):
            for _ in range(2):
                self.assertTrue(controller.start())
                self.assertTrue(controller.policy.registration_enabled)
                self.assertTrue(controller.running)
                self.assertTrue(controller.stop())
                self.assertFalse(controller.running)
        reopened = self.controller(allow_candidate_registration=console.load_registration_policy())
        self.assertTrue(reopened.policy.registration_enabled)

    def test_console_shows_the_loaded_registration_state_as_static_text(self):
        for allowed, label in ((True, '开启'), (False, '关闭')):
            output = []
            controller = self.controller(allow_candidate_registration=allowed)
            console.run_console(controller, input_fn=lambda _: '0', output=output.append)
            self.assertIn('本实例普通端注册：' + label, '\n'.join(output))


if __name__ == '__main__':
    unittest.main()
