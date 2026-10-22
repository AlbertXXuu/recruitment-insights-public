"""Offline invitation account provisioning, using only temporary databases."""
import contextlib
import getpass
import hashlib
import io
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app
from core import Store
from recruitment_insights.config import DEMO_PASSWORD
from scripts import provision_test_account as provision


PASSWORD = 'private-invitation-account-2026!'
OTHER_PASSWORD = 'other-private-account-2026!'


class MaskedPasswordTests(unittest.TestCase):
    def read_keys(self, keys, *, input_tty=True, output_tty=True):
        output = io.StringIO()
        output.isatty = lambda: output_tty
        console = Mock(getwch=Mock(side_effect=keys))
        with patch.object(provision.sys, 'platform', 'win32'), \
                patch.object(provision.sys, 'stdin', Mock(isatty=Mock(return_value=input_tty))), \
                patch.object(provision.sys, 'stdout', output), \
                patch.dict(sys.modules, {'msvcrt': console}):
            password = provision.read_hidden_password('密码：')
        return password, output.getvalue(), console

    def test_windows_password_characters_are_replaced_with_stars(self):
        password, output, _ = self.read_keys(list('private密码 2026!') + ['\r'])
        self.assertEqual(password, 'private密码 2026!')
        self.assertEqual(output, '密码：' + '*' * len(password) + '\n')
        self.assertNotIn(password, output)

    def test_backspace_only_removes_existing_password_character(self):
        password, output, _ = self.read_keys(['\b', 'a', 'b', '\b', '中', '\r'])
        self.assertEqual(password, 'a中')
        self.assertEqual(output, '密码：**\b \b*\n')

    def test_windows_utf16_surrogate_pair_is_one_character_and_one_star(self):
        password, output, _ = self.read_keys(['A', '\ud83d', '\ude00', 'B', '\r'])
        self.assertEqual(password, 'A😀B')
        self.assertEqual(output, '密码：***\n')

    def test_backspace_removes_entire_surrogate_pair_character(self):
        password, output, _ = self.read_keys(['A', '\ud83d', '\ude00', '\b', 'B', '\r'])
        self.assertEqual(password, 'AB')
        self.assertEqual(output, '密码：**\b \b*\n')

    def test_invalid_utf16_surrogate_input_fails_instead_of_altering_password(self):
        for keys in (['A', '\ude00'], ['A', '\ud83d', 'B'], ['A', '\ud83d', ''],
                     ['A', '\ud83d', '\ud83d']):
            with self.subTest(keys=repr(keys)):
                with self.assertRaises(OSError):
                    self.read_keys(keys)

    def test_special_keys_and_control_keys_do_not_become_password_characters(self):
        password, output, console = self.read_keys(
            ['a', '\x00', 'H', '\xe0', 'S', '\t', '\x1b', 'b', '\n'])
        self.assertEqual(password, 'ab')
        self.assertEqual(output, '密码：**\n')
        self.assertEqual(console.getwch.call_count, 9)

    def test_windows_cancel_and_eof_do_not_return_partial_password(self):
        for key in ('\x03', '\x04', '\x1a', '', KeyboardInterrupt(), EOFError()):
            with self.subTest(key=repr(key)):
                with self.assertRaises((KeyboardInterrupt, EOFError)):
                    self.read_keys(['a', key])

    def test_windows_requires_both_terminal_input_and_output_before_reading(self):
        for input_tty, output_tty in ((False, True), (True, False), (False, False)):
            with self.subTest(input_tty=input_tty, output_tty=output_tty):
                output = io.StringIO()
                output.isatty = lambda: output_tty
                console = Mock()
                with patch.object(provision.sys, 'platform', 'win32'), \
                        patch.object(provision.sys, 'stdin', Mock(isatty=Mock(return_value=input_tty))), \
                        patch.object(provision.sys, 'stdout', output), \
                        patch.dict(sys.modules, {'msvcrt': console}), \
                        self.assertRaises(OSError):
                    provision.read_hidden_password('密码：')
                console.getwch.assert_not_called()
                self.assertEqual(output.getvalue(), '')

    def test_windows_console_read_failure_does_not_fall_back_to_echoed_input(self):
        with patch.object(provision.getpass, 'getpass') as fallback:
            with self.assertRaises(OSError):
                self.read_keys([OSError('console unavailable')])
        fallback.assert_not_called()

    def test_non_windows_preserves_getpass_input(self):
        with patch.object(provision.sys, 'platform', 'linux'), \
                patch.object(provision.getpass, 'getpass', return_value=PASSWORD) as hidden:
            self.assertEqual(provision.read_hidden_password('密码：'), PASSWORD)
        hidden.assert_called_once_with('密码：')


class InvitationAccountTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='invitation-accounts-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / 'accounts.sqlite3'

    def arguments(self, *, database=None, username='new_invite', display_name='邀请账号',
                  role=None, organization=None):
        arguments = ['--db', str(database or self.database), '--username', username,
                     '--display-name', display_name]
        if role is not None:
            arguments.extend(('--role', role))
        if organization is not None:
            arguments.extend(('--organization', organization))
        return arguments

    def run_success(self, arguments, password=PASSWORD):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(provision.sys, 'stdin', Mock(isatty=Mock(return_value=True))), \
                patch.object(provision, 'read_hidden_password', side_effect=[password, password]) as hidden, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            user_id = provision.main(arguments)
        self.assertEqual(hidden.call_count, 2)
        self.assertNotIn(password, stdout.getvalue() + stderr.getvalue())
        self.assertEqual(stderr.getvalue(), '')
        return user_id, stdout.getvalue()

    def assert_invalid(self, arguments, *, passwords=None, tty=True, effect=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(provision.sys, 'stdin', Mock(isatty=Mock(return_value=tty))), \
                patch.object(provision, 'read_hidden_password',
                             side_effect=effect or passwords or [PASSWORD, PASSWORD]) as hidden, \
                patch.object(provision, 'Store') as constructor, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as exit_context:
                provision.main(arguments)
            self.assertEqual(exit_context.exception.code, 2)
            constructor.assert_not_called()
        output = stdout.getvalue() + stderr.getvalue()
        for secret in (PASSWORD, OTHER_PASSWORD, DEMO_PASSWORD):
            self.assertNotIn(secret, output)
        return output, hidden.call_count

    def snapshot(self):
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.root.iterdir() if path.is_file()}

    def test_default_candidate_admin_and_boss_can_login_with_independent_password(self):
        for role in ('candidate', 'admin', 'boss'):
            with self.subTest(role=role):
                database = self.root / (role + '.sqlite3')
                user_id, output = self.run_success(self.arguments(
                    database=database, username=role if role == 'admin' else 'invite_' + role,
                    display_name='  独立邀请账号  ', role=None if role == 'candidate' else role,
                    organization='  独立测试企业  ' if role == 'boss' else None))
                self.assertIsInstance(user_id, int)
                self.assertIn(role, output)
                self.assertIn(str(database), output)
                store = Store(database)
                token, actor = store.accounts.login(
                    role if role == 'admin' else 'invite_' + role, PASSWORD)
                self.assertEqual(actor['id'], user_id)
                self.assertEqual(actor['role'], role)
                self.assertEqual(actor['display_name'], '独立邀请账号')
                self.assertFalse(actor['can_manage_jobs'])
                self.assertEqual(store.accounts.user(token)['id'], user_id)
                with store.connect() as db:
                    organizations = db.execute('SELECT id,name FROM organizations').fetchall()
                if role == 'boss':
                    self.assertEqual(len(organizations), 1)
                    self.assertEqual(organizations[0]['name'], '独立测试企业')
                    self.assertEqual(actor['org_id'], organizations[0]['id'])
                else:
                    self.assertIsNone(actor['org_id'])
                    self.assertEqual(organizations, [])

    def test_boss_reuses_existing_organization(self):
        store = Store(self.database)
        existing_id = store.accounts.organization('已有独立企业')
        self.run_success(self.arguments(role='boss', organization='已有独立企业'))
        _, actor = store.accounts.login('new_invite', PASSWORD)
        self.assertEqual(actor['org_id'], existing_id)
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM organizations').fetchone()[0], 1)

    def test_duplicate_username_does_not_reset_password_or_create_organization(self):
        store = Store(self.database)
        existing_org = store.accounts.organization('原企业')
        store.accounts.create_user('new_invite', PASSWORD, '原账号', 'boss', existing_org)
        before = self.snapshot()
        output, _ = self.assert_invalid(self.arguments(role='boss', organization='不应创建的新企业'),
                                       passwords=[OTHER_PASSWORD, OTHER_PASSWORD])
        self.assertIn('账号已存在', output)
        self.assertEqual(self.snapshot(), before)
        _, actor = store.accounts.login('new_invite', PASSWORD)
        self.assertEqual(actor['display_name'], '原账号')
        self.assertEqual(actor['org_id'], existing_org)
        with self.assertRaises(ValueError):
            store.accounts.login('new_invite', OTHER_PASSWORD)
        with store.connect() as db:
            self.assertEqual([row['name'] for row in db.execute('SELECT name FROM organizations')], ['原企业'])

    def test_invalid_identity_and_role_parameters_do_not_change_existing_database(self):
        Store(self.database)
        cases = (
            self.arguments(username='ab'), self.arguments(username='bad user'),
            self.arguments(username='x' * 41), self.arguments(display_name='  '),
            self.arguments(display_name='名' * 61), self.arguments(role='hr'),
            self.arguments(role='boss'), self.arguments(role='boss', organization=''),
            self.arguments(role='boss', organization='  '),
            self.arguments(role='boss', organization='名' * 121),
            self.arguments(role='admin', organization='不得创建'),
            self.arguments(organization='不得创建'),
        )
        before = self.snapshot()
        for arguments in cases:
            with self.subTest(arguments=arguments):
                _, prompts = self.assert_invalid(arguments)
                self.assertEqual(prompts, 0)
                self.assertEqual(self.snapshot(), before)

    def test_missing_required_arguments_and_relative_database_fail_before_prompt(self):
        cases = (
            [], ['--username', 'new_invite', '--display-name', '邀请账号'],
            ['--db', str(self.database), '--display-name', '邀请账号'],
            ['--db', str(self.database), '--username', 'new_invite'],
            self.arguments(database=Path('relative.sqlite3')),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                _, prompts = self.assert_invalid(arguments)
                self.assertEqual(prompts, 0)
                self.assertFalse(self.database.exists())

    def test_formal_and_group04_database_paths_rejected_before_prompt(self):
        for database in (
            app.ROOT / 'data' / 'recruitment.sqlite3',
            app.ROOT / 'deliverables' / 'team-handoff' / '04_组员测试版' / 'data' / 'recruitment.sqlite3',
        ):
            with self.subTest(database=database):
                _, prompts = self.assert_invalid(self.arguments(database=database))
                self.assertEqual(prompts, 0)

    def test_invalid_passwords_leave_new_database_and_parent_uncreated(self):
        database = self.root / 'must-stay-absent' / 'accounts.sqlite3'
        for passwords in (
            [PASSWORD, OTHER_PASSWORD], ['shortpass', 'shortpass'],
            ['x' * 129, 'x' * 129], [DEMO_PASSWORD, DEMO_PASSWORD],
        ):
            with self.subTest(reason=('mismatch' if passwords[0] != passwords[1] else len(passwords[0]))):
                output, prompts = self.assert_invalid(
                    self.arguments(database=database, role='boss', organization='不能留下孤立企业'),
                    passwords=passwords)
                self.assertEqual(prompts, 2)
                self.assertNotIn(passwords[0], output)
                self.assertNotIn(passwords[1], output)
                self.assertFalse(database.parent.exists())

    def test_invalid_password_does_not_change_existing_database(self):
        store = Store(self.database)
        store.accounts.organization('已有企业')
        before = self.snapshot()
        self.assert_invalid(self.arguments(role='boss', organization='不得创建的新企业'),
                            passwords=[PASSWORD, OTHER_PASSWORD])
        self.assertEqual(self.snapshot(), before)

    def test_unknown_existing_schema_rejected_without_migration(self):
        with contextlib.closing(sqlite3.connect(self.database)) as db, db:
            db.execute('CREATE TABLE unrelated(marker TEXT)')
            db.execute('INSERT INTO unrelated VALUES(?)', ('保留内容',))
        before = self.snapshot()
        _, prompts = self.assert_invalid(self.arguments())
        self.assertEqual(prompts, 0)
        self.assertEqual(self.snapshot(), before)

    def test_existing_public_demo_password_rejected_without_writes(self):
        store = Store(self.database)
        store.accounts.create_user('unrelated_old_account', DEMO_PASSWORD, '原账号')
        before = self.snapshot()
        _, prompts = self.assert_invalid(self.arguments())
        self.assertEqual(prompts, 0)
        self.assertEqual(self.snapshot(), before)

    def test_non_tty_input_rejected_before_reading_any_password(self):
        output, prompts = self.assert_invalid(self.arguments(), tty=False)
        self.assertIn('交互终端', output)
        self.assertEqual(prompts, 0)
        self.assertFalse(self.database.exists())

    def test_actual_getpass_fallback_stops_before_echo_warning_or_reading_secret(self):
        fallback_output = io.StringIO()
        def fallback(prompt):
            return getpass.fallback_getpass(prompt, stream=fallback_output)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(provision.sys, 'platform', 'linux'), \
                patch.object(provision.sys, 'stdin', Mock(isatty=Mock(return_value=True))), \
                patch.object(provision.getpass, 'getpass', side_effect=fallback) as hidden, \
                patch.object(provision, 'Store') as constructor, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as exit_context:
                provision.main(self.arguments())
            self.assertEqual(exit_context.exception.code, 2)
            constructor.assert_not_called()
        output, prompts = stdout.getvalue() + stderr.getvalue(), hidden.call_count
        self.assertIn('无法安全读取密码', output)
        self.assertEqual(prompts, 1)
        self.assertEqual(fallback_output.getvalue(), '')
        self.assertFalse(self.database.exists())

    def test_eof_and_interrupted_password_input_leave_database_uncreated(self):
        for problem in (EOFError(), KeyboardInterrupt()):
            with self.subTest(problem=type(problem).__name__):
                self.assert_invalid(self.arguments(), effect=problem)
                self.assertFalse(self.database.exists())

    def test_windows_masked_input_cancellation_and_console_failure_never_create_database(self):
        database = self.root / 'must-stay-absent' / 'accounts.sqlite3'
        cases = (
            ['p', '\x03'], ['p', '\x1a'], ['p', EOFError()],
            ['p', KeyboardInterrupt()], [OSError('console unavailable')],
            list(PASSWORD) + ['\r', 'p', '\x03'],
            ['p', '\ude00'], ['p', '\ud83d', 'q'],
            list(PASSWORD) + ['\r', 'p', '\ud83d', 'q'],
        )
        for keys in cases:
            with self.subTest(cancellation=repr(keys[-1])):
                stdout, stderr = io.StringIO(), io.StringIO()
                stdout.isatty = lambda: True
                with patch.object(provision.sys, 'platform', 'win32'), \
                        patch.object(provision.sys, 'stdin', Mock(isatty=Mock(return_value=True))), \
                        patch.dict(sys.modules, {'msvcrt': Mock(getwch=Mock(side_effect=keys))}), \
                        patch.object(provision.getpass, 'getpass') as fallback, \
                        patch.object(provision, 'Store') as constructor, \
                        contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as exit_context:
                        provision.main(self.arguments(database=database))
                    self.assertEqual(exit_context.exception.code, 2)
                    fallback.assert_not_called()
                    constructor.assert_not_called()
                self.assertNotIn(PASSWORD, stdout.getvalue() + stderr.getvalue())
                self.assertIn('无法安全读取密码', stderr.getvalue())
                self.assertFalse(database.parent.exists())

    def test_windows_redirected_output_and_unavailable_console_fail_without_password_reads(self):
        for output_tty, console in ((False, Mock()), (True, None)):
            with self.subTest(output_tty=output_tty, console_available=console is not None):
                stdout, stderr = io.StringIO(), io.StringIO()
                stdout.isatty = lambda: output_tty
                with patch.object(provision.sys, 'platform', 'win32'), \
                        patch.object(provision.sys, 'stdin', Mock(isatty=Mock(return_value=True))), \
                        patch.dict(sys.modules, {'msvcrt': console}), \
                        patch.object(provision.getpass, 'getpass') as fallback, \
                        patch.object(provision, 'Store') as constructor, \
                        contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as exit_context:
                        provision.main(self.arguments())
                    self.assertEqual(exit_context.exception.code, 2)
                    fallback.assert_not_called()
                    constructor.assert_not_called()
                if console is not None:
                    console.getwch.assert_not_called()
                self.assertEqual(stdout.getvalue(), '')
                self.assertIn('无法安全读取密码', stderr.getvalue())
                self.assertFalse(self.database.exists())

    def test_command_line_password_is_unsupported_and_not_repeated_in_error(self):
        for extra in (['--password', PASSWORD], ['--password=' + PASSWORD]):
            with self.subTest(style=extra[0].split('=')[0]):
                output, prompts = self.assert_invalid(self.arguments() + extra)
                self.assertIn('隐藏输入', output)
                self.assertEqual(prompts, 0)
                self.assertFalse(self.database.exists())

    def test_script_help_runs_from_another_working_directory(self):
        result = subprocess.run([sys.executable, '-X', 'utf8', str(Path(provision.__file__).resolve()), '--help'],
                                cwd=self.root, capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--db', result.stdout)
        self.assertIn('--organization', result.stdout)
        self.assertNotIn('--password', result.stdout)

    def test_real_redirected_input_is_rejected_without_echo_or_database(self):
        result = subprocess.run(
            [sys.executable, '-X', 'utf8', str(Path(provision.__file__).resolve())] + self.arguments(),
            cwd=self.root, input=PASSWORD + '\n' + PASSWORD + '\n', capture_output=True,
            text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(PASSWORD, result.stdout + result.stderr)
        self.assertIn('交互终端', result.stderr)
        self.assertFalse(self.database.exists())


if __name__ == '__main__':
    unittest.main()
