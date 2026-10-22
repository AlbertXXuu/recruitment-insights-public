"""Invitation deployment boundaries, exercised over loopback HTTP and temp DBs.

The external HTTPS proxy is represented by its forwarded Host/Origin headers.
These tests do not exercise a TLS terminator or make a public network connection.
"""
import contextlib
import hashlib
import io
import json
import os
import socket
import sqlite3
import tempfile
import threading
import unittest
from http.client import HTTPConnection, HTTPResponse
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import Mock, patch

import app
from core import Store
from recruitment_insights.application.accounts import Accounts
from recruitment_insights.config import (
    LEGACY_SESSION_COOKIE, SESSION_COOKIE, WebPolicy,
)
from recruitment_insights.web.server import make_server
from seed_demo_accounts import PASSWORD as DEMO_PASSWORD


ORIGIN = 'https://invite.example.test'
HOST = 'invite.example.test'
INVITE_COOKIE = '__Host-recruitment_insights_test_session'
PASSWORD = 'isolated-invite-fixture-2026!'
WAIT = 5


class WebPolicyTests(unittest.TestCase):
    def test_invalid_policy_cannot_fall_back_to_local_or_start_a_server(self):
        for policy in (False, 0, '', object()):
            with self.subTest(policy=policy), patch('recruitment_insights.web.server.Routes') as routes:
                with self.assertRaises(ValueError):
                    make_server(Mock(), 0, policy=policy)
                routes.assert_not_called()

    def test_default_and_explicit_invitation_cookie_names(self):
        local = WebPolicy()
        invitation = WebPolicy(test_origin=ORIGIN)
        self.assertFalse(local.invited)
        self.assertEqual(local.cookie_name, SESSION_COOKIE)
        self.assertTrue(invitation.invited)
        self.assertEqual(invitation.cookie_name, INVITE_COOKIE)

    def test_accepts_canonical_https_dns_origins(self):
        for origin in (ORIGIN, 'https://a.example', 'https://trial-1.example.test',
                       'https://xn--fiqs8s.example'):
            with self.subTest(origin=origin):
                self.assertTrue(WebPolicy(test_origin=origin).invited)

    def test_noncanonical_or_nonpublic_authorities_fail_closed(self):
        invalid = (
            '', 'http://invite.example.test', 'HTTPS://invite.example.test',
            'https://Invite.example.test', 'invite.example.test',
            'https://invite.example.test/', 'https://invite.example.test/path',
            'https://invite.example.test:443', 'https://invite.example.test:8765',
            'https://invite.example.test?x=1', 'https://invite.example.test#fragment',
            'https://invite.example.test?', 'https://invite.example.test#',
            'https://user@invite.example.test', 'https://user:pass@invite.example.test',
            'https://127.0.0.1', 'https://192.0.2.1', 'https://[::1]',
            'https://127.1', 'https://127.0.1', 'https://0x7f.0.0.1',
            'https://localhost', 'https://trial.localhost', 'https://singlelabel',
            'https://invite.example.test.', 'https://.example.test',
            'https://invite..example.test', 'https://-invite.example.test',
            'https://invite-.example.test', 'https://invite_example.test',
            'https://邀请.example.test', ' https://invite.example.test',
            'https://invite.example.test ', 'https://invite.example.test\n',
            'https://invite.\texample.test', 'https://invite.example.test\\other',
            'https://invite.example.test%2fother',
            'https://' + 'a' * 64 + '.example.test',
            'https://' + '.'.join(['a' * 63] * 4),
        )
        for origin in invalid:
            with self.subTest(origin=origin):
                with self.assertRaises(ValueError):
                    WebPolicy(test_origin=origin)


class InvitationHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='invitation-http-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.store = Store(Path(cls.temporary.name) / 'invitation.sqlite3')
        cls.accounts = cls.store.accounts
        org_a = cls.accounts.organization('邀请测试企业 A')
        org_b = cls.accounts.organization('邀请测试企业 B')
        users = (
            ('invite_candidate', 'candidate', None, False),
            ('invite_boss_a', 'boss', org_a, False),
            ('invite_boss_b', 'boss', org_b, False),
            ('invite_hr_a', 'hr', org_a, True),
        )
        cls.tokens, cls.actors = {}, {}
        for username, role, org_id, allowed in users:
            cls.accounts.create_user(username, PASSWORD, username, role, org_id, allowed)
            cls.tokens[username], cls.actors[username] = cls.accounts.login(username, PASSWORD)
        cls.server = make_server(cls.store, 0, policy=WebPolicy(test_origin=ORIGIN))
        cls.listener = threading.Thread(
            target=lambda: cls.server.serve_forever(poll_interval=0.02),
            name='invitation-test-listener', daemon=True)
        cls.listener.start()
        cls.addClassCleanup(cls.stop_server, cls.server, cls.listener)

    @classmethod
    def stop_server(cls, server, listener):
        if listener.is_alive():
            server.shutdown()
        server.server_close()
        listener.join(WAIT)
        if listener.is_alive():
            raise AssertionError('Invitation HTTP listener did not stop')

    def request(self, path, *, body=None, user=None, headers=None,
                host=HOST, origin=ORIGIN, server=None):
        """Keep header pairs intact so duplicate trust headers reach the parser."""
        server = server or self.server
        connection = HTTPConnection('127.0.0.1', server.server_port, timeout=WAIT)
        payload = json.dumps(body).encode('utf-8') if body is not None else None
        pairs = []
        if host is not None:
            pairs.append(('Host', host))
        if user:
            pairs.append(('Cookie', INVITE_COOKIE + '=' + self.tokens[user]))
        if body is not None:
            pairs.extend((('Content-Type', 'application/json'),
                          ('Content-Length', str(len(payload))), ('X-Local-Request', '1')))
            if origin is not None:
                pairs.append(('Origin', origin))
        pairs.extend(headers or ())
        try:
            connection.putrequest('POST' if body is not None else 'GET', path,
                                  skip_host=True, skip_accept_encoding=True)
            for key, value in pairs:
                connection.putheader(key, value)
            connection.endheaders(payload)
            response = connection.getresponse()
            return response.status, response.getheaders(), response.read()
        finally:
            connection.close()

    def json_request(self, path, *, status=200, **kwargs):
        actual, _, payload = self.request(path, **kwargs)
        self.assertEqual(actual, status, (path, payload.decode('utf-8', errors='replace')))
        return json.loads(payload)

    def login(self, **kwargs):
        status, headers, body = self.request('/api/auth/login',
            body=dict(username='invite_candidate', password=PASSWORD), **kwargs)
        self.assertEqual(status, 200, body)
        cookies = [value for key, value in headers if key.lower() == 'set-cookie']
        self.assertTrue(cookies)
        return cookies

    def test_invitation_transport_still_binds_only_to_loopback(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        self.assertEqual(self.server.socket.getsockname()[0], '127.0.0.1')
        self.json_request('/api/health')

    def test_exact_external_host_is_required_on_get_and_post(self):
        for host in (None, 'evil.example.test', HOST + ':443', HOST.upper(),
                     HOST + '.evil.test', f'127.0.0.1:{self.server.server_port}',
                     f'localhost:{self.server.server_port}'):
            for body in (None, {}):
                with self.subTest(host=host, method='POST' if body is not None else 'GET'):
                    self.assertEqual(self.request('/api/health', host=host, body=body)[0], 403)
        self.json_request('/api/auth/me', user='invite_candidate')

    def test_post_requires_exact_external_origin_even_with_local_marker(self):
        for origin in (None, '', 'null', 'https://evil.example.test', ORIGIN + '/',
                       ORIGIN + ':443', ORIGIN.upper(), 'http://' + HOST,
                       f'http://127.0.0.1:{self.server.server_port}'):
            with self.subTest(origin=origin):
                self.assertEqual(self.request('/api/auth/login', origin=origin,
                    body=dict(username='invite_candidate', password=PASSWORD))[0], 403)
        self.login()

    def test_early_origin_rejection_preserves_response_while_late_body_is_sent(self):
        # Header/body may arrive separately: an origin refusal must half-close
        # output before bounded draining, instead of resetting the late writer.
        closed = threading.Event()
        original_close = self.server.shutdown_request
        def close(request):
            try:
                return original_close(request)
            finally:
                closed.set()
        payload = b'{"username":"invite_candidate","password":"unused"}'
        headers = (f'POST /api/auth/login HTTP/1.1\r\nHost: {HOST}\r\n'
                   f'Origin: {ORIGIN}:443\r\nContent-Type: application/json\r\n'
                   f'X-Local-Request: 1\r\nContent-Length: {len(payload)}\r\n\r\n')
        with patch.object(self.server, 'shutdown_request', side_effect=close), \
                socket.create_connection(('127.0.0.1', self.server.server_port), timeout=WAIT) as client:
            client.sendall(headers.encode('ascii'))
            response = HTTPResponse(client)
            response.begin()
            try:
                self.assertEqual(response.status, 403)
                self.assertFalse(closed.wait(0.02),
                                 'The server fully closed before the delayed body could arrive')
                client.sendall(payload)
                client.shutdown(socket.SHUT_WR)
                self.assertEqual(json.loads(response.read())['error'], '拒绝跨站写入')
                self.assertTrue(closed.wait(WAIT), 'Refused request did not release its worker')
            finally:
                response.close()

    def test_normal_get_and_fully_read_post_do_not_enter_early_refusal_drain(self):
        self.server._threads.join()
        with patch.object(self.server, 'reject_request', wraps=self.server.reject_request) as drain:
            self.json_request('/api/health')
            self.login()
            self.server._threads.join()
            drain.assert_not_called()

    def test_duplicate_host_and_origin_headers_are_rejected(self):
        for value in (HOST, 'evil.example.test'):
            for body in (None, {}):
                with self.subTest(header='Host', value=value, body=body):
                    self.assertEqual(self.request('/api/auth/me', body=body,
                        user='invite_candidate', headers=[('Host', value)])[0], 403)
        for value in (ORIGIN, 'https://evil.example.test'):
            with self.subTest(header='Origin', value=value):
                self.assertEqual(self.request('/api/auth/login', body={},
                    headers=[('Origin', value)])[0], 403)
                self.assertEqual(self.request('/api/auth/me', user='invite_candidate',
                    headers=[('Origin', ORIGIN), ('origin', value)])[0], 403)

    def test_single_proxy_headers_cannot_change_host_origin_or_cookie_security(self):
        proxy_headers = [
            ('Forwarded', 'for=192.0.2.10;host=evil.example.test;proto=http'),
            ('X-Forwarded-Host', 'evil.example.test'),
            ('X-Forwarded-Proto', 'http'), ('X-Forwarded-For', '192.0.2.10'),
            ('X-Forwarded-Port', '80'),
        ]
        self.json_request('/api/auth/me', user='invite_candidate', headers=proxy_headers)
        for cookie in self.login(headers=proxy_headers):
            self.assertIn('; Secure', cookie)
        self.assertEqual(self.request('/api/health', host='evil.example.test',
            headers=[('Forwarded', f'host={HOST};proto=https'),
                     ('X-Forwarded-Host', HOST), ('X-Forwarded-Proto', 'https')])[0], 403)
        self.assertEqual(self.request('/api/auth/login', body={},
            origin='https://evil.example.test',
            headers=[('Forwarded', f'host={HOST};proto=https'),
                     ('X-Forwarded-Host', HOST), ('X-Forwarded-Proto', 'https')])[0], 403)

    def test_duplicate_proxy_headers_and_cookie_headers_are_rejected(self):
        cases = (
            ('Forwarded', 'for=192.0.2.10;proto=https'),
            ('X-Forwarded-For', '192.0.2.10'),
            ('X-Forwarded-Host', HOST), ('X-Forwarded-Proto', 'https'),
            ('X-Forwarded-Port', '443'),
            ('Cookie', INVITE_COOKIE + '=' + self.tokens['invite_candidate']),
        )
        for name, value in cases:
            for body in (None, {}):
                with self.subTest(header=name, method='POST' if body is not None else 'GET'):
                    self.assertEqual(self.request('/api/auth/me', body=body,
                        headers=[(name, value), (name.lower(), value)])[0], 403)

    def test_duplicate_session_cookie_names_cannot_authenticate(self):
        valid = self.tokens['invite_candidate']
        for cookie in (f'{INVITE_COOKIE}={valid}; {INVITE_COOKIE}=forged',
                       f'{INVITE_COOKIE}=forged; {INVITE_COOKIE}={valid}',
                       f'{INVITE_COOKIE}={valid}; {INVITE_COOKIE}={valid}'):
            with self.subTest(cookie=cookie.split(';')[1]):
                self.assertIn(self.request('/api/auth/me', headers=[('Cookie', cookie)])[0],
                              (401, 403))

    def test_login_sets_only_invitation_cookie_with_https_attributes(self):
        cookies = self.login()
        self.assertEqual(len(cookies), 1, cookies)
        parsed = SimpleCookie()
        parsed.load(cookies[0])
        self.assertEqual(set(parsed), {INVITE_COOKIE})
        session = parsed[INVITE_COOKIE]
        self.assertTrue(session.value)
        self.assertTrue(session['secure'])
        self.assertTrue(session['httponly'])
        self.assertEqual(session['samesite'], 'Strict')
        self.assertEqual(session['path'], '/')
        self.assertEqual(session['domain'], '')
        self.json_request('/api/auth/me', headers=[('Cookie', session.OutputString().split(';')[0])])

    def test_local_and_legacy_cookies_are_not_read_in_invitation_mode(self):
        token = self.tokens['invite_candidate']
        for name in (SESSION_COOKIE, LEGACY_SESSION_COOKIE):
            with self.subTest(name=name):
                self.assertEqual(self.request('/api/auth/me', headers=[('Cookie', name + '=' + token)])[0], 401)
                self.login(headers=[('Cookie', name + '=' + token)])
                self.assertIsNotNone(self.accounts.user(token), 'An ignored local cookie revoked its session')
        self.json_request('/api/auth/me', user='invite_candidate',
                          headers=[('X-Forwarded-Proto', 'http')])

    def test_logout_clears_secure_invitation_cookie_and_invalidates_session(self):
        cookies = self.login()
        cookie = cookies[0].split(';')[0]
        status, headers, body = self.request('/api/auth/logout', body={}, headers=[('Cookie', cookie)])
        self.assertEqual(status, 200, body)
        deleted = [value for key, value in headers if key.lower() == 'set-cookie']
        self.assertEqual(len(deleted), 1)
        parsed = SimpleCookie()
        parsed.load(deleted[0])
        self.assertEqual(set(parsed), {INVITE_COOKIE})
        self.assertTrue(parsed[INVITE_COOKIE]['secure'])
        self.assertEqual(parsed[INVITE_COOKIE]['max-age'], '0')
        self.assertEqual(self.request('/api/auth/me', headers=[('Cookie', cookie)])[0], 401)

    def test_registration_rejected_without_creating_user(self):
        with self.store.connect() as db:
            before = db.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        self.json_request('/api/auth/register', status=403, body=dict(
            username='should_not_exist', password=PASSWORD, display_name='禁止公开注册', role='admin'))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM users').fetchone()[0], before)
            self.assertIsNone(db.execute('SELECT id FROM users WHERE username=?', ('should_not_exist',)).fetchone())

    def test_invitation_login_page_excludes_demo_secrets_and_registration(self):
        for path in ('/', '/login'):
            with self.subTest(path=path):
                status, _, content = self.request(path)
                self.assertEqual(status, 200)
                html = content.decode('utf-8')
                self.assertIn('id="login-form"', html)
                self.assertNotIn(DEMO_PASSWORD, html)
                self.assertNotIn('demo-accounts', html)
                self.assertNotIn('boss_a', html)
                self.assertNotIn('candidate_a', html)
                self.assertIn('data-registration="disabled"', html)
                self.assertRegex(html, r'<button\b[^>]*id="switch-auth"[^>]*\bhidden(?:\s|>)')
                self.assertRegex(html, r'<form\b[^>]*id="register-form"[^>]*\bhidden(?:\s|>)')

    def test_invitation_hr_creation_rejects_public_demo_password(self):
        self.json_request('/api/team', user='invite_boss_a', status=400, body=dict(
            username='invite_shared_secret_hr', password=DEMO_PASSWORD,
            display_name='不得共享密码的 HR', can_manage=False))
        with self.store.connect() as db:
            self.assertIsNone(db.execute('SELECT id FROM users WHERE username=?',
                                        ('invite_shared_secret_hr',)).fetchone())

    def test_invitation_mode_preserves_company_read_and_write_isolation(self):
        marker = '邀请模式私有岗位'
        saved = self.json_request('/api/company/jobs', user='invite_boss_a', body=dict(
            record=dict(title=marker, location='测试地区', description='Python 接口开发'),
            published=False))
        job_id = saved['id']
        self.json_request('/api/jobs/' + str(job_id), user='invite_boss_a')
        for user in ('invite_boss_b', 'invite_candidate'):
            with self.subTest(user=user):
                self.json_request('/api/jobs/' + str(job_id) + '?org_id=1', user=user, status=404)
                jobs = self.json_request('/api/jobs?dataset=demo&org_id=1', user=user)
                self.assertFalse(any(job['id'] == job_id for job in jobs['items']))
        self.json_request('/api/company/jobs', user='invite_boss_b', status=403,
                          body=dict(id=job_id, expected_edit_token=saved['edit_token'], published=True))
        self.assertFalse(self.json_request('/api/jobs/' + str(job_id), user='invite_boss_a')['published'])

    def test_revocation_applies_to_existing_invitation_session(self):
        hr_id = self.actors['invite_hr_a']['id']
        self.json_request('/api/team', user='invite_boss_a', body=dict(id=hr_id, can_manage=False))
        try:
            self.json_request('/api/company/jobs', user='invite_hr_a', status=403, body=dict(
                record=dict(title='撤权后不应创建', location='测试地区', description='Python')))
            self.json_request('/api/import', user='invite_hr_a', status=403,
                              body=dict(csv='', source='invitation'))
            self.json_request('/api/company/jobs', user='invite_hr_a')
        finally:
            self.json_request('/api/team', user='invite_boss_a', body=dict(id=hr_id, can_manage=True))

    def test_local_policy_retains_local_cookie_without_secure_from_forwarding(self):
        server = make_server(self.store, 0, policy=WebPolicy())
        listener = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
        listener.start()
        self.addCleanup(self.stop_server, server, listener)
        local_host = f'127.0.0.1:{server.server_port}'
        token = self.tokens['invite_candidate']
        for name in (SESSION_COOKIE, LEGACY_SESSION_COOKIE):
            with self.subTest(name=name):
                self.json_request('/api/auth/me', server=server, host=local_host,
                                  headers=[('Cookie', name + '=' + token)])
        self.json_request('/api/auth/me', status=401, server=server, host=local_host,
                          headers=[('Cookie', INVITE_COOKIE + '=' + token)])
        status, headers, body = self.request('/api/auth/login', server=server, host=local_host,
            origin=None, body=dict(username='invite_candidate', password=PASSWORD),
            headers=[('Forwarded', f'host={HOST};proto=https'), ('X-Forwarded-Proto', 'https')])
        self.assertEqual(status, 200, body)
        cookies = [value for key, value in headers if key.lower() == 'set-cookie']
        self.assertTrue(any(value.startswith(SESSION_COOKIE + '=') for value in cookies))
        self.assertFalse(any('; Secure' in value for value in cookies))
        self.assertEqual(self.request('/api/health', server=server, host=HOST,
            headers=[('Forwarded', f'host={local_host};proto=http')])[0], 403)


class InvitationDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='invitation-database-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    @staticmethod
    def files(directory):
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in directory.iterdir() if path.is_file()}

    def test_independent_absolute_new_path_resolves_without_creating_database(self):
        database = self.root / 'new' / 'invitation.sqlite3'
        self.assertEqual(app.validate_test_database(database), database.resolve())
        self.assertFalse(database.exists())
        self.assertFalse(database.parent.exists())
        with self.assertRaises(ValueError):
            app.validate_test_database(Path('relative-invitation.sqlite3'))

    def test_formal_and_group04_paths_rejected_without_reading_or_creating_them(self):
        with patch.object(app, 'ROOT', self.root):
            for database in (
                self.root / 'data' / 'recruitment.sqlite3',
                self.root / 'deliverables' / 'team-handoff' / '04_组员测试版' / 'data' / 'recruitment.sqlite3',
            ):
                with self.subTest(database=database):
                    with self.assertRaises(ValueError):
                        app.validate_test_database(database)
                    self.assertFalse(database.exists())

    def test_legacy_database_rejected_without_schema_migration(self):
        database = self.root / 'legacy.sqlite3'
        with contextlib.closing(sqlite3.connect(database)) as connection, connection:
            connection.execute('CREATE TABLE users(username TEXT, password_hash TEXT)')
            values = self.password_hash(PASSWORD)
            connection.execute('INSERT INTO users VALUES(?,?)', ('custom_invite', values))
            connection.execute('PRAGMA user_version=0')
        before = self.files(self.root)
        with self.assertRaises(ValueError):
            app.validate_test_database(database)
        self.assertEqual(self.files(self.root), before)
        with contextlib.closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),
                             [('users',)])

    def test_current_database_with_custom_password_is_accepted_without_writes(self):
        database = self.root / 'current.sqlite3'
        store = Store(database)
        store.accounts.create_user('custom_safe', PASSWORD, '独立账号')
        before = self.files(self.root)
        self.assertEqual(app.validate_test_database(database), database.resolve())
        self.assertEqual(self.files(self.root), before)

    def test_views_cannot_be_treated_as_empty_or_as_required_tables(self):
        for kind in ('only-view', 'required-table-view'):
            with self.subTest(kind=kind):
                database = self.root / (kind + '.sqlite3')
                if kind == 'required-table-view':
                    Store(database)
                with contextlib.closing(sqlite3.connect(database)) as connection, connection:
                    if kind == 'only-view':
                        connection.execute('CREATE VIEW unknown_view AS SELECT 1 AS id')
                    else:
                        connection.execute('ALTER TABLE users RENAME TO archived_users')
                        connection.execute('CREATE VIEW users AS SELECT * FROM archived_users')
                before = self.files(self.root)
                with self.assertRaises(ValueError):
                    app.validate_test_database(database)
                self.assertEqual(self.files(self.root), before)

    def test_empty_existing_database_passes_without_writes(self):
        database = self.root / 'empty.sqlite3'
        with contextlib.closing(sqlite3.connect(database)):
            pass
        before = self.files(self.root)
        self.assertEqual(app.validate_test_database(database), database.resolve())
        self.assertEqual(self.files(self.root), before)

    def test_malformed_password_record_rejected_without_writes(self):
        database = self.root / 'malformed-password.sqlite3'
        store = Store(database)
        store.accounts.create_user('custom_safe', PASSWORD, '独立账号')
        with store.connect() as db:
            db.execute('UPDATE users SET password_hash=?', ('broken-salt:broken-hash',))
        before = self.files(self.root)
        with self.assertRaises(ValueError):
            app.validate_test_database(database)
        self.assertEqual(self.files(self.root), before)

    @staticmethod
    def password_hash(password):
        return Accounts._user_values(
            'custom_invite', password, '邀请测试账号', 'candidate', None, False)['password_hash']

    def test_demo_password_under_unrelated_username_rejected_without_writes(self):
        database = self.root / 'shared-password.sqlite3'
        store = Store(database)
        store.accounts.create_user('custom_safe', PASSWORD, '独立账号')
        store.accounts.create_user('unrelated_imported_account', DEMO_PASSWORD, '共享密码')
        before = self.files(self.root)
        with self.assertRaises(ValueError):
            app.validate_test_database(database)
        self.assertEqual(self.files(self.root), before)

    def test_hardlinked_database_rejected(self):
        original = self.root / 'original.sqlite3'
        Store(original)
        alias = self.root / 'alias.sqlite3'
        os.link(original, alias)
        for path in (original, alias):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    app.validate_test_database(path)

    def test_symlink_database_and_symlink_parent_rejected(self):
        original = self.root / 'original.sqlite3'
        Store(original)
        alias = self.root / 'alias.sqlite3'
        linked_directory = self.root / 'linked-directory'
        target_directory = self.root / 'actual-directory'
        target_directory.mkdir()
        try:
            os.symlink(original, alias)
            os.symlink(target_directory, linked_directory, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f'Windows account cannot create symbolic links: {exc}')
        for path in (alias, linked_directory / 'new.sqlite3'):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    app.validate_test_database(path)


class InvitationCliTests(unittest.TestCase):
    def assert_cli_database_rejected_without_write(self, database):
        before = InvitationDatabaseTests.files(database.parent)
        with patch('sys.argv', ['app.py', '--test-origin', ORIGIN, '--db', str(database)]), \
                patch.object(app, 'Store') as constructor, patch.object(app, 'make_server') as server, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exit_context:
                app.main()
            self.assertEqual(exit_context.exception.code, 2)
            constructor.assert_not_called()
            server.assert_not_called()
        self.assertEqual(InvitationDatabaseTests.files(database.parent), before)

    def test_invalid_invitation_configuration_fails_before_store_or_listener(self):
        with tempfile.TemporaryDirectory(prefix='invitation-cli-invalid-') as temporary:
            root = Path(temporary)
            target = root / 'must-not-be-created.sqlite3'
            cases = (
                ['--test-origin', ORIGIN],
                ['--test-origin', ORIGIN, '--db', 'relative.sqlite3'],
                ['--test-origin', 'http://' + HOST, '--db', str(target)],
                ['--test-origin', ORIGIN, '--db', str(root / 'data' / 'recruitment.sqlite3')],
                ['--test-origin', ORIGIN, '--db', str(root / 'deliverables' / 'team-handoff' /
                                                   '04_组员测试版' / 'data' / 'recruitment.sqlite3')],
            )
            for arguments in cases:
                with self.subTest(arguments=arguments), patch.object(app, 'ROOT', root), \
                        patch('sys.argv', ['app.py'] + arguments), patch.object(app, 'Store') as store, \
                        patch.object(app, 'make_server') as server, contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as exit_context:
                        app.main()
                    self.assertEqual(exit_context.exception.code, 2)
                    store.assert_not_called()
                    server.assert_not_called()
            self.assertFalse(target.exists())

    def test_existing_shared_password_rejected_before_store_or_listener(self):
        with tempfile.TemporaryDirectory(prefix='invitation-cli-shared-') as temporary:
            database = Path(temporary) / 'unsafe.sqlite3'
            store = Store(database)
            store.accounts.create_user('renamed_demo_account', DEMO_PASSWORD, '共享密码')
            self.assert_cli_database_rejected_without_write(database)

    def test_live_wal_with_uncheckpointed_password_rejected_without_writes(self):
        with tempfile.TemporaryDirectory(prefix='invitation-cli-wal-') as temporary:
            database = Path(temporary) / 'active-wal.sqlite3'
            store = Store(database)
            store.accounts.create_user('custom_safe', PASSWORD, '独立账号')
            with store.connect() as db:
                db.execute('UPDATE users SET password_hash=?',
                           (InvitationDatabaseTests.password_hash(DEMO_PASSWORD),))
                db.commit()
                self.assertGreater(Path(str(database) + '-wal').stat().st_size, 0)
                self.assertTrue(Path(str(database) + '-shm').exists())
                self.assert_cli_database_rejected_without_write(database)

    def test_in_use_rollback_journal_rejected_without_writes(self):
        with tempfile.TemporaryDirectory(prefix='invitation-cli-journal-') as temporary:
            database = Path(temporary) / 'active-journal.sqlite3'
            store = Store(database)
            store.accounts.create_user('custom_safe', PASSWORD, '独立账号')
            with store.connect() as db:
                self.assertEqual(db.execute('PRAGMA journal_mode=DELETE').fetchone()[0], 'delete')
                db.execute('BEGIN IMMEDIATE')
                db.execute('UPDATE users SET display_name=?', ('尚未提交的新名称',))
                self.assertGreater(Path(str(database) + '-journal').stat().st_size, 0)
                self.assert_cli_database_rejected_without_write(database)
                db.rollback()

    def test_valid_invitation_cli_passes_policy_and_independent_database(self):
        with tempfile.TemporaryDirectory(prefix='invitation-cli-valid-') as temporary:
            target = Path(temporary) / 'independent.sqlite3'
            store = Mock(path=target)
            server = Mock(server_port=8765)
            server.serve_forever.side_effect = KeyboardInterrupt
            with patch('sys.argv', ['app.py', '--test-origin', ORIGIN, '--db', str(target)]), \
                    patch.object(app, 'Store', return_value=store) as constructor, \
                    patch.object(app, 'make_server', return_value=server) as listener, \
                    contextlib.redirect_stdout(io.StringIO()):
                app.main()
            self.assertEqual(Path(constructor.call_args.args[0]), target.resolve())
            self.assertIs(listener.call_args.args[0], store)
            policy = listener.call_args.kwargs['policy']
            self.assertTrue(policy.invited)
            self.assertEqual(policy.cookie_name, INVITE_COOKIE)
            server.server_close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
