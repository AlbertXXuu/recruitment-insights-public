"""Real-socket regressions for bounded HTTP admission and graceful shutdown."""
import io
import json
import os
import socket
import struct
import tempfile
import threading
import unittest
from concurrent.futures import Future
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import Store
from recruitment_insights.web.routes import Response
from recruitment_insights.web.server import make_server


WAIT = 5


class HttpAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.servers, self.clients, self.releases, self.temporary = [], [], [], []
        self.addCleanup(self.cleanup_servers)

    def cleanup_servers(self):
        for release in self.releases:
            release.set()
        for server, listener in self.servers:
            if listener.is_alive():
                server.shutdown()
            server.server_close()
            listener.join(WAIT)
            self.assertFalse(listener.is_alive(), 'HTTP listener did not stop')
        for client in self.clients:
            client.join(WAIT)
            self.assertFalse(client.is_alive(), 'HTTP client did not finish')
        for temporary in self.temporary:
            temporary.cleanup()

    def start_server(self, *, get=None, post=None, **settings):
        routes = SimpleNamespace(
            get=get or (lambda *args: Response(200, {'ok': True})),
            post=post or (lambda *args: Response(200, {'ok': True})))
        with patch('recruitment_insights.web.server.Routes', return_value=routes):
            server = make_server(SimpleNamespace(accounts=SimpleNamespace()), 0, **settings)
        listener = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02),
                                    name='admission-test-listener', daemon=True)
        self.servers.append((server, listener))
        listener.start()
        return server

    @staticmethod
    def request(server, path='/ok', body=None):
        connection = HTTPConnection('127.0.0.1', server.server_port, timeout=WAIT)
        headers = {}
        method = 'GET'
        if body is not None:
            method = 'POST'
            headers = {'Content-Type': 'application/json', 'X-Local-Request': '1'}
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def start_client(self, server, **kwargs):
        result = Future()
        def run():
            try:
                result.set_result(self.request(server, **kwargs))
            except BaseException as exc:
                result.set_exception(exc)
        worker = threading.Thread(target=run, name='admission-test-client', daemon=True)
        self.clients.append(worker)
        worker.start()
        return result

    @staticmethod
    def receive_all(connection):
        chunks = []
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                return b''.join(chunks)
            chunks.append(chunk)

    @staticmethod
    def record_calls(calls):
        def route(*args):
            calls.append(args)
            return Response(200, {})
        return route

    def raw_request(self, server, raw):
        with socket.create_connection(('127.0.0.1', server.server_port), timeout=WAIT) as connection:
            connection.sendall(raw)
            return self.receive_all(connection)

    def rejected_connection(self, server):
        # Admission runs before reading any request bytes. Receiving the
        # refusal immediately also avoids racing a header upload against the
        # early close, which can surface as an OS connection reset on Windows.
        with socket.create_connection(('127.0.0.1', server.server_port), timeout=WAIT) as connection:
            response = self.receive_all(connection)
        head, body = response.split(b'\r\n\r\n', 1)
        lines = head.decode('iso-8859-1').split('\r\n')
        return int(lines[0].split()[1]), dict(line.split(': ', 1) for line in lines[1:]), body

    @staticmethod
    def worker_exit(server, finished):
        original = server.process_request_thread
        def tracked(*args):
            try:
                return original(*args)
            finally:
                finished.set()
        return patch.object(server, 'process_request_thread', side_effect=tracked)

    def test_capacity_rejects_without_worker_or_route_and_recovers(self):
        entered = [threading.Event(), threading.Event()]
        release = threading.Event()
        self.releases.append(release)
        calls, lock = [], threading.Lock()
        def get(path, *args):
            with lock:
                calls.append(path)
            if path.startswith('/hold/'):
                entered[int(path[-1])].set()
                if not release.wait(WAIT):
                    raise AssertionError('Test did not release the held request')
            return Response(200, {'path': path})
        server = self.start_server(get=get, max_workers=2)
        responses = [self.start_client(server, path='/hold/' + str(index)) for index in range(2)]
        for event in entered:
            self.assertTrue(event.wait(WAIT), 'Held request did not reach its route')
        starts = []
        original_start = threading.Thread.start
        def tracked_start(worker):
            starts.append(worker.name)
            return original_start(worker)
        with patch('recruitment_insights.web.server.threading.Thread.start', new=tracked_start):
            status, headers, body = self.rejected_connection(server)
            http_status, http_headers, _ = self.request(server, '/rejected')
        self.assertEqual(status, 503)
        self.assertEqual(headers.get('Retry-After'), '1')
        self.assertEqual(headers.get('Connection', '').lower(), 'close')
        self.assertIn('error', json.loads(body))
        self.assertEqual(http_status, 503)
        self.assertEqual(http_headers.get('Retry-After'), '1')
        self.assertEqual(starts, [], 'Overload started a request thread')
        self.assertEqual(sorted(calls), ['/hold/0', '/hold/1'])
        release.set()
        for result in responses:
            self.assertEqual(result.result(WAIT)[0], 200)
        # The response body can arrive before the worker's final cleanup. Close
        # and join admitted workers before asserting the restored capacity.
        server._threads.join()
        self.assertEqual(self.request(server, '/recovered')[0], 200)

    def test_request_line_and_header_stalls_release_the_slot(self):
        for raw in (b'GET /ok HTTP/1.1', b'GET /ok HTTP/1.1\r\nHost:'):
            with self.subTest(raw=raw):
                calls = []
                server = self.start_server(get=self.record_calls(calls),
                                           max_workers=1, socket_timeout=0.15)
                setup, finished = threading.Event(), threading.Event()
                original_setup = server.RequestHandlerClass.setup
                def tracked_setup(handler):
                    original_setup(handler)
                    setup.set()
                with patch.object(server.RequestHandlerClass, 'setup', new=tracked_setup), \
                        self.worker_exit(server, finished):
                    with socket.create_connection(('127.0.0.1', server.server_port), timeout=WAIT) as connection:
                        connection.sendall(raw)
                        self.assertTrue(setup.wait(WAIT))
                        self.assertEqual(self.receive_all(connection), b'')
                    self.assertTrue(finished.wait(WAIT), 'Stalled worker retained its permit')
                self.assertEqual(calls, [])
                self.assertEqual(self.request(server)[0], 200)

    def test_incomplete_body_returns_408_without_executing_a_write(self):
        calls = []
        server = self.start_server(post=self.record_calls(calls),
                                   max_workers=1, socket_timeout=0.15)
        finished = threading.Event()
        raw = (f'POST /write HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n'
               'Content-Type: application/json\r\nX-Local-Request: 1\r\n'
               'Content-Length: 10\r\n\r\n{').encode('ascii')
        with self.worker_exit(server, finished):
            response = self.raw_request(server, raw)
            self.assertTrue(finished.wait(WAIT))
        self.assertTrue(response.startswith(b'HTTP/1.0 408'), response)
        self.assertEqual(calls, [])
        self.assertEqual(self.request(server)[0], 200)

    def test_business_timeout_is_not_mapped_to_client_body_timeout(self):
        def fail(*args):
            raise TimeoutError('Business operation failed after its body was read')
        server = self.start_server(post=fail, max_workers=1)
        with self.assertLogs('recruitment', level='ERROR'):
            status, _, payload = self.request(server, '/write', body=b'{}')
        self.assertEqual(status, 500)
        self.assertIn('error', json.loads(payload))
        server._threads.join()
        self.assertEqual(self.request(server)[0], 200)

    def test_handler_construction_failure_releases_the_slot(self):
        server = self.start_server(max_workers=1)
        finished, failed = threading.Event(), threading.Event()
        with patch.object(server, 'finish_request', side_effect=RuntimeError('handler construction failed')), \
                patch.object(server, 'handle_error', side_effect=lambda *args: failed.set()), \
                self.worker_exit(server, finished):
            response = self.raw_request(server, b'GET /ok HTTP/1.0\r\n\r\n')
            self.assertEqual(response, b'')
            self.assertTrue(failed.wait(WAIT))
            self.assertTrue(finished.wait(WAIT))
        self.assertEqual(self.request(server)[0], 200)

    def test_failure_after_response_does_not_append_503_and_releases_the_slot(self):
        server = self.start_server(max_workers=1)
        finished, failed = threading.Event(), threading.Event()
        expected = b'HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\n{}'
        def fail_after_response(request, client_address):
            # Leave the request unread to exercise the failed-worker drain.
            request.sendall(expected)
            raise RuntimeError('Handler failed after sending its response')
        with patch.object(server, 'finish_request', side_effect=fail_after_response), \
                patch.object(server, 'handle_error', side_effect=lambda *args: failed.set()) as handle_error, \
                self.worker_exit(server, finished):
            response = self.raw_request(server, b'GET /ok HTTP/1.0\r\n\r\n')
            self.assertTrue(failed.wait(WAIT))
            self.assertTrue(finished.wait(WAIT))
        self.assertEqual(response, expected, 'Failure changed or appended to the completed response')
        handle_error.assert_called_once()
        self.assertEqual(self.request(server)[0], 200)

    def test_error_handler_failure_closes_connection_and_releases_the_slot(self):
        server = self.start_server(max_workers=1)
        for error in (RuntimeError('Error handler failed'), KeyboardInterrupt('Error handler interrupted')):
            with self.subTest(error=type(error).__name__):
                request, client = socket.socketpair()
                with request, client:
                    client.settimeout(WAIT)
                    client.sendall(b'GET /ok HTTP/1.0\r\n\r\n')
                    client.shutdown(socket.SHUT_WR)
                    self.assertTrue(server._permits.acquire(blocking=False))
                    with patch.object(server, 'finish_request', side_effect=RuntimeError('Handler failed')), \
                            patch.object(server, 'handle_error', side_effect=error) as handle_error, \
                            self.assertRaises(type(error)):
                        server.process_request_thread(request, ('127.0.0.1', 0))
                    handle_error.assert_called_once()
                    self.assertEqual(client.recv(4096), b'')
                    self.assertEqual(request.fileno(), -1, 'Failed request socket remained open')
                self.assertEqual(self.request(server)[0], 200)
                server._threads.join()

    def test_thread_start_failure_rejects_then_recovers_and_closes(self):
        calls = []
        server = self.start_server(get=self.record_calls(calls),
                                   max_workers=1)
        original_start = threading.Thread.start
        listener = self.servers[-1][1]
        failed = threading.Event()
        def fail_start(worker):
            if threading.current_thread() is listener and not failed.is_set():
                failed.set()
                raise RuntimeError('Injected request-thread start failure')
            return original_start(worker)
        with patch('recruitment_insights.web.server.threading.Thread.start', new=fail_start), \
                self.assertLogs('recruitment', level='ERROR'):
            status, headers, _ = self.request(server)
        self.assertEqual(status, 503)
        self.assertEqual(headers.get('Retry-After'), '1')
        self.assertTrue(failed.is_set())
        self.assertEqual(calls, [])
        self.assertEqual(self.request(server)[0], 200)
        server.shutdown()
        server.server_close()  # Must not join the thread whose start failed.
        listener.join(WAIT)
        self.assertFalse(listener.is_alive())

    def test_ambiguous_framing_and_duplicate_trust_headers_do_not_reach_routes(self):
        calls = []
        server = self.start_server(get=self.record_calls(calls), post=self.record_calls(calls))
        host = f'Host: 127.0.0.1:{server.server_port}\r\n'
        post_headers = 'Content-Type: application/json\r\nX-Local-Request: 1\r\n'
        cases = [
            ('POST', host + post_headers + 'Content-Length: 2\r\nContent-Length: 2\r\n', 400),
            ('POST', host + post_headers + 'Content-Length: 2\r\nContent-Length: 50\r\n', 400),
            ('POST', host + post_headers + 'Transfer-Encoding: chunked\r\n', 400),
            ('POST', host + post_headers + 'Transfer-Encoding: chunked\r\nContent-Length: 2\r\n', 400),
            ('GET', host + 'Host: evil.example\r\n', 403),
            ('GET', 'Host: evil.example\r\n' + host, 403),
            ('POST', host + post_headers + f'Origin: http://127.0.0.1:{server.server_port}\r\n'
             'Origin: https://evil.example\r\nContent-Length: 2\r\n', 403),
        ]
        for method, headers, expected in cases:
            with self.subTest(headers=headers):
                raw = (method + ' /ok HTTP/1.1\r\n' + headers + '\r\n{}').encode('ascii')
                response = self.raw_request(server, raw)
                self.assertTrue(response.startswith(f'HTTP/1.0 {expected}'.encode('ascii')), response)
        self.assertEqual(calls, [])
        self.assertEqual(self.request(server, '/write', body=b'{}')[0], 200)
        self.assertEqual(len(calls), 1)

    def test_server_close_waits_for_real_write_to_commit(self):
        temporary = tempfile.TemporaryDirectory(prefix='recruitment_insights-admission-close-')
        self.temporary.append(temporary)
        store = Store(Path(temporary.name) / 'isolated.sqlite3')
        entered, release, committed = threading.Event(), threading.Event(), threading.Event()
        self.releases.append(release)
        def post(*args):
            with store.connect() as db:
                db.execute('INSERT INTO organizations(name) VALUES(?)', ('完成关闭前写入',))
                entered.set()
                if not release.wait(WAIT):
                    raise AssertionError('Test did not release its active write')
            committed.set()
            return Response(200, {'committed': True})
        server = self.start_server(post=post, max_workers=1, socket_timeout=0.15)
        response = self.start_client(server, path='/write', body=b'{}')
        self.assertTrue(entered.wait(WAIT))
        server.shutdown()
        joining, closed, close_result = threading.Event(), threading.Event(), Future()
        original_join = server._threads.join
        def tracked_join():
            joining.set()
            return original_join()
        def close():
            try:
                server.server_close()
                close_result.set_result(None)
            except BaseException as exc:
                close_result.set_exception(exc)
            finally:
                closed.set()
        with patch.object(server._threads, 'join', side_effect=tracked_join):
            closer = threading.Thread(target=close, name='admission-test-close', daemon=True)
            self.clients.append(closer)
            closer.start()
            self.assertTrue(joining.wait(WAIT), 'server_close never reached worker join')
            self.assertFalse(closed.is_set(), 'server_close returned while a write was still active')
            self.assertFalse(committed.is_set())
            release.set()
            self.assertTrue(committed.wait(WAIT))
            close_result.result(WAIT)
            self.assertEqual(response.result(WAIT)[0], 200)
            closer.join(WAIT)
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT name FROM organizations').fetchall()[0]['name'],
                             '完成关闭前写入')

    def test_invalid_capacity_and_timeout_are_rejected_before_listening(self):
        for setting, values in (
                ('max_workers', (0, -1, True, 1.5, '2', None)),
                ('socket_timeout', (0, -1, True, float('nan'), float('inf'), '2', None,
                                    1e10, 1e100, 10**1000))):
            for value in values:
                with self.subTest(setting=setting, value=value):
                    with self.assertRaises((TypeError, ValueError)):
                        self.start_server(**{setting: value})

    def test_client_disconnect_during_read_or_response_releases_the_slot(self):
        for during in ('read', 'response'):
            with self.subTest(during=during):
                entered, finished, release = threading.Event(), threading.Event(), threading.Event()
                self.releases.append(release)
                calls = []
                def get(path, *args):
                    calls.append(path)
                    if path == '/disconnect':
                        entered.set()
                        if not release.wait(WAIT):
                            raise AssertionError('Disconnect route was not released')
                        return Response(200, b'x' * (1024 * 1024))
                    return Response(200, {})
                server = self.start_server(get=get, max_workers=1, socket_timeout=0.15)
                original_setup = server.RequestHandlerClass.setup
                def tracked_setup(handler):
                    original_setup(handler)
                    if during == 'read':
                        entered.set()
                with patch.object(server.RequestHandlerClass, 'setup', new=tracked_setup), \
                        self.worker_exit(server, finished), \
                        self.assertNoLogs('recruitment', level='ERROR'):
                    with socket.create_connection(('127.0.0.1', server.server_port), timeout=WAIT) as connection:
                        raw = b'GET /disconnect HTTP/1.1\r\nHost:'
                        if during == 'response':
                            raw = (f'GET /disconnect HTTP/1.1\r\n'
                                   f'Host: 127.0.0.1:{server.server_port}\r\n\r\n').encode('ascii')
                        connection.sendall(raw)
                        self.assertTrue(entered.wait(WAIT))
                        # Force an active disconnect rather than a graceful
                        # FIN which can leave a response buffered successfully.
                        linger = struct.pack('HH' if os.name == 'nt' else 'ii', 1, 0)
                        connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
                    release.set()
                    self.assertTrue(finished.wait(WAIT), 'Disconnected worker retained its permit')
                self.assertEqual(calls, [] if during == 'read' else ['/disconnect'])
                self.assertEqual(self.request(server)[0], 200)

    def test_cli_invalid_limits_exit_before_database_initialization(self):
        import app
        cases = [('--max-workers', '0'), ('--max-workers', '-1'), ('--max-workers', '1.5'),
                 ('--socket-timeout', '0'), ('--socket-timeout', '-1'),
                 ('--socket-timeout', 'nan'), ('--socket-timeout', 'inf'),
                 ('--socket-timeout', '1e100')]
        for args in cases:
            with self.subTest(args=args), patch('sys.argv', ['app.py', *args]), \
                    patch('sys.stderr', new=io.StringIO()), patch.object(app, 'Store') as factory, \
                    patch.object(app, 'make_server') as server_factory:
                with self.assertRaises(SystemExit) as raised:
                    app.main()
                self.assertEqual(raised.exception.code, 2)
                factory.assert_not_called()
                server_factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
