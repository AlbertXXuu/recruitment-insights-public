"""Explicit, idempotent local classroom accounts; never runs at server startup."""
import argparse
from pathlib import Path
from accounts import Accounts
from core import ROOT, Store
from recruitment_insights.config import DEMO_PASSWORD

PASSWORD = DEMO_PASSWORD


def seed(store):
    accounts = Accounts(store)
    organizations = [accounts.organization('薄荷科技（演示 A）'), accounts.organization('远山数据（演示 B）')]
    definitions = [('admin', '数据管理员', 'admin', None),
                   ('boss_a', '企业 A 负责人', 'boss', organizations[0]),
                   ('hr_a', '企业 A HR', 'hr', organizations[0]),
                   ('boss_b', '企业 B 负责人', 'boss', organizations[1]),
                   ('hr_b', '企业 B HR', 'hr', organizations[1]),
                   ('candidate_a', '求职者 A', 'candidate', None),
                   ('candidate_b', '求职者 B', 'candidate', None)]
    created = 0
    for username, name, role, org in definitions:
        with store.connect() as db:
            exists = db.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone()
        if not exists:
            accounts.create_user(username, PASSWORD, name, role, org, role == 'hr')
            created += 1
    return created


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=ROOT/'data/recruitment.sqlite3')
    args = parser.parse_args()
    print(f'Created {seed(Store(args.db))} demo accounts. Existing accounts and jobs are unchanged.')
