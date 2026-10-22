"""Synthetic variation remains separate, repeatable and traceable."""
import tempfile
import subprocess
import sys
import sqlite3
from contextlib import closing
import unittest
from pathlib import Path
from collections import Counter
from scripts.seed_db import fixtures


class DemoGenerationTests(unittest.TestCase):
    def test_demo_only_cli_uses_chosen_database_without_real_snapshot_or_fixture_writes(self):
        root = Path(__file__).resolve().parents[1]
        paths = [root/'data'/name for name in ('demo_jobs.csv', 'demo_update.csv', 'import_template.csv')]
        before = [p.read_bytes() for p in paths]
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary)/'nested/demo.sqlite3'
            command = [sys.executable, str(root/'scripts/seed_db.py'), '--demo-only', '--db', str(database)]
            for _ in range(2):
                result = subprocess.run(command, cwd=temporary, capture_output=True, text=True, encoding='utf-8')
                self.assertEqual(result.returncode, 0, result.stderr)
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 180)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs WHERE is_demo=0').fetchone()[0], 0)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM users').fetchone()[0], 0)
        self.assertEqual([p.read_bytes() for p in paths], before)

    def test_seed_replays_and_another_seed_changes_only_synthetic_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary)
            first=fixtures(20261004,base/'a')
            replay=fixtures(20261004,base/'b')
            other=fixtures(20261005,base/'c')
            self.assertEqual(first,replay)
            self.assertNotEqual(first,other)
            self.assertEqual((base/'a/demo_jobs.csv').read_bytes(),(base/'b/demo_jobs.csv').read_bytes())
            self.assertEqual(len(first),180)
            self.assertEqual(len({x['job_id'] for x in first}),180)
            self.assertTrue(all('模拟' in x['company'] and '不是真实招聘' in x['description'] and
                                not x['source_url'] for x in first))
            self.assertGreater(len(set(Counter(x['education'] for x in first).values())),1)


if __name__=='__main__':
    unittest.main()
