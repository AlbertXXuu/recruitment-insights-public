"""The documented showcase is reproducible and preserves subsequent user edits."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing

from core import Store
from scripts.seed_showcase import prepare


class ShowcaseTests(unittest.TestCase):
    def test_roles_visibility_and_repeat_preserve_existing_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary)/'showcase.sqlite3'
            store = Store(database)
            self.assertEqual(prepare(store), dict(created_accounts=8, created_company_jobs=4,
                                                 imported_base_jobs=180))
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs WHERE is_demo=1').fetchone()[0], 184)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs WHERE is_demo=0').fetchone()[0], 0)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM users').fetchone()[0], 8)
                self.assertEqual(db.execute("SELECT can_manage_jobs FROM users WHERE username='hr_readonly_a'").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT can_manage_jobs FROM users WHERE username='hr_a'").fetchone()[0], 1)
            with store.uow() as tx:
                boss = store.accounts.public_user(tx.accounts.by_username('boss_a'))
                candidate = store.accounts.public_user(tx.accounts.by_username('candidate_a'))
                readonly = store.accounts.public_user(tx.accounts.by_username('hr_readonly_a'))
            self.assertEqual(len(store.company_jobs(boss)), 2)
            self.assertEqual(store.queries.overview({'dataset': 'demo'}, candidate)['stats']['total'], 182)
            with self.assertRaises(PermissionError):
                store.manage_job(readonly, {'record': {'title': 'should not be created'}})
            job = store.company_jobs(boss)[0]
            store.manage_job(boss, dict(id=job['id'], expected_edit_token=job['edit_token'],
                                       record={'title': '保留用户编辑的合成岗位'}))
            with closing(sqlite3.connect(database)) as db:
                before = '\n'.join(db.iterdump())
            self.assertEqual(prepare(store), dict(created_accounts=0, created_company_jobs=0,
                                                 imported_base_jobs=0))
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual('\n'.join(db.iterdump()), before)


if __name__ == '__main__':
    unittest.main()
