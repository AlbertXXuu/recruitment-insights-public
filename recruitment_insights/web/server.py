"""Standard-library HTTP transport, local origin validation and error mapping."""
import json
import logging
import math
import socket
import threading
import time
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from recruitment_insights.domain.errors import ConflictError
from recruitment_insights.config import LEGACY_SESSION_COOKIE, WebPolicy
from recruitment_insights.web.routes import Response, Routes

LOG = logging.getLogger('recruitment')


def validate_http_limits(max_workers, socket_timeout):
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
        raise ValueError('请求线程上限必须是正整数')
    if (isinstance(socket_timeout, bool) or not isinstance(socket_timeout, (int, float))
            or socket_timeout <= 0):
        raise ValueError('连接空闲超时必须是有限的正数')
    try:
        if not math.isfinite(socket_timeout):
            raise ValueError('连接空闲超时必须是有限的正数')
        # Check the native socket range before listening or creating a database.
        # It differs from threading.TIMEOUT_MAX on Windows.
        with socket.socket() as probe:
            probe.settimeout(socket_timeout)
    except OverflowError as exc:
        raise ValueError('连接空闲超时超出本机支持范围') from exc


class BoundedHTTPServer(ThreadingHTTPServer):
    """Bound connection workers before starting threads; drain them on close."""

    daemon_threads = False
    block_on_close = True

    def __init__(self, address, handler, *, max_workers=16, socket_timeout=10):
        validate_http_limits(max_workers, socket_timeout)
        self.socket_timeout = socket_timeout
        self._permits = threading.BoundedSemaphore(max_workers)
        super().__init__(address, handler)

    def reject_request(self, request, *, respond=True):
        body = '{"error":"服务繁忙，请稍后重试"}'.encode('utf-8')
        response = (
            'HTTP/1.0 503 Service Unavailable\r\n'
            'Content-Type: application/json; charset=utf-8\r\n'
            f'Content-Length: {len(body)}\r\n'
            'Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n'
            'Retry-After: 1\r\nConnection: close\r\n\r\n').encode('ascii') + body
        deadline = time.monotonic() + 0.2
        try:
            # Refusal and failed-handler cleanup share a total I/O budget.
            request.settimeout(0.2)
            if respond:
                request.sendall(response)
            # Half-close first so HTTP clients can read the response and EOF.
            # Closing with unread request bytes can otherwise reset on Windows.
            request.shutdown(socket.SHUT_WR)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                request.settimeout(remaining)
                if not request.recv(4096):
                    break
        except OSError:
            pass
        finally:
            self.shutdown_request(request)

    def process_request(self, request, client_address):
        if not self._permits.acquire(blocking=False):
            self.reject_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            # ThreadingMixIn records the thread before start(); an unstarted
            # thread must be removed before server_close tries to join it.
            self._threads.reap()
            self._permits.release()
            LOG.exception('Request worker could not start')
            self.reject_request(request)

    def process_request_thread(self, request, client_address):
        completed = False
        try:
            try:
                self.finish_request(request, client_address)
                completed = True
            except Exception:
                self.handle_error(request, client_address)
        finally:
            try:
                if completed:
                    self.shutdown_request(request)
                else:
                    # The handler may have sent a response before failing.
                    self.reject_request(request, respond=False)
            finally:
                self._permits.release()


def make_server(store, port=8765, *, max_workers=16, socket_timeout=10, policy=None):
    policy = WebPolicy() if policy is None else policy
    if not isinstance(policy, WebPolicy):
        raise ValueError('HTTP配置必须为WebPolicy')
    routes = Routes(store, policy)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            # Cover the request line and headers as well as body/write I/O.
            self._post_body_read = False
            self.request.settimeout(self.server.socket_timeout)
            super().setup()

        def finish(self):
            try:
                super().finish()
            finally:
                if getattr(self, 'command', None) == 'POST' and not self._post_body_read:
                    # An early refusal may precede a separately sent body. Keep
                    # the response readable while draining late bytes briefly.
                    self.server.reject_request(self.request, respond=False)

        def handle(self):
            try:
                super().handle()
            except (ConnectionError, TimeoutError):
                self.close_connection = True

        def log_message(self, fmt, *args):
            LOG.info(fmt, *args)

        def send(self, response):
            data = response.data
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(response.status)
            headers = {
                'Content-Type': response.content_type, 'Content-Length': str(len(data)),
                'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'",
                **response.headers}
            if response.download:
                headers['Content-Disposition'] = f'attachment; filename="{response.download}"'
            for key, value in headers.items():
                for item in value if isinstance(value, tuple) else (value,):
                    self.send_header(key, item)
            self.end_headers()
            self.wfile.write(data)

        def valid_host(self):
            hosts = self.headers.get_all('Host', [])
            if policy.invited:
                return len(hosts) == 1 and hosts[0] == policy.test_origin[8:]
            return len(hosts) == 1 and hosts[0] in (
                f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

        def valid_trust_headers(self):
            # Proxies may provide these; none is used to select a trusted origin.
            for name in ('Origin', 'Cookie', 'Forwarded', 'X-Forwarded-Host',
                         'X-Forwarded-Proto', 'X-Forwarded-For', 'X-Forwarded-Port'):
                if len(self.headers.get_all(name, [])) > 1:
                    return False
            pairs = self.cookie_pairs()
            names = [name for name, value in pairs]
            if len(names) != len(set(names)):
                return False
            session_names = {policy.cookie_name}
            if not policy.invited:
                session_names.add(LEGACY_SESSION_COOKIE)
            return all(name not in session_names or re.fullmatch(r'[A-Za-z0-9_-]*', value)
                       for name, value in pairs)

        def cookie_pairs(self):
            result = []
            for part in self.headers.get('Cookie', '').split(';'):
                name, separator, value = part.partition('=')
                if separator:
                    value = value.strip()
                    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
                        value = value[1:-1]
                    result.append((name.strip(), value))
            return result

        def valid_framing(self):
            if (self.headers.get_all('Transfer-Encoding') is not None
                    or len(self.headers.get_all('Content-Length', [])) > 1):
                self.close_connection = True
                self.send(Response(400, {'error': '请求长度字段重复或使用了不支持的传输编码'}))
                return False
            return True

        def token(self):
            cookie = dict(self.cookie_pairs())
            if policy.cookie_name in cookie:
                return cookie[policy.cookie_name]
            if policy.invited:
                return ''
            return cookie.get(LEGACY_SESSION_COOKIE, '')

        def failure(self, exc, operation):
            if isinstance(exc, PermissionError):
                return Response(403, {'error': str(exc)})
            if isinstance(exc, ConflictError):
                return Response(409, {'error': str(exc)})
            if isinstance(exc, OverflowError):
                return Response(400, {'error': '参数超出支持范围'})
            if isinstance(exc, (ValueError, UnicodeError, TypeError)):
                return Response(400, {'error': str(exc)})
            LOG.exception('%s failed', operation)
            return Response(500, {'error': '读取失败，请查看本机运行日志' if operation == 'GET' else '操作失败，请查看本机运行日志'})

        def do_GET(self):
            if not self.valid_host() or not self.valid_trust_headers():
                return self.send(Response(403, {'error': '请求地址或头字段不符合工作空间配置'}))
            if not self.valid_framing():
                return
            try:
                parsed = urlsplit(self.path)
                response = routes.get(parsed.path, parsed.query, self.token())
            except Exception as exc:
                response = self.failure(exc, 'GET')
            self.send(response)

        def do_POST(self):
            origin, port = self.headers.get('Origin'), self.server.server_port
            origins = (policy.test_origin,) if policy.invited else (None, f'http://127.0.0.1:{port}', f'http://localhost:{port}')
            if (not self.valid_host() or not self.valid_trust_headers() or origin not in origins):
                return self.send(Response(403, {'error': '拒绝跨站写入'}))
            if not self.valid_framing():
                return
            if self.headers.get('X-Local-Request') != '1':
                return self.send(Response(403, {'error': '缺少本机请求标记'}))
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                return self.send(Response(415, {'error': '请使用 application/json'}))
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError as exc:
                return self.send(self.failure(exc, 'POST'))
            if not 0 <= length <= 3*1024*1024:
                return self.send(Response(413, {'error': '请求超过 3 MB 限制'}))
            try:
                raw = self.rfile.read(length)
                self._post_body_read = len(raw) == length
            except TimeoutError:
                return self.send(Response(408, {'error': '请求内容读取超时，请重试'}))
            except ConnectionError:
                self.close_connection = True
                return
            try:
                if len(raw) != length:
                    raise ValueError('请求内容不完整')
                body = json.loads(raw or b'{}')
                if not isinstance(body, dict):
                    raise ValueError('请求必须是 JSON 对象')
                response = routes.post(self.path, body, self.token())
            except Exception as exc:
                response = self.failure(exc, 'POST')
            self.send(response)

    server = BoundedHTTPServer(('127.0.0.1', port), Handler,
                               max_workers=max_workers, socket_timeout=socket_timeout)
    server.accounts = store.accounts
    return server
