"""Offline public-snapshot regressions; every file and database is temporary.

These tests never read the bundled production snapshot or call a network source.
They verify provenance, access control and all-or-nothing import behavior.
"""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from core import Store
from recruitment_insights.application.accounts import Accounts
from recruitment_insights.infrastructure.jobs import JobRepository
from recruitment_insights.infrastructure.sources import BundledPublicSource
from recruitment_insights.web.routes import Routes


CAPTURED = '2026-10-01T00:00:00+00:00'


def raw(job_id='one', **changes):
    return dict(dict(job_id=job_id, title='Python 后端工程师 '+job_id,
                     company='独立样本企业', location='杭州',
                     description='负责 Python 接口开发，要求本科及以上学历。',
                     source_url='https://example.org/jobs/'+job_id,
                     salary_min='10000', salary_max='20000', currency='CNY',
                     salary_period='month', original_note='采集原文必须保留。'), **changes)


def fixtures():
    return [dict(source='greenhouse:fixture_a', label='公开来源 A', fetched_at=CAPTURED,
                 records=[raw('one'), raw('two')]),
            dict(source='lever:fixture_b', label='公开来源 B', fetched_at=CAPTURED,
                 records=[raw('one', company='另一来源企业', location='Singapore')])]


class BundledPublicSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='recruitment_insights-snapshot-adapter-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'snapshot.json'

    def read(self, datasets):
        self.path.write_text(json.dumps(datasets, ensure_ascii=False), encoding='utf-8')
        return BundledPublicSource(self.path).read()

    def test_valid_snapshot_preserves_source_time_and_original_records(self):
        expected = fixtures()
        self.assertEqual(self.read(expected), expected)

    def test_invalid_source_shapes_and_empty_records_are_rejected(self):
        cases = []
        for source in ('csv:fixture_a', 'greenhouse:../fixture', 'greenhouse:', 123):
            datasets = fixtures()
            datasets[0]['source'] = source
            cases.append((repr(source), datasets))
        duplicated = fixtures()
        duplicated[1]['source'] = duplicated[0]['source']
        cases.append(('duplicate source', duplicated))
        empty = fixtures()
        empty[1]['records'] = []
        cases.append(('empty records', empty))
        cases.extend([('empty dataset', []), ('non-object source', [None]),
                      ('non-list bundle', {'sources': fixtures()})])
        for name, datasets in cases:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.read(datasets)

    def test_invalid_future_and_timezone_free_capture_dates_are_rejected(self):
        future = (datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
        for stamp in ('invalid-date', '2026-10-01T00:00:00', future, ''):
            with self.subTest(stamp=stamp):
                datasets = fixtures()
                datasets[0]['fetched_at'] = stamp
                with self.assertRaises(ValueError):
                    self.read(datasets)

    def test_per_source_and_aggregate_limits_are_enforced_before_import(self):
        oversized = fixtures()[:1]
        oversized[0]['records'] = [raw()]*5001
        total = [dict(source='greenhouse:fixture_'+str(i), label='temporary source',
                      fetched_at=CAPTURED, records=[raw()]*4001) for i in range(5)]
        for name, datasets in (('one source', oversized), ('aggregate', total)):
            with self.subTest(limit=name), self.assertRaises(ValueError):
                self.read(datasets)

    def test_missing_or_corrupt_file_does_not_become_an_empty_snapshot(self):
        with self.assertRaises(ValueError):
            BundledPublicSource(self.path).read()
        self.path.write_text('{broken JSON', encoding='utf-8')
        with self.assertRaises(ValueError):
            BundledPublicSource(self.path).read()


class PublicSampleImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='recruitment_insights-public-sample-')
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name)/'temporary.sqlite3')
        self.actors = {}
        owner = self.store.accounts.organization('样本测试企业')
        for role in ('admin', 'candidate', 'boss', 'hr'):
            user_id = self.store.accounts.create_user('fixture_'+role,
                'Temporary-sample-2026!', '临时'+role, role,
                owner if role in ('boss', 'hr') else None, role in ('boss', 'hr'))
            with self.store.uow() as tx:
                self.actors[role] = tx.accounts.get(user_id)
        self.reader = Mock(side_effect=lambda: copy.deepcopy(fixtures()))
        self.store.importer.sample_reader = self.reader

    def rows(self, table):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid')]

    def business(self):
        return {table: self.rows(table) for table in ('jobs', 'revisions', 'runs', 'audit_events')}

    def load(self):
        return self.store.load_public_sample_for(self.actors['admin'])

    def test_admin_import_is_real_public_and_reload_is_content_idempotent(self):
        loaded = self.load()
        self.assertEqual((loaded['total'], loaded['inserted'], loaded['source_count']), (3, 3, 2))
        self.assertEqual([source['source'] for source in loaded['sources']],
                         ['greenhouse:fixture_a', 'lever:fixture_b'])
        self.assertEqual(loaded['captured_at'], CAPTURED)
        saved = self.rows('jobs')
        original_ids = [(row['source'], row['external_id'], row['id']) for row in saved]
        self.assertTrue(all(row['is_demo'] == 0 and row['owner_org_id'] is None and
                            row['published'] == 1 and row['active'] == 1 for row in saved))
        self.assertEqual(len(self.store.jobs({'dataset': 'real'}, self.actors['candidate'])), 3)
        self.assertEqual(self.store.jobs({'dataset': 'demo'}, self.actors['candidate']), [])
        revisions = self.rows('revisions')
        # A later offline reload must not pretend the source was observed today.
        with patch('recruitment_insights.application.imports.now', return_value='2026-10-04T12:00:00+00:00'):
            reloaded = self.load()
        self.assertEqual((reloaded['inserted'], reloaded['updated'], reloaded['unchanged']), (0, 0, 3))
        self.assertEqual([(row['source'], row['external_id'], row['id']) for row in self.rows('jobs')],
                         original_ids)
        self.assertEqual(self.rows('revisions'), revisions)
        self.assertEqual({row['last_seen'] for row in self.rows('jobs')}, {CAPTURED})
        self.assertEqual(len(self.rows('runs')), 4)
        self.assertEqual([row['action'] for row in self.rows('audit_events')], ['load_public_sample']*2)
        self.assertEqual(json.loads(self.rows('jobs')[0]['raw'])['original_note'], '采集原文必须保留。')

    def test_non_admin_imports_and_routes_reject_before_reading_sample(self):
        before = self.business()
        routes = Routes(self.store)
        for role in ('candidate', 'boss', 'hr'):
            with self.subTest(role=role):
                actor = self.actors[role]
                with self.assertRaises(PermissionError):
                    self.store.load_public_sample_for(actor)
                with patch.object(routes.accounts, 'user', return_value=actor):
                    with self.assertRaises(PermissionError):
                        routes.post('/api/public-sample', {}, 'temporary-token')
        self.reader.assert_not_called()
        self.assertEqual(self.business(), before)

    def test_older_offline_snapshot_cannot_replace_newer_source_observation(self):
        self.load()
        fresh = raw('one', title='新的原站岗位标题')
        with patch('recruitment_insights.application.imports.now', return_value='2026-10-03T12:00:00+00:00'):
            self.store.import_records([fresh], 'greenhouse:fixture_a', 'newer real observation')
        before = self.rows('revisions')
        self.load()
        saved = next(row for row in self.rows('jobs')
                     if row['source']=='greenhouse:fixture_a' and row['external_id']=='one')
        self.assertEqual(json.loads(saved['payload'])['title'], fresh['title'])
        self.assertEqual(saved['last_seen'], '2026-10-03T12:00:00+00:00')
        self.assertEqual(self.rows('revisions'), before)

    def test_admin_route_loads_through_authenticated_application_use_case(self):
        routes = Routes(self.store)
        with patch.object(routes.accounts, 'user', return_value=self.actors['admin']):
            response = routes.post('/api/public-sample', {}, 'temporary-token')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.data['inserted'], 3)
        self.reader.assert_called_once_with()

    def test_permission_change_during_read_is_checked_under_write_transaction(self):
        before = self.business()
        def read_after_revocation():
            with self.store.connect() as db:
                db.execute("UPDATE users SET role='candidate' WHERE id=?", (self.actors['admin']['id'],))
            return fixtures()
        self.store.importer.sample_reader = Mock(side_effect=read_after_revocation)
        with self.assertRaises(PermissionError):
            self.load()
        self.store.importer.sample_reader.assert_called_once_with()
        self.assertEqual(self.business(), before)

    def test_second_source_failure_rolls_back_first_source_and_run(self):
        self.load()
        before = self.business()
        self.reader.side_effect = lambda: [dict(dataset, records=[raw('new-'+str(i))])
                                          for i, dataset in enumerate(fixtures())]
        insert = JobRepository.insert
        written_sources = []
        def fail_second(repository, values):
            if values['source'] == 'lever:fixture_b':
                raise RuntimeError('temporary second-source persistence failure')
            job_id = insert(repository, values)
            written_sources.append(values['source'])
            return job_id
        with patch.object(JobRepository, 'insert', new=fail_second):
            with self.assertRaisesRegex(RuntimeError, 'second-source'):
                self.load()
        self.assertEqual(written_sources, ['greenhouse:fixture_a'])
        self.assertEqual(self.business(), before)

    def test_audit_failure_rolls_back_every_source_and_import_run(self):
        before = self.business()
        with patch.object(Accounts, 'audit', side_effect=RuntimeError('temporary audit failure')):
            with self.assertRaisesRegex(RuntimeError, 'audit failure'):
                self.load()
        self.assertEqual(self.business(), before)

    def test_bad_record_or_duplicate_id_prevents_all_sources_from_importing(self):
        before = self.business()
        cases = [raw('broken', title=''), dict(raw('unsafe'), source_url='javascript:alert(1)')]
        for record in cases:
            with self.subTest(record=record['job_id']):
                datasets = fixtures()
                datasets[1]['records'].append(record)
                self.reader.side_effect = None
                self.reader.return_value = datasets
                with self.assertRaises(ValueError):
                    self.load()
                self.assertEqual(self.business(), before)
        datasets = fixtures()
        datasets[1]['records'].append(copy.deepcopy(datasets[1]['records'][0]))
        self.reader.return_value = datasets
        with self.assertRaises(ValueError):
            self.load()
        self.assertEqual(self.business(), before)

    def test_manual_withdrawal_survives_offline_reload(self):
        self.load()
        job_id = self.rows('jobs')[0]['id']
        detail = self.store.detail(job_id, self.actors['admin'])
        self.store.set_lifecycle(self.actors['admin'], dict(id=job_id, action='deactivate',
            reason='岗位已招满，保持人工下架。', expected_edit_token=detail['edit_token']))
        revisions = self.rows('revisions')
        self.load()
        withdrawn = self.store.detail(job_id, self.actors['admin'])
        self.assertFalse(withdrawn['active'])
        self.assertTrue(withdrawn['source_active'])
        self.assertTrue(withdrawn['manual_inactive'])
        self.assertEqual(withdrawn['lifecycle_reason'], '岗位已招满，保持人工下架。')
        self.assertIsNone(self.store.detail(job_id, self.actors['candidate']))
        self.assertEqual(len(self.store.jobs({'dataset': 'real'}, self.actors['candidate'])), 2)
        self.assertEqual(self.rows('revisions'), revisions)
        self.assertEqual(len(self.rows('jobs')), 3)


if __name__ == '__main__':
    unittest.main()
