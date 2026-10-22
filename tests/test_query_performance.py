"""Stable workload regressions for pure extraction reuse and live query state."""
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from core import Store
from seed_demo_accounts import seed
from recruitment_insights.domain import reviews


class QueryExtractionReuseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='recruitment_insights-query-test-')
        self.store = Store(Path(self.temporary.name)/'queries.sqlite3')
        seed(self.store)
        with self.store.uow() as tx:
            self.admin = tx.accounts.by_username('admin')
            self.candidate = tx.accounts.by_username('candidate_a')
        reviews.cached_candidates.cache_clear()

    def tearDown(self):
        reviews.cached_candidates.cache_clear()
        self.temporary.cleanup()

    @staticmethod
    def record(index, description=None):
        return dict(job_id=str(index), title='开发岗位 '+str(index), company='性能回归企业', location='北京',
                    description=description or f'本科及以上学历。要求至少 2 年开发经验。岗位编号 {index}。',
                    education='', experience='')

    def test_repeated_queries_reuse_extraction_across_more_than_512_distinct_jobs(self):
        records = [self.record(index) for index in range(600)]
        self.store.import_records(records, 'test:query', 'pure-cache regression')
        with patch.object(reviews, 'candidates', wraps=reviews.candidates) as extraction:
            options = self.store.queries.options({'dataset': 'real'}, self.candidate)
            self.assertEqual(extraction.call_count, 600)
            self.assertEqual(options['education'], ['待核验'])
            self.assertEqual(self.store.queries.stats({'dataset': 'real'}, self.candidate)['total'], 600)
            page = self.store.queries.paginated({'dataset': 'real'}, self.candidate)
            self.assertEqual((page['total'], len(page['items'])), (600, 15))
            self.assertIn(b'test:query', self.store.queries.export({'dataset': 'real'}, self.candidate))
            self.assertEqual(extraction.call_count, 600, 'Warm queries must not rerun the pure extractor')
            self.assertLessEqual(reviews.cached_candidates.cache_info().currsize,
                                 reviews.cached_candidates.cache_info().maxsize)

    def test_warm_cache_keeps_review_changes_and_changed_source_visible_immediately(self):
        raw = self.record(1)
        self.store.import_records([raw], 'test:query', 'review-state regression')
        job_id = self.store.jobs({'dataset': 'real'}, self.admin)[0]['id']
        before = self.store.detail(job_id, self.admin)
        requirement = before['requirements']['education']
        old_source_hash = requirement['source_hash']
        self.store.review_field(self.admin, dict(job_id=job_id, field='education', action='confirm',
            source_hash=old_source_hash, version=requirement['version'],
            value='本科', condition='硬性要求', evidence='本科及以上学历。', alternatives=[]))
        self.assertEqual(self.store.queries.stats({'dataset': 'real'}, self.candidate)['education'],
                         [{'name': '本科', 'count': 1}])
        confirmed = self.store.detail(job_id, self.admin)['requirements']['education']
        self.store.review_field(self.admin, dict(job_id=job_id, field='education', action='revoke',
            source_hash=confirmed['source_hash'], version=confirmed['version']))
        self.assertEqual(self.store.queries.stats({'dataset': 'real'}, self.candidate)['education'],
                         [{'name': '待核验', 'count': 1}])
        revoked = self.store.detail(job_id, self.admin)['requirements']['education']
        self.store.review_field(self.admin, dict(job_id=job_id, field='education', action='confirm',
            source_hash=revoked['source_hash'], version=revoked['version'],
            value='本科', condition='硬性要求', evidence='本科及以上学历。', alternatives=[]))
        changed = self.record(1, '硕士及以上学历。岗位要求已经修改。')
        self.store.import_records([changed], 'test:query', 'source change')
        updated_requirement = self.store.detail(job_id, self.admin)['requirements']['education']
        self.assertNotEqual(updated_requirement['source_hash'], old_source_hash)
        current = self.store.detail(job_id, self.candidate)
        self.assertEqual(current['requirements']['education']['status'], 'stale')
        self.assertEqual(current['requirements']['education']['value'], '硕士')
        self.assertEqual(self.store.queries.stats({'dataset': 'real'}, self.candidate)['education'],
                         [{'name': '待复核', 'count': 1}])
        # The cached pure output cannot override the source snapshot or active state.
        manageable = self.store.detail(job_id, self.admin)
        self.store.set_lifecycle(self.admin, dict(id=job_id, action='deactivate', reason='查询状态回归',
            expected_edit_token=manageable['edit_token']))
        self.assertEqual(self.store.queries.stats({'dataset': 'real'}, self.candidate)['total'], 0)
        self.assertIsNone(self.store.detail(job_id, self.candidate))

    def test_narrow_search_extracts_only_matching_jobs_and_then_applies_requirement_filters(self):
        self.store.import_records([self.record(index) for index in range(600)],
                                  'test:query', 'narrow-query regression')
        with patch.object(reviews, 'candidates', wraps=reviews.candidates) as extraction:
            jobs = self.store.jobs({'dataset': 'real', 'q': '岗位编号 314。',
                                   'education': '待核验', 'experience': '待核验'}, self.candidate)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]['title'], '开发岗位 314')
            self.assertEqual(extraction.call_count, 1)
            empty = self.store.jobs({'dataset': 'real', 'q': '岗位编号 314。',
                                    'education': '本科'}, self.candidate)
            self.assertEqual(empty, [], 'Unconfirmed requirements cannot pass a specific confirmed-value filter')
            self.assertEqual(extraction.call_count, 1)

    def test_concurrent_cold_queries_extract_each_pure_input_once(self):
        # Three clients reach the same cold input while its extraction is in
        # flight. Coordinate at JSON encoding, before the cache lookup/lock.
        serialization_count = 0
        serialization_guard = threading.Lock()
        all_encoded = threading.Event()
        encode = reviews.json.dumps
        extract = reviews.candidates
        raw = {'education': '', 'experience': '', 'description': '本科及以上学历。至少 2 年开发经验。'}
        target_fields = {field: raw[field] for field in ('education', 'experience')}

        def encoded(*args, **kwargs):
            nonlocal serialization_count
            result = encode(*args, **kwargs)
            # The same JSON module also encodes field-fingerprint lists inside
            # the pure cache. Coordinate only the three clients' input objects.
            if args[0] == target_fields:
                with serialization_guard:
                    serialization_count += 1
                    if serialization_count == 3:
                        all_encoded.set()
            return result

        def in_flight(value):
            self.assertTrue(all_encoded.wait(timeout=5), 'All clients must reach the same input')
            return extract(value)

        with patch.object(reviews.json, 'dumps', side_effect=encoded), \
             patch.object(reviews, 'candidates', side_effect=in_flight) as extraction, \
             ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(reviews.propose, [raw]*3))
        self.assertEqual(serialization_count, 3)
        self.assertEqual(extraction.call_count, 1, 'Concurrent misses must share one completed pure extraction')
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])
        self.assertEqual(results[0]['education']['value'], '本科')


if __name__ == '__main__':
    unittest.main()
