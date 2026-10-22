"""Create one invitation account in an offline, independent test database."""
import argparse
import getpass
import re
import sqlite3
import sys
import warnings
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import validate_test_database
from core import Store
from recruitment_insights.config import DEMO_PASSWORD


def validate_identity(args):
    if not re.fullmatch(r'[A-Za-z0-9_-]{3,40}', args.username):
        raise ValueError('账号须为 3–40 位英文字母、数字、下划线或横线')
    display_name = args.display_name.strip()
    if not 1 <= len(display_name) <= 60:
        raise ValueError('显示名称须为 1–60 个字符')
    if args.role == 'boss':
        organization = (args.organization or '').strip()
        if not 1 <= len(organization) <= 120:
            raise ValueError('企业负责人必须提供 --organization，企业名称须为 1–120 个字符')
    else:
        if args.organization is not None:
            raise ValueError('仅企业负责人可以使用 --organization；HR 由负责人在系统中创建')
        organization = None
    return display_name, organization


def read_hidden_password(prompt):
    """Read without echo; Windows consoles display one star per character."""
    if sys.platform != 'win32':
        return getpass.getpass(prompt)
    # msvcrt reads the console directly, so redirected stdin must not be ignored.
    # Requiring terminal output also prevents password-length masks entering logs.
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise OSError('密码输入需要交互终端')
    try:
        import msvcrt
    except ImportError:
        raise OSError('交互终端不可用') from None
    sys.stdout.write(prompt)
    sys.stdout.flush()
    characters = []
    while True:
        key = msvcrt.getwch()
        if key in ('\r', '\n'):
            sys.stdout.write('\n')
            sys.stdout.flush()
            return ''.join(characters)
        if key == '\x03':
            raise KeyboardInterrupt
        if key in ('', '\x04', '\x1a'):
            raise EOFError
        if key in ('\x00', '\xe0'):
            # Function/navigation keys arrive as a prefix followed by a key code.
            msvcrt.getwch()
            continue
        if '\ud800' <= key <= '\udbff':
            # Windows reads UTF-16 units; keep a supplementary character intact.
            following = msvcrt.getwch()
            if len(following) != 1 or not '\udc00' <= following <= '\udfff':
                raise OSError('输入字符无效，请重新输入密码')
            key = chr(0x10000 + ((ord(key) - 0xD800) << 10) + ord(following) - 0xDC00)
        elif '\udc00' <= key <= '\udfff':
            raise OSError('输入字符无效，请重新输入密码')
        if key == '\b':
            if characters:
                characters.pop()
                sys.stdout.write('\b \b')
                sys.stdout.flush()
        elif key.isprintable():
            characters.append(key)
            sys.stdout.write('*')
            sys.stdout.flush()


def read_password(parser):
    if not sys.stdin.isatty():
        parser.error('密码必须在交互终端中隐藏输入，不支持管道或输入重定向')
    try:
        with warnings.catch_warnings():
            # getpass normally warns and then falls back to echoed input.
            # Stop at the warning so a broken terminal cannot reveal a password.
            warnings.simplefilter('error', getpass.GetPassWarning)
            password = read_hidden_password('新账号密码：')
            confirmation = read_hidden_password('再次输入密码：')
    except (getpass.GetPassWarning, EOFError, KeyboardInterrupt, OSError):
        parser.error('无法安全读取密码，请在支持隐藏输入的交互终端中重试')
    if password != confirmation:
        parser.error('两次密码输入不一致')
    if not 10 <= len(password) <= 128:
        parser.error('密码须为 10–128 个字符')
    if password == DEMO_PASSWORD:
        parser.error('邀请账号不能使用公开演示密码')
    return password


def username_exists(database, username):
    if not database.exists():
        return False
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True)) as connection:
        has_users = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
        return bool(has_users and connection.execute(
            'SELECT 1 FROM users WHERE username=?', (username,)).fetchone())


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='在已停止服务的独立测试库中创建邀请账号；HR 由企业负责人在系统中创建。',
        allow_abbrev=False)
    parser.add_argument('--db', required=True, help='独立测试数据库的绝对路径')
    parser.add_argument('--username', required=True, help='3–40 位账号名称')
    parser.add_argument('--display-name', required=True, help='1–60 位显示名称')
    parser.add_argument('--role', choices=('admin', 'boss', 'candidate'), default='candidate')
    parser.add_argument('--organization', help='企业负责人所属企业名称；仅用于 boss')
    args, unsupported = parser.parse_known_args(argv)
    if unsupported:
        # Do not repeat a mistakenly supplied command-line password in errors.
        parser.error('存在不支持的参数；密码仅接受交互终端中的隐藏输入')
    try:
        display_name, organization = validate_identity(args)
        database = validate_test_database(args.db)
    except ValueError as exc:
        parser.error(str(exc))
    password = read_password(parser)
    try:
        if username_exists(database, args.username):
            parser.error('账号已存在；不会重置密码或改变企业归属')
    except sqlite3.Error:
        parser.error('无法只读检查已有账号，请停止其他连接并检查独立测试库')
    try:
        store = Store(database)
        # Full input and duplicate checks precede organization(). These public
        # maintenance methods are separate transactions; I/O failures may leave
        # an organization, so this CLI makes no all-or-nothing claim.
        org_id = store.accounts.organization(organization) if organization else None
        user_id = store.accounts.create_user(
            args.username, password, display_name, role=args.role, org_id=org_id)
    except (ValueError, sqlite3.Error, OSError):
        parser.error('创建账号失败。企业与账号分步保存，请检查独立测试库的企业记录后重试')
    print(f'已创建邀请账号：{args.username}（{args.role}，ID {user_id}）。')
    print(f'独立测试库：{database}')
    return user_id


if __name__ == '__main__':
    main()
