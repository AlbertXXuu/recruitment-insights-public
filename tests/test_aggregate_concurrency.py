"""Aggregate admission preserves current permissions and independent small work."""
import tempfile
import threading
import unittest
from concurrent.futures import Future
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from core import Store
from recruitment_insights.application import queries


WAIT = 5
AGGREGATES = ('overview', 'stats', 'options')


class ObservedGate:
    """Signal an actual admission attempt while retaining the production gate."""
    def __init__(self, gate, names):
        self.gate = gate
        self.waiting = {name: threading.Event() for name in names}

    def __enter__(self):
        self.waiting[threading.current_thread().name].set()
        return self.gate.__enter__()

    def __exit__(self, *exception):
        return self.gate.__exit__(*exception)


class AggregateConcurrencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='recruitment_insights-aggregate-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.store = Store(self.directory / 'primary.sqlite3')
        self.org = self.store.accounts.organization('并发测试企业')
        self.actors = {}
        for role in ('boss', 'hr', 'candidate'):
            self.store.accounts.create_user(role + '_aggregate', 'Concurrency-2026!', role,
                                            role, self.org if role != 'candidate' else None,
                                            role == 'hr')
            with self.store.uow() as tx:
                self.actors[role] = tx.accounts.by_username(role + '_aggregate')

    def start(self, name, operation):
        future = Future()

        def run():
            try:
                future.set_result(operation())
            except BaseException as exc:
                future.set_exception(exc)

        worker = threading.Thread(target=run, name=name, daemon=True)
        worker.start()
        self.addCleanup(worker.join, WAIT)
        return future

    def wait_for(self, event):
        self.assertTrue(event.wait(WAIT), 'Worker did not reach the admission gate')

    def insert(self, name, *, published=True):
        self.store.import_records([dict(job_id=name, title='Python 岗位 ' + name,
            company='并发测试企业', location=name, description='本科及以上学历，负责 Python 开发。',
            education='本科', experience='不限')], 'test:aggregate', 'aggregate fixture',
            owner_org_id=self.org)
        with self.store.uow(write=True) as tx:
            job_id = tx.jobs.by_source('test:aggregate', name)['id']
            tx.jobs.flags(job_id, published, True)
        return job_id

    def test_all_aggregate_entries_wait_before_opening_a_unit_of_work(self):
        self.insert('公开地区')
        read = self.store.queries
        gate = ObservedGate(read._aggregate_slots, AGGREGATES)
        with patch.object(read, '_aggregate_slots', gate), \
             patch.object(read, 'uow', wraps=read.uow) as opened:
            with gate.gate:
                pending = {name: self.start(name, lambda name=name:
                    getattr(read, name)({}, self.actors['candidate'])) for name in AGGREGATES}
                for event in gate.waiting.values():
                    self.wait_for(event)
                self.assertEqual(opened.call_count, 0, 'Queued reads must not open a snapshot')
                self.assertTrue(all(not future.done() for future in pending.values()))
            results = {name: future.result(WAIT) for name, future in pending.items()}
        self.assertEqual(opened.call_count, 3)
        self.assertEqual(results['overview']['jobs']['total'], 1)
        self.assertEqual(results['stats']['total'], 1)
        self.assertEqual(results['options']['location'], ['公开地区'])

    def test_queued_reads_reload_downgraded_role_and_withdrawn_publication(self):
        visible = self.insert('仍公开地区')
        withdrawn = self.insert('已撤回地区')
        self.insert('企业私密地区', published=False)
        read = self.store.queries
        stale_actor = self.actors['hr']
        gate = ObservedGate(read._aggregate_slots, AGGREGATES)
        with patch.object(read, '_aggregate_slots', gate):
            with gate.gate:
                pending = {name: self.start(name, lambda name=name:
                    getattr(read, name)({'scope': 'company'}, stale_actor)) for name in AGGREGATES}
                for event in gate.waiting.values():
                    self.wait_for(event)
                with self.store.connect() as db:
                    # Retaining org_id exercises the domain guard after SQL company filtering.
                    db.execute("UPDATE users SET role='candidate',can_manage_jobs=0 WHERE id=?",
                               (stale_actor['id'],))
                    db.execute('UPDATE jobs SET published=0 WHERE id=?', (withdrawn,))
            results = {name: future.result(WAIT) for name, future in pending.items()}
        overview = results['overview']
        self.assertEqual((overview['jobs']['total'], overview['stats']['total']), (1, 1))
        self.assertEqual([job['id'] for job in overview['jobs']['items']], [visible])
        self.assertEqual(results['stats']['total'], 1)
        self.assertEqual(overview['options']['location'], ['仍公开地区'])
        self.assertEqual(results['options']['location'], ['仍公开地区'])

    def test_entry_projection_and_permission_errors_release_admission(self):
        self.insert('异常恢复地区')
        read = self.store.queries
        for name in AGGREGATES:
            for failure in ('entry', 'projection', 'permission'):
                with self.subTest(query=name, failure=failure):
                    actor = self.actors['candidate']
                    if failure == 'entry':
                        replacement = patch.object(self.store.database, '_open',
                                                   side_effect=RuntimeError('unit of work entry failed'))
                    elif failure == 'projection':
                        replacement = patch.object(queries, 'effective_fields',
                                                   side_effect=RuntimeError('projection failed'))
                    else:
                        replacement = nullcontext()
                        actor = {'id': -1}
                    with replacement, self.assertRaises(PermissionError if failure == 'permission' else RuntimeError):
                        getattr(read, name)({}, actor)
                    # A bounded acquire makes a leaked permit fail without hanging this suite.
                    try:
                        self.assertTrue(read._aggregate_slots.acquire(timeout=WAIT), 'Admission permit leaked')
                    finally:
                        # Restore even a leaked permit so later subtests can report their failures.
                        read._aggregate_slots.release()
                    self.assertEqual(read.stats({}, self.actors['candidate'])['total'], 1)

    def test_waiting_aggregate_does_not_block_detail_fast_page_or_authorized_write(self):
        job_id = self.insert('即将下架地区')
        read = self.store.queries
        before = self.store.detail(job_id, self.actors['boss'])
        gate = ObservedGate(read._aggregate_slots, ('overview',))

        def independent_work():
            detail = self.store.detail(job_id, self.actors['candidate'])
            page = read.paginated({}, self.actors['candidate'])
            changed = self.store.set_lifecycle(self.actors['boss'], dict(id=job_id,
                expected_edit_token=before['edit_token'], action='deactivate', reason='并发写入回归'))
            return detail, page, changed

        with patch.object(read, '_aggregate_slots', gate):
            with gate.gate:
                pending = self.start('overview', lambda: read.overview({}, self.actors['candidate']))
                self.wait_for(gate.waiting['overview'])
                detail, page, changed = self.start('small-work', independent_work).result(WAIT)
                self.assertEqual(detail['id'], job_id)
                self.assertEqual((page['total'], page['items'][0]['id']), (1, job_id))
                self.assertTrue(changed['manual_inactive'])
                self.assertFalse(pending.done())
            result = pending.result(WAIT)
        self.assertEqual((result['jobs']['total'], result['stats']['total']), (0, 0))
        self.assertEqual(result['options']['location'], [])

    def test_one_store_does_not_hold_another_stores_aggregate(self):
        other = Store(self.directory / 'other.sqlite3')
        read = self.store.queries
        gate = ObservedGate(read._aggregate_slots, ('primary',))
        with patch.object(read, '_aggregate_slots', gate):
            with gate.gate:
                pending = self.start('primary', lambda: read.overview({}, self.actors['candidate']))
                self.wait_for(gate.waiting['primary'])
                result = self.start('other', lambda: other.queries.overview({}, None)).result(WAIT)
                self.assertEqual(result['stats']['total'], 0)
                self.assertFalse(pending.done())
            self.assertEqual(pending.result(WAIT)['stats']['total'], 0)


if __name__ == '__main__':
    unittest.main()
