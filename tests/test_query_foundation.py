"""Query pushdown and deferred reads preserve authorization and one snapshot."""
import csv
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import Store
from seed_demo_accounts import seed
from recruitment_insights.application import queries
from recruitment_insights.domain import requirements, reviews
from recruitment_insights.infrastructure.jobs import JobRepository
from recruitment_insights.infrastructure.reviews import ReviewRepository


class QueryFoundationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='recruitment_insights-query-foundation-')
        self.store = Store(Path(self.temporary.name) / 'queries.sqlite3')
        seed(self.store)
        with self.store.uow() as tx:
            self.actors = {name: tx.accounts.by_username(name) for name in
                           ('admin', 'boss_a', 'hr_a', 'boss_b', 'candidate_a')}
        self.admin = self.actors['admin']
        self.candidate = self.actors['candidate_a']
        self.org_a = self.actors['boss_a']['org_id']
        self.org_b = self.actors['boss_b']['org_id']
        reviews.cached_candidates.cache_clear()

    def tearDown(self):
        reviews.cached_candidates.cache_clear()
        self.temporary.cleanup()

    @staticmethod
    def record(name, **values):
        return dict(dict(job_id=str(name), title='Python 开发岗位 ' + str(name),
                         company='基础查询回归企业', location='北京',
                         description='本科及以上学历。要求 3–5年开发经验。',
                         education='', experience=''), **values)

    def insert(self, name, *, source='test:foundation', owner=None,
               demo=False, published=True, active=True, **values):
        raw = self.record(name, **values)
        self.store.import_records([raw], source, 'query foundation fixture',
                                  is_demo=demo, owner_org_id=owner)
        with self.store.uow(write=True) as tx:
            job_id = tx.jobs.by_source(source, str(name))['id']
            tx.jobs.flags(job_id, published, active)
        return job_id, raw

    def confirm(self, job_id, field='education'):
        item = self.store.detail(job_id, self.admin)['requirements'][field]
        body = dict(job_id=job_id, field=field, action='confirm',
                    source_hash=item['source_hash'], version=item['version'],
                    condition='硬性要求', alternatives=[])
        if field == 'education':
            body.update(value='本科', evidence='本科及以上学历')
        else:
            body.update(value='3–5年', evidence='3–5年开发经验', min_years=3, max_years=5)
        return self.store.review_field(self.admin, body)

    def test_role_scope_and_dataset_matrix_preserves_public_and_company_visibility(self):
        cases = [('public', None, False, True),
                 ('a_draft', self.org_a, False, True),
                 ('a_published', self.org_a, True, True),
                 ('b_draft', self.org_b, False, True),
                 ('b_published', self.org_b, True, True),
                 ('public_inactive', None, True, False),
                 ('a_inactive', self.org_a, True, False)]
        ids = {}
        for dataset in ('real', 'demo'):
            for name, owner, published, active in cases:
                ids[dataset, name], _ = self.insert(dataset + ':' + name,
                    source='test:matrix:' + dataset, owner=owner, demo=dataset == 'demo',
                    published=published, active=active)
        # This account dictionary remains stale on purpose: read access is live,
        # and revoking management permission must not hide the HR's own drafts.
        with self.store.connect() as db:
            db.execute('UPDATE users SET can_manage_jobs=0 WHERE id=?',
                       (self.actors['hr_a']['id'],))
        public = {'public', 'a_published', 'b_published'}
        all_active = public | {'a_draft', 'b_draft'}
        expectations = [
            (None, {'all': all_active, 'public': public, 'company': set()}),
            ('admin', {'all': all_active, 'public': public, 'company': set()}),
            ('boss_a', {'all': public | {'a_draft'}, 'public': public,
                        'company': {'a_draft', 'a_published'}}),
            ('hr_a', {'all': public | {'a_draft'}, 'public': public,
                      'company': {'a_draft', 'a_published'}}),
            ('boss_b', {'all': public | {'b_draft'}, 'public': public,
                        'company': {'b_draft', 'b_published'}}),
            ('candidate_a', {'all': public, 'public': public, 'company': set()}),
        ]
        for name, scopes in expectations:
            actor = self.actors[name] if name else None
            for scope, visible in scopes.items():
                for dataset in ('real', 'demo', 'all'):
                    with self.subTest(actor=name, scope=scope, dataset=dataset):
                        selected = ('real', 'demo') if dataset == 'all' else (dataset,)
                        expected = {ids[group, job] for group in selected for job in visible}
                        rows = self.store.jobs({'dataset': dataset, 'scope': scope,
                                                'org_id': self.org_a}, actor)
                        self.assertEqual([row['id'] for row in rows], sorted(expected, reverse=True))
        # The general query excludes inactive rows even for an owner, while the
        # existing enterprise maintenance view still needs them.
        company = self.store.company_jobs(self.actors['hr_a'])
        self.assertIn(ids['real', 'a_inactive'], {row['id'] for row in company})

    def test_metadata_filters_precede_payload_materialization_and_exclude_raw(self):
        source = "test:source' OR 1=1 --"
        wanted, _ = self.insert('wanted', source=source, owner=self.org_a)
        excluded = [
            self.insert('wrong-source', source='test:other', owner=self.org_a)[0],
            self.insert('wrong-dataset', source=source, owner=self.org_a, demo=True)[0],
            self.insert('wrong-company', source=source, owner=self.org_b)[0],
            self.insert('public-source', source=source)[0],
            self.insert('inactive', source=source, owner=self.org_a, active=False)[0],
        ]
        # A rejected row must not be decoded; the poison is confined to this
        # disposable fixture and does not represent accepted import input.
        with self.store.connect() as db:
            db.executemany('UPDATE jobs SET payload=?,raw=? WHERE id=?',
                           [('not json', 'not json', job_id) for job_id in excluded])
        original = JobRepository.active
        materialized = []

        def active(repository, *args, **kwargs):
            rows = original(repository, *args, **kwargs)
            materialized.extend(dict(row) for row in rows)
            return rows

        with patch.object(JobRepository, 'active', autospec=True, side_effect=active):
            jobs = self.store.jobs({'dataset': 'real', 'source': source, 'scope': 'company'},
                                   self.actors['boss_a'])
        self.assertEqual([job['id'] for job in jobs], [wanted])
        self.assertEqual([row['id'] for row in materialized], [wanted])
        self.assertNotIn('raw', materialized[0], 'The initial read must not carry raw payloads')
        self.assertNotIn('fingerprint', materialized[0])

    def test_content_filter_reads_raw_and_reviews_only_for_matches_and_decodes_payload_once(self):
        records = [self.record(index) for index in range(20)]
        records.append(self.record('target', title='Straße_100% Python 开发岗位',
                                   description='<p>本科及以上学历。要求 3–5年开发经验。</p>'))
        self.store.import_records(records, 'test:content', 'deferred reads')
        with self.store.uow() as tx:
            target = tx.jobs.by_source('test:content', 'target')
        raw_reads, review_reads, payload_reads = [], [], []
        read_raw, read_reviews = JobRepository.raw_inputs, ReviewRepository.fields
        decode = json.loads

        def raw_inputs(repository, ids):
            raw_reads.append(list(ids))
            return read_raw(repository, ids)

        def fields(repository, ids):
            review_reads.append(list(ids))
            return read_reviews(repository, ids)

        def loads(value, *args, **kwargs):
            if value == target['payload']:
                payload_reads.append(value)
            return decode(value, *args, **kwargs)

        with patch.object(JobRepository, 'raw_inputs', autospec=True, side_effect=raw_inputs), \
             patch.object(ReviewRepository, 'fields', autospec=True, side_effect=fields), \
             patch.object(queries.json, 'loads', side_effect=loads):
            jobs = self.store.jobs({'source': 'test:content', 'q': 'STRASSE_100%',
                                   'education': '待核验', 'experience': '待核验'}, self.candidate)
        self.assertEqual([job['id'] for job in jobs], [target['id']])
        self.assertEqual(raw_reads, [[target['id']]])
        self.assertEqual(review_reads, [[target['id']]])
        self.assertEqual(len(payload_reads), 1)
        # Empty content results must not produce SQL, even if a repository
        # method is called with an empty list.
        with self.store.uow() as tx:
            statements = []
            tx.jobs.db.set_trace_callback(statements.append)
            self.assertEqual(self.store.queries.jobs_in(tx, {'q': 'certainly absent'}, self.candidate), [])
        self.assertFalse(any('field_reviews' in statement.lower() for statement in statements))
        self.assertFalse(any('raw' in statement.lower() for statement in statements))

    def test_raw_inputs_batch_more_than_800_ids_and_empty_input_without_sql(self):
        records = [self.record(index) for index in range(801)]
        self.store.import_records(records, 'test:batch', 'raw batch boundary')
        with self.store.uow() as tx:
            ids = [row['id'] for row in tx.jobs.active(source='test:batch')]
            statements = []
            tx.jobs.db.set_trace_callback(statements.append)
            self.assertEqual(tx.jobs.raw_inputs([]), {})
            self.assertEqual(statements, [])
            # Lower the actual connection limit so an unbounded IN clause is
            # a functional failure regardless of SQLite's build-time maximum.
            tx.jobs.db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 800)
            raw = tx.jobs.raw_inputs(ids)
        self.assertEqual(set(raw), set(ids))
        self.assertEqual({json.loads(value)['job_id'] for value in raw.values()},
                         {str(index) for index in range(801)})
        reads = [statement for statement in statements if statement.lstrip().upper().startswith('SELECT')]
        self.assertEqual(len(reads), 2, '801 IDs require two bounded reads')

    def test_requirement_filters_precede_pagination_statistics_and_export(self):
        records = [self.record('match-' + str(index), education='本科', experience='3–5年')
                   for index in range(15)]
        records.append(self.record('reviewed-match'))
        # These newer records occupy the first unfiltered page, and each fails
        # a different requirement. A SQL LIMIT before projection loses matches.
        records.extend(self.record('education-miss-' + str(index), education='硕士', experience='3–5年')
                       for index in range(10))
        records.extend(self.record('experience-miss-' + str(index), education='本科', experience='应届')
                       for index in range(10))
        self.store.import_records(records, 'test:pages', 'requirement pagination')
        with self.store.uow() as tx:
            reviewed = tx.jobs.by_source('test:pages', 'reviewed-match')['id']
        self.confirm(reviewed)
        self.confirm(reviewed, 'experience')
        filters = {'source': 'test:pages', 'education': '本科', 'experience': '3–5年'}
        first = self.store.queries.paginated(filters, self.candidate)
        last = self.store.queries.paginated(dict(filters, page=2), self.candidate)
        self.assertEqual((first['total'], len(first['items']), last['total'], len(last['items'])),
                         (16, 15, 16, 1))
        self.assertEqual(first['items'][0]['id'], reviewed)
        self.assertEqual(last['items'][0]['job_id'], 'match-0')
        self.assertEqual(len({job['id'] for job in first['items'] + last['items']}), 16)
        stats = self.store.queries.stats(filters, self.candidate)
        self.assertEqual(stats['total'], 16)
        self.assertEqual(stats['education'], [{'name': '本科', 'count': 16}])
        self.assertEqual(stats['experience'], [{'name': '3–5年', 'count': 16}])
        self.assertEqual(stats['requirement_coverage']['education']['confirmed'], 1)
        exported = list(csv.DictReader(io.StringIO(
            self.store.queries.export(filters, self.candidate).decode('utf-8-sig'))))
        self.assertEqual({row['job_id'] for row in exported},
                         {'reviewed-match'} | {'match-' + str(index) for index in range(15)})

    def test_deferred_raw_read_uses_initial_snapshot_after_source_change_and_withdrawal(self):
        changed, before = self.insert('changed', source='test:snapshot', owner=self.org_a,
                                      education='本科', experience='3–5年')
        withdrawn, _ = self.insert('withdrawn', source='test:snapshot', owner=self.org_a,
                                    education='本科', experience='3–5年')
        read_raw = JobRepository.raw_inputs
        writes = []

        def update_between_reads(repository, ids):
            # The first SELECT established this request's read snapshot. WAL
            # permits a second connection to commit without ending that snapshot.
            writes.append(list(ids))
            updated = dict(before, education='硕士', description='硕士及以上学历。要求 3–5年开发经验。')
            self.store.import_records([updated], 'test:snapshot', 'concurrent source change',
                                      owner_org_id=self.org_a)
            with self.store.uow(write=True) as writer:
                writer.jobs.flags(withdrawn, False, True)
            return read_raw(repository, ids)

        filters = {'source': 'test:snapshot'}
        with patch.object(JobRepository, 'raw_inputs', autospec=True, side_effect=update_between_reads):
            original_snapshot = self.store.jobs(filters, self.candidate)
        self.assertEqual(len(writes), 1)
        self.assertEqual({job['id'] for job in original_snapshot}, {changed, withdrawn})
        self.assertTrue(all(job['education'] == '本科' and job['published']
                            for job in original_snapshot))
        current = self.store.jobs(filters, self.candidate)
        self.assertEqual([job['id'] for job in current], [changed])
        self.assertEqual(current[0]['education'], '硕士')
        self.assertEqual(current[0]['description'], '硕士及以上学历。要求 3–5年开发经验。')

    def test_deferred_review_read_uses_snapshot_and_next_request_sees_revocation(self):
        job_id, _ = self.insert('review-snapshot')
        confirmed = self.confirm(job_id)['requirements']['education']
        revoke = dict(job_id=job_id, field='education', action='revoke',
                      source_hash=confirmed['source_hash'], version=confirmed['version'])
        read_raw = JobRepository.raw_inputs

        def revoke_after_raw(repository, ids):
            result = read_raw(repository, ids)
            self.store.review_field(self.admin, revoke)
            return result

        with patch.object(JobRepository, 'raw_inputs', autospec=True, side_effect=revoke_after_raw):
            original_snapshot = self.store.jobs({'q': 'review-snapshot'}, self.candidate)
        self.assertEqual([(job['education'], job['education_status']) for job in original_snapshot],
                         [('本科', 'confirmed')])
        current = self.store.jobs({'q': 'review-snapshot'}, self.candidate)
        self.assertEqual([(job['education'], job['education_status']) for job in current],
                         [('待核验', 'candidate')])

    def test_rule_version_change_invalidates_warm_extraction_and_marks_review_stale(self):
        job_id, _ = self.insert('rule-change')
        original_hash = self.confirm(job_id)['requirements']['education']['source_hash']
        self.assertEqual(self.store.jobs(actor=self.candidate)[0]['education'], '本科')
        with patch.object(requirements, 'RULE_VERSION', requirements.RULE_VERSION + '-foundation'), \
             patch.object(reviews, 'candidates', wraps=reviews.candidates) as extraction:
            jobs = self.store.jobs(actor=self.candidate)
            self.assertEqual((jobs[0]['education'], jobs[0]['education_status']), ('待复核', 'stale'))
            self.assertEqual(extraction.call_count, 1, 'A new rule version must recompute a warm input')
            current = self.store.detail(job_id, self.admin)['requirements']['education']
            self.assertEqual(current['status'], 'stale')
            self.assertNotEqual(current['source_hash'], original_hash)
            self.assertEqual(self.store.queries.stats({}, self.candidate)['education'],
                             [{'name': '待复核', 'count': 1}])
            self.assertEqual(extraction.call_count, 1, 'The updated pure result is reusable within its version')


if __name__ == '__main__':
    unittest.main()
