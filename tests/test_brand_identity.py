"""Real HTTP session migration when the product cookie name changes."""
import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.cookiejar import Cookie, CookieJar
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from recruitment_insights.bootstrap import Store
from recruitment_insights.web.server import make_server


CANONICAL_COOKIE = 'recruitment_insights_session'
LEGACY_COOKIE = 'joblens_session'
PASSWORD = 'Migration-Fixture-2026!'


class BrandIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='recruitment-insights-identity-')
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'isolated.sqlite3')
        self.store.accounts.create_user('identity_a', PASSWORD, 'Migration fixture A')
        self.server = make_server(self.store, 0)
        self.worker = threading.Thread(target=self.server.serve_forever)
        self.worker.start()
        self.addCleanup(self.close_server)
        self.base_url = f'http://127.0.0.1:{self.server.server_port}'

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5)
        self.assertFalse(self.worker.is_alive())

    def request(self, path='/api/auth/me', *, body=None, cookie=None):
        headers = {}
        if cookie is not None:
            headers['Cookie'] = cookie
        method = 'GET'
        if body is not None:
            method = 'POST'
            headers.update({'Content-Type': 'application/json', 'X-Local-Request': '1'})
            body = json.dumps(body).encode('utf-8')
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.headers, json.loads(response.read())
        finally:
            connection.close()

    def assert_session_cookies(self, headers, *, cleared):
        values = headers.get_all('Set-Cookie', [])
        self.assertEqual(len(values), 2, 'Cookies must use separate HTTP header fields.')
        cookies = SimpleCookie()
        for value in values:
            cookies.load(value)
        self.assertEqual(set(cookies), {CANONICAL_COOKIE, LEGACY_COOKIE})
        for name in (CANONICAL_COOKIE, LEGACY_COOKIE):
            morsel = cookies[name]
            self.assertEqual(morsel['path'], '/')
            self.assertTrue(morsel['httponly'])
            self.assertEqual(morsel['samesite'], 'Strict')
            if cleared or name == LEGACY_COOKIE:
                self.assertEqual(morsel.value, '')
                self.assertEqual(morsel['max-age'], '0')
        return cookies

    def test_legacy_session_can_authenticate_and_login_rotates_to_new_cookie(self):
        old_token, _ = self.store.accounts.login('identity_a', PASSWORD)
        legacy_header = f'{LEGACY_COOKIE}={old_token}'
        status, _, actor = self.request(cookie=legacy_header)
        self.assertEqual((status, actor['username']), (200, 'identity_a'))

        status, headers, actor = self.request(
            '/api/auth/login', body={'username': 'identity_a', 'password': PASSWORD},
            cookie=legacy_header)
        self.assertEqual((status, actor['username']), (200, 'identity_a'))
        cookies = self.assert_session_cookies(headers, cleared=False)
        new_token = cookies[CANONICAL_COOKIE].value
        self.assertTrue(new_token)
        self.assertNotEqual(new_token, old_token)
        self.assertEqual(cookies[CANONICAL_COOKIE]['max-age'], '28800')
        self.assertIsNone(self.store.accounts.user(old_token))
        self.assertEqual(self.request(cookie=legacy_header)[0], 401)
        status, _, actor = self.request(cookie=f'{CANONICAL_COOKIE}={new_token}')
        self.assertEqual((status, actor['username']), (200, 'identity_a'))

    @staticmethod
    def browser_cookie(name, token):
        return Cookie(version=0, name=name, value=token, port=None, port_specified=False,
                      domain='127.0.0.1', domain_specified=False, domain_initial_dot=False,
                      path='/', path_specified=True, secure=False, expires=None,
                      discard=True, comment=None, comment_url=None, rest={'HttpOnly': None})

    def test_new_cookie_has_priority_and_browser_logout_cannot_fall_back_to_legacy(self):
        self.store.accounts.create_user('identity_b', PASSWORD, 'Migration fixture B')
        legacy_token, _ = self.store.accounts.login('identity_a', PASSWORD)
        current_token, _ = self.store.accounts.login('identity_b', PASSWORD)

        for current_value in ('invalid-current-session', ''):
            with self.subTest(current_value=current_value):
                status, _, _ = self.request(cookie=(
                    f'{CANONICAL_COOKIE}={current_value}; {LEGACY_COOKIE}={legacy_token}'))
                self.assertEqual(status, 401, 'An existing new cookie must not fall back to the old identity.')

        jar = CookieJar()
        jar.set_cookie(self.browser_cookie(LEGACY_COOKIE, legacy_token))
        jar.set_cookie(self.browser_cookie(CANONICAL_COOKIE, current_token))
        browser = build_opener(HTTPCookieProcessor(jar))
        with browser.open(self.base_url + '/api/auth/me', timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())['username'], 'identity_b')

        logout = Request(self.base_url + '/api/auth/logout', data=b'{}', headers={
            'Content-Type': 'application/json', 'X-Local-Request': '1'})
        with browser.open(logout, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assert_session_cookies(response.headers, cleared=True)
            response.read()
        self.assertIsNone(self.store.accounts.user(current_token))
        self.assertEqual({cookie.name for cookie in jar} & {CANONICAL_COOKIE, LEGACY_COOKIE}, set())
        with self.assertRaises(HTTPError) as rejected:
            browser.open(self.base_url + '/api/auth/me', timeout=5)
        self.assertEqual(rejected.exception.code, 401)
        rejected.exception.close()


if __name__ == '__main__':
    unittest.main()
