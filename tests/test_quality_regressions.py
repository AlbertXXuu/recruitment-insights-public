"""Private duplicate evidence and decision-aware quality counts, using temporary DBs."""
import json
import tempfile
import unittest
from itertools import combinations
from pathlib import Path

from accounts import Accounts
from core import Store


class DuplicateQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'quality.sqlite3')
        self.accounts = Accounts(self.store)
        self.org_a = self.accounts.organization('Quality A')
        self.org_b = self.accounts.organization('Quality B')
        self.actors = {}
        for name, role, org in (('admin', 'admin', None), ('boss_a', 'boss', self.org_a),
                                ('boss_b', 'boss', self.org_b), ('hr_a', 'hr', self.org_a)):
            user_id = self.accounts.create_user(name, 'Quality-Test-Only-2026', name, role, org)
            self.actors[name] = dict(id=user_id, role=role, org_id=org,
                                     org_name='Quality A' if org == self.org_a else 'Quality B',
                                     can_manage_jobs=0)

    def records(self, count=2, owner=None):
        rows = [dict(job_id=str(i), title='Python 软件工程师', company='Quality A', location='上海',
                     description='本科及以上学历，3–5年开发经验。负责软件系统设计、接口开发与自动化测试，协作维护产品功能。',
                     education='本科', experience='3–5年', salary_min=12000, salary_max=18000,
                     currency='CNY', salary_period='month', source_url='https://example.com/jobs/'+str(i))
                for i in range(count)]
        self.store.import_records(rows, 'quality-fixture', 'quality fixture', owner_org_id=owner)
        with self.store.connect() as db:
            return [r['id'] for r in db.execute('SELECT id FROM jobs ORDER BY id')]

    def report(self, actor='admin', **filters):
        return self.store.quality(dict(dataset='real', **filters), self.actors[actor])

    def decide(self, left, right, decision, actor='admin', reason='经核验属于不同招聘批次'):
        pair = next(p for p in self.report(actor)['duplicates']
                    if (p['left']['id'], p['right']['id']) == (left, right))
        self.store.decide_duplicate(self.actors[actor], dict(
            left_id=left, right_id=right, left_hash=pair['left_hash'], right_hash=pair['right_hash'],
            version=pair['version'], decision=decision, reason=reason))

    def publish(self, ids):
        with self.store.connect() as db:
            db.executemany('UPDATE jobs SET published=1 WHERE id=?', [(i,) for i in ids])

    def change(self, job_id, **fields):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        raw = dict(json.loads(row['raw']), **fields)
        self.store.import_records([raw], row['source'], 'source update', owner_org_id=row['owner_org_id'])

    def test_private_reason_is_not_exposed_to_other_enterprise_after_publication(self):
        ids = self.records(owner=self.org_a)
        reason = '内部草稿依据：仅本企业可读的招聘预算说明'
        self.decide(*ids, 'different', 'boss_a', reason)
        self.publish(ids)
        foreign = self.report('boss_b')['duplicates'][0]
        self.assertFalse(foreign['can_review'])
        self.assertFalse(foreign['can_view_reason'])
        self.assertNotIn(reason, json.dumps(foreign, ensure_ascii=False))
        self.assertEqual(foreign.get('reason', ''), '')
        for reader in ('boss_a', 'hr_a', 'admin'):
            self.assertEqual(self.report(reader)['duplicates'][0]['reason'], reason)
        self.assertFalse(self.report('hr_a')['duplicates'][0]['can_review'])

    def test_stale_reason_remains_private_after_source_cleanup(self):
        ids = self.records(owner=self.org_a)
        reason = '内部草稿专用证据，不随岗位公开'
        self.decide(*ids, 'same', 'boss_a', reason)
        self.change(ids[0], description='本科及以上学历，3–5年开发经验。公开更新后的岗位说明，负责设计接口并完成代码审查与自动化验证。')
        self.publish(ids)
        foreign = self.report('boss_b')['duplicates'][0]
        self.assertEqual(foreign['decision'], 'stale')
        self.assertEqual(foreign.get('reason', ''), '')
        self.assertEqual(self.report('boss_a')['duplicates'][0]['reason'], reason)

    def test_admin_reason_for_public_sources_is_not_shared_with_enterprises(self):
        ids = self.records()
        self.decide(*ids, 'different', reason='管理员内部采集核验说明')
        self.assertEqual(self.report('boss_b')['duplicates'][0].get('reason', ''), '')
        self.assertEqual(self.report()['duplicates'][0]['reason'], '管理员内部采集核验说明')

    def test_reason_requires_read_ownership_of_both_records(self):
        ids = self.records(owner=self.org_a)
        with self.store.connect() as db:
            db.execute('UPDATE jobs SET owner_org_id=? WHERE id=?', (self.org_b, ids[1]))
        self.publish(ids)
        self.decide(*ids, 'different', reason='涉及两个企业的管理员内部判断')
        for reader in ('boss_a', 'boss_b'):
            pair = self.report(reader)['duplicates'][0]
            self.assertFalse(pair['can_view_reason'])
            self.assertEqual(pair['reason'], '')
        self.assertEqual(self.report()['duplicates'][0]['reason'], '涉及两个企业的管理员内部判断')

    def test_same_and_different_exit_pending_without_removing_source_records(self):
        ids = self.records()
        with self.store.connect() as db:
            original = [dict(r) for r in db.execute('SELECT * FROM jobs ORDER BY id')]
        initial = self.report()
        self.assertEqual(initial['warning_jobs'], 2)
        for decision in ('same', 'different'):
            with self.subTest(decision=decision):
                self.decide(*ids, decision)
                report = self.report()
                self.assertNotIn('duplicate_pending_ids', report)
                self.assertEqual(report['warning_jobs'], 0)
                self.assertEqual(report['clean_jobs'], 2)
                self.assertEqual(report['duplicate_pending_pairs'], 0)
                self.assertEqual(report['duplicate_pending_jobs'], 0)
                self.assertEqual(report['duplicate_status_counts'][decision], 1)
                self.assertEqual((report['total'], report['duplicate_groups'], report['duplicate_jobs'], report['duplicate_pairs']), (2, 1, 2, 1))
                self.assertNotIn('possible_duplicate', {i['code'] for i in report['issues']})
        with self.store.connect() as db:
            self.assertEqual([dict(r) for r in db.execute('SELECT * FROM jobs ORDER BY id')], original)

    def test_revoked_and_stale_decisions_return_to_pending(self):
        ids = self.records()
        self.decide(*ids, 'same')
        self.decide(*ids, 'revoke')
        report = self.report()
        self.assertEqual(report['duplicate_status_counts']['revoked'], 1)
        self.assertEqual((report['duplicate_pending_pairs'], report['duplicate_pending_jobs'], report['warning_jobs']), (1, 2, 2))
        self.decide(*ids, 'different')
        self.change(ids[0], title='修改后的 Python 软件工程师')
        report = self.report()
        self.assertEqual(report['duplicate_status_counts']['stale'], 1)
        self.assertEqual(report['duplicate_groups'], 0)
        self.assertEqual(report['duplicate_pairs'], 1)
        self.assertEqual((report['duplicate_pending_pairs'], report['duplicate_pending_jobs'], report['warning_jobs']), (1, 2, 2))

    def test_a_record_stays_pending_while_another_pair_is_unresolved(self):
        a, b, c = self.records(3)
        self.decide(a, b, 'different')
        self.decide(a, c, 'same')
        report = self.report()
        self.assertEqual((report['duplicate_pending_pairs'], report['duplicate_pending_jobs']), (1, 2))
        self.assertEqual({r['id'] for r in report['rows']}, {b, c})
        self.assertEqual(report['clean_jobs'], 1)

    def test_pending_counts_cover_all_pages_and_respect_current_scope(self):
        ids = self.records(16)
        with self.store.connect() as db:
            hashes = {r['id']:r['fingerprint'] for r in db.execute('SELECT id,fingerprint FROM jobs')}
            pairs = list(combinations(ids, 2))
            db.executemany('''INSERT INTO duplicate_decisions
                (left_id,right_id,decision,reason,left_hash,right_hash,version,user_id,reviewed_at)
                VALUES(?,?,'different','fixture',?,?,1,?,'2026-10-02T00:00:00+00:00')''',
                [(a,b,hashes[a],hashes[b],self.actors['admin']['id']) for a,b in pairs[:-1]])
        for page in (1, 2):
            report = self.report(duplicate_page=page)
            self.assertEqual(report['duplicate_pairs'], 120)
            self.assertEqual((report['duplicate_pending_pairs'], report['duplicate_pending_jobs']), (1, 2))
            self.assertEqual(report['duplicate_status_counts']['different'], 119)
            self.assertEqual({r['id'] for r in report['rows']}, set(pairs[-1]))
        scoped = self.report(q='does-not-exist')
        self.assertEqual((scoped['duplicate_pairs'], scoped['duplicate_pending_pairs'], scoped['duplicate_pending_jobs']), (0, 0, 0))


if __name__ == '__main__':
    unittest.main()
