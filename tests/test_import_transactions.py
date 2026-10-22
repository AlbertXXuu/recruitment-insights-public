"""Real SQLite regressions for preparation outside bulk-import transactions."""
import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from core import Store
from recruitment_insights.application import imports


PASSWORD = 'Import-transactions-2026!'
WAIT = 5
JOIN_WAIT = 35  # Includes the existing 30-second SQLite busy timeout on failure.
CAPTURED = '2026-10-01T00:00:00+00:00'


def raw(job_id='one', **changes):
    return dict(dict(job_id=job_id, title='Python 后端工程师 ' + job_id,
                     company='输入企业', location='杭州',
                     description='开发 Python 接口，要求本科及以上学历。',
                     original_note={'evidence': ['原始记录']}), **changes)


class ImportTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='recruitment_insights-import-transactions-')
        self.workers, self.releases = [], []
        self.addCleanup(self.cleanup_database)
        self.store = Store(Path(self.temporary.name) / 'isolated.sqlite3')
        self.org_a = self.store.accounts.organization('导入企业 A')
        self.org_b = self.store.accounts.organization('导入企业 B')
        self.admin = self.create('import_admin', 'admin')
        self.hr = self.create('import_hr', 'hr', self.org_a)
        self.candidate = self.create('import_candidate')

    def cleanup_database(self):
        for release in self.releases:
            release.set()
        for worker in self.workers:
            worker.join(JOIN_WAIT)
        self.assertFalse(any(worker.is_alive() for worker in self.workers),
                         'Import worker did not stop; preserve its database until it exits')
        self.temporary.cleanup()

    def create(self, username, role='candidate', org=None):
        user_id = self.store.accounts.create_user(username, PASSWORD, username, role, org, role == 'hr')
        with self.store.uow() as tx:
            return tx.accounts.get(user_id)

    def rows(self, sql, args=()):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute(sql, args)]

    def business(self):
        return {table: self.rows('SELECT * FROM ' + table + ' ORDER BY id')
                for table in ('jobs', 'runs', 'revisions', 'audit_events')}

    def start(self, name, operation):
        result = Future()

        def run():
            try:
                result.set_result(operation())
            except BaseException as exc:
                result.set_exception(exc)

        worker = threading.Thread(name=name, target=run, daemon=True)
        self.workers.append(worker)
        worker.start()
        return result, worker

    def write(self, mutate):
        def operation():
            with self.store.uow(write=True) as tx:
                return mutate(tx.accounts.db)
        pending, _ = self.start('independent-import-writer', operation)
        return pending.result(WAIT)

    def csv_body(self, source='transaction-fixture'):
        return dict(source=source, csv='job_id,title,company,location,original_note\n'
                    'one,Python 后端开发,输入企业,杭州,原始记录一\n'
                    'two,Python 后端测试,输入企业,北京,原始记录二\n')

    def datasets(self, records=None):
        return [dict(source='greenhouse:transaction_fixture', label='隔离公开样本',
                     fetched_at=CAPTURED, records=records or [raw(), raw('two')])]

    def payload(self):
        return {'jobs': [dict(id=job_id, title='Software Engineer ' + str(job_id),
                              location={'name': '杭州'}, content='Python 原始岗位描述')
                         for job_id in (1, 2)]}

    @contextmanager
    def paused_normalization(self, operation):
        """Pause real normalization; track this request's live UOW and raw encoding."""
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        observed = dict(active=0, transactions=[], normalized=[], raw_encoding=[])
        name = 'paused-import-' + str(len(self.releases))
        original_uow = self.store.importer.uow
        original_normalize, original_dumps = imports.normalize, json.dumps

        @contextmanager
        def tracked_uow(*, write=False):
            with original_uow(write=write) as tx:
                tracked = threading.current_thread().name == name
                if tracked:
                    observed['transactions'].append(write)
                    observed['active'] += 1
                try:
                    yield tx
                finally:
                    if tracked:
                        observed['active'] -= 1

        def real_normalize(record):
            if threading.current_thread().name == name:
                observed['normalized'].append((record['job_id'], observed['active']))
                if len(observed['normalized']) == 1:
                    entered.set()
                    if not release.wait(JOIN_WAIT):
                        raise TimeoutError('Test did not release import normalization')
            return original_normalize(record)

        def real_dumps(value, *args, **kwargs):
            if (threading.current_thread().name == name and isinstance(value, dict)
                    and 'job_id' in value and ('original_note' in value or '_source_record' in value)):
                observed['raw_encoding'].append((value['job_id'], observed['active']))
            return original_dumps(value, *args, **kwargs)

        with patch.object(self.store.importer, 'uow', new=tracked_uow), \
             patch.object(imports, 'normalize', new=real_normalize), \
             patch.object(json, 'dumps', new=real_dumps):
            pending, worker = self.start(name, operation)
            try:
                self.assertTrue(entered.wait(WAIT), 'Normalization was not reached')
                self.assertFalse(pending.done())
                self.assertEqual(observed['normalized'][0][1], 0,
                                 'Normalization must run outside every UOW')
                yield pending, release, observed
            finally:
                release.set()
                worker.join(JOIN_WAIT)
                self.assertFalse(worker.is_alive(), 'Paused import did not stop')

    def test_four_bulk_entries_prepare_once_outside_uow_and_allow_independent_io(self):
        self.store.importer.sample_reader = self.datasets
        operations = (
            ('records', lambda: self.store.import_records([raw(), raw('two')], 'local:fixture', 'local')),
            ('csv', lambda: self.store.import_for(self.hr, self.csv_body())),
            ('sync', lambda: self.store.importer.sync(self.payload(), actor=self.admin)),
            ('sample', lambda: self.store.load_public_sample_for(self.admin)),
        )
        for entry, operation in operations:
            with self.subTest(entry=entry), self.paused_normalization(operation) as paused:
                pending, release, observed = paused
                self.write(lambda db: db.execute('UPDATE users SET display_name=? WHERE id=?',
                                                 (entry, self.candidate['id'])))

                def read():
                    with self.store.uow() as tx:
                        return tx.accounts.get(self.candidate['id'])['display_name']
                reading, _ = self.start('independent-import-reader', read)
                self.assertEqual(reading.result(WAIT), entry)
                self.assertFalse(pending.done(), 'Independent I/O must finish while import is paused')
                release.set()
                result = pending.result(WAIT)
            self.assertEqual(result['inserted'], 2)
            self.assertEqual(len(observed['normalized']), 2, 'Each input row is normalized exactly once')
            self.assertTrue(all(active == 0 for _, active in observed['normalized']))
            self.assertEqual({job_id for job_id, _ in observed['raw_encoding']},
                             {job_id for job_id, _ in observed['normalized']})
            self.assertTrue(all(active == 0 for _, active in observed['raw_encoding']),
                            'Raw record serialization must finish before the write transaction')
            self.assertEqual(observed['active'], 0)

    def test_csv_rechecks_grant_organization_and_company_name_after_preparation(self):
        for change in ('permission', 'organization', 'company_name'):
            with self.subTest(change=change):
                hr = self.create('csv_' + change, 'hr', self.org_a)
                before = self.business()

                def mutate(db):
                    if change == 'permission':
                        db.execute('UPDATE users SET can_manage_jobs=0 WHERE id=?', (hr['id'],))
                    elif change == 'organization':
                        db.execute('UPDATE users SET org_id=? WHERE id=?', (self.org_b, hr['id']))
                    else:
                        db.execute('UPDATE organizations SET name=? WHERE id=?', ('更名后的企业', self.org_a))

                with self.paused_normalization(lambda: self.store.import_for(hr, self.csv_body(change))) as paused:
                    pending, release, _ = paused
                    self.write(mutate)
                    release.set()
                    with self.assertRaises(PermissionError):
                        pending.result(WAIT)
                self.assertEqual(self.business(), before)

    def test_csv_accepts_current_authorized_role_and_keeps_company_bound(self):
        with self.paused_normalization(lambda: self.store.import_for(self.hr, self.csv_body())) as paused:
            pending, release, _ = paused
            self.write(lambda db: db.execute("UPDATE users SET role='boss' WHERE id=?", (self.hr['id'],)))
            release.set()
            result = pending.result(WAIT)
        self.assertEqual(result['inserted'], 2)
        for job in self.rows('SELECT * FROM jobs'):
            self.assertEqual(job['owner_org_id'], self.org_a)
            self.assertEqual(job['published'], 0)
            self.assertEqual(json.loads(job['payload'])['company'], '导入企业 A')
            self.assertEqual(json.loads(job['raw'])['_imported_company'], '输入企业')
        self.assertEqual(self.rows('SELECT action FROM audit_events'), [{'action': 'import_csv'}])

    def test_public_entries_recheck_admin_and_only_sync_retains_failed_run(self):
        self.store.importer.sample_reader = self.datasets
        for entry in ('sample', 'sync'):
            with self.subTest(entry=entry):
                admin = self.create('revoked_' + entry, 'admin')
                operation = (lambda: self.store.load_public_sample_for(admin)) if entry == 'sample' else \
                            (lambda: self.store.importer.sync(self.payload(), actor=admin))
                with self.paused_normalization(operation) as paused:
                    pending, release, _ = paused
                    self.write(lambda db: db.execute("UPDATE users SET role='candidate' WHERE id=?", (admin['id'],)))
                    release.set()
                    with self.assertRaises(PermissionError):
                        pending.result(WAIT)
                self.assertEqual(self.rows('SELECT * FROM jobs'), [])
                self.assertEqual(self.rows('SELECT * FROM revisions'), [])
                self.assertEqual(self.rows('SELECT * FROM audit_events'), [])
                self.assertEqual(self.rows('SELECT status FROM runs'),
                                 [] if entry == 'sample' else [{'status': 'failed'}])
                self.assertFalse(self.store.importer.sync_lock.locked())

    def test_offline_preparation_rechecks_newer_observation_and_manual_withdrawal(self):
        source = 'greenhouse:transaction_fixture'
        self.store.import_records([raw()], source, 'existing')
        self.store.importer.sample_reader = lambda: self.datasets([raw(title='离线快照旧标题')])
        job_id = self.rows('SELECT id FROM jobs')[0]['id']
        newer_stamp = '2026-10-03T12:00:00+00:00'
        with self.paused_normalization(lambda: self.store.load_public_sample_for(self.admin)) as paused:
            pending, release, _ = paused
            with patch.object(imports, 'now', return_value=newer_stamp):
                self.store.import_records([raw(title='准备期间的新来源内容')], source, 'fresh observation')
            before = self.business()
            release.set()
            result = pending.result(WAIT)
        self.assertEqual((result['updated'], result['unchanged']), (0, 1))
        self.assertEqual(self.rows('SELECT * FROM jobs'), before['jobs'])
        self.assertEqual(self.rows('SELECT * FROM revisions'), before['revisions'])

        self.store.importer.sample_reader = lambda: [dict(self.datasets([raw(title='更新的离线标题')])[0],
                                                        fetched_at='2026-10-04T00:00:00+00:00')]
        with self.paused_normalization(lambda: self.store.load_public_sample_for(self.admin)) as paused:
            pending, release, _ = paused
            detail = self.store.detail(job_id, self.admin)
            self.store.set_lifecycle(self.admin, dict(id=job_id, action='deactivate', reason='岗位已招满',
                expected_edit_token=detail['edit_token']))
            release.set()
            result = pending.result(WAIT)
        self.assertEqual(result['updated'], 1)
        saved = self.rows('SELECT * FROM jobs')[0]
        self.assertEqual(json.loads(saved['payload'])['title'], '更新的离线标题')
        self.assertEqual((saved['source_active'], saved['manual_inactive'], saved['active']), (1, 1, 0))
        self.assertEqual(saved['lifecycle_reason'], '岗位已招满')
        self.assertIsNone(self.store.detail(job_id, self.candidate))

    def test_prepared_batch_reuse_has_detached_evidence_and_fresh_result(self):
        valid, invalid = raw(), raw('invalid', title='')
        originals = copy.deepcopy([valid, invalid])
        batch = imports.prepare_batch([valid, invalid, copy.deepcopy(valid)])
        template = batch.result_json
        valid['title'] = '不能进入已准备批次'
        valid['original_note']['evidence'].append('后续更改')
        invalid['original_note']['evidence'].clear()
        with self.store.uow(write=True) as tx:
            first = self.store.importer.apply_prepared(tx, batch, 'prepared:fixture', 'first')
        self.assertEqual((first['total'], first['inserted'], first['rejected'], first['duplicated']), (3, 1, 1, 1))
        self.assertEqual(first['errors'][0]['raw'], originals[1])
        first['errors'][0]['raw']['original_note']['evidence'].append('结果不能污染下次')
        first['inserted'] = 999
        with self.store.uow(write=True) as tx:
            repeat = self.store.importer.apply_prepared(tx, batch, 'prepared:fixture', 'repeat')
        self.assertEqual((repeat['inserted'], repeat['updated'], repeat['unchanged'], repeat['rejected']), (0, 0, 1, 1))
        self.assertEqual(repeat['errors'][0]['raw'], originals[1])
        self.assertEqual(batch.result_json, template)
        saved = self.rows('SELECT * FROM jobs')[0]
        self.assertEqual(json.loads(saved['raw']), originals[0])
        self.assertEqual(json.loads(saved['payload'])['title'], originals[0]['title'])
        self.assertEqual(len(self.rows('SELECT * FROM revisions')), 1)

    def test_prepared_source_ownership_conflict_rolls_back_prior_insert_and_run(self):
        self.store.import_records([raw()], 'owned:fixture', 'existing', owner_org_id=self.org_a)
        before = self.business()
        batch = imports.prepare_batch([raw('new-first'), raw()])
        with self.assertRaisesRegex(ValueError, '归属'):
            with self.store.uow(write=True) as tx:
                self.store.importer.apply_prepared(tx, batch, 'owned:fixture', 'conflict', owner_org_id=self.org_b)
        self.assertEqual(self.business(), before)

    def test_apply_remains_in_callers_transaction_and_outer_failure_rolls_back(self):
        before = self.business()
        with patch.object(self.store.importer, 'uow', side_effect=AssertionError('Nested transaction')):
            with self.assertRaisesRegex(RuntimeError, 'outer operation failed'):
                with self.store.uow(write=True) as tx:
                    result = self.store.importer.apply(tx, [raw()], 'composed:fixture', 'single-job edit')
                    self.assertEqual(result['inserted'], 1)
                    raise RuntimeError('outer operation failed')
        self.assertEqual(self.business(), before)


if __name__ == '__main__':
    unittest.main()
