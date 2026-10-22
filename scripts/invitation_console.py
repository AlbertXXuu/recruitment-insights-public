"""Interactive, single-process operator console for the invitation source."""
import argparse
import json
import logging
import os
import signal
import socket
import sqlite3
import stat
import sys
import threading
import webbrowser
from collections import deque
from contextlib import closing, contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import validate_test_database, validate_invitation_runtime as validate_runtime
from core import Store
from recruitment_insights.config import ROOT, WebPolicy
from recruitment_insights.web.server import make_server, validate_http_limits
from scripts import provision_test_account
from scripts.invitation_tunnel import InvitationTunnel

from scripts.deployment_settings import ORIGIN as TEST_ORIGIN
RUNTIME_DIRECTORY = ROOT / 'outputs/invitation-test'
LOG = logging.getLogger('recruitment.console')


def reject_links(path):
    for component in (path, *path.parents):
        if component.exists() or component.is_symlink():
            details = component.lstat()
            if (component.is_symlink() or
                    getattr(details, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT or
                    (stat.S_ISREG(details.st_mode) and details.st_nlink > 1)):
                raise ValueError('控制台运行路径不能经过链接或共享文件')


def load_registration_policy():
    """Read this project's non-secret opt-in on each normal console opening."""
    path = RUNTIME_DIRECTORY / 'web-policy.json'
    try:
        reject_links(path)
        if not path.exists():
            return False
        if not path.is_file():
            raise ValueError('普通端注册配置必须是普通文件')
        with path.open('rb') as stream:
            raw = stream.read(4097)
    except OSError as exc:
        raise ValueError('无法读取普通端注册配置，请检查文件及权限') from exc
    if len(raw) > 4096:
        raise ValueError('普通端注册配置不能超过 4096 字节')
    try:
        values = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('普通端注册配置须为有效的 UTF-8 JSON') from exc
    if not isinstance(values, dict) or set(values) != {'allow_candidate_registration'}:
        raise ValueError('普通端注册配置必须是仅含 allow_candidate_registration 的 JSON 对象')
    allowed = values['allow_candidate_registration']
    if not isinstance(allowed, bool):
        raise ValueError('普通端注册配置 allow_candidate_registration 必须为布尔值 true 或 false')
    return allowed


class ConsoleLock:
    """OS-held lock; restarting the PC needs no stale PID cleanup."""
    def __init__(self, directory):
        self.path = Path(directory) / 'console.lock'
        self.handle = None

    def __enter__(self):
        reject_links(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open('a+b')
        try:
            if self.path.stat().st_size == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError('控制台已经打开，请使用原窗口；不会停止或接管其他进程') from exc
        self.handle = handle
        return self

    def __exit__(self, *exc):
        if self.handle is not None:
            self.handle.close()  # Closing releases the OS lock, even after a crash.
            self.handle = None


def require_prepared_database(database):
    chosen = validate_test_database(str(database))
    if not chosen.exists():
        raise ValueError('还没有独立测试库，请先选择“准备账号”创建管理员')
    try:
        with closing(sqlite3.connect(chosen.as_uri() + '?mode=ro&immutable=1', uri=True)) as connection:
            count = connection.execute("SELECT count(*) FROM users WHERE role='admin'").fetchone()[0]
        if not count:
            raise ValueError('独立测试库尚无管理员，请先选择“准备账号”')
    except sqlite3.Error as exc:
        raise ValueError('测试库尚未准备好，请核对账号准备结果') from exc
    return chosen


class InvitationController:
    def __init__(self, database, origin=TEST_ORIGIN, port=8775, max_workers=16, socket_timeout=10,
                 *, allow_candidate_registration=False):
        self.database = Path(database)
        self.policy = WebPolicy(origin, allow_candidate_registration=allow_candidate_registration)
        if not self.policy.invited:
            raise ValueError('此控制台仅用于独立 HTTPS 邀请模式')
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError('端口必须在 0–65535 之间')
        validate_http_limits(max_workers, socket_timeout)
        self.port, self.max_workers, self.socket_timeout = port, max_workers, socket_timeout
        self.server = self.thread = None
        self.last_error = None
        self.tunnel = InvitationTunnel(RUNTIME_DIRECTORY, origin=origin, source_port=port)

    @property
    def running(self):
        return self.server is not None and self.thread is not None and self.thread.is_alive()

    def check_port(self):
        with socket.socket() as probe:
            if os.name == 'nt':
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(('127.0.0.1', self.port))

    def start(self):
        if self.running:
            return False
        if self.tunnel.running and self.tunnel.last_error:
            raise ValueError('请先选择 8 正常停止未确认退出的 Tunnel，再启动源站')
        if self.server is not None:
            self.stop()
        validate_runtime()
        database = require_prepared_database(self.database)
        self.check_port()  # Refuse an occupied port before Store initialization.
        store = Store(database)
        server = make_server(store, self.port, max_workers=self.max_workers,
                             socket_timeout=self.socket_timeout, policy=self.policy)
        entered = threading.Event()
        self.last_error = None

        def serve():
            entered.set()
            try:
                server.serve_forever(poll_interval=0.2)
            except Exception as exc:
                self.last_error = str(exc)
                LOG.exception('Invitation source stopped unexpectedly')

        thread = threading.Thread(target=serve, name='invitation-http', daemon=False)
        try:
            thread.start()
        except BaseException:
            if thread.is_alive():
                server.shutdown()
            server.server_close()
            if thread.ident is not None:
                thread.join()
            raise
        self.server, self.thread = server, thread
        entered.wait()
        LOG.info('Invitation source started on loopback port %s', server.server_port)
        return True

    def stop(self):
        server, thread = self.server, self.thread
        tunnel_error = None
        tunnel_stopped = False
        try:
            tunnel_stopped = self.tunnel.stop()
        except RuntimeError as exc:
            tunnel_error = exc
        finally:
            # A stuck connector must lose its source, even if its exit could not
            # be confirmed. Keep its Popen ownership available for menu retry.
            if server is not None:
                if thread.is_alive():
                    server.shutdown()
                server.server_close()  # Wait for in-flight writes to commit.
                thread.join()
                self.server = self.thread = None
                LOG.info('Invitation source stopped normally')
        if tunnel_error:
            raise tunnel_error
        return server is not None or tunnel_stopped

    def start_tunnel(self):
        if not self.running:
            raise ValueError('请先启动源站，再启动 Tunnel')
        return self.tunnel.start()


@contextmanager
def uninterrupted_stop():
    # A second Ctrl+C during worker drain must not turn a normal stop into a kill.
    main_thread = threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN) if main_thread else None
    try:
        yield
    finally:
        if main_thread:
            signal.signal(signal.SIGINT, previous)


def prepare_account(controller, input_fn, output):
    if controller.server is not None or controller.tunnel.running:
        raise ValueError('请先正常停止源站与 Tunnel，再准备账号')
    controller.check_port()
    output('只新增独立邀请账号，密码随后隐藏输入两次。HR 由企业负责人在网页中创建。')
    role = input_fn('角色（admin / boss / candidate）：').strip()
    args = ['--db', str(controller.database), '--role', role,
            '--username', input_fn('账号名称：').strip(),
            '--display-name', input_fn('显示名称：').strip()]
    if role == 'boss':
        args.extend(['--organization', input_fn('企业名称：').strip()])
    try:
        provision_test_account.main(args)
    except SystemExit as exc:
        output('账号未创建，请按上面的提示修正后重试。')
        return exc.code
    return 0


def run_console(controller, input_fn=input, output=print, log_path=None, start_services=False):
    output('recruitment-insights | 本机邀请测试控制台')
    output('首次先准备 admin 账号；已有账号无需重建。公网可用性以实际网址为准。')
    output('组员通过应用账号登录；HTTPS 和各角色业务流程仍须从真实邀请网址实测。')
    output('本实例普通端注册：' + ('开启（仅求职者账号）' if controller.policy.registration_enabled else '关闭（使用已有账号）'))
    output('关闭或重启电脑前，选择 0 正常停止并退出；不要直接关闭正在运行的窗口。')
    try:
        if start_services:
            try:
                controller.start()
                connected = controller.start_tunnel()
                output('源站与 Tunnel 已启动并连接；公网登录和业务流程仍须实测。' if connected
                       else controller.tunnel.last_error or 'Tunnel 尚未连接，请选择 7 检查。')
            except (ValueError, OSError, RuntimeError) as exc:
                output(f'自动启动未完成：{exc}；可以在下面菜单继续操作。')
        while True:
            state = '运行中' if controller.running else ('异常停止，请查看日志' if controller.last_error else '已停止')
            output('\n' + '-' * 56)
            output(f'源站：{state}  |  公网入口状态请另行核对')
            output(f'Tunnel：{controller.tunnel.status()}')
            output('1 启动服务    2 正常停止    3 运行详情')
            output('4 准备账号    5 最近日志    6 打开邀请网址    0 停止并退出')
            output('7 启动或检查 Tunnel    8 正常停止 Tunnel')
            try:
                choice = input_fn('请选择：').strip()
            except (EOFError, KeyboardInterrupt):
                output('\n正在退出控制台。')
                return
            try:
                if choice == '0':
                    with uninterrupted_stop():
                        controller.stop()
                    return
                if choice == '1':
                    with uninterrupted_stop():
                        started = controller.start()
                    output('源站已启动。' if started else '源站已经运行，无需重复启动。')
                    output('运行期间保持这个窗口打开；完成 Tunnel 入口配置和实测后，组员从邀请网址使用应用账号登录。')
                elif choice == '2':
                    output('正在停止接收请求，并等待已开始的操作完成……')
                    with uninterrupted_stop():
                        stopped = controller.stop()
                    output('源站已正常停止，测试数据保留。' if stopped else '源站原本就已停止。')
                elif choice == '3':
                    actual_port = controller.server.server_port if controller.server else controller.port
                    output(f'源站：127.0.0.1:{actual_port}（仅本机监听）')
                    output(f'邀请网址：{controller.policy.test_origin}')
                    output(f'独立测试库：{controller.database}')
                    output(f'Python：{sys.version.split()[0]}；SQLite：{sqlite3.sqlite_version}')
                    output(f'运行时：{sys.executable}')
                    if log_path:
                        output(f'日志：{log_path}')
                    output('入口完成配置和实测后从邀请网址访问；直接打开 loopback 地址会被 Host 策略拒绝。')
                elif choice == '4':
                    prepare_account(controller, input_fn, output)
                elif choice == '5':
                    if log_path and Path(log_path).exists():
                        with Path(log_path).open(encoding='utf-8', errors='replace') as stream:
                            output(''.join(deque(stream, maxlen=30)).rstrip() or '暂无日志。')
                    else:
                        output('暂无日志。')
                elif choice == '6':
                    if not controller.running:
                        output('请先启动源站。')
                    else:
                        output('正在打开邀请网址；可访问性与应用账号登录仍以真实入口实测为准。')
                        webbrowser.open(controller.policy.test_origin)
                elif choice == '7':
                    with uninterrupted_stop():
                        connected = controller.start_tunnel()
                    output('Tunnel 已连接 edge；HTTPS、应用账号登录和业务流程仍须实测。' if connected
                           else controller.tunnel.last_error or 'Tunnel 尚未确认连接，请查看状态。')
                elif choice == '8':
                    with uninterrupted_stop():
                        tunnel_stopped = controller.tunnel.stop()
                    output('Tunnel 已正常停止。源站保留运行。' if tunnel_stopped else 'Tunnel 原本就未运行。')
                else:
                    output('请输入菜单中的数字。')
            except (ValueError, OSError, RuntimeError) as exc:
                output(f'未完成：{exc}')
                if controller.tunnel.running and not controller.running:
                    output('源站已停止；保留窗口，选择 8 重试停止本控制台的 Tunnel。')
                elif 'checkpoint' in str(exc) or '旁文件' in str(exc):
                    output('保留 WAL / SHM 文件，按恢复说明处理，不要删除它们。')
            except KeyboardInterrupt:
                output('\n操作已取消。')
    finally:
        output('正在正常停止源站，请等待正在执行的操作完成……')
        try:
            with uninterrupted_stop():
                controller.stop()
        except RuntimeError as exc:
            output(f'源站已停止，但控制台不能确认 Tunnel 退出：{exc}')
            output('保留这条子进程 PID 信息；不要把本次退出视为完整停机。')
            raise
        output('控制台已退出。独立测试库保留，重启后可再次打开同一入口。')


def main(argv=None):
    parser = argparse.ArgumentParser(description='本机邀请测试控制台；管理源站、已配置 Tunnel 和应用账号。')
    parser.add_argument('--check-runtime', action='store_true', help='只检查运行时，不读取或创建数据库')
    parser.add_argument('--start-services', action='store_true', help='打开后启动源站与已配置 Tunnel，保留交互控制台')
    parser.add_argument('--db', default=str(RUNTIME_DIRECTORY / 'test.sqlite3'))
    parser.add_argument('--port', type=int, default=8775)
    parser.add_argument('--origin', default=TEST_ORIGIN)
    args = parser.parse_args(argv)
    try:
        versions = validate_runtime()
        if args.check_runtime:
            print(f"运行时通过：Python {versions['python']} / SQLite {versions['sqlite']}")
            return 0
        if not sys.stdin.isatty():
            raise ValueError('请双击 invitation-console.cmd，在交互控制台中操作；不接受重定向输入')
        allow_registration = load_registration_policy()
        controller = InvitationController(args.db, args.origin, args.port,
                                          allow_candidate_registration=allow_registration)
        log_path = RUNTIME_DIRECTORY / 'console.log'
        with ConsoleLock(RUNTIME_DIRECTORY):
            reject_links(log_path)
            handler = RotatingFileHandler(log_path, maxBytes=2 * 1024 * 1024,
                                          backupCount=3, encoding='utf-8')
            handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
            logger = logging.getLogger('recruitment')
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)
            try:
                run_console(controller, log_path=log_path, start_services=args.start_services)
            finally:
                logger.removeHandler(handler)
                handler.close()
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(f'控制台未打开：{exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
