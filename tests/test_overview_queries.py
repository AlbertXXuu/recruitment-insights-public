"""Overview, bounded projection and streaming statistics preserve live reads."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import Store, analyze, normalize
from seed_demo_accounts import PASSWORD, seed
from recruitment_insights.application import queries
from recruitment_insights.domain import requirements, reviews
from recruitment_insights.infrastructure.jobs import JobRepository
from recruitment_insights.infrastructure.reviews import ReviewRepository
from recruitment_insights.web.routes import Routes


class OverviewQueryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='recruitment_insights-overview-')
        self.store = Store(Path(self.temporary.name) / 'overview.sqlite3')
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
        return dict(dict(job_id=str(name), title='Python 岗位 ' + str(name), company='首屏回归企业',
                         location='北京', description='本科及以上学历。要求 3–5年开发经验。',
                         education='', experience=''), **values)

    def insert(self, name, *, source='test:overview', owner=None,
               demo=False, published=True, active=True, **values):
        raw = self.record(name, **values)
        self.store.import_records([raw], source, 'overview fixture', is_demo=demo, owner_org_id=owner)
        with self.store.uow(write=True) as tx:
            job_id = tx.jobs.by_source(source, str(name))['id']
            tx.jobs.flags(job_id, published, active)
        return job_id, raw

    def confirm(self, job_id, *, unknown=False):
        item = self.store.detail(job_id, self.admin)['requirements']['education']
        body = dict(job_id=job_id, field='education', action='confirm',
                    source_hash=item['source_hash'], version=item['version'], alternatives=[])
        body.update(value='未知' if unknown else '本科',
                    condition='未发现明确要求' if unknown else '硬性要求',
                    evidence='' if unknown else '本科及以上学历')
        return self.store.review_field(self.admin, body)

    def test_overview_uses_one_scan_and_options_keep_the_full_base_scope(self):
        selected, _ = self.insert('selected', source='test:selected', education='本科', experience='3–5年')
        self.insert('other', source='test:other', title='产品经理', location='上海',
                    education='硕士', experience='应届')
        self.insert('private', source='test:private', owner=self.org_b, published=False, location='私密地区')
        self.insert('demo', source='test:demo', demo=True, location='演示地区')
        original = JobRepository.iter_active
        scanned = []

        def stream(repository, **scope):
            for row in original(repository, **scope):
                scanned.append(row['id'])
                yield row

        filters = {'dataset': 'real', 'source': 'test:selected', 'q': 'selected',
                   'location': '北京', 'education': '本科', 'experience': '3–5年'}
        with patch.object(JobRepository, 'iter_active', autospec=True, side_effect=stream) as scan:
            result = self.store.queries.overview(filters, self.candidate)
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(len(scanned), 2)
        self.assertEqual(result['stats']['total'], 1)
        self.assertEqual((result['jobs']['total'], result['jobs']['page'], result['jobs']['page_size']), (1, 1, 15))
        self.assertEqual([job['id'] for job in result['jobs']['items']], [selected])
        self.assertEqual(result['options']['source'], ['test:other', 'test:selected'])
        self.assertEqual(result['options']['location'], ['上海', '北京'])
        self.assertEqual(result['options']['education'], ['本科', '硕士'])
        self.assertEqual(result['options'], self.store.queries.options({'dataset': 'real'}, self.candidate))

    def test_overview_without_options_keeps_filters_and_empty_result_shape(self):
        selected, _ = self.insert('selected', source='test:selected')
        self.insert('other', source='test:other')
        original = JobRepository.iter_active
        scanned = []

        def stream(repository, **scope):
            for row in original(repository, **scope):
                scanned.append(row['id'])
                yield row

        with patch.object(JobRepository, 'iter_active', autospec=True, side_effect=stream) as scan:
            result = self.store.queries.overview({'source': 'test:selected'}, self.candidate, include_options=False)
        self.assertNotIn('options', result)
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(scanned, [selected])
        self.assertEqual((result['stats']['total'], result['jobs']['total']), (1, 1))
        empty = self.store.queries.overview({'q': 'absent marker', 'page': 2}, self.candidate, include_options=False)
        self.assertNotIn('options', empty)
        self.assertEqual(empty['stats']['total'], 0)
        self.assertEqual(empty['jobs'], dict(items=[], total=0, page=2, page_size=15))

    def test_metadata_only_pagination_fetches_the_sql_page_and_projects_only_15(self):
        source = "test:page' OR 1=1 --"
        self.store.import_records([self.record(index) for index in range(42)], source, 'SQL page')
        with self.store.connect() as db:
            ids = [row['id'] for row in db.execute('SELECT id FROM jobs WHERE source=? ORDER BY id DESC', (source,))]
            expected = ids[15:30]
            # Records outside this page must not be decoded even to calculate
            # total; only metadata predicates are active for this request.
            db.executemany('UPDATE jobs SET payload=?,raw=? WHERE id=?',
                           [('not json', 'not json', job_id) for job_id in ids if job_id not in expected])
        original = JobRepository.active_page
        with patch.object(JobRepository, 'active_page', autospec=True, side_effect=original) as sql_page, \
             patch.object(JobRepository, 'iter_active', side_effect=AssertionError('metadata page scanned full rows')), \
             patch.object(queries, 'effective_fields', wraps=queries.effective_fields) as projection:
            result = self.store.queries.paginated({'source': source, 'page': 2}, self.candidate)
        self.assertEqual(sql_page.call_count, 1)
        self.assertEqual(projection.call_count, 15)
        self.assertEqual((result['total'], result['page_size']), (42, 15))
        self.assertEqual([job['id'] for job in result['items']], expected)

    def test_content_pagination_counts_all_matches_but_projects_only_the_page(self):
        records = [self.record('keep-' + str(index), description='kept marker Python 开发。', location='上海')
                   for index in range(40)]
        records.extend(self.record('miss-' + str(index), description='其他岗位。', location='北京')
                       for index in range(20))
        self.store.import_records(records, 'test:content-page', 'content page')
        filters = {'source': 'test:content-page', 'q': 'kept marker', 'location': '上海', 'skill': 'Python',
                   'category': normalize(records[0])['category'], 'page': 2}
        with patch.object(queries, 'effective_fields', wraps=queries.effective_fields) as projection:
            result = self.store.queries.paginated(filters, self.candidate)
        self.assertEqual((result['total'], len(result['items'])), (40, 15))
        self.assertEqual(projection.call_count, 15)
        self.assertEqual([job['job_id'] for job in result['items']],
                         ['keep-' + str(index) for index in range(24, 9, -1)])

    def test_requirement_filters_precede_page_and_overview_statistics(self):
        records = [self.record('match-' + str(index), education='本科', experience='3–5年')
                   for index in range(16)]
        records.extend(self.record('education-miss-' + str(index), education='硕士', experience='3–5年')
                       for index in range(10))
        records.extend(self.record('experience-miss-' + str(index), education='本科', experience='应届')
                       for index in range(10))
        self.store.import_records(records, 'test:requirements-page', 'requirements page')
        filters = {'source': 'test:requirements-page', 'education': '本科', 'experience': '3–5年', 'page': 2}
        page = self.store.queries.paginated(filters, self.candidate)
        result = self.store.queries.overview(filters, self.candidate)
        self.assertEqual(page, result['jobs'])
        self.assertEqual((page['total'], len(page['items'])), (16, 1))
        self.assertEqual(page['items'][0]['job_id'], 'match-0')
        self.assertEqual(result['stats']['total'], 16)
        self.assertEqual(result['stats']['education'], [{'name': '本科', 'count': 16}])
        self.assertEqual(result['stats']['experience'], [{'name': '3–5年', 'count': 16}])
        self.assertEqual(set(result['options']['education']), {'本科', '硕士'})
        self.assertEqual(set(result['options']['experience']), {'3–5年', '应届'})

    def test_overview_role_scope_dataset_matrix_excludes_private_and_inactive_options(self):
        cases = [('public', None, False, True), ('a_draft', self.org_a, False, True),
                 ('a_published', self.org_a, True, True), ('b_draft', self.org_b, False, True),
                 ('b_published', self.org_b, True, True), ('inactive', self.org_a, True, False)]
        ids = {}
        for dataset in ('real', 'demo'):
            for name, owner, published, active in cases:
                ids[dataset, name], _ = self.insert(dataset + ':' + name, owner=owner, demo=dataset == 'demo',
                    published=published, active=active, location=dataset + ':' + name)
        with self.store.connect() as db:
            db.execute('UPDATE users SET can_manage_jobs=0 WHERE id=?', (self.actors['hr_a']['id'],))
        public = {'public', 'a_published', 'b_published'}
        all_active = public | {'a_draft', 'b_draft'}
        cases = [
            (None, {'all': all_active, 'public': public, 'company': set()}),
            ('admin', {'all': all_active, 'public': public, 'company': set()}),
            ('boss_a', {'all': public | {'a_draft'}, 'public': public, 'company': {'a_draft', 'a_published'}}),
            ('hr_a', {'all': public | {'a_draft'}, 'public': public, 'company': {'a_draft', 'a_published'}}),
            ('boss_b', {'all': public | {'b_draft'}, 'public': public, 'company': {'b_draft', 'b_published'}}),
            ('candidate_a', {'all': public, 'public': public, 'company': set()}),
        ]
        for name, scopes in cases:
            for scope, visible in scopes.items():
                for dataset in ('real', 'demo', 'all'):
                    with self.subTest(actor=name, scope=scope, dataset=dataset):
                        groups = ('real', 'demo') if dataset == 'all' else (dataset,)
                        expected = {ids[group, job] for group in groups for job in visible}
                        actor = self.actors[name] if name else None
                        result = self.store.queries.overview({'dataset': dataset, 'scope': scope,
                                                             'org_id': self.org_a}, actor)
                        self.assertEqual(result['stats']['total'], len(expected))
                        self.assertEqual(result['jobs']['total'], len(expected))
                        self.assertEqual([job['id'] for job in result['jobs']['items']], sorted(expected, reverse=True))
                        self.assertEqual(set(result['options']['location']),
                                         {group + ':' + job for group in groups for job in visible})

    def test_overview_reloads_actor_and_publication_after_previous_success(self):
        private, _ = self.insert('private', owner=self.org_a, published=False, location='私密地区')
        public, _ = self.insert('published', owner=self.org_b, location='公开地区')
        actor = self.actors['hr_a']
        before = self.store.queries.overview({}, actor)
        self.assertEqual({job['id'] for job in before['jobs']['items']}, {private, public})
        with self.store.connect() as db:
            db.execute("UPDATE users SET role='candidate',org_id=NULL,can_manage_jobs=0 WHERE id=?", (actor['id'],))
        after = self.store.queries.overview({}, actor)
        self.assertEqual([job['id'] for job in after['jobs']['items']], [public])
        self.assertEqual(after['options']['location'], ['公开地区'])
        with self.store.uow(write=True) as tx:
            tx.jobs.flags(public, False, True)
        withdrawn = self.store.queries.overview({}, actor)
        self.assertEqual((withdrawn['stats']['total'], withdrawn['jobs']['total']), (0, 0))
        self.assertEqual(withdrawn['options']['location'], [])

    def test_sql_page_count_matches_domain_guard_after_role_downgrade_retains_org(self):
        published, _ = self.insert('published', owner=self.org_a)
        for index in range(16):
            self.insert('draft-' + str(index), owner=self.org_a, published=False)
        actor = self.actors['hr_a']
        # A role revocation must take effect even if trusted maintenance has not
        # also cleared organization metadata. The domain guard already handles it.
        with self.store.connect() as db:
            db.execute("UPDATE users SET role='candidate' WHERE id=?", (actor['id'],))
        filters = {'scope': 'company'}
        expected = self.store.jobs(filters, actor)
        self.assertEqual([job['id'] for job in expected], [published])
        page = self.store.queries.paginated(filters, actor)
        self.assertEqual(page['total'], 1)
        self.assertEqual([job['id'] for job in page['items']], [published])
        self.assertEqual(page, self.store.queries.overview(filters, actor)['jobs'])

    def test_overview_route_requires_login_honors_options_and_rejects_invalid_filters(self):
        published, _ = self.insert('public')
        routes = Routes(self.store)
        self.assertEqual(routes.get('/api/overview', '', '').status, 401)
        self.assertEqual(routes.get('/api/overview', '', 'invalid-session').status, 401)
        token, _ = self.store.accounts.login('candidate_a', PASSWORD)
        result = routes.get('/api/overview', 'include_options=0&dataset=real', token)
        self.assertEqual(result.status, 200)
        self.assertNotIn('options', result.data)
        self.assertEqual([job['id'] for job in result.data['jobs']['items']], [published])
        self.assertIn('options', routes.get('/api/overview', '', token).data)
        self.assertIn('options', routes.get('/api/overview', 'include_options=1', token).data)
        with patch.object(self.store.queries, 'overview', wraps=self.store.queries.overview) as query:
            for parameters in ('include_options=2', 'include_options=true', 'include_options=-1',
                               'dataset=all', 'dataset=invalid', 'scope=unknown'):
                with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                    routes.get('/api/overview', parameters, token)
            query.assert_not_called()

    def test_overview_route_rechecks_actor_after_session_lookup_in_the_query_transaction(self):
        private, _ = self.insert('private', owner=self.org_a, published=False, location='私密地区')
        published, _ = self.insert('public', location='公开地区')
        routes = Routes(self.store)
        token, actor = self.store.accounts.login('hr_a', PASSWORD)
        before = routes.get('/api/overview', '', token)
        self.assertEqual({job['id'] for job in before.data['jobs']['items']}, {private, published})
        read_user = routes.accounts.user

        def revoke_after_lookup(session_token):
            stale = read_user(session_token)
            with self.store.connect() as db:
                db.execute("UPDATE users SET role='candidate',org_id=NULL,can_manage_jobs=0 WHERE id=?", (actor['id'],))
            return stale

        with patch.object(routes.accounts, 'user', side_effect=revoke_after_lookup):
            result = routes.get('/api/overview', '', token)
        self.assertEqual(result.status, 200)
        self.assertEqual((result.data['stats']['total'], result.data['jobs']['total']), (1, 1))
        self.assertEqual([job['id'] for job in result.data['jobs']['items']], [published])
        self.assertEqual(result.data['options']['location'], ['公开地区'])

    def test_raw_only_change_preserves_payload_but_invalidates_latest_review(self):
        job_id, raw = self.insert('raw-only', education='本科')
        self.confirm(job_id)
        with self.store.uow() as tx:
            before = tx.jobs.get(job_id)
        confirmed = self.store.queries.overview({}, self.candidate)
        self.assertEqual(confirmed['jobs']['items'][0]['education_status'], 'confirmed')
        self.store.import_records([dict(raw, education='本科 ')], 'test:overview', 'raw-only source change')
        with self.store.uow() as tx:
            after = tx.jobs.get(job_id)
        self.assertEqual((before['payload'], before['fingerprint']), (after['payload'], after['fingerprint']))
        stale = self.store.queries.overview({}, self.candidate)
        self.assertEqual(stale['stats']['education'], [{'name': '待复核', 'count': 1}])
        self.assertEqual(stale['options']['education'], ['待复核'])
        item = self.store.detail(job_id, self.admin)['requirements']['education']
        self.store.review_field(self.admin, dict(job_id=job_id, field='education', action='revoke',
            source_hash=item['source_hash'], version=item['version']))
        revoked = self.store.queries.overview({}, self.candidate)
        self.assertEqual(revoked['jobs']['items'][0]['education_status'], 'source')
        self.assertEqual(revoked['stats']['education'], [{'name': '本科', 'count': 1}])

    def test_unknown_unrestricted_and_human_unknown_remain_distinct(self):
        self.insert('unresolved', description='欢迎加入团队，福利待遇良好。')
        unrestricted, _ = self.insert('unrestricted', education='不限', experience='无要求')
        unknown, _ = self.insert('human-unknown', description='欢迎加入团队，福利待遇良好。')
        self.confirm(unknown, unknown=True)
        result = self.store.queries.overview({}, self.candidate)
        self.assertEqual({row['name']: row['count'] for row in result['stats']['education']},
                         {'待核验': 1, '不限': 1, '未发现明确要求': 1})
        self.assertEqual(set(result['options']['education']), {'待核验', '不限', '未发现明确要求'})
        only_unrestricted = self.store.queries.overview({'education': '不限'}, self.candidate)
        self.assertEqual([job['id'] for job in only_unrestricted['jobs']['items']], [unrestricted])
        only_unknown = self.store.queries.overview({'education': '未发现明确要求'}, self.candidate)
        self.assertEqual([job['id'] for job in only_unknown['jobs']['items']], [unknown])

    def test_overview_keeps_one_snapshot_across_review_batches_and_concurrent_writes(self):
        changed, raw = self.insert('changed', education='本科', location='原地区')
        reviewed, _ = self.insert('reviewed')
        review = self.confirm(reviewed)['requirements']['education']
        withdrawn, _ = self.insert('withdrawn', owner=self.org_a, location='将撤回地区')
        self.store.import_records([self.record('filler-' + str(index), education='本科') for index in range(128)],
                                  'test:fillers', 'multiple projection batches')
        read_fields = ReviewRepository.fields
        primary_connection = None
        batches = []
        mutated = False

        def fields(repository, ids):
            nonlocal primary_connection, mutated
            if primary_connection is None:
                primary_connection = repository.connection
            if repository.connection is primary_connection:
                batches.append(list(ids))
            result = read_fields(repository, ids)
            if not mutated:
                mutated = True
                self.store.import_records([dict(raw, education='硕士', location='新地区')],
                                          'test:overview', 'concurrent source change')
                self.store.review_field(self.admin, dict(job_id=reviewed, field='education', action='revoke',
                    source_hash=review['source_hash'], version=review['version']))
                with self.store.uow(write=True) as writer:
                    writer.jobs.flags(withdrawn, False, True)
            return result

        with patch.object(ReviewRepository, 'fields', autospec=True, side_effect=fields):
            snapshot = self.store.queries.overview({'page': 9}, self.candidate)
        self.assertGreaterEqual(len(batches), 2)
        self.assertLessEqual(max(map(len, batches)), 128)
        self.assertEqual(sum(map(len, batches)), 131)
        self.assertEqual((snapshot['stats']['total'], snapshot['jobs']['total']), (131, 131))
        last_page = {job['id']: job for job in snapshot['jobs']['items']}
        self.assertEqual(last_page[changed]['education'], '本科')
        self.assertEqual(last_page[reviewed]['education_status'], 'confirmed')
        self.assertIn(withdrawn, last_page)
        self.assertIn('原地区', snapshot['options']['location'])
        self.assertIn('将撤回地区', snapshot['options']['location'])
        self.assertNotIn('新地区', snapshot['options']['location'])
        current = self.store.queries.overview({'page': 9}, self.candidate)
        self.assertEqual((current['stats']['total'], current['jobs']['total']), (130, 130))
        last_page = {job['id']: job for job in current['jobs']['items']}
        self.assertEqual(last_page[changed]['education'], '硕士')
        self.assertEqual(last_page[reviewed]['education_status'], 'candidate')
        self.assertNotIn(withdrawn, last_page)
        self.assertIn('新地区', current['options']['location'])
        self.assertNotIn('将撤回地区', current['options']['location'])


class ProposalCacheKeyTests(unittest.TestCase):
    def setUp(self):
        reviews.cached_candidates.cache_clear()

    def tearDown(self):
        reviews.cached_candidates.cache_clear()

    def test_separate_description_key_preserves_legacy_candidates_and_hashes_for_all_value_shapes(self):
        missing = object()
        descriptions = (missing, None, '', '本科及以上学历。要求 3–5年开发经验。',
                        '带有引号"、反斜线\\和换行\n的原文。', 0, False, 17,
                        [], ['本科', '3年经验'], {}, {'requirement': '本科', 'years': 3})
        bundles = []
        for description in descriptions:
            raw = {'education': '本科', 'experience': '3–5年', 'unrelated': 'not part of the key'}
            if description is not missing:
                raw['description'] = description
            with self.subTest(description='missing' if description is missing else description):
                # Reconstruct only the old canonical serialization, then call
                # the actual domain rules; do not duplicate extraction logic.
                legacy = json.loads(json.dumps({key: raw.get(key) for key in
                                                ('education', 'experience', 'description')},
                                               ensure_ascii=False, sort_keys=True))
                expected = (requirements.candidates(legacy),
                            {field: requirements.field_fingerprint(legacy, field)
                             for field in ('education', 'experience')})
                actual = reviews._proposal(raw)
                self.assertEqual(actual, expected)
                self.assertEqual(reviews.propose(raw), expected[0])
                self.assertEqual(set(reviews.propose(raw)), {'education', 'experience'})
                bundles.append(actual)
        self.assertEqual(bundles[0], bundles[1], 'Missing and null retain their prior get()-based equivalence')
        self.assertNotEqual(bundles[1][1], bundles[2][1], 'Null and empty string must not share source hashes')
        self.assertNotEqual(bundles[5][1], bundles[6][1], 'Non-string JSON number and boolean remain distinct')
        self.assertEqual(reviews.cached_candidates.cache_info().maxsize, 8192)

    def test_separate_description_key_invalidates_for_raw_fields_description_and_rule_version(self):
        raw = {'education': '本科', 'experience': '3–5年',
               'description': '本科及以上学历。要求 3–5年开发经验。'}
        with patch.object(reviews, 'candidates', wraps=reviews.candidates) as extraction:
            original = reviews._proposal(raw)
            self.assertEqual(reviews._proposal(dict(raw, salary_text='unrelated change')), original)
            self.assertEqual(extraction.call_count, 1)
            education = reviews._proposal(dict(raw, education='本科 '))
            self.assertEqual(education[0], original[0])
            self.assertNotEqual(education[1]['education'], original[1]['education'])
            self.assertEqual(education[1]['experience'], original[1]['experience'])
            self.assertEqual(extraction.call_count, 2)
            experience = reviews._proposal(dict(raw, experience='3–5年 '))
            self.assertEqual(experience[0], original[0])
            self.assertEqual(experience[1]['education'], original[1]['education'])
            self.assertNotEqual(experience[1]['experience'], original[1]['experience'])
            self.assertEqual(extraction.call_count, 3)
            changed_description = reviews._proposal(dict(raw, description=raw['description'] + ' 已更新。'))
            for field in ('education', 'experience'):
                self.assertNotEqual(changed_description[1][field], original[1][field])
            self.assertEqual(extraction.call_count, 4)
            with patch.object(requirements, 'RULE_VERSION', requirements.RULE_VERSION + '-description-key'):
                versioned = reviews._proposal(raw)
                for field in ('education', 'experience'):
                    self.assertNotEqual(versioned[1][field], original[1][field])
                self.assertEqual(reviews._proposal(raw), versioned)
                self.assertEqual(extraction.call_count, 5)
            self.assertEqual(reviews._proposal(raw), original)
            self.assertEqual(extraction.call_count, 5)

    def test_separate_description_key_warm_hits_do_not_json_serialize_the_full_description(self):
        description = '本科及以上学历。要求 3–5年开发经验。\n' + '原文包含"引号"与反斜线\\。' * 3000
        raw = {'education': '', 'experience': '', 'description': description}
        expected = reviews._proposal(raw)
        encode = reviews.json.dumps
        serialized = []

        def track(value, *args, **kwargs):
            serialized.append(value)
            return encode(value, *args, **kwargs)

        with patch.object(reviews.json, 'dumps', side_effect=track), \
             patch.object(reviews, 'candidates', side_effect=AssertionError('warm hit reran extraction')), \
             patch.object(reviews, 'field_fingerprint', side_effect=AssertionError('warm hit recalculated hashes')):
            for _ in range(3):
                self.assertEqual(reviews._proposal(raw), expected)
                self.assertEqual(reviews.propose(raw), expected[0])
        self.assertEqual(serialized, [{'education': '', 'experience': ''}] * 6)


class StreamingAnalysisTests(unittest.TestCase):
    def test_analyze_consumes_one_pass_and_preserves_counts_salary_coverage_and_empty_values(self):
        def job(name, **values):
            raw = dict(job_id=name, title='Python 开发', company='企业 A', location='北京',
                       description='Python 开发', education='本科', experience='3–5年', salary_period='month')
            raw.update(values)
            return dict(normalize(raw), source=values.get('source', 'a'),
                        is_demo=values.get('is_demo', False), last_seen=values.get('last_seen', '2026-10-01'))

        rows = [job('a', locations=['北京', '上海'], salary_min=10000, salary_max=20000, currency='CNY'),
                job('b', source='b', location='上海', salary_min=20000, salary_max=40000, currency='CNY',
                    is_demo=True, last_seen='2026-10-02'),
                job('c', company='企业 B', currency='USD', last_seen='2026-10-03')]
        rows[0].update(education_status='source', experience_status='source')
        rows[1].update(education='待复核', education_status='stale', experience='待核验', experience_status='candidate')
        rows[2].update(education='未发现明确要求', education_status='confirmed', experience='不限', experience_status='source')

        class OnePass:
            used = False

            def __iter__(self):
                if self.used:
                    raise AssertionError('Statistics attempted a second pass over the input')
                self.used = True
                yield from rows

        result = analyze(OnePass())
        self.assertEqual(result, analyze(rows))
        self.assertEqual((result['total'], result['companies'], result['demos'], result['last_seen']),
                         (3, 2, 1, '2026-10-03'))
        self.assertEqual(result['sources'], [{'name': 'a', 'count': 2}, {'name': 'b', 'count': 1}])
        self.assertEqual({row['name']: row['count'] for row in result['locations']}, {'北京': 2, '上海': 2})
        self.assertEqual((result['education_missing'], result['experience_missing'], result['invalid_salary']), (2, 1, 1))
        self.assertEqual(result['requirement_coverage']['education'],
                         dict(source=1, confirmed=1, pending=0, stale=1, unknown=2))
        cny, usd = result['salaries']
        self.assertEqual((cny['valid'], cny['median'], cny['mean']), (2, 22500, 22500))
        self.assertEqual([entry['count'] for entry in cny['bins']], [0, 1, 0, 1])
        self.assertEqual((usd['valid'], usd['median'], usd['mean']), (0, None, None))
        self.assertEqual(analyze(iter(())), analyze([]))


if __name__ == '__main__':
    unittest.main()
