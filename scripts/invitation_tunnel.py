"""Own one invitation connector without exposing its token or raw output."""
import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import signal
import socket
import subprocess
import time
from http.client import HTTPConnection, HTTPException
from pathlib import Path

from scripts.tunnel_proxy import LoopbackEdgeProxy


from scripts.deployment_settings import ORIGIN, HOSTNAME, TUNNEL_ID as PINNED_TUNNEL_ID, ACCOUNT as PINNED_ACCOUNT_ID
METRICS_PORT = 8776
LOG = logging.getLogger('recruitment.tunnel')


def validate_tunnel_token(token):
    """Bind a locally supplied run token to this invitation account and tunnel."""
    error = 'Tunnel 运行 Token 格式或固定账号/Tunnel 身份不符合要求'
    if not isinstance(token, str) or not 0 < len(token) <= 16384:
        raise ValueError(error) from None
    try:
        decoded = base64.b64decode(token, validate=True)
        if base64.b64encode(decoded).decode('ascii') != token:
            raise ValueError()
        payload = json.loads(decoded.decode('utf-8'))
        # Go matches struct fields without case sensitivity; reject aliases so
        # cloudflared cannot interpret a different identity from this validator.
        if (not isinstance(payload, dict) or set(payload) - {'a', 't', 's', 'e'} or
                payload.get('a') != PINNED_ACCOUNT_ID or
                payload.get('t') != PINNED_TUNNEL_ID):
            raise ValueError()
        secret = payload.get('s')
        if not isinstance(secret, str) or not secret:
            raise ValueError()
        decoded_secret = base64.b64decode(secret, validate=True)
        if not decoded_secret or base64.b64encode(decoded_secret).decode('ascii') != secret:
            raise ValueError()
    except (ValueError, RecursionError):
        raise ValueError(error) from None


class InvitationTunnel:
    def __init__(self, directory, *, origin=ORIGIN, source_port=8775):
        self.directory = Path(directory)
        self.origin, self.source_port = origin, source_port
        self.process = None
        self.ready = False
        self.last_error = None
        self.edge_proxy = None
        self.edge_proxy_port = None
        self.edge_preferred_addresses = ()

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None

    def validate_files(self):
        # Delayed import avoids a console/tunnel composition cycle.
        from scripts.invitation_console import reject_links
        if self.origin != ORIGIN or self.source_port != 8775:
            raise ValueError('Tunnel 仅支持已配置的精确邀请网址与源站端口 8775')
        paths = {
            'runtime': self.directory / 'tunnel-runtime.json',
            'binary': self.directory / 'tools' / 'cloudflared.exe',
            'token': self.directory / 'tunnel-token.txt',
            'config': self.directory / 'cloudflared-config.json',
        }
        for key, path in paths.items():
            reject_links(path)
            if key != 'config' and not path.is_file():
                raise ValueError('Tunnel 尚未配置完成；请先完成独立入口配置')
        if not 0 < paths['runtime'].stat().st_size <= 16384:
            raise ValueError('Tunnel 配置文件不符合要求')
        try:
            settings = json.loads(paths['runtime'].read_text(encoding='utf-8'))
        except (ValueError, UnicodeError):
            raise ValueError('Tunnel 配置文件无法解析') from None
        if not isinstance(settings, dict):
            raise ValueError('Tunnel 配置文件不符合要求')
        if (settings.get('name') != 'recruitment-insights' or
                settings.get('hostname') != HOSTNAME):
            raise ValueError('Tunnel 配置不属于此独立邀请入口')
        if settings.get('tunnel_id') != PINNED_TUNNEL_ID:
            raise ValueError('Tunnel 标识不属于本次独立邀请入口')
        if settings.get('authentication') != 'application':
            raise ValueError('Tunnel 仅支持显式 application 应用账号登录模式；旧 Access 或未知配置不会启动')
        if 'team_name' in settings or 'access_aud' in settings:
            raise ValueError('Tunnel 配置仍含旧 Access 字段；请先重新配置应用账号登录模式')
        expected = settings.get('cloudflared_sha256')
        if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise ValueError('Tunnel 缺少已验证的客户端校验值')
        digest = hashlib.sha256()
        with paths['binary'].open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError('Tunnel 客户端与已验证版本不符，不会启动')
        if not 0 < paths['token'].stat().st_size <= 16384:
            raise ValueError('Tunnel 凭据文件尚未准备好')
        try:
            with paths['token'].open('rb') as stream:
                token = stream.read(16384).decode('ascii').strip()
        except UnicodeError:
            raise ValueError('Tunnel 运行 Token 格式或固定账号/Tunnel 身份不符合要求') from None
        validate_tunnel_token(token)
        if paths['config'].exists() and not paths['config'].is_file():
            raise ValueError('Tunnel 本地配置路径必须为普通文件')
        proxy_port = settings.get('edge_proxy_port')
        preferred = settings.get('edge_preferred_addresses', [])
        if proxy_port is not None:
            if isinstance(proxy_port, bool) or not isinstance(proxy_port, int) or not 1 <= proxy_port <= 65535:
                raise ValueError('Tunnel 本地代理端口不符合要求')
        if (not isinstance(preferred, list) or len(preferred) > 2 or
                (preferred and proxy_port is None)):
            raise ValueError('Tunnel 首选节点配置不符合要求')
        try:
            for address in preferred:
                if not isinstance(address, str) or str(ipaddress.IPv4Address(address)) != address:
                    raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('Tunnel 首选节点配置不符合要求') from None
        self.edge_proxy_port = proxy_port
        self.edge_preferred_addresses = tuple(preferred)
        return paths

    @staticmethod
    def check_metrics_port():
        with socket.socket() as probe:
            if os.name == 'nt':
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(('127.0.0.1', METRICS_PORT))

    def check_ready(self):
        self.ready = False
        if not self.running:
            return False
        connection = HTTPConnection('127.0.0.1', METRICS_PORT, timeout=0.5)
        try:
            connection.request('GET', '/ready')
            response = connection.getresponse()
            response.read(4096)  # Never display connector output or response bodies.
            self.ready = response.status == 200 and self.running
        except (OSError, TimeoutError, HTTPException):
            pass
        finally:
            connection.close()
        return self.ready

    def wait_ready(self, timeout):
        deadline = time.monotonic() + timeout
        while self.running:
            if self.check_ready():
                self.last_error = None
                LOG.info('Invitation connector is connected to the edge')
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                self.process.wait(timeout=min(0.25, remaining))
            except subprocess.TimeoutExpired:
                pass
        self.ready = False
        self.last_error = ('Tunnel 连接等待超时，进程仍在运行；选择 7 再检查或 8 停止后重试'
                           if self.running else 'Tunnel 客户端已退出，尚未建立连接；选择 7 重试')
        LOG.warning('%s', self.last_error)
        return False

    def start(self, *, timeout=15):
        if self.running:
            return self.wait_ready(timeout)
        if self.process is not None or self.edge_proxy is not None:
            self.stop()  # Clean up an exited client's relay before a retry.
        paths = self.validate_files()
        self.check_metrics_port()
        # An explicit empty config prevents ~/.cloudflared/config.yml discovery.
        # Actual ingress remains remotely configured; the application handles login.
        paths['config'].write_text('{}\n', encoding='utf-8')
        environment = {key: value for key, value in os.environ.items()
                       if not key.upper().startswith(('TUNNEL_', 'CLOUDFLARE_', 'CLOUDFLARED_'))}
        arguments = [str(paths['binary']), 'tunnel', '--config', str(paths['config']),
                     '--no-autoupdate', '--metrics', '127.0.0.1:8776']
        if self.edge_proxy_port is not None:
            self.edge_proxy = LoopbackEdgeProxy(self.edge_proxy_port, self.edge_preferred_addresses)
            edges = self.edge_proxy.start()
            arguments.extend(['--protocol', 'http2', '--no-prechecks'])
            for edge in edges:
                arguments.extend(['--edge', edge])
        arguments.extend(['run', '--token-file', str(paths['token'])])
        options = dict(cwd=str(self.directory), env=environment, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options['start_new_session'] = True
        self.ready = False
        self.last_error = None
        try:
            self.process = subprocess.Popen(arguments, **options)
        except BaseException:
            if self.edge_proxy is not None:
                self.edge_proxy.stop()
                self.edge_proxy = None
            raise
        LOG.info('Invitation connector started, PID %s', self.process.pid)
        return self.wait_ready(timeout)

    def stop(self, *, timeout=30):
        if self.process is None:
            if self.edge_proxy is None:
                return False
            self.edge_proxy.stop()
            self.edge_proxy = None
            return True
        process = self.process
        if process.poll() is None:
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT if os.name == 'nt' else signal.SIGTERM)
                process.wait(timeout=timeout)
            except (OSError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    self.ready = False
                    self.last_error = (f'Tunnel 未确认退出（本控制台子进程 PID {process.pid}）；'
                                       '源站将停止，请选择 8 重试，不能直接关闭窗口')
                    LOG.warning('%s', self.last_error)
                    raise RuntimeError(self.last_error) from None
        else:
            process.wait()
        if self.edge_proxy is not None:
            self.edge_proxy.stop()
            self.edge_proxy = None
        self.process = None
        self.ready = False
        self.last_error = None
        LOG.info('Invitation connector stopped normally')
        return True

    def status(self):
        if not self.running:
            self.ready = False
            return '已退出' if self.process is not None else '未运行'
        return '已连接 edge（公网验收另行核对）' if self.check_ready() else '进程运行中，尚未确认 edge 连接'
