"""Real SQLite checks for reuse of pure, fixed public-snapshot preparation."""
import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from core import Store
from recruitment_insights.application import imports
from recruitment_insights.infrastructure.accounts import AuditRepository
from recruitment_insights.infrastructure.sources import BundledPublicSource


def samples():
    def record(job_id):
        return dict(job_id=job_id, title='Python Engineer ' + job_id,
                    company='Isolated Fixture', location='Test',
                    description='Python / Docker', original_note={'evidence': ['original']})
    return [dict(source='greenhouse:cache_a', label='Fixture A',
                 fetched_at='2026-10-01T00:00:00+00:00',
                 records=[record('one'), record('two')]),
            dict(source='lever:cache_b', label='Fixture B',
                 fetched_at='2026-10-01T00:00:00+00:00', records=[record('three')])]


class PreparedSampleCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='recruitment-prepared-sample-cache-')
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'isolated.sqlite3')
        user_id = self.store.accounts.create_user('fixture_admin', 'Sample-cache-fixture-2026!',
                                                   'Fixture', 'admin')
        with self.store.uow() as tx:
            self.admin = tx.accounts.get(user_id)
        self.input = samples()
        self.reader = Mock(side_effect=lambda: copy.deepcopy(self.input))
        self.store.importer.sample_reader = self.reader

    def load(self):
        return self.store.load_public_sample_for(self.admin)

    def rows(self, table):
        with self.store.uow() as tx:
            return [dict(row) for row in tx.accounts.db.execute('SELECT * FROM ' + table + ' ORDER BY id')]

    def business(self):
        with self.store.uow() as tx:
            return {table: [dict(row) for row in tx.accounts.db.execute('SELECT * FROM ' + table + ' ORDER BY id')]
                    for table in ('jobs', 'runs', 'revisions', 'audit_events')}

    def role(self, role):
        with self.store.uow(write=True) as tx:
            tx.accounts.db.execute('UPDATE users SET role=? WHERE id=?', (role, self.admin['id']))

    def test_identical_reload_reuses_preparation_but_reads_writes_and_counts_are_fresh(self):
        with patch.object(imports, 'normalize', wraps=imports.normalize) as normalize:
            first = self.load()
            second = self.load()
            second['inserted'] = 999
            second['runs'][0]['unchanged'] = 999
            third = self.load()
        self.assertEqual(normalize.call_count, 3)
        self.assertEqual(self.reader.call_count, 3)
        self.assertEqual(first['inserted'], 3)
        self.assertEqual((third['inserted'], third['updated'], third['unchanged']), (0, 0, 3))
        self.assertEqual([r['unchanged'] for r in third['runs']], [2, 1])
        self.assertEqual(len(self.rows('runs')), 6)
        self.assertEqual(len(self.rows('audit_events')), 3)
        self.assertEqual(len(self.rows('revisions')), 3)
        self.assertEqual({row['run_id'] for row in self.rows('jobs')}, {5, 6})

    def test_source_label_observation_raw_metadata_and_payload_changes_invalidate(self):
        with patch.object(imports, 'normalize', wraps=imports.normalize) as normalize:
            self.load()
            self.input[0]['label'] = 'Changed Label'
            self.load()
            self.assertEqual(self.rows('runs')[-2]['label'], 'Changed Label')
            self.input[0]['fetched_at'] = '2026-10-02T00:00:00+00:00'
            self.load()
            self.assertEqual(self.rows('jobs')[0]['last_seen'], self.input[0]['fetched_at'])
            self.input[0]['records'][0]['original_note']['evidence'].append('new evidence')
            changed_metadata = self.load()
            self.assertEqual(changed_metadata['unchanged'], 3)
            self.assertEqual(json.loads(self.rows('jobs')[0]['raw'])['original_note'],
                             {'evidence': ['original', 'new evidence']})
            self.input[0]['records'][0]['title'] = 'Changed Python Engineer'
            changed_title = self.load()
            self.assertEqual(changed_title['updated'], 1)
            self.input[0]['source'] = 'greenhouse:changed_source'
            changed_source = self.load()
            self.assertEqual(changed_source['inserted'], 2)
        self.assertEqual(normalize.call_count, 18)
        self.assertEqual(len(self.rows('jobs')), 5)

    def test_input_key_order_change_preserves_original_raw_bytes_and_edit_token(self):
        with patch.object(imports, 'normalize', wraps=imports.normalize) as normalize:
            self.load()
            before = self.rows('jobs')[0]
            before_token = self.store.detail(before['id'], self.admin)['edit_token']
            original = self.input[0]['records'][0]
            self.input[0]['records'][0] = dict(reversed(list(original.items())))
            self.load()
            after = self.rows('jobs')[0]
        self.assertEqual(normalize.call_count, 6)
        self.assertEqual(after['raw'], json.dumps(self.input[0]['records'][0], ensure_ascii=False))
        self.assertNotEqual(before['raw'], after['raw'])
        self.assertEqual(before['fingerprint'], after['fingerprint'])
        self.assertNotEqual(before_token, self.store.detail(after['id'], self.admin)['edit_token'])

    def test_missing_corrupt_and_invalid_snapshot_never_fall_back_to_cached_input(self):
        path = Path(self.temp.name) / 'snapshot.json'
        valid = json.dumps(self.input, ensure_ascii=False)
        path.write_text(valid, encoding='utf-8')
        self.store.importer.sample_reader = BundledPublicSource(path).read
        with patch.object(imports, 'normalize', wraps=imports.normalize) as normalize:
            self.load()
            before = self.business()
            path.write_text('{invalid JSON', encoding='utf-8')
            with self.assertRaises(ValueError):
                self.load()
            self.assertEqual(self.business(), before)
            path.unlink()
            with self.assertRaises(ValueError):
                self.load()
            self.assertEqual(self.business(), before)
            broken = copy.deepcopy(self.input)
            broken[1]['records'].append(copy.deepcopy(broken[1]['records'][0]))
            path.write_text(json.dumps(broken, ensure_ascii=False), encoding='utf-8')
            with self.assertRaises(ValueError):
                self.load()
            self.assertEqual(self.business(), before)
            path.write_text(valid, encoding='utf-8')
            calls_before = normalize.call_count
            self.load()
            self.assertEqual(normalize.call_count, calls_before)
        self.assertFalse(self.store.importer._sample_prepare_lock.locked())

    def test_hot_cache_still_requires_admin_before_read_and_inside_write(self):
        self.load()
        before = self.business()
        self.role('candidate')
        with self.assertRaises(PermissionError):
            self.load()
        self.assertEqual(self.reader.call_count, 1)
        self.role('admin')
        def revoke_while_reading():
            self.role('candidate')
            return copy.deepcopy(self.input)
        self.reader.side_effect = revoke_while_reading
        with self.assertRaises(PermissionError):
            self.load()
        self.assertEqual(self.reader.call_count, 2)
        self.assertEqual(self.business(), before)

    def test_audit_failure_rolls_back_and_retry_reuses_only_preparation(self):
        with patch.object(imports, 'normalize', wraps=imports.normalize) as normalize:
            self.load()
            before = self.business()
            with patch.object(AuditRepository, 'append', side_effect=RuntimeError('injected audit failure')):
                with self.assertRaisesRegex(RuntimeError, 'injected audit failure'):
                    self.load()
            self.assertEqual(self.business(), before)
            result = self.load()
            self.assertEqual(normalize.call_count, 3)
        self.assertEqual((result['inserted'], result['updated'], result['unchanged']), (0, 0, 3))
        self.assertEqual(len(self.rows('runs')), 4)
        self.assertEqual(len(self.rows('audit_events')), 2)

    def test_preparation_detaches_caller_mutation(self):
        entered, release = threading.Event(), threading.Event()
        original = imports.normalize
        def paused_normalize(raw):
            entered.set()
            if not release.wait(10):
                raise TimeoutError('Fixture did not release preparation')
            return original(raw)
        # The reader deliberately returns its shared input; serialization must
        # detach it before any preparation or concurrent caller modification.
        self.reader.side_effect = lambda: self.input
        with patch.object(imports, 'normalize', side_effect=paused_normalize) as normalize:
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(self.load)
                try:
                    self.assertTrue(entered.wait(5))
                    self.input[0]['records'][0]['original_note']['evidence'].append('caller mutation')
                    release.set()
                    first_result = first.result(10)
                finally:
                    release.set()
            self.assertEqual(json.loads(self.rows('jobs')[0]['raw'])['original_note'],
                             {'evidence': ['original']})
            second_result = self.load()
            self.assertEqual(normalize.call_count, 6)
        self.assertEqual(first_result['inserted'] + second_result['inserted'], 3)
        revision = next(row for row in self.rows('revisions') if row['job_id'] == 1)
        self.assertEqual(json.loads(revision['raw'])['original_note'], {'evidence': ['original']})
        self.assertEqual(json.loads(self.rows('jobs')[0]['raw'])['original_note'],
                         {'evidence': ['original', 'caller mutation']})
        self.assertFalse(self.store.importer._sample_prepare_lock.locked())

    def test_parallel_identical_cold_input_has_one_preparation(self):
        entered, release = threading.Event(), threading.Event()
        original = imports.normalize
        def paused_normalize(raw):
            entered.set()
            if not release.wait(10):
                raise TimeoutError('Fixture did not release preparation')
            return original(raw)
        with patch.object(imports, 'normalize', side_effect=paused_normalize) as normalize:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.load)
                try:
                    self.assertTrue(entered.wait(5))
                    second = pool.submit(self.load)
                    release.set()
                    results = [first.result(10), second.result(10)]
                finally:
                    release.set()
            self.assertEqual(normalize.call_count, 3)
        self.assertEqual(self.reader.call_count, 2)
        self.assertEqual(sorted(result['inserted'] for result in results), [0, 3])
        self.assertEqual(sorted(result['unchanged'] for result in results), [0, 3])
        self.assertEqual(len(self.rows('audit_events')), 2)

    def test_cold_preparation_failure_unlocks_and_later_valid_input_works(self):
        with patch.object(imports, 'prepare_batch', side_effect=RuntimeError('cold prepare failure')):
            with self.assertRaisesRegex(RuntimeError, 'cold prepare failure'):
                self.load()
        self.assertFalse(self.store.importer._sample_prepare_lock.locked())
        self.assertIsNone(self.store.importer._prepared_sample)
        self.assertEqual(self.load()['inserted'], 3)
        self.store.importer.sample_reader = None
        before = self.business()
        with self.assertRaisesRegex(ValueError, '未提供'):
            self.load()
        self.assertEqual(self.business(), before)

    def test_replacing_normalizer_recomputes_even_when_input_is_identical(self):
        self.load()
        original = imports.normalize
        with patch.object(imports, 'normalize', wraps=original) as normalize:
            self.load()
            self.load()
            self.assertEqual(normalize.call_count, 3)


if __name__ == '__main__':
    unittest.main()
