"""Candidate registration contracts; temp databases and loopback HTTP only.

Fixed Host/Origin headers represent the HTTPS proxy. No live database, TLS
terminator, public network, production account or Tunnel is used here.
"""
import contextlib
import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app
from core import Store
from recruitment_insights.config import DEMO_PASSWORD, INVITE_SESSION_COOKIE, WebPolicy
from recruitment_insights.web.registration_throttle import RegistrationThrottle
from recruitment_insights.web.routes import Routes
from recruitment_insights.web.server import make_server


ORIGIN = 'https://registration.example.test'
HOST = 'registration.example.test'
PASSWORD = 'Registration-fixture-2026!'
WAIT = 5


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class RegistrationPolicyTests(unittest.TestCase):
    def test_local_registration_is_preserved_external_requires_explicit_opt_in(self):
        self.assertTrue(WebPolicy().registration_enabled)
        self.assertFalse(WebPolicy(test_origin=ORIGIN).registration_enabled)
        opened = WebPolicy(test_origin=ORIGIN, allow_candidate_registration=True)
        self.assertTrue(opened.registration_enabled)
        self.assertTrue(opened.invited)
        self.assertEqual(opened.cookie_name, INVITE_SESSION_COOKIE)
        for invalid in (1, 'true', None, [], {}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                WebPolicy(test_origin=ORIGIN, allow_candidate_registration=invalid)

    def test_cli_registration_flag_without_origin_fails_before_database_initialization(self):
        with tempfile.TemporaryDirectory(prefix='registration-cli-') as temporary:
            database = Path(temporary) / 'must-not-exist.sqlite3'
            with patch('sys.argv', ['app.py', '--db', str(database),
                                    '--allow-candidate-registration']), \
                    patch.object(app, 'Store') as store, \
                    patch.object(app, 'make_server') as server, \
                    contextlib.redirect_stderr(io.StringIO()) as error:
                with self.assertRaises(SystemExit) as raised:
                    app.main()
                self.assertEqual(raised.exception.code, 2)
                self.assertIn('--test-origin', error.getvalue())
                store.assert_not_called()
                server.assert_not_called()
                self.assertFalse(database.exists())


class RegistrationHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='registration-http-')
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / 'registration.sqlite3')
        self.server = self.start_server(WebPolicy(test_origin=ORIGIN,
                                                  allow_candidate_registration=True))

    def start_server(self, policy):
        server = make_server(self.store, 0, policy=policy)
        listener = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01),
                                    name='registration-test-listener', daemon=True)
        listener.start()
        self.addCleanup(self.stop_server, server, listener)
        return server

    @staticmethod
    def stop_server(server, listener):
        server.shutdown()
        server.server_close()
        listener.join(WAIT)
        if listener.is_alive():
            raise AssertionError('Registration test listener did not stop')

    def request(self, path, *, body=None, cookie=None, host=HOST, origin=ORIGIN,
                marker=True, server=None):
        server = server or self.server
        connection = HTTPConnection('127.0.0.1', server.server_port, timeout=WAIT)
        payload = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
        headers = {'Host': host}
        if cookie:
            headers['Cookie'] = cookie
        if body is not None:
            headers['Content-Type'] = 'application/json'
            if marker:
                headers['X-Local-Request'] = '1'
            if origin is not None:
                headers['Origin'] = origin
        try:
            connection.request('POST' if body is not None else 'GET', path,
                               body=payload, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def register(self, username='new_candidate', **overrides):
        body = dict(username=username, password=PASSWORD, display_name='测试求职者')
        body.update(overrides)
        return self.request('/api/auth/register', body=body)

    def rows(self, sql, parameters=()):
        with self.store.connect() as database:
            return [dict(row) for row in database.execute(sql, parameters)]

    def test_external_default_cannot_be_opened_by_query_or_body_fields(self):
        closed = self.start_server(WebPolicy(test_origin=ORIGIN))
        fields = dict(username='forged_open', password=PASSWORD, display_name='不能创建',
                      allow_candidate_registration=True, registration_enabled=True,
                      registration='enabled', invited=False)
        status, _, page = self.request('/login?allow_candidate_registration=true', server=closed)
        self.assertEqual(status, 200)
        self.assertIn(b'data-registration="disabled"', page)
        self.assertEqual(self.request('/api/auth/register', body=fields, server=closed)[0], 403)
        self.assertIn(self.request('/api/auth/register?registration_enabled=true',
                                   body=fields, server=closed)[0], (401, 403, 404))
        self.assertEqual(self.rows('SELECT id FROM users'), [])

    def test_open_external_login_page_exposes_registration_without_demo_credentials(self):
        status, headers, page = self.request('/login')
        self.assertEqual(status, 200)
        html = page.decode('utf-8')
        self.assertNotIn('data-registration="disabled"', html)
        self.assertIn('id="switch-auth"', html)
        self.assertNotRegex(html, r'<button\b[^>]*id="switch-auth"[^>]*\bhidden(?:\s|>)')
        self.assertIn('id="register-form"', html)
        self.assertNotIn('<details class="demo-accounts">', html)
        self.assertNotIn(DEMO_PASSWORD, html)
        self.assertIn('企业账号申请入驻后由管理员审核开通', html)
        self.assertIn('href="/join"', html)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        self.assertEqual(self.register()[0], 201)

    def test_client_cannot_select_privileged_role_organization_or_management_permission(self):
        organization = self.store.accounts.organization('隔离测试企业')
        for role in ('admin', 'boss', 'hr'):
            with self.subTest(role=role):
                status, _, payload = self.register('forged_' + role, role=role,
                    org_id=organization, org_name='隔离测试企业', can_manage=True,
                    can_manage_jobs=1)
                self.assertEqual(status, 201, payload)
        rows = self.rows('SELECT role,org_id,can_manage_jobs FROM users ORDER BY id')
        self.assertEqual(rows, [dict(role='candidate', org_id=None, can_manage_jobs=0)] * 3)

    def test_new_account_login_has_secure_cookie_and_only_candidate_access(self):
        self.assertEqual(self.register()[0], 201)
        status, headers, payload = self.request('/api/auth/login',
            body=dict(username='new_candidate', password=PASSWORD))
        self.assertEqual(status, 200, payload)
        actor = json.loads(payload)
        self.assertEqual(actor['role'], 'candidate')
        self.assertIsNone(actor['org_id'])
        self.assertEqual(actor['can_manage_jobs'], 0)
        self.assertNotIn('password_hash', actor)
        cookies = SimpleCookie()
        cookies.load(headers['Set-Cookie'])
        self.assertEqual(set(cookies), {INVITE_SESSION_COOKIE})
        session = cookies[INVITE_SESSION_COOKIE]
        self.assertTrue(session['secure'])
        self.assertTrue(session['httponly'])
        self.assertEqual(session['samesite'], 'Strict')
        self.assertEqual((session['path'], session['domain']), ('/', ''))
        cookie = INVITE_SESSION_COOKIE + '=' + session.value
        self.assertEqual(self.request('/public', cookie=cookie)[0], 200)
        self.assertEqual(self.request('/api/auth/me', cookie=cookie)[0], 200)
        status, redirect, _ = self.request('/enterprise', cookie=cookie)
        self.assertEqual(status, 303)
        self.assertEqual(redirect['Location'], '/public')
        for path in ('/api/company/jobs', '/api/team', '/api/admin/jobs', '/api/quality'):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, cookie=cookie)[0], 403)
        for path in ('/api/company/jobs', '/api/team', '/api/import', '/api/demo'):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, cookie=cookie, body={})[0], 403)

    def test_invalid_account_fields_are_rejected_without_creating_accounts(self):
        invalid = (dict(username=None), dict(username='ab'), dict(username='a' * 41),
                   dict(username='中文账号'), dict(password=None), dict(password='a' * 9),
                   dict(password='a' * 129), dict(display_name=None),
                   dict(display_name='   '), dict(display_name='名' * 61))
        for fields in invalid:
            with self.subTest(field=next(iter(fields))):
                status, _, payload = self.register(**fields)
                self.assertEqual(status, 400, payload)
                self.assertIn('error', json.loads(payload))
        self.assertEqual(self.rows('SELECT id FROM users'), [])

    def test_valid_boundary_lengths_and_display_name_trimming(self):
        for username, password, display in (('abc', 'a' * 10, '名'),
                                             ('b' * 40, 'b' * 128, '名' * 60)):
            with self.subTest(length=len(username)):
                self.assertEqual(self.register(username, password=password,
                                                display_name=' ' + display + ' ')[0], 201)
        self.assertEqual([row['display_name'] for row in self.rows(
            'SELECT display_name FROM users ORDER BY id')], ['名', '名' * 60])

    def test_duplicate_account_preserves_existing_password_and_user_count(self):
        self.assertEqual(self.register()[0], 201)
        before = self.rows('SELECT * FROM users')
        status, _, payload = self.register(password='A-different-password-2026!')
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(payload)['error'], '账号已存在')
        self.assertEqual(self.rows('SELECT * FROM users'), before)
        self.assertEqual(self.request('/api/auth/login', body=dict(
            username='new_candidate', password=PASSWORD))[0], 200)
        self.assertEqual(self.request('/api/auth/login', body=dict(
            username='new_candidate', password='A-different-password-2026!'))[0], 401)

    def test_passwords_are_individually_salted_and_not_returned(self):
        responses = []
        for username in ('salted_one', 'salted_two'):
            status, _, payload = self.register(username)
            self.assertEqual(status, 201)
            responses.append(payload)
        rows = self.rows('SELECT password_hash FROM users ORDER BY id')
        encoded = [row['password_hash'] for row in rows]
        self.assertNotEqual(encoded[0], encoded[1])
        self.assertNotEqual(encoded[0].split(':')[0], encoded[1].split(':')[0])
        for password_hash in encoded:
            self.assertRegex(password_hash, r'^[0-9a-f]{32}:[0-9a-f]{64}$')
            for payload in responses:
                self.assertNotIn(password_hash.encode(), payload)
                self.assertNotIn(PASSWORD.encode(), payload)
                self.assertNotIn(b'password_hash', payload)

    def test_external_registration_refuses_public_demo_password(self):
        status, _, payload = self.register(password=DEMO_PASSWORD)
        self.assertEqual(status, 400)
        self.assertIn('公开演示密码', json.loads(payload)['error'])
        self.assertEqual(self.rows('SELECT id FROM users'), [])

    def test_external_host_origin_and_request_marker_still_guard_registration(self):
        body = dict(username='protected_user', password=PASSWORD, display_name='测试')
        for options in (dict(host='evil.example.test'), dict(origin='https://evil.example.test'),
                        dict(origin=None), dict(marker=False)):
            with self.subTest(options=options):
                self.assertEqual(self.request('/api/auth/register', body=body, **options)[0], 403)
        self.assertEqual(self.rows('SELECT id FROM users'), [])
        self.assertEqual(self.register()[0], 201)

    def test_external_budget_counts_invalid_rotating_usernames_and_returns_retry_after(self):
        for index in range(10):
            self.assertEqual(self.register('rotating_' + str(index), password='short')[0], 400)
        status, headers, payload = self.register('fresh_valid_candidate')
        self.assertEqual(status, 429)
        self.assertGreaterEqual(int(headers['Retry-After']), 1)
        self.assertLessEqual(int(headers['Retry-After']), 60)
        self.assertIn('注册请求较多', json.loads(payload)['error'])
        self.assertEqual(self.rows('SELECT id FROM users'), [])


class RegistrationThrottleTests(unittest.TestCase):
    def test_window_boundary_recovers_exactly_without_retry_extending_the_window(self):
        clock = Clock()
        throttle = RegistrationThrottle(clock=clock)
        self.assertEqual([throttle.reserve() for _ in range(10)], [0] * 10)
        self.assertEqual(throttle.reserve(), 60)
        clock.now += 59.01
        self.assertEqual(throttle.reserve(), 1)
        clock.now = 1060.0
        self.assertEqual([throttle.reserve() for _ in range(10)], [0] * 10)
        self.assertEqual(throttle.reserve(), 60)

    def test_concurrent_reservations_admit_only_ten_before_password_work(self):
        clock = Clock()
        throttle = RegistrationThrottle(clock=clock)
        barrier = threading.Barrier(20)

        def reserve(_):
            barrier.wait(WAIT)
            return throttle.reserve()

        with ThreadPoolExecutor(max_workers=20) as executor:
            results = list(executor.map(reserve, range(20)))
        self.assertEqual(results.count(0), 10)
        self.assertEqual(results.count(60), 10)

    def test_route_reserves_budget_before_account_password_work_even_on_validation_error(self):
        accounts = SimpleNamespace(create_user=Mock(side_effect=ValueError('invalid fixture')))
        routes = Routes(SimpleNamespace(accounts=accounts), WebPolicy(
            test_origin=ORIGIN, allow_candidate_registration=True))
        clock = Clock()
        routes.registration_throttle = RegistrationThrottle(clock=clock)
        for index in range(10):
            with self.assertRaisesRegex(ValueError, 'invalid fixture'):
                routes.post('/api/auth/register', dict(username='rotating_' + str(index)), '')
        response = routes.post('/api/auth/register', dict(username='valid_candidate'), '')
        self.assertEqual(response.status, 429)
        self.assertEqual(accounts.create_user.call_count, 10)
        clock.now += 60
        with self.assertRaises(ValueError):
            routes.post('/api/auth/register', dict(username='window_recovered'), '')
        self.assertEqual(accounts.create_user.call_count, 11)


if __name__ == '__main__':
    unittest.main()
