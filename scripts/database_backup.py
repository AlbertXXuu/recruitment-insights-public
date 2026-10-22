"""Verified SQLite snapshots and recovery to a new file; never overwrite a DB."""
import argparse
import hashlib
import json
import math
import os
import sqlite3
import stat
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recruitment_insights.config import ROOT
from recruitment_insights.infrastructure.database import MIGRATIONS, REQUIRED_COLUMNS, SCHEMA_VERSION


SIDECARS = ('-wal', '-shm', '-journal')


def plain_path(value, description):
    entered = Path(value)
    if not entered.is_absolute():
        raise ValueError(f'{description}必须是绝对路径')
    for component in (entered, *entered.parents):
        if component.exists() or component.is_symlink():
            details = component.lstat()
            if (component.is_symlink() or
                    getattr(details, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise ValueError(f'{description}不能经过符号链接或目录联接')
    resolved = entered.resolve()
    if resolved.exists() and (not resolved.is_file() or resolved.stat().st_nlink > 1):
        raise ValueError(f'{description}必须是普通文件且不能共享硬链接')
    return resolved


def sidecars(database):
    return [Path(str(database) + suffix) for suffix in SIDECARS]


def validate_paths(source, target, mode):
    source = plain_path(source, '来源路径')
    target = plain_path(target, '目标路径')
    protected = (ROOT / 'data/recruitment.sqlite3',
                 ROOT / 'deliverables/team-handoff/04_组员测试版/data/recruitment.sqlite3')
    protected_names = [name for database in protected for name in (database, *sidecars(database))]
    if any(os.path.normcase(str(target)) == os.path.normcase(str(path.resolve())) for path in protected_names):
        raise ValueError('正式库、组员库及其附属文件不能作为备份或恢复的写入目标')
    if not source.exists():
        raise ValueError('来源数据库不存在')
    if os.path.normcase(str(source)) == os.path.normcase(str(target)):
        raise ValueError('来源和目标不能是同一文件')
    if any(os.path.normcase(str(target)) == os.path.normcase(str(path)) for path in sidecars(source)):
        raise ValueError('来源的 WAL、SHM 和 journal 文件名不能作为写入目标')
    if target.exists() or any(path.exists() or path.is_symlink() for path in sidecars(target)):
        raise ValueError('目标必须是全新文件，且不能已有 WAL、SHM 或 journal')
    for path in sidecars(source):
        if path.exists() or path.is_symlink():
            plain_path(path, '来源附属文件')
            if mode == 'restore' or path.name.endswith('-journal'):
                raise ValueError('恢复来源必须离线且无附属文件；备份不接受待恢复的 journal')
    return source, target


def check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError('备份或恢复超过总时间限制，目标未发布')


def quote_identifier(value):
    return '"' + value.replace('"', '""') + '"'


def table_constraints(connection, table):
    """Read actual SQLite declarations, including composite keys and uniqueness."""
    name = quote_identifier(table)
    columns = sorted((row[1], row[2].upper(), row[3], row[4], row[5])
                     for row in connection.execute('PRAGMA table_info(' + name + ')'))
    foreign_keys = sorted(tuple(row[1:])
                          for row in connection.execute('PRAGMA foreign_key_list(' + name + ')'))
    unique = []
    for index in connection.execute('PRAGMA index_list(' + name + ')'):
        if index[2]:
            keys = tuple((row[2], row[3], row[4]) for row in connection.execute(
                'PRAGMA index_xinfo(' + quote_identifier(index[1]) + ')') if row[5])
            unique.append((keys, index[4]))
    return dict(columns=columns, foreign_keys=foreign_keys, unique=sorted(unique, key=repr))


def schema_objects(connection):
    return [tuple(row) for row in connection.execute(
        'SELECT name,type,tbl_name,sql FROM sqlite_master ORDER BY name')]


def current_constraints():
    # Run the existing schema definitions only in a disposable memory database.
    # Source and destination are never initialized or migrated by this tool.
    with closing(sqlite3.connect(':memory:')) as reference:
        reference.row_factory = sqlite3.Row
        reference.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, '
                          'applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
        for version in range(1, SCHEMA_VERSION + 1):
            MIGRATIONS[version](reference)
        constraints = {table: table_constraints(reference, table)
                       for table in (*REQUIRED_COLUMNS, 'schema_migrations')}
        return constraints, schema_objects(reference)


def inspect_snapshot(connection, deadline):
    """Validate and hash one pinned SQLite read transaction, without migrations."""
    check_deadline(deadline)
    if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise ValueError('数据库完整性检查失败')
    objects = schema_objects(connection)
    tables = {row[0] for row in objects if row[1] == 'table' and not row[0].startswith('sqlite_')}
    expected = set(REQUIRED_COLUMNS) | {'schema_migrations'}
    if tables != expected or any(row[1] in ('view', 'trigger') for row in objects):
        raise ValueError('数据库不是当前版本的真实数据表结构')
    versions = [row[0] for row in connection.execute('SELECT version FROM schema_migrations ORDER BY version')]
    if versions != list(range(1, SCHEMA_VERSION + 1)):
        raise ValueError('数据库版本与当前程序不一致，不会自动迁移')
    constraints, expected_objects = current_constraints()
    check_deadline(deadline)
    # Accept only the canonical DDL produced by the current migrations. Exact
    # declarations cover CHECK clauses and non-unique named indexes as well as
    # the constraints SQLite exposes through the PRAGMAs below.
    if objects != expected_objects:
        raise ValueError('数据库对象声明与规范当前版本不一致，不会自动迁移')
    for table in sorted(tables):
        check_deadline(deadline)
        if table_constraints(connection, table) != constraints[table]:
            raise ValueError('数据库列、主键、外键或唯一约束与当前版本不一致：' + table)
    if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise ValueError('数据库外键检查失败')
    summary = {}
    for table in sorted(tables):
        check_deadline(deadline)
        name = quote_identifier(table)
        metadata = connection.execute('PRAGMA table_info(' + name + ')').fetchall()
        columns = [row[1] for row in metadata]
        required = REQUIRED_COLUMNS.get(table, {'version', 'applied_at'})
        if set(columns) != required:
            raise ValueError('数据库字段与当前版本不一致：' + table)
        primary = [row[1] for row in sorted(metadata, key=lambda row: row[5]) if row[5]]
        ordering = ','.join(quote_identifier(column) for column in (primary or columns))
        digest = hashlib.sha256()
        digest.update(json.dumps(columns, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        count = 0
        for row in connection.execute('SELECT * FROM ' + name + ' ORDER BY ' + ordering):
            check_deadline(deadline)
            values = [dict(blob=value.hex()) if isinstance(value, bytes) else value for value in row]
            digest.update(b'\n')
            digest.update(json.dumps(values, ensure_ascii=False, allow_nan=False,
                                     separators=(',', ':')).encode('utf-8'))
            count += 1
        summary[table] = dict(rows=count, sha256=digest.hexdigest())
    schema_digest = hashlib.sha256(json.dumps(
        objects, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
    return dict(schema_version=SCHEMA_VERSION, schema_sha256=schema_digest, tables=summary)


def file_digest(path, deadline):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            check_deadline(deadline)
            digest.update(chunk)
    return digest.hexdigest()


def copy_database(source, target, *, mode='backup', progress=None, timeout=30.0):
    """Copy a consistent committed snapshot and publish it only after validation.

    Online backup holds one read transaction. Recovery uses an offline snapshot;
    applications switch --db to the new file and retain the former file for rollback.
    """
    if mode not in ('backup', 'restore'):
        raise ValueError('操作必须是 backup 或 restore')
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
            not math.isfinite(timeout) or timeout <= 0):
        raise ValueError('总时间限制必须是有限正数')
    deadline = time.monotonic() + timeout
    source, target = validate_paths(source, target, mode)
    before = source.stat()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Recheck newly created/existing parents before placing the temporary file.
    validate_paths(source, target, mode)
    descriptor, temporary_name = tempfile.mkstemp(prefix='.' + target.name + '-', suffix='.tmp', dir=target.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        uri = source.as_uri() + '?mode=ro' + ('&immutable=1' if mode == 'restore' else '')
        with closing(sqlite3.connect(uri, uri=True, timeout=min(timeout, 0.2))) as origin, \
                closing(sqlite3.connect(temporary)) as destination:
            origin.execute('PRAGMA trusted_schema=OFF')
            origin.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            origin.execute('BEGIN')
            original_summary = inspect_snapshot(origin, deadline)
            def on_progress(status, remaining, total):
                check_deadline(deadline)
                if progress is not None:
                    progress(status, remaining, total)
                check_deadline(deadline)
            origin.backup(destination, pages=128, progress=on_progress, sleep=0.01)
            destination.execute('PRAGMA journal_mode=DELETE')
            destination.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            restored_summary = inspect_snapshot(destination, deadline)
            if original_summary != restored_summary:
                raise ValueError('快照与来源事务的表摘要不一致，目标未发布')
        if any(path.exists() for path in sidecars(temporary)):
            raise ValueError('临时快照未完成独立保存，目标未发布')
        if mode == 'restore':
            after = source.stat()
            if ((before.st_mtime_ns, before.st_size, before.st_ino) !=
                    (after.st_mtime_ns, after.st_size, after.st_ino)):
                raise ValueError('恢复来源在检查期间发生变化，目标未发布')
        digest = file_digest(temporary, deadline)
        validate_paths(source, target, mode)
        check_deadline(deadline)
        # Hard-link publication is atomic and refuses an existing filename on
        # Windows and POSIX. Removing our temporary name leaves one final link.
        os.link(temporary, target)
        temporary.unlink()
        return dict(operation=mode, source=str(source), target=str(target),
                    file_sha256=digest, **restored_summary)
    except sqlite3.Error as exc:
        if time.monotonic() >= deadline:
            raise TimeoutError('备份或恢复超过总时间限制，目标未发布') from exc
        raise ValueError('无法读取、备份或验证数据库，目标未发布') from exc
    finally:
        # These exact names were created for this attempt. Never delete source,
        # target, unknown files, or directories, even after an interruption.
        for path in [temporary, *sidecars(temporary)]:
            path.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('backup', 'restore'))
    parser.add_argument('--source', required=True, help='来源数据库的绝对路径')
    parser.add_argument('--target', required=True, help='全新目标文件的绝对路径；从不覆盖已有文件')
    parser.add_argument('--timeout', type=float, default=30, help='整个操作的时间限制秒数（默认30）')
    args = parser.parse_args(argv)
    try:
        result = copy_database(args.source, args.target, mode=args.operation, timeout=args.timeout)
    except (ValueError, OSError, TimeoutError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return result


if __name__ == '__main__':
    main()
