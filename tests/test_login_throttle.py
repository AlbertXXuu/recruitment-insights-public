"""Bounded username login protection without public network or live databases."""
import json
import tempfile
import threading
import unittest
from concurrent.futures import Future
from http.client import HTTPConnection
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from recruitment_insights.bootstrap import Store
from recruitment_insights.config import LEGACY_SESSION_COOKIE, SESSION_COOKIE, WebPolicy
from recruitment_insights.web.login_throttle import LoginThrottle
from recruitment_insights.web.routes import Routes
from recruitment_insights.web.server import make_server


PASSWORD = 'Throttle-fixture-password-2026!'
ERROR = '账号或密码错误'
WAIT = 5


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class LoginThrottleTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.throttle = LoginThrottle(clock=self.clock)
        self.calls = []
        self.accounts = SimpleNamespace(login=self.login_account, logout=lambda token: None)
        self.routes = Routes(SimpleNamespace(accounts=self.accounts),
                             WebPolicy(test_origin='https://invite.example.test'))
        self.routes.login_throttle = self.throttle

    def login_account(self, username, password):
        self.calls.append((username, password))
        if password != PASSWORD:
            raise ValueError(ERROR)
        return 'new-token', {'username': username, 'role': 'candidate'}

    def login(self, username='candidate_a', password='wrong', **extra):
        return self.routes.post('/api/auth/login', dict(username=username, password=password, **extra), '')

    def fail_five(self, username='candidate_a'):
        for _ in range(5):
            response = self.login(username)
            self.assertEqual((response.status, response.data), (401, {'error': ERROR}))

    def test_fifth_failure_starts_fixed_cooldown_and_correct_password_waits(self):
        self.fail_five()
        response = self.login(password=PASSWORD, limit=1000000, cooldown=0)
        self.assertEqual(response.status, 429)
        self.assertEqual(response.headers['Retry-After'], '60')
        self.assertIn('60', response.data['error'])
        self.assertEqual(len(self.calls), 5)
        self.clock.advance(0.25)
        self.assertEqual(self.login().headers['Retry-After'], '60')
        self.clock.advance(58.8)
        self.assertEqual(self.login().headers['Retry-After'], '1')
        self.clock.now = 1060.0
        self.assertEqual(self.login(password=PASSWORD).status, 200)
        self.fail_five()
        self.assertEqual(self.login().status, 429)

    def test_other_accounts_and_exact_case_are_independent(self):
        self.fail_five()
        for username in ('candidate_b', 'Candidate_a'):
            with self.subTest(username=username):
                self.assertEqual(self.login(username, PASSWORD).status, 200)
        self.assertEqual(self.login().status, 429)

    def test_local_demo_preserves_unlimited_login_and_has_no_throttle_state(self):
        local = Routes(SimpleNamespace(accounts=self.accounts), WebPolicy())
        self.assertIsNone(local.login_throttle)
        for _ in range(8):
            response = local.post('/api/auth/login', dict(username='candidate_a', password='wrong'), '')
            self.assertEqual(response.status, 401)
        self.assertEqual(local.post('/api/auth/login',
                                  dict(username='candidate_a', password=PASSWORD), '').status, 200)

    def test_window_expires_old_failures_and_success_resets_budget(self):
        for _ in range(4):
            self.assertEqual(self.login().status, 401)
        self.clock.advance(300)
        for _ in range(4):
            self.assertEqual(self.login().status, 401)
        self.assertEqual(self.login(password=PASSWORD).status, 200)
        self.fail_five()
        self.assertEqual(self.login().status, 429)

    def test_malformed_login_inputs_neither_verify_nor_consume_budget(self):
        invalid = (None, [], 'body', {}, {'username': 'candidate_a'},
                   {'username': None, 'password': 'wrong'},
                   {'username': [], 'password': 'wrong'},
                   {'username': 'candidate_a', 'password': False},
                   {'username': 'candidate_a', 'password': 'x' * 129},
                   {'username': 'a' * 41, 'password': 'wrong'},
                   {'username': ' candidate_a ', 'password': 'wrong'})
        for body in invalid:
            with self.subTest(body=body):
                response = self.routes.post('/api/auth/login', body, '')
                self.assertEqual((response.status, response.data), (401, {'error': ERROR}))
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.throttle._entries), 0)
        self.fail_five()

    def test_short_or_empty_wrong_passwords_are_verified_and_count(self):
        for password in ('', 'a', 'x' * 9, 'wrong', 'wrong-again'):
            self.assertEqual(self.login(password=password).status, 401)
        self.assertEqual(len(self.calls), 5)
        self.assertEqual(self.login().status, 429)

    def test_unexpected_account_error_releases_reservation_without_resetting_failures(self):
        for _ in range(4):
            self.assertEqual(self.login().status, 401)
        with patch.object(self.accounts, 'login', side_effect=RuntimeError('temporary DB error')):
            for _ in range(8):
                with self.assertRaisesRegex(RuntimeError, 'temporary DB error'):
                    self.login()
        self.assertEqual(self.login().status, 401)
        self.assertEqual(self.login().status, 429)

    def test_internal_value_error_is_generic_and_does_not_count_password_failure(self):
        with patch.object(self.accounts, 'login', side_effect=ValueError('invalid stored hash')):
            for _ in range(8):
                response = self.login()
                self.assertEqual((response.status, response.data), (401, {'error': ERROR}))
        self.fail_five()
        self.assertEqual(self.login().status, 429)

    def test_same_account_concurrency_reserves_only_remaining_budget_and_releases_lock(self):
        for _ in range(4):
            self.assertEqual(self.login().status, 401)
        entered, release = threading.Event(), threading.Event()
        result = Future()

        def paused_login(username, password):
            if username == 'candidate_a':
                entered.set()
                if not release.wait(WAIT):
                    raise AssertionError('Paused verification was not released')
            return self.login_account(username, password)

        def work():
            try:
                result.set_result(self.login())
            except BaseException as exc:
                result.set_exception(exc)

        with patch.object(self.accounts, 'login', side_effect=paused_login):
            worker = threading.Thread(target=work)
            worker.start()
            try:
                self.assertTrue(entered.wait(WAIT))
                for _ in range(20):
                    self.assertEqual(self.login().status, 429)
                self.assertEqual(self.login('candidate_b', PASSWORD).status, 200)
            finally:
                release.set()
                worker.join(WAIT)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result.result(WAIT).status, 401)
        self.assertEqual(sum(name == 'candidate_a' for name, _ in self.calls), 5)
        self.assertEqual(self.login().headers['Retry-After'], '60')

    def test_concurrent_first_attempts_cannot_exceed_five_verifications(self):
        attempts = [self.throttle.begin('candidate_a') for _ in range(12)]
        admitted = [attempt for attempt, retry in attempts if attempt is not None]
        self.assertEqual(len(admitted), 5)
        self.assertTrue(all(retry == 1 for attempt, retry in attempts if attempt is None))
        for attempt in admitted:
            self.throttle.finish(attempt, 'failure')
        self.assertEqual(self.throttle.begin('candidate_a'), (None, 60))

    def test_older_success_cannot_clear_newer_failed_attempts(self):
        old, _ = self.throttle.begin('candidate_a')
        newer, _ = self.throttle.begin('candidate_a')
        self.throttle.finish(newer, 'failure')
        self.throttle.finish(old, 'success')
        for _ in range(4):
            self.assertEqual(self.login().status, 401)
        self.assertEqual(self.login().status, 429)

    def test_previous_generation_results_cannot_clear_or_poison_new_budget(self):
        old_success, _ = self.throttle.begin('candidate_a')
        old_failure, _ = self.throttle.begin('candidate_a')
        success, _ = self.throttle.begin('candidate_a')
        self.throttle.finish(success, 'success')
        self.assertEqual(self.login().status, 401)
        self.throttle.finish(old_success, 'success')
        self.throttle.finish(old_failure, 'failure')
        for _ in range(4):
            self.assertEqual(self.login().status, 401)
        self.assertEqual(self.login().status, 429)

    def test_later_started_failure_still_counts_if_older_success_finishes_first(self):
        success, _ = self.throttle.begin('candidate_a')
        later_failure, _ = self.throttle.begin('candidate_a')
        self.throttle.finish(success, 'success')
        self.throttle.finish(later_failure, 'failure')
        for _ in range(4):
            self.assertEqual(self.login().status, 401)
        self.assertEqual(self.login().status, 429)

    def test_capacity_preserves_active_protection_and_recovers_after_expiry(self):
        self.throttle = LoginThrottle(clock=self.clock, capacity=3)
        self.routes.login_throttle = self.throttle
        self.fail_five('protected')
        self.login('active_one')
        pending, _ = self.throttle.begin('pending_one')
        for index in range(100):
            self.assertEqual(self.login('new_user_' + str(index)).status, 429)
        self.assertEqual(len(self.throttle._entries), 3)
        self.assertEqual(self.login('protected').headers['Retry-After'], '60')
        # Existing unrelated entries can still authenticate while capacity is full.
        self.assertEqual(self.login('active_one', PASSWORD).status, 200)
        self.assertEqual(self.login('new_user', PASSWORD).status, 200)
        self.assertEqual(len(self.throttle._entries), 2)
        self.clock.advance(300)
        self.assertEqual(self.login('next_user').status, 401)
        self.assertIn('pending_one', self.throttle._entries)
        self.throttle.finish(pending, None)
        self.assertNotIn('pending_one', self.throttle._entries)

    def test_default_capacity_and_pending_records_remain_bounded(self):
        for index in range(1024):
            self.assertEqual(self.login('unknown_' + str(index)).status, 401)
        self.assertEqual(len(self.throttle._entries), 1024)
        for index in range(1024, 1050):
            self.assertEqual(self.login('unknown_' + str(index)).status, 429)
        self.assertEqual(len(self.throttle._entries), 1024)
        self.clock.advance(300)
        self.assertEqual(self.login('recovered_user').status, 401)
        self.assertEqual(len(self.throttle._entries), 1)

    def test_monotonic_default_clock_ignores_wall_time_changes(self):
        with patch('recruitment_insights.web.login_throttle.time.monotonic', side_effect=self.clock), \
             patch('recruitment_insights.web.login_throttle.time.time', side_effect=AssertionError('wall clock')):
            self.routes.login_throttle = LoginThrottle()
            self.fail_five()
            self.clock.advance(60)
            self.assertEqual(self.login(password=PASSWORD).status, 200)


class LoginThrottleHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='login-throttle-http-')
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / 'isolated.sqlite3')
        self.store.accounts.create_user('Throttle_A', PASSWORD, '限速测试 A')
        self.store.accounts.create_user('Throttle_B', PASSWORD, '限速测试 B')
        self.clock = Clock()
        self.routes = Routes(self.store, WebPolicy(test_origin='https://invite.example.test'))
        self.routes.login_throttle = LoginThrottle(clock=self.clock)
        with patch('recruitment_insights.web.server.Routes', return_value=self.routes):
            self.server = make_server(self.store, 0, policy=self.routes.policy)
        self.listener = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02))
        self.listener.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.listener.join(WAIT)
        self.assertFalse(self.listener.is_alive())

    def request(self, path='/api/auth/login', *, body=None, raw=None, cookie=None, forwarded=None):
        method = 'GET' if body is None and raw is None else 'POST'
        headers = {'Host': 'invite.example.test'}
        payload = None
        if method == 'POST':
            headers.update({'Origin': 'https://invite.example.test', 'X-Local-Request': '1',
                            'Content-Type': 'application/json'})
            payload = raw if raw is not None else json.dumps(body).encode('utf-8')
        if cookie:
            headers['Cookie'] = cookie
        if forwarded:
            headers['X-Forwarded-For'] = forwarded
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=WAIT)
        try:
            connection.request(method, path, payload, headers)
            response = connection.getresponse()
            return response.status, response.headers, json.loads(response.read())
        finally:
            connection.close()

    def test_unknown_accounts_match_known_failures_and_forwarded_ip_cannot_reset_budget(self):
        for username in ('Throttle_A', 'unknown_user'):
            for index in range(5):
                status, _, data = self.request(body=dict(username=username, password='wrong'),
                                               forwarded='198.51.100.' + str(index))
                self.assertEqual((status, data), (401, {'error': ERROR}))
            status, headers, data = self.request(body=dict(username=username, password=PASSWORD),
                                                 forwarded='203.0.113.99')
            self.assertEqual(status, 429)
            self.assertEqual(headers['Retry-After'], '60')
            self.assertIn('稍后', data['error'])
            self.assertIsNone(headers.get('Set-Cookie'))
        self.assertEqual(self.request(body=dict(username='Throttle_B', password=PASSWORD))[0], 200)
        self.clock.advance(60)
        self.assertEqual(self.request(body=dict(username='Throttle_A', password=PASSWORD))[0], 200)

    def test_http_malformed_json_rejected_before_password_and_cookie_permissions_remain_live(self):
        for raw in (b'{', b'[]', b'null'):
            self.assertEqual(self.request(raw=raw)[0], 400)
        self.assertEqual(len(self.routes.login_throttle._entries), 0)
        old_token, _ = self.store.accounts.login('Throttle_A', PASSWORD)
        cookie_name = self.routes.policy.cookie_name
        status, headers, actor = self.request(body=dict(username='Throttle_A', password=PASSWORD),
                                              cookie=cookie_name + '=' + old_token)
        self.assertEqual((status, actor['role']), (200, 'candidate'))
        self.assertIsNone(self.store.accounts.user(old_token))
        parsed = SimpleCookie()
        for value in headers.get_all('Set-Cookie', []):
            parsed.load(value)
        self.assertEqual(set(parsed), {cookie_name})
        self.assertTrue(parsed[cookie_name]['secure'])
        self.assertTrue(parsed[cookie_name]['httponly'])
        self.assertEqual(parsed[cookie_name]['samesite'], 'Strict')
        cookie = cookie_name + '=' + parsed[cookie_name].value
        with self.store.connect() as db:
            db.execute("UPDATE users SET role='admin' WHERE username='Throttle_A'")
        self.assertEqual(self.request('/api/auth/me', cookie=cookie)[2]['role'], 'admin')
        self.assertEqual(self.request('/api/auth/register', body=dict(username='created', password=PASSWORD,
                                                                     display_name='禁止注册'))[0], 403)
        for _ in range(5):
            self.assertEqual(self.request(body=dict(username='Throttle_A', password='wrong'), cookie=cookie)[0], 401)
        self.assertEqual(self.request(body=dict(username='Throttle_A', password=PASSWORD), cookie=cookie)[0], 429)
        self.assertEqual(self.request('/api/auth/me', cookie=cookie)[0], 200)
        status, headers, _ = self.request('/api/auth/logout', body={}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn('Max-Age=0', headers['Set-Cookie'])
        self.assertEqual(self.request('/api/auth/me', cookie=cookie)[0], 401)
        for name in (SESSION_COOKIE, LEGACY_SESSION_COOKIE):
            self.assertEqual(self.request('/api/auth/me', cookie=name + '=' + old_token)[0], 401)


if __name__ == '__main__':
    unittest.main()
