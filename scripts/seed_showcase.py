"""Prepare a local synthetic role showcase without resetting existing records."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import Store
from recruitment_insights.config import DEMO_PASSWORD
from seed_demo_accounts import seed


def prepare(store):
    with store.connect() as db:
        empty = db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
    if empty:
        store.import_csv((ROOT/'data/demo_jobs.csv').read_text(encoding='utf-8-sig'),
                         '课程模拟数据', True)
    created = seed(store)
    with store.uow() as tx:
        boss_a = tx.accounts.by_username('boss_a')
        readonly = tx.accounts.by_username('hr_readonly_a')
    if boss_a['role'] != 'boss' or boss_a['org_name'] != '薄荷科技（演示 A）':
        raise ValueError('boss_a 已用于其他身份；请为演示选择独立数据库，不会重置已有账号。')
    if readonly is None:
        store.accounts.create_user('hr_readonly_a', DEMO_PASSWORD, '企业 A 只读 HR',
                                   'hr', boss_a['org_id'], False)
        created += 1
    job_count = 0
    for username, letter, organization in (
            ('boss_a', 'A', '薄荷科技（演示 A）'), ('boss_b', 'B', '远山数据（演示 B）')):
        with store.uow() as tx:
            row = tx.accounts.by_username(username)
        if row['role'] != 'boss' or row['org_name'] != organization:
            raise ValueError(f'{username} 已用于其他身份；请为演示选择独立数据库。')
        actor = store.accounts.public_user(row)
        # Existing company records, including user edits, are kept as they are.
        if store.company_jobs(actor):
            continue
        for published, title in ((True, 'Python 后端工程师'), (False, '前端开发工程师')):
            store.manage_job(actor, dict(is_demo=True, published=published, record=dict(
                title=f'【合成演示 {letter}】{title}', location='杭州',
                description='仅用于开源候选版功能演示，不是真实招聘。技能：Python SQL JavaScript Git。',
                salary_text='12–18K/月', salary_min=12000, salary_max=18000,
                currency='CNY', salary_period='month', education='本科', experience='1–3年')))
            job_count += 1
    return dict(created_accounts=created, created_company_jobs=job_count,
                imported_base_jobs=180 if empty else 0)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True,
                        help='Explicit independent local demonstration database')
    args = parser.parse_args()
    print(json.dumps(prepare(Store(args.db)), ensure_ascii=False))
