"""Run the isolated UI worktree with its own loopback port and synthetic database."""
import logging
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import Store
from recruitment_insights.config import DEMO_PASSWORD
from recruitment_insights.web.server import make_server

PORT = 8785
DATABASE = ROOT / 'outputs/ui-preview/preview.sqlite3'


def main():
    # Refuse another listener before initializing or changing preview data.
    with socket.socket() as probe:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(('127.0.0.1', PORT))
    fresh_database = not DATABASE.exists()
    store = Store(DATABASE)
    for username, role, name in (('admin', 'admin', '预览管理员'),
                                 ('candidate_a', 'candidate', '预览求职者')):
        with store.uow() as tx:
            prepared = tx.accounts.by_username(username) is not None
        if not prepared:
            store.accounts.create_user(username, DEMO_PASSWORD, name, role=role)
    if fresh_database:
        store.import_csv((ROOT/'data/demo_jobs.csv').read_text(encoding='utf-8-sig'),
                         '课程模拟数据', True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    server = make_server(store, PORT)
    print('普通端注册预览（仅本机，不启动 Tunnel）', flush=True)
    print(f'网址：http://localhost:{PORT}/', flush=True)
    print(f'预览数据库：{DATABASE}', flush=True)
    print('本机演示账号：admin / candidate_a；密码见登录页。也可自行注册普通端账号。', flush=True)
    print('选择课程模拟数据查看 180 条岗位；Ctrl+C 正常停止预览。', flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print('预览已正常停止，预览数据保留。', flush=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        print(f'预览未启动：{error}', file=sys.stderr)
        raise SystemExit(1) from None
