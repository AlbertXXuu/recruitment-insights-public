"""Invitation console lifecycles, using only temporary databases and real sockets.

Runtime support is tested separately from these socket regressions. No Cloudflare
configuration, public tunnel, formal database, or existing team database is used.
"""
import contextlib
import hashlib
import io
import json
import socket
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import Future
from http.client import HTTPConnection
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app
from core import Store
from recruitment_insights.config import DEMO_PASSWORD
from recruitment_insights.web.routes import Response
from scripts import invitation_console as console


ORIGIN = 'https://console.example.test'
HOST = 'console.example.test'
PASSWORD = 'isolated-console-fixture-2026!'
WAIT = 5


class InvitationControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='invitation-console-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / 'invitation.sqlite3'
        self.controllers = []
        self.addCleanup(self.stop_controllers)
        # The selected supported runtime is verified by the launcher probe;
        # transport behavior remains testable on older course CI runtimes.
        runtime = patch.object(console, 'validate_runtime')
        runtime.start()
        self.addCleanup(runtime.stop)

    def stop_controllers(self):
        for controller in reversed(self.controllers):
            controller.stop()

    def controller(self, *, database=None, port=0):
        controller = console.InvitationController(
            database or self.database, ORIGIN, port=port, socket_timeout=0.3)
        self.controllers.append(controller)
        return controller

    def create_admin(self, *, password=PASSWORD):
        store = Store(self.database)
        store.accounts.create_user('console_admin', password, '控制台测试管理员', role='admin')
        return store

    def files(self):
        return {path.relative_to(self.root).as_posix():
                hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.root.rglob('*') if path.is_file()}

    @staticmethod
    def request(controller, path='/api/health', *, body=None, cookie=None, host=HOST):
        server = controller.server
        connection = HTTPConnection('127.0.0.1', server.server_port, timeout=WAIT)
        headers = {'Host': host}
        payload = None
        if cookie:
            headers['Cookie'] = cookie
        if body is not None:
            payload = json.dumps(body).encode('utf-8')
            headers.update({'Origin': ORIGIN, 'Content-Type': 'application/json',
                            'X-Local-Request': '1'})
        try:
            connection.request('POST' if body is not None else 'GET', path,
                               body=payload, headers=headers)
            response = connection.getresponse()
            return response.status, response.getheaders(), response.read()
        finally:
            connection.close()

    def assert_stopped(self, controller, *, previous_thread=None, previous_server=None):
        self.assertFalse(controller.running)
        if previous_thread is not None:
            self.assertFalse(previous_thread.is_alive(), 'Listener survived a normal stop')
        if previous_server is not None:
            self.assertEqual(previous_server.socket.fileno(), -1,
                             'Stopped listener still owns its listening socket')

    def test_missing_database_rejected_without_creating_a_directory_or_database(self):
        missing = self.root / 'never-created' / 'invitation.sqlite3'
        controller = self.controller(database=missing)
        with self.assertRaises(ValueError):
            controller.start()
        self.assert_stopped(controller)
        self.assertFalse(missing.parent.exists())
        self.assertEqual(self.files(), {})

    def test_existing_current_database_requires_admin_without_modifying_it(self):
        store = Store(self.database)
        store.accounts.create_user('candidate_only', PASSWORD, '只有普通账号')
        before = self.files()
        controller = self.controller()
        with self.assertRaises(ValueError):
            controller.start()
        self.assert_stopped(controller)
        self.assertEqual(self.files(), before)

    def test_protected_database_paths_rejected_without_accessing_real_project_data(self):
        with patch.object(app, 'ROOT', self.root):
            for database in (
                self.root / 'data' / 'recruitment.sqlite3',
                self.root / 'deliverables' / 'team-handoff' /
                '04_组员测试版' / 'data' / 'recruitment.sqlite3',
            ):
                with self.subTest(database=database):
                    controller = self.controller(database=database)
                    with self.assertRaises(ValueError):
                        controller.start()
                    self.assert_stopped(controller)
                    self.assertFalse(database.exists())
        self.assertEqual(self.files(), {})

    def test_legacy_database_rejected_without_automatic_migration(self):
        with contextlib.closing(sqlite3.connect(self.database)) as db, db:
            db.execute('CREATE TABLE users(username TEXT, role TEXT)')
            db.execute('INSERT INTO users VALUES(?,?)', ('legacy_admin', 'admin'))
        before = self.files()
        controller = self.controller()
        with self.assertRaises(ValueError):
            controller.start()
        self.assert_stopped(controller)
        self.assertEqual(self.files(), before)

    def test_public_demo_password_rejected_before_opening_a_listener(self):
        self.create_admin(password=DEMO_PASSWORD)
        before = self.files()
        controller = self.controller()
        with self.assertRaises(ValueError):
            controller.start()
        self.assert_stopped(controller)
        self.assertEqual(self.files(), before)

    def test_real_http_duplicate_start_and_idempotent_stop(self):
        self.create_admin()
        controller = self.controller()
        self.assertTrue(controller.start())
        first_server, first_thread = controller.server, controller.thread
        self.assertTrue(controller.running)
        self.assertEqual(first_server.server_address[0], '127.0.0.1')
        status, _, body = self.request(controller)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)['status'], 'ok')
        self.assertFalse(controller.start())
        self.assertIs(controller.server, first_server)
        self.assertIs(controller.thread, first_thread)
        self.assertEqual(self.request(controller, host=f'localhost:{first_server.server_port}')[0], 403)
        self.assertTrue(controller.stop())
        self.assert_stopped(controller, previous_thread=first_thread, previous_server=first_server)
        self.assertFalse(controller.stop())

    def test_login_session_and_accounts_survive_stop_and_a_new_controller(self):
        self.create_admin()
        first = self.controller()
        first.start()
        status, headers, body = self.request(first, '/api/auth/login', body={
            'username': 'console_admin', 'password': PASSWORD})
        self.assertEqual(status, 200, body)
        cookies = SimpleCookie()
        for name, value in headers:
            if name.lower() == 'set-cookie':
                cookies.load(value)
        name = '__Host-recruitment_insights_test_session'
        self.assertIn(name, cookies)
        cookie = f'{name}={cookies[name].value}'
        self.assertEqual(json.loads(body)['role'], 'admin')
        first_thread, first_server = first.thread, first.server
        self.assertTrue(first.stop())
        self.assert_stopped(first, previous_thread=first_thread, previous_server=first_server)
        self.assertFalse(Path(str(self.database) + '-wal').exists())
        self.assertFalse(Path(str(self.database) + '-shm').exists())
        second = self.controller()
        self.assertTrue(second.start())
        status, _, body = self.request(second, '/api/auth/me', cookie=cookie)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)['username'], 'console_admin')
        status, _, body = self.request(second, '/api/auth/login', body={
            'username': 'console_admin', 'password': PASSWORD})
        self.assertEqual(status, 200, body)

    def test_port_conflict_leaves_no_listener_and_existing_database_unchanged(self):
        self.create_admin()
        before = self.files()
        with socket.socket() as blocker:
            blocker.bind(('127.0.0.1', 0))
            blocker.listen()
            controller = self.controller(port=blocker.getsockname()[1])
            with self.assertRaises(OSError):
                controller.start()
            self.assert_stopped(controller)
            self.assertTrue(controller.thread is None or not controller.thread.is_alive())
            self.assertEqual(self.files(), before)
        self.assertTrue(controller.start(), 'Port conflict poisoned a later retry')
        self.assertEqual(self.request(controller)[0], 200)

    def test_listener_thread_start_failure_closes_socket_and_allows_retry(self):
        self.create_admin()
        controller = self.controller()
        with patch.object(console.threading.Thread, 'start', side_effect=RuntimeError('thread start failed')):
            with self.assertRaises(RuntimeError):
                controller.start()
        self.assert_stopped(controller)
        self.assertTrue(controller.thread is None or not controller.thread.is_alive())
        self.assertTrue(controller.start())
        self.assertEqual(self.request(controller)[0], 200)

    def test_interruption_after_listener_thread_started_drains_it_before_retry(self):
        self.create_admin()
        controller = self.controller()
        launched = []
        native_start = threading.Thread.start
        def start_then_interrupt(thread):
            native_start(thread)
            launched.append(thread)
            raise KeyboardInterrupt('interruption after actual thread start')
        with patch.object(console.threading.Thread, 'start', new=start_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                controller.start()
        self.assertEqual(len(launched), 1)
        self.assertFalse(launched[0].is_alive(), 'Interrupted start left an unowned listener')
        self.assert_stopped(controller)
        self.assertTrue(controller.start())
        self.assertEqual(self.request(controller)[0], 200)

    def test_menu_start_status_exit_uses_real_listener_and_stops_it(self):
        self.create_admin()
        controller = self.controller()
        states, threads, messages = [], [], []
        choices = iter(('1', '3', '0'))
        def answer(prompt):
            states.append(controller.running)
            if controller.running:
                threads.append(controller.thread)
                self.assertEqual(self.request(controller)[0], 200)
            return next(choices)
        console.run_console(controller, input_fn=answer, output=messages.append)
        self.assertEqual(states, [False, True, True])
        self.assertTrue(messages)
        self.assert_stopped(controller)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertNotIn(PASSWORD, '\n'.join(messages))

    def test_menu_explains_application_login_and_pending_https_business_acceptance(self):
        self.create_admin()
        controller = self.controller()
        choices = iter(('1', '3', '6', '7', '0'))
        messages = []
        with patch.object(console.webbrowser, 'open') as open_browser, \
                patch.object(controller, 'start_tunnel', return_value=True):
            console.run_console(controller, input_fn=lambda _: next(choices), output=messages.append)
        text = '\n'.join(messages)
        self.assertIn('应用账号登录', text)
        self.assertIn('HTTPS、应用账号登录和业务流程仍须实测', text)
        self.assertNotIn('Access', text)
        self.assertIn('已有账号无需重建。公网可用性以实际网址为准', text)
        self.assertIn('7 启动或检查 Tunnel    8 正常停止 Tunnel', text)
        open_browser.assert_called_once_with(ORIGIN)
        self.assert_stopped(controller)

    def test_optional_start_services_keeps_the_interactive_menu_and_stops_normally(self):
        self.create_admin()
        controller = self.controller()
        messages, states = [], []
        def answer(prompt):
            states.append(controller.running)
            self.assertEqual(self.request(controller)[0], 200)
            return '0'
        with patch.object(controller, 'start_tunnel', return_value=True) as connect:
            console.run_console(controller, input_fn=answer, output=messages.append, start_services=True)
        connect.assert_called_once_with()
        self.assertEqual(states, [True])
        self.assertIn('源站与 Tunnel 已启动并连接', '\n'.join(messages))
        self.assert_stopped(controller)

    def test_optional_start_failure_returns_to_menu_without_creating_a_database(self):
        controller = self.controller()
        messages = []
        with patch.object(controller, 'start_tunnel') as connect:
            console.run_console(controller, input_fn=lambda _: '0', output=messages.append,
                                start_services=True)
        connect.assert_not_called()
        self.assertIn('自动启动未完成', '\n'.join(messages))
        self.assertFalse(controller.database.exists())
        self.assert_stopped(controller)

    def test_menu_eof_and_keyboard_interrupt_stop_an_active_listener(self):
        self.create_admin()
        for interruption in (EOFError, KeyboardInterrupt):
            with self.subTest(interruption=interruption.__name__):
                controller = self.controller()
                controller.start()
                thread, server = controller.thread, controller.server
                with patch('builtins.input', side_effect=interruption):
                    console.run_console(controller, input_fn=lambda _: input(), output=lambda _: None)
                self.assert_stopped(controller, previous_thread=thread, previous_server=server)

    def test_account_preparation_refuses_running_source_before_reading_inputs(self):
        self.create_admin()
        controller = self.controller()
        controller.start()
        inputs = Mock()
        with patch.object(console.provision_test_account, 'main') as provision:
            with self.assertRaises(ValueError):
                console.prepare_account(controller, inputs, lambda _: None)
            inputs.assert_not_called()
            provision.assert_not_called()
        self.assertTrue(controller.running)
        self.assertEqual(self.request(controller)[0], 200)

    def test_tunnel_cannot_start_before_source_and_missing_setup_preserves_console(self):
        controller = self.controller()
        with patch.object(controller.tunnel, 'start') as start:
            with self.assertRaises(ValueError):
                controller.start_tunnel()
            start.assert_not_called()
        self.create_admin()
        choices = iter(('1', '7', '3', '0'))
        output = []
        console.run_console(controller, input_fn=lambda _: next(choices), output=output.append)
        self.assertTrue(any('未完成' in message for message in output))
        self.assert_stopped(controller)

    def test_connector_stop_failure_still_stops_source_and_retains_retry_ownership(self):
        self.create_admin()
        controller = self.controller()
        controller.start()
        server, listener = controller.server, controller.thread
        connector = Mock(running=True, last_error='未确认退出', process=Mock(pid=12345))
        connector.stop.side_effect = RuntimeError('owned connector PID 12345 still running')
        controller.tunnel = connector
        with self.assertRaisesRegex(RuntimeError, '12345'):
            controller.stop()
        self.assert_stopped(controller, previous_thread=listener, previous_server=server)
        self.assertIs(controller.tunnel, connector)
        with self.assertRaises(ValueError):
            controller.start()
        connector.stop.side_effect = None
        connector.stop.return_value = True
        self.assertTrue(controller.stop())

    def test_normal_stop_closes_owned_connector_before_source_listener(self):
        self.create_admin()
        controller = self.controller()
        controller.start()
        server = controller.server
        steps = []
        connector = Mock(running=True, last_error=None)
        connector.stop.side_effect = lambda: steps.append('connector') or True
        controller.tunnel = connector
        original_shutdown = server.shutdown
        def shutdown():
            steps.append('source')
            return original_shutdown()
        with patch.object(server, 'shutdown', side_effect=shutdown):
            self.assertTrue(controller.stop())
        self.assertEqual(steps, ['connector', 'source'])
        self.assert_stopped(controller)
        connector.stop.side_effect = None
        connector.stop.return_value = False

    def test_eof_with_unstopped_connector_reports_pid_and_still_drains_source(self):
        self.create_admin()
        controller = self.controller()
        controller.start()
        server, listener = controller.server, controller.thread
        connector = Mock(running=True, last_error='未确认退出')
        connector.status.return_value = '进程运行中'
        connector.stop.side_effect = RuntimeError('owned connector PID 12345 did not stop')
        controller.tunnel = connector
        output = []
        with self.assertRaisesRegex(RuntimeError, '12345'):
            console.run_console(controller, input_fn=Mock(side_effect=EOFError), output=output.append)
        self.assert_stopped(controller, previous_thread=listener, previous_server=server)
        self.assertTrue(any('12345' in message for message in output))
        self.assertFalse(any('控制台已退出' in message for message in output))
        connector.stop.side_effect = None
        connector.stop.return_value = False

    def test_exit_with_connector_stop_failure_returns_to_menu_for_retry(self):
        self.create_admin()
        controller = self.controller()
        controller.start()
        connector = Mock(running=True, last_error='未确认退出')
        connector.status.return_value = '进程运行中'
        connector.stop.side_effect = [RuntimeError('owned connector did not stop'), True, False, False]
        controller.tunnel = connector
        choices = iter(('0', '8', '0'))
        states, output = [], []
        def answer(_):
            states.append(controller.running)
            return next(choices)
        console.run_console(controller, input_fn=answer, output=output.append)
        self.assertEqual(states, [True, False, False])
        self.assertEqual(connector.stop.call_count, 4)
        self.assert_stopped(controller)
        self.assertTrue(any('未完成' in message for message in output))
        # Avoid exhausting the fixture during the ordinary controller cleanup.
        connector.stop.side_effect = None
        connector.stop.return_value = False

    def test_stop_waits_for_an_in_flight_real_database_write_to_commit(self):
        store = self.create_admin()
        entered, release, joining = threading.Event(), threading.Event(), threading.Event()
        response, stopped = Future(), Future()
        threads = []
        def cleanup_workers():
            release.set()
            for thread in threads:
                if thread.ident is not None:
                    thread.join(WAIT)
                    self.assertFalse(thread.is_alive(), 'Drain fixture thread remained alive')
        self.addCleanup(cleanup_workers)
        def write(*args):
            with store.connect() as db:
                db.execute('INSERT INTO organizations(name) VALUES(?)', ('停止前已提交企业',))
                entered.set()
                if not release.wait(WAIT):
                    raise AssertionError('Test did not release the active write')
            return Response(200, {'committed': True})
        routes = SimpleNamespace(get=lambda *args: Response(200, {}), post=write)
        controller = self.controller()
        with patch('recruitment_insights.web.server.Routes', return_value=routes):
            controller.start()
        server, listener = controller.server, controller.thread
        def request():
            try:
                response.set_result(self.request(controller, '/write', body={}))
            except BaseException as exc:
                response.set_exception(exc)
        worker = threading.Thread(target=request, name='console-test-write')
        threads.append(worker)
        worker.start()
        self.assertTrue(entered.wait(WAIT), 'Real write did not reach the database')
        original_join = server._threads.join
        def record_join():
            joining.set()
            return original_join()
        def stop():
            try:
                stopped.set_result(controller.stop())
            except BaseException as exc:
                stopped.set_exception(exc)
        with patch.object(server._threads, 'join', side_effect=record_join):
            stopper = threading.Thread(target=stop, name='console-test-stop')
            threads.append(stopper)
            stopper.start()
            self.assertTrue(joining.wait(WAIT), 'Console did not wait for active request workers')
            self.assertFalse(stopped.done(), 'Console returned before an active write completed')
            release.set()
            self.assertTrue(stopped.result(WAIT))
            self.assertEqual(response.result(WAIT)[0], 200)
        self.assert_stopped(controller, previous_thread=listener, previous_server=server)
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT name FROM organizations').fetchall()[0]['name'],
                             '停止前已提交企业')


class ConsoleLockTests(unittest.TestCase):
    def test_duplicate_console_rejected_then_lock_can_be_reacquired(self):
        with tempfile.TemporaryDirectory(prefix='invitation-console-lock-') as temporary:
            directory = Path(temporary)
            with console.ConsoleLock(directory):
                with self.assertRaises((OSError, RuntimeError, ValueError)):
                    with console.ConsoleLock(directory):
                        self.fail('Two controllers acquired the same console lock')
            with console.ConsoleLock(directory):
                pass


class ConsoleRuntimeTests(unittest.TestCase):
    def test_unsupported_python_rejected_before_any_sqlite_connection(self):
        with patch.object(console.sys, 'version_info', (3, 10, 99)), \
                patch.object(console.sqlite3, 'connect') as connection:
            with self.assertRaisesRegex(ValueError, 'Python'):
                console.validate_runtime()
            connection.assert_not_called()

    def test_old_sqlite_rejected_before_any_connection_and_newer_sqlite_accepted(self):
        with patch.object(console.sys, 'version_info', (3, 11, 0)), \
                patch.object(console.sqlite3, 'sqlite_version_info', (3, 51, 2)), \
                patch.object(console.sqlite3, 'sqlite_version', '3.51.2'), \
                patch.object(console.sqlite3, 'connect') as connection:
            with self.assertRaisesRegex(ValueError, '3.51.3'):
                console.validate_runtime()
            connection.assert_not_called()
        native_connect = sqlite3.connect
        for version in ((3, 51, 3), (3, 53, 1)):
            with self.subTest(version=version), \
                    patch.object(console.sys, 'version_info', (3, 11, 0)), \
                    patch.object(console.sqlite3, 'sqlite_version_info', version), \
                    patch.object(console.sqlite3, 'connect', wraps=native_connect) as connection:
                versions = console.validate_runtime()
                self.assertIn('python', versions)
                self.assertIn('sqlite', versions)
                connection.assert_called_once_with(':memory:')

    def test_runtime_probe_does_not_construct_controller_lock_or_create_database(self):
        with tempfile.TemporaryDirectory(prefix='invitation-console-runtime-') as temporary:
            target = Path(temporary) / 'uncreated' / 'invitation.sqlite3'
            output = io.StringIO()
            with patch.object(console, 'InvitationController') as controller, \
                    patch.object(console, 'ConsoleLock') as lock, \
                    patch.object(console, 'Store') as store, \
                    patch.object(console, 'validate_test_database') as preflight, \
                    patch.object(console.sys, 'version_info', (3, 11, 0)), \
                    patch.object(console.sqlite3, 'sqlite_version_info', (3, 53, 1)), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(console.main(['--check-runtime', '--db', str(target)]), 0)
                controller.assert_not_called()
                lock.assert_not_called()
                store.assert_not_called()
                preflight.assert_not_called()
            self.assertFalse(target.parent.exists())
            self.assertIn('运行时通过', output.getvalue())

    def test_manual_invitation_app_start_with_old_sqlite_rejected_before_database_preflight(self):
        with tempfile.TemporaryDirectory(prefix='invitation-app-runtime-') as temporary:
            target = Path(temporary) / 'must-not-be-created' / 'invitation.sqlite3'
            error = io.StringIO()
            with patch.object(app.sys, 'argv', [
                    'app.py', '--test-origin', ORIGIN, '--db', str(target)]), \
                    patch.object(app.sys, 'version_info', (3, 11, 0)), \
                    patch.object(app.sqlite3, 'sqlite_version_info', (3, 51, 2)), \
                    patch.object(app.sqlite3, 'sqlite_version', '3.51.2'), \
                    patch.object(app, 'Store') as store, \
                    patch.object(app, 'make_server') as server, \
                    patch.object(app, 'validate_test_database') as preflight, \
                    contextlib.redirect_stderr(error):
                with self.assertRaises(SystemExit) as result:
                    app.main()
                self.assertEqual(result.exception.code, 2)
                store.assert_not_called()
                server.assert_not_called()
                preflight.assert_not_called()
            self.assertIn('3.51.3', error.getvalue())
            self.assertFalse(target.parent.exists())


if __name__ == '__main__':
    unittest.main()
