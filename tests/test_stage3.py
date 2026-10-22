"""Stage 3 acceptance: reviewed fields, duplicate decisions and safe batch imports.

All fixtures live in temporary SQLite files. No live source or course database is used.
"""
import csv
import io
import json
import tempfile
import threading
import unittest
from itertools import combinations
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from accounts import Accounts
from app import make_server
from core import Store
from seed_demo_accounts import PASSWORD, seed


def record(job_id='one', **overrides):
    return dict(dict(job_id=job_id, title='阶段三 Python 工程师', company='测试公司',
                     location='杭州', education='', experience='',
                     description='本科及以上学历，至少 3 年 Python 开发经验。负责系统设计、接口开发和自动化测试。',
                     source_url='https://example.com/jobs/'+job_id), **overrides)


class ReviewWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.store = Store(Path(cls.temp.name) / 'stage3.sqlite3')
        seed(cls.store)
        cls.server = make_server(cls.store, 0)
        cls.accounts = cls.server.accounts
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'
        cls.cookies, cls.actors = {}, {}
        for name in ('admin', 'boss_a', 'hr_a', 'boss_b', 'candidate_a'):
            token, actor = cls.accounts.login(name, PASSWORD)
            cls.cookies[name] = 'recruitment_insights_session='+token
            cls.actors[name] = actor

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def request(self, path, user='admin', body=None, status=200):
        headers = {'Cookie': self.cookies.get(user, '')}
        payload = None
        if body is not None:
            headers.update({'Content-Type': 'application/json', 'X-Local-Request': '1'})
            payload = json.dumps(body).encode()
        try:
            response = urlopen(Request(self.url+quote(path, safe='/?=&'), data=payload,
                                       headers=headers), timeout=10)
        except HTTPError as exc:
            response = exc
        with response:
            content = response.read().decode('utf-8-sig')
            self.assertEqual(response.status, status, (path, content))
            return json.loads(content) if response.headers.get_content_type() == 'application/json' else content

    def insert(self, suffix='', owner=None, **overrides):
        source = self._testMethodName+suffix
        raw = record(**overrides)
        self.store.import_records([raw], source, 'Stage 3 test', owner_org_id=owner)
        with self.store.connect() as db:
            job_id = db.execute('SELECT id FROM jobs WHERE source=? AND external_id=?',
                                (source, raw['job_id'])).fetchone()[0]
        return job_id, source, raw

    def detail(self, job_id, user='admin'):
        return self.request('/api/jobs/'+str(job_id), user)

    def review_body(self, job_id, field='education', **overrides):
        current = self.detail(job_id)['requirements'][field]
        body = dict(job_id=job_id, field=field, action='confirm',
                    source_hash=current['source_hash'], version=current['version'],
                    value='本科', condition='硬性要求', evidence='本科及以上学历', alternatives=[])
        return dict(body, **overrides)

    def pair(self, suffix='', owner=None, same_source=False):
        title = self._testMethodName+suffix
        left_id, left_source, left_raw = self.insert(suffix+'left', owner=owner, title=title)
        right_source = left_source if same_source else left_source+'right'
        right_raw = record(job_id='two', title=title)
        self.store.import_records([right_raw], right_source, 'duplicate fixture', owner_org_id=owner)
        with self.store.connect() as db:
            right_id = db.execute('SELECT id FROM jobs WHERE source=? AND external_id=?',
                                 (right_source, 'two')).fetchone()[0]
        return left_id, right_id, left_source, left_raw, title

    def pair_state(self, left_id, right_id, title, user='admin'):
        report = self.request('/api/quality?dataset=real&q='+title, user)
        return next(pair for pair in report['duplicates']
                    if {pair['left']['id'], pair['right']['id']} == {left_id, right_id})

    @staticmethod
    def decision_body(pair, decision='same', reason='来源内容和任职条件一致，经人工核验为同岗'):
        return dict(left_id=pair['left']['id'], right_id=pair['right']['id'],
                    left_hash=pair['left_hash'], right_hash=pair['right_hash'],
                    version=pair['version'], decision=decision, reason=reason)

    def test_review_options_and_anonymous_write_protection(self):
        options = self.request('/api/review-options')
        self.assertIn('本科', options['education_values'])
        self.assertIn('未知', options['education_values'])
        self.assertIn('experience_values', options)
        self.assertIn('硬性要求', options['conditions'])
        self.request('/api/review-options', None, status=401)
        self.request('/api/reviews', None, {}, status=401)
        self.request('/api/duplicates', None, {}, status=401)

    def test_candidate_is_not_a_confirmed_requirement_and_source_fields_remain_usable(self):
        job_id, _, _ = self.insert()
        detail = self.detail(job_id)
        education = detail['requirements']['education']
        self.assertEqual(education['status'], 'candidate')
        self.assertEqual(education['value'], '本科')
        self.assertNotEqual(detail['payload']['education'], '本科')
        self.assertNotEqual(detail['payload']['education'], '不限')
        self.assertTrue(education['evidence'])
        explicit_id, _, _ = self.insert('explicit', education='本科', experience='1–3年')
        explicit = self.detail(explicit_id)
        self.assertEqual(explicit['requirements']['education']['status'], 'source')
        self.assertEqual(explicit['payload']['education'], '本科')

    def test_confirmed_fields_flow_to_list_filters_stats_and_export_without_rewriting_raw(self):
        title = self._testMethodName
        job_id, source, raw = self.insert(title=title)
        before = self.detail(job_id)
        reviewed = self.request('/api/reviews', body=self.review_body(job_id))
        self.assertEqual(reviewed['requirements']['education']['status'], 'confirmed')
        self.assertEqual(reviewed['payload']['education'], '本科')
        self.assertEqual(reviewed['raw'], raw)
        self.assertEqual(reviewed['revisions'], before['revisions'])
        listing = self.request('/api/jobs?dataset=real&q='+title+'&education=本科', 'candidate_a')
        self.assertEqual(listing['total'], 1)
        stats = self.request('/api/stats?dataset=real&q='+title, 'candidate_a')
        self.assertEqual(stats['education'], [{'name': '本科', 'count': 1}])
        exported = list(csv.DictReader(io.StringIO(self.request('/api/export?dataset=real&q='+title, 'candidate_a'))))
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0]['education'], '本科')
        self.assertEqual(exported[0]['source'], source)
        # A fresh Store instance sees the persisted review; no process-local cache is required.
        reopened = Store(self.store.path).detail(job_id, self.actors['admin'])
        self.assertEqual(reopened['requirements']['education']['status'], 'confirmed')

    def test_review_revoke_restores_candidate_and_keeps_original_content(self):
        job_id, _, raw = self.insert()
        self.request('/api/reviews', body=self.review_body(job_id))
        current = self.detail(job_id)['requirements']['education']
        revoked = self.request('/api/reviews', body=dict(job_id=job_id, field='education', action='revoke',
                                source_hash=current['source_hash'], version=current['version']))
        self.assertEqual(revoked['requirements']['education']['status'], 'candidate')
        self.assertNotEqual(revoked['payload']['education'], '本科')
        self.assertEqual(revoked['raw'], raw)

    def test_unknown_confirmation_does_not_mean_unrestricted(self):
        job_id, _, _ = self.insert(description='负责项目开发、维护和与团队协作，提供培训。')
        self.assertEqual(self.detail(job_id)['requirements']['education']['status'], 'unresolved')
        reviewed = self.request('/api/reviews', body=self.review_body(job_id, value='未知',
                                  condition='未发现明确要求', evidence=''))
        self.assertEqual(reviewed['requirements']['education']['status'], 'confirmed')
        self.assertEqual(reviewed['requirements']['education']['value'], '未知')
        self.assertNotEqual(reviewed['payload']['education'], '不限')

    def test_experience_bounds_and_education_conditions_survive_review(self):
        description = '本科优先，可由同等工作经验替代。要求 3–5 年开发经验。'
        job_id, _, _ = self.insert(description=description)
        proposed = self.detail(job_id)['requirements']
        self.assertEqual(proposed['education']['condition'], '等价经验可替代')
        self.assertEqual((proposed['experience']['min_years'], proposed['experience']['max_years']), (3, 5))
        education = self.review_body(job_id, condition='等价经验可替代', evidence='本科优先，可由同等工作经验替代。')
        result = self.request('/api/reviews', body=education)
        self.assertEqual(result['requirements']['education']['condition'], '等价经验可替代')
        experience = self.review_body(job_id, field='experience', value='3–5年',
                                     evidence='要求 3–5 年开发经验。', min_years=3, max_years=5)
        self.request('/api/reviews', body=dict(experience, min_years=6), status=400)
        result = self.request('/api/reviews', body=experience)
        effective = result['requirements']['experience']
        self.assertEqual((effective['status'], effective['min_years'], effective['max_years']), ('confirmed', 3, 5))
        self.assertEqual(result['payload']['experience'], '3–5年')

    def test_missing_requirement_cannot_be_confirmed_as_unrestricted(self):
        job_id, _, _ = self.insert(description='负责软件开发与团队协作。')
        self.request('/api/reviews', body=self.review_body(job_id, value='不限',
                     condition='条件不明确', evidence='负责软件开发与团队协作。'), status=400)
        self.assertNotEqual(self.detail(job_id)['payload']['education'], '不限')

    def test_review_optimistic_version_and_original_text_conflicts(self):
        job_id, source, raw = self.insert()
        body = self.review_body(job_id)
        self.request('/api/reviews', body=body)
        self.request('/api/reviews', body=body, status=409)
        fresh = self.review_body(job_id)
        self.store.import_records([dict(raw, description='硕士及以上学历，至少 5 年开发经验。')], source, 'source changed')
        self.request('/api/reviews', body=fresh, status=409)
        stale = self.detail(job_id)
        self.assertEqual(stale['requirements']['education']['status'], 'stale')
        self.assertNotEqual(stale['payload']['education'], '本科')
        self.assertEqual(stale['raw']['description'], '硕士及以上学历，至少 5 年开发经验。')
        # The stale human conclusion is retained separately, never replaced by the upstream update.
        with self.store.connect() as db:
            review = dict(db.execute('SELECT * FROM field_reviews WHERE job_id=? AND field=?',
                                     (job_id, 'education')).fetchone())
        self.assertIn('本科', json.dumps(review, ensure_ascii=False))

    def test_review_input_rejects_fabricated_evidence_and_unsupported_fields(self):
        job_id, _, _ = self.insert()
        before = self.detail(job_id)
        bad_bodies = [self.review_body(job_id, evidence='原文并没有这一句'),
                      self.review_body(job_id, value='自定义学历'),
                      self.review_body(job_id, condition='无需证据直接相信'),
                      self.review_body(job_id, alternatives='硕士'),
                      dict(self.review_body(job_id), field='salary'),
                      self.review_body(job_id, action='delete')]
        for body in bad_bodies:
            with self.subTest(body=body):
                self.request('/api/reviews', body=body, status=400)
        self.assertEqual(self.detail(job_id), before)

    def test_review_permissions_restrict_public_and_other_company_records(self):
        public_id, _, _ = self.insert('public')
        for user in ('boss_a', 'hr_a', 'boss_b', 'candidate_a'):
            self.assertFalse(self.detail(public_id, user)['can_review'])
            self.request('/api/reviews', user, self.review_body(public_id), status=403)
        own_id, _, _ = self.insert('owned', owner=self.actors['boss_a']['org_id'])
        self.assertTrue(self.detail(own_id, 'boss_a')['can_review'])
        self.assertTrue(self.detail(own_id, 'hr_a')['can_review'])
        self.request('/api/reviews', 'boss_b', self.review_body(own_id), status=403)
        self.request('/api/reviews', 'candidate_a', self.review_body(own_id), status=403)
        self.request('/api/reviews', 'hr_a', self.review_body(own_id))
        self.assertTrue(self.detail(own_id)['can_review'])
        self.request('/api/reviews', body=self.review_body(own_id))

    def test_existing_hr_session_and_stale_actor_cannot_review_after_revocation(self):
        job_id, _, _ = self.insert(owner=self.actors['boss_a']['org_id'])
        body = self.review_body(job_id)
        hr_id = self.actors['hr_a']['id']
        self.request('/api/team', 'boss_a', dict(id=hr_id, can_manage=False))
        try:
            self.assertFalse(self.detail(job_id, 'hr_a')['can_review'])
            self.request('/api/reviews', 'hr_a', body, status=403)
            with self.assertRaises(PermissionError):
                self.store.review_field(self.actors['hr_a'], body)
        finally:
            self.request('/api/team', 'boss_a', dict(id=hr_id, can_manage=True))
        self.request('/api/reviews', 'hr_a', body)

    def test_review_and_audit_roll_back_together(self):
        job_id, _, _ = self.insert()
        before = self.detail(job_id)
        body = self.review_body(job_id)
        with patch.object(Accounts, 'audit', side_effect=RuntimeError('simulated audit failure')):
            with self.assertRaises(RuntimeError):
                self.store.review_field(self.actors['admin'], body)
        self.assertEqual(self.detail(job_id), before)

    def test_same_and_different_decisions_are_reversible_without_changing_record_counts(self):
        left, right, _, _, title = self.pair()
        pair = self.pair_state(left, right, title)
        self.assertTrue(pair['can_review'])
        self.request('/api/duplicates', body=self.decision_body(pair))
        same = self.pair_state(left, right, title)
        self.assertEqual(same['decision'], 'same')
        self.assertEqual(self.request('/api/stats?dataset=real&q='+title)['total'], 2)
        self.assertEqual(self.request('/api/jobs?dataset=real&q='+title)['total'], 2)
        self.request('/api/duplicates', body=self.decision_body(same, 'different', '复核后确认招聘范围不同'))
        different = self.pair_state(left, right, title)
        self.assertEqual(different['decision'], 'different')
        self.request('/api/duplicates', body=self.decision_body(different, 'revoke', '撤销后重新核查'))
        self.assertNotIn(self.pair_state(left, right, title)['decision'], ('same', 'different'))
        self.assertEqual(self.request('/api/jobs?dataset=real&q='+title)['total'], 2)

    def test_same_source_distinct_ids_are_duplicate_candidates(self):
        left, right, _, _, title = self.pair(same_source=True)
        self.assertEqual({self.pair_state(left, right, title)['left']['id'],
                          self.pair_state(left, right, title)['right']['id']}, {left, right})

    def test_duplicate_version_and_content_changes_reject_stale_decisions(self):
        left, right, source, raw, title = self.pair()
        body = self.decision_body(self.pair_state(left, right, title))
        self.request('/api/duplicates', body=body)
        self.request('/api/duplicates', body=body, status=409)
        fresh = self.decision_body(self.pair_state(left, right, title), 'different', '招聘条件存在区别')
        self.store.import_records([dict(raw, description=raw['description']+'新增跨城市出差要求。')], source, 'changed')
        self.request('/api/duplicates', body=fresh, status=409)
        stale = self.pair_state(left, right, title)
        self.assertEqual(stale['decision'], 'stale')

    def test_duplicate_decisions_require_authority_over_both_records(self):
        left, right, _, _, title = self.pair('public')
        body = self.decision_body(self.pair_state(left, right, title))
        for user in ('boss_a', 'hr_a', 'boss_b', 'candidate_a'):
            self.request('/api/duplicates', user, body, status=403)
        own_left, own_right, _, _, own_title = self.pair('owned', owner=self.actors['boss_a']['org_id'])
        own_pair = self.pair_state(own_left, own_right, own_title, 'hr_a')
        self.assertTrue(own_pair['can_review'])
        self.request('/api/duplicates', 'boss_b', self.decision_body(own_pair), status=403)
        self.request('/api/duplicates', 'hr_a', self.decision_body(own_pair))
        own_hash = own_pair['left_hash'] if own_pair['left']['id'] == own_left else own_pair['right_hash']
        # Public fixtures were inserted first, so this is an ordered, well-formed pair.
        mixed = dict(body, right_id=own_left, right_hash=own_hash)
        self.request('/api/duplicates', 'boss_a', mixed, status=403)

    def test_duplicate_and_audit_roll_back_together(self):
        left, right, _, _, title = self.pair()
        before = self.pair_state(left, right, title)
        with patch.object(Accounts, 'audit', side_effect=RuntimeError('simulated audit failure')):
            with self.assertRaises(RuntimeError):
                self.store.decide_duplicate(self.actors['admin'], self.decision_body(before))
        self.assertEqual(self.pair_state(left, right, title), before)

    def test_duplicate_review_obeys_live_hr_revocation_and_draft_visibility(self):
        left, right, _, _, title = self.pair(owner=self.actors['boss_a']['org_id'])
        body = self.decision_body(self.pair_state(left, right, title, 'hr_a'))
        foreign = self.request('/api/quality?dataset=real&q='+title, 'boss_b')
        self.assertEqual(foreign['total'], 0)
        self.assertEqual(foreign['duplicates'], [])
        hr_id = self.actors['hr_a']['id']
        self.request('/api/team', 'boss_a', dict(id=hr_id, can_manage=False))
        try:
            self.assertFalse(self.pair_state(left, right, title, 'hr_a')['can_review'])
            self.request('/api/duplicates', 'hr_a', body, status=403)
            with self.assertRaises(PermissionError):
                self.store.decide_duplicate(self.actors['hr_a'], body)
        finally:
            self.request('/api/team', 'boss_a', dict(id=hr_id, can_manage=True))

    def test_extreme_integer_ids_and_versions_return_client_errors(self):
        job_id, _, _ = self.insert()
        review = self.review_body(job_id)
        left, right, _, _, title = self.pair('duplicate')
        duplicate = self.decision_body(self.pair_state(left, right, title))
        for oversized in (2**63, 10**100):
            with self.subTest(value=oversized):
                self.request('/api/jobs/'+str(oversized), status=400)
                for key in ('job_id', 'version'):
                    self.request('/api/reviews', body=dict(review, **{key: oversized}), status=400)
                for key in ('left_id', 'right_id', 'version'):
                    self.request('/api/duplicates', body=dict(duplicate, **{key: oversized}), status=400)
        self.assertEqual(self.detail(job_id)['requirements']['education']['status'], 'candidate')

    def test_reviewed_open_year_range_and_degree_alternatives_survive_list_and_export(self):
        title = self._testMethodName
        job_id, _, _ = self.insert(title=title, description='本科或硕士学历。至少 2 年开发经验。')
        self.request('/api/reviews', body=self.review_body(job_id, value='多种学历选择',
                     evidence='本科或硕士学历。', alternatives=['本科', '硕士']))
        self.request('/api/reviews', body=self.review_body(job_id, field='experience', value='年限范围',
                     evidence='至少 2 年开发经验。', min_years=2, max_years=None))
        listing = self.request('/api/jobs?dataset=real&q='+title, 'candidate_a')
        self.assertEqual(listing['total'], 1)
        row = listing['items'][0]
        self.assertEqual(row['experience'], '年限范围')
        self.assertEqual((row['experience_min_years'], row['experience_max_years']), (2, None))
        self.assertEqual(row['education_alternatives'], '本科 / 硕士')
        exported = list(csv.DictReader(io.StringIO(self.request('/api/export?dataset=real&q='+title, 'candidate_a'))))
        self.assertEqual(len(exported), 1)
        self.assertEqual((exported[0]['experience_min_years'], exported[0]['experience_max_years']), ('2', ''))
        self.assertEqual(exported[0]['education_alternatives'], '本科 / 硕士')

    def test_stale_private_draft_evidence_is_not_exposed_after_publication(self):
        private_text = '本科及以上学历，内部草稿保密标记Stage3。'
        public_text = '硕士及以上学历，负责公开项目交付。'
        owner = self.actors['boss_a']['org_id']
        job_id, source, raw = self.insert(owner=owner, description=private_text)
        self.request('/api/reviews', 'boss_a', self.review_body(job_id, evidence=private_text))
        self.request('/api/jobs/'+str(job_id), 'candidate_a', status=404)
        self.store.import_records([dict(raw, description=public_text)], source, 'source revised', owner_org_id=owner)
        self.request('/api/company/jobs', 'boss_a', dict(id=job_id, published=True,
                     expected_edit_token=self.detail(job_id, 'boss_a')['edit_token']))
        owned = self.detail(job_id, 'boss_a')
        self.assertEqual(owned['requirements']['education']['status'], 'stale')
        self.assertEqual(owned['requirements']['education']['evidence'], private_text)
        for user in ('candidate_a', 'boss_b'):
            with self.subTest(user=user):
                detail = self.detail(job_id, user)
                requirement = detail['requirements']['education']
                self.assertEqual(requirement['status'], 'stale')
                self.assertEqual(requirement['value'], '硕士')
                self.assertIn(requirement['evidence'], public_text)
                self.assertNotIn('内部草稿保密标记Stage3', json.dumps(detail, ensure_ascii=False))
                self.assertNotIn('raw', detail)
                self.assertNotIn('revisions', detail)

    def test_duplicate_pagination_prioritizes_unfinished_work_without_missing_pairs(self):
        title = self._testMethodName
        source = title
        raws = [record(str(index), title=title) for index in range(16)]
        self.store.import_records(raws, source, 'pagination fixture')
        with self.store.connect() as db:
            rows = [dict(row) for row in db.execute('SELECT id,fingerprint FROM jobs WHERE source=? ORDER BY id', (source,))]
            all_pairs = list(combinations(rows, 2))
            # Seed 100 completed decisions to recreate a busy queue without 100 HTTP writes.
            db.executemany('''INSERT INTO duplicate_decisions
                (left_id,right_id,decision,reason,left_hash,right_hash,version,user_id,reviewed_at)
                VALUES(?,?,?,?,?,?,?,?,?)''',
                [(left['id'], right['id'], 'same', '分页测试已完成记录', left['fingerprint'], right['fingerprint'],
                  1, self.actors['admin']['id'], '2026-09-27T00:00:00+00:00') for left, right in all_pairs[:100]])
        # Updating the first source makes its old decisions stale, ahead of completed work.
        self.store.import_records([dict(raws[0], description=raws[0]['description']+'新增出差要求。')], source, 'revision')
        first = self.request('/api/quality?dataset=real&q='+title+'&duplicate_page=1')
        second = self.request('/api/quality?dataset=real&q='+title+'&duplicate_page=2')
        self.assertEqual((first['duplicate_page'], second['duplicate_page']), (1, 2))
        self.assertEqual((first['duplicate_pages'], second['duplicate_pages']), (2, 2))
        self.assertEqual((first['duplicate_pairs'], second['duplicate_pairs']), (120, 120))
        self.assertEqual((len(first['duplicates']), len(second['duplicates'])), (100, 20))
        combined = first['duplicates']+second['duplicates']
        observed = [(item['left']['id'], item['right']['id']) for item in combined]
        expected = {(left['id'], right['id']) for left, right in all_pairs}
        self.assertEqual(len(set(observed)), 120)
        self.assertEqual(set(observed), expected)
        statuses = [item['decision'] for item in combined]
        self.assertEqual(statuses.count('pending'), 20)
        self.assertGreater(statuses.count('stale'), 0)
        self.assertLess(max(i for i, state in enumerate(statuses) if state in ('pending', 'stale')),
                        min(i for i, state in enumerate(statuses) if state in ('same', 'different')))
        self.request('/api/quality?dataset=real&q='+title+'&duplicate_page=0', status=400)
        self.request('/api/quality?dataset=real&q='+title+'&duplicate_page=invalid', status=400)


class BatchConflictTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'batch.sqlite3')

    def tearDown(self):
        self.temp.cleanup()

    def test_identical_batch_rows_are_folded_and_reimport_is_idempotent(self):
        rows = [record(), record(), record()]
        result = self.store.import_records(rows, 'source', 'duplicates')
        self.assertEqual((result['inserted'], result['duplicated'], result['conflicts'], result['rejected']), (1, 2, 0, 0))
        repeat = self.store.import_records(rows, 'source', 'repeat')
        self.assertEqual((repeat['inserted'], repeat['updated'], repeat['unchanged'], repeat['duplicated']), (0, 0, 1, 2))
        self.assertEqual(len(self.store.jobs()), 1)
        self.assertEqual(len(self.store.detail(1)['revisions']), 1)

    def test_all_conflicting_id_rows_are_rejected_regardless_of_order(self):
        for source, rows in [('forward', [record(title='A'), record(title='B'), record('ok')]),
                             ('reverse', [record(title='B'), record(title='A'), record('ok')])]:
            with self.subTest(source=source):
                result = self.store.import_records(rows, source, 'conflict')
                self.assertEqual((result['inserted'], result['conflicts'], result['rejected']), (1, 2, 2))
                self.assertEqual({e['row'] for e in result['errors']}, {2, 3})
                self.assertTrue(all(e['reason'] for e in result['errors']))
                self.assertEqual([j['job_id'] for j in self.store.jobs({'source': source})], ['ok'])

    def test_conflicts_preserve_existing_record_and_prevent_authoritative_deactivation(self):
        self.store.import_records([record(), record('absent')], 'source', 'baseline')
        before = self.store.detail(1)
        rows = [record(title='new A'), record(title='new B'), record('accepted')]
        result = self.store.import_records(rows, 'source', 'conflicting snapshot', authoritative=True)
        self.assertEqual(result['deactivated'], 0)
        self.assertEqual(result['conflicts'], 2)
        self.assertEqual(result['inserted'], 1)
        self.assertEqual(self.store.detail(1), before)
        self.assertEqual(len(self.store.jobs()), 3)
        self.assertEqual(self.store.runs()[0]['status'], 'partial')

    def test_raw_difference_is_not_silently_discarded_as_identical_input(self):
        # Equivalent normalized text can still carry different original evidence.
        first = record(description='<p>本科</p>')
        second = record(description='本科')
        result = self.store.import_records([first, second], 'source', 'raw conflict')
        self.assertEqual((result['inserted'], result['conflicts'], result['rejected']), (0, 2, 2))

    def test_additive_review_migration_preserves_jobs_history_users_and_audit(self):
        self.store.import_records([record()], 'legacy', 'before review schema')
        accounts = Accounts(self.store)
        user_id = accounts.create_user('legacy_admin', PASSWORD, '旧管理员', 'admin')
        with self.store.connect() as db:
            Accounts.audit(db, dict(id=user_id, org_id=None), 'legacy_action', 'one')
            db.execute('DROP TABLE IF EXISTS field_reviews')
            db.execute('DROP TABLE IF EXISTS duplicate_decisions')
            # A historical schema predates the new ordered migration marker.
            db.execute('DROP TABLE schema_migrations')
            if 'details' in {row['name'] for row in db.execute('PRAGMA table_info(audit_events)')}:
                db.execute('ALTER TABLE audit_events DROP COLUMN details')
            before = {table: [dict(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY id')]
                      for table in ('jobs', 'runs', 'revisions', 'users', 'audit_events')}
        migrated = Store(self.store.path)
        Accounts(migrated)
        with migrated.connect() as db:
            for table, old_rows in before.items():
                new_rows = [dict(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY id')]
                self.assertEqual(len(new_rows), len(old_rows), table)
                self.assertEqual([{key: new[key] for key in old} for new, old in zip(new_rows, old_rows)], old_rows, table)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')


if __name__ == '__main__':
    unittest.main(verbosity=2)
