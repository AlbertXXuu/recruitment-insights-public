"""Stable command-line entry point for the local recruitment application."""
import argparse
import logging
import threading
import hashlib
import hmac
import os
import sqlite3
import re
import stat
import sys
from contextlib import closing
from pathlib import Path

from core import Store
from recruitment_insights.config import ROOT, PROJECT_NAME, DEMO_PASSWORD, WebPolicy
from recruitment_insights.web.server import make_server, validate_http_limits
from recruitment_insights.infrastructure.database import REQUIRED_COLUMNS, SCHEMA_VERSION

LOG = logging.getLogger('recruitment')


def validate_invitation_runtime():
    """Check the invitation WAL runtime before opening a database."""
    if sys.version_info < (3, 11):
        raise ValueError('需要 Python 3.11 或更新版本')
    if sqlite3.sqlite_version_info < (3, 51, 3):
        raise ValueError(f'SQLite {sqlite3.sqlite_version} 未达到邀请模式的 3.51.3 门槛，请配置已修复的运行时')
    try:
        with closing(sqlite3.connect(':memory:')) as connection:
            result = connection.execute(
                '''SELECT json_valid('{"n":7}'), json_extract('{"n":7}', '$.n'),
                          json_type('{"n":7}', '$.n')''').fetchone()
        if result != (1, 7, 'integer'):
            raise ValueError('当前 SQLite 缺少应用所需的 JSON 功能')
    except sqlite3.Error as exc:
        raise ValueError('当前 SQLite 缺少应用所需的 JSON 功能') from exc
    return {'python': sys.version.split()[0], 'sqlite': sqlite3.sqlite_version}


def validate_test_database(path):
    """Read-only preflight before Store can initialize/migrate the chosen DB."""
    if path is None or not Path(path).is_absolute():
        raise ValueError('邀请模式必须通过 --db 指定独立测试库的绝对路径')
    chosen = Path(path).resolve()
    protected = (ROOT/'data/recruitment.sqlite3',
                 ROOT/'deliverables/team-handoff/04_组员测试版/data/recruitment.sqlite3')
    for original in protected:
        target = original.resolve()
        if (os.path.normcase(str(chosen)) == os.path.normcase(str(target)) or
                (chosen.exists() and target.exists() and chosen.samefile(target))):
            raise ValueError('邀请模式不能使用正式库或已有组员库')
    # A test database must not alias a different path through links/junctions.
    entered = Path(path)
    for component in (entered, *entered.parents):
        if component.exists() or component.is_symlink():
            details = component.lstat()
            if (component.is_symlink() or
                    getattr(details, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise ValueError('测试库路径不能经过符号链接或目录联接')
    if not chosen.exists():
        return chosen
    if not chosen.is_file():
        raise ValueError('测试数据库路径必须是文件')
    if chosen.stat().st_nlink > 1:
        raise ValueError('测试数据库不能与其他路径共享硬链接')
    sidecars = [Path(str(chosen) + suffix) for suffix in ('-wal', '-shm', '-journal')]
    if any(p.exists() for p in sidecars):
        raise ValueError('请先正常停止测试库的其他连接并完成checkpoint；不能检查在用或待恢复的库')
    before = chosen.stat()
    try:
        # Only an offline, checkpointed file is accepted. immutable prevents
        # SQLite read-only WAL access from creating WAL/SHM sidecars.
        with closing(sqlite3.connect(chosen.as_uri() + '?mode=ro&immutable=1', uri=True)) as connection:
            objects = connection.execute('SELECT name, type FROM sqlite_master').fetchall()
            tables = {name for name, kind in objects if kind == 'table'}
            if any(not name.startswith('sqlite_') for name, kind in objects):
                if not (set(REQUIRED_COLUMNS) | {'schema_migrations'}) <= tables:
                    raise ValueError('已有测试库缺少当前版本的真实数据表，不能自动迁移为邀请库')
                versions = [row[0] for row in connection.execute('SELECT version FROM schema_migrations ORDER BY version')]
                if versions != list(range(1, SCHEMA_VERSION + 1)):
                    raise ValueError('已有邀请库必须使用当前数据库版本')
                for table, required in REQUIRED_COLUMNS.items():
                    columns = {row[1] for row in connection.execute('PRAGMA table_info(' + table + ')')}
                    if not required <= columns:
                        raise ValueError('测试数据库结构与当前版本不符：' + table)
                if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
                    raise ValueError('测试数据库存在无关联记录')
                for (encoded,) in connection.execute('SELECT password_hash FROM users'):
                    if not isinstance(encoded, str) or not re.fullmatch(r'[0-9a-f]{32}:[0-9a-f]{64}', encoded):
                        raise ValueError('测试库包含不受支持的密码记录')
                    salt, expected = encoded.split(':')
                    actual = hashlib.pbkdf2_hmac('sha256', DEMO_PASSWORD.encode(), bytes.fromhex(salt), 240000).hex()
                    if hmac.compare_digest(actual, expected):
                        raise ValueError('测试库含公开演示密码，须重新建立独立邀请账号')
    except (sqlite3.Error, TypeError, AttributeError) as exc:
        raise ValueError('无法只读检查测试库，请使用新测试库') from exc
    after = chosen.stat()
    if (any(p.exists() for p in sidecars) or
            (before.st_mtime_ns, before.st_size, before.st_ino) !=
            (after.st_mtime_ns, after.st_size, after.st_ino)):
        raise ValueError('检查期间测试库发生变化，请停止其他连接后重试')
    return chosen


def main():
    parser = argparse.ArgumentParser(description=f'{PROJECT_NAME} · 招聘数据分析系统（仅本机）')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--db', help='数据库路径；邀请模式必须为独立绝对路径')
    parser.add_argument('--test-origin', help='邀请测试的精确 HTTPS 来源；不创建DNS或TLS')
    parser.add_argument('--allow-candidate-registration', action='store_true',
                        help='在 --test-origin 模式显式允许普通端注册；默认仅邀请账号')
    parser.add_argument('--sync-interval', type=int, default=0)
    parser.add_argument('--max-workers', type=int, default=16, help='同时处理的连接上限（默认 16）')
    parser.add_argument('--socket-timeout', type=float, default=10,
                        help='连接读写空闲超时秒数（默认 10，不是业务总时限）')
    args = parser.parse_args()
    if args.sync_interval and args.sync_interval < 300:
        parser.error('定时同步间隔至少 300 秒')
    try:
        validate_http_limits(args.max_workers, args.socket_timeout)
        if args.allow_candidate_registration and not args.test_origin:
            raise ValueError('--allow-candidate-registration 仅用于 --test-origin；本机默认允许普通端注册')
        policy = WebPolicy(args.test_origin,
                           allow_candidate_registration=args.allow_candidate_registration)
        if policy.invited:
            validate_invitation_runtime()
        database = validate_test_database(args.db) if policy.invited else (args.db or ROOT/'data/recruitment.sqlite3')
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    store = Store(database)
    server = make_server(store, args.port, max_workers=args.max_workers,
                         socket_timeout=args.socket_timeout, policy=policy)
    stop = threading.Event()
    if args.sync_interval:
        def sync_loop():
            while not stop.wait(args.sync_interval):
                try:
                    store.sync()
                except Exception:
                    LOG.exception('Scheduled sync failed; existing data retained')
        threading.Thread(target=sync_loop, daemon=True).start()
    print(f'READY http://127.0.0.1:{server.server_port} | database: {store.path}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


if __name__ == '__main__':
    main()
