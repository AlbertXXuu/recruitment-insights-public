"""Job withdrawal regressions through HTTP; all data and sessions are temporary."""
import csv
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from accounts import Accounts
from app import make_server
from core import Store
from seed_demo_accounts import PASSWORD, seed


class JobLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='recruitment_insights-lifecycle-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'lifecycle.sqlite3'
        self.store = Store(self.path)
        seed(self.store)
        self.accounts = self.store.accounts
        self.cookies, self.actors = {}, {}
        for name in ('admin', 'boss_a', 'hr_a', 'boss_b', 'hr_b', 'candidate_a', 'candidate_b'):
            token, actor = self.accounts.login(name, PASSWORD)
            self.cookies[name] = 'recruitment_insights_session='+token
            self.actors[name] = actor
        self.server = make_server(self.store, 0)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())

    def request(self, path, user='admin', body=None, status=200):
        headers = {'Cookie': self.cookies[user]} if user else {}
        payload = None
        if body is not None:
            headers.update({'Content-Type': 'application/json', 'X-Local-Request': '1'})
            payload = json.dumps(body, ensure_ascii=False).encode('utf-8')
        try:
            response = urlopen(Request(self.url+quote(path, safe='/?=&'), data=payload, headers=headers), timeout=10)
        except HTTPError as exc:
            response = exc
        with response:
            content = response.read().decode('utf-8-sig')
            self.assertEqual(response.status, status, (path, content))
            return json.loads(content) if response.headers.get_content_type() == 'application/json' else content

    @staticmethod
    def raw(job_id='one', **changes):
        return dict(dict(job_id=job_id, title='生命周期 Python 工程师 '+job_id,
                         company='公开来源样本', location='杭州',
                         description='负责 Python 接口开发与测试，要求本科及以上学历。',
                         education='本科', experience='1–3年', salary_min='10000',
                         salary_max='20000', currency='CNY', salary_period='month',
                         original_note='原始字段逐字保留。'), **changes)

    def public_job(self, job_id='one', source='test:lifecycle', demo=False):
        self.store.import_records([self.raw(job_id)], source, 'temporary lifecycle fixture', is_demo=demo)
        with self.store.connect() as db:
            return db.execute('SELECT id FROM jobs WHERE source=? AND external_id=?', (source, job_id)).fetchone()[0]

    def enterprise_job(self, user='boss_a', **flags):
        body = dict(record=dict(
            title='企业生命周期岗位', location='杭州', description='Python 后端接口开发与测试。'),
            is_demo=False, published=True)
        body.update(flags)
        saved = self.request('/api/company/jobs', user, body)
        return saved['id']

    def detail(self, job_id, user='admin'):
        return self.request('/api/jobs/'+str(job_id), user)

    def lifecycle(self, job_id, action='deactivate', user='admin', reason='岗位已关闭，请保留历史记录。', token=None):
        if token is None:
            token = self.detail(job_id, user)['edit_token']
        return self.request('/api/jobs/lifecycle', user, dict(id=job_id, action=action, reason=reason,
                                                           expected_edit_token=token))

    def rows(self, table):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid')]

    def business_rows(self):
        return {table: self.rows(table) for table in ('jobs', 'revisions', 'runs', 'favorites', 'field_reviews',
                                                     'duplicate_decisions', 'audit_events')}

    def assert_state(self, job_id, source, manual, active, user='admin'):
        item = self.detail(job_id, user)
        for key in ('source_active', 'manual_inactive', 'active'):
            self.assertIsInstance(item[key], bool)
        self.assertEqual((bool(item['source_active']), bool(item['manual_inactive']), bool(item['active'])),
                         (source, manual, active))
        self.assertRegex(item['edit_token'], r'^[0-9a-f]{64}$')
        return item

    def test_admin_withdrawal_and_restore_keep_public_views_and_favorite_consistent(self):
        job_id = self.public_job()
        self.request('/api/favorites', 'candidate_a', dict(job_id=job_id, note='恢复后仍需显示此收藏备注。'))
        original = self.detail(job_id)
        history = self.rows('revisions')

        def visible(expected):
            jobs = self.request('/api/jobs?dataset=real', 'candidate_a')
            stats = self.request('/api/stats?dataset=real', 'candidate_a')
            export = list(csv.DictReader(io.StringIO(self.request('/api/export?dataset=real', 'candidate_a'))))
            favorites = self.request('/api/favorites', 'candidate_a')
            self.assertEqual((jobs['total'], stats['total'], len(export), len(favorites)), (expected,)*4)
            if expected:
                self.assertEqual(jobs['items'][0]['id'], job_id)
                self.assertEqual(favorites[0]['note'], '恢复后仍需显示此收藏备注。')

        visible(1)
        self.lifecycle(job_id)
        withdrawn = self.assert_state(job_id, True, True, False)
        self.assertEqual(withdrawn['lifecycle_reason'], '岗位已关闭，请保留历史记录。')
        self.assertTrue(withdrawn['lifecycle_updated_at'])
        self.assertNotEqual(withdrawn['edit_token'], original['edit_token'])
        visible(0)
        self.request('/api/jobs/'+str(job_id), 'candidate_a', status=404)
        self.assertEqual(len(self.rows('jobs')), 1)
        self.assertEqual(self.rows('revisions'), history)
        self.assertEqual(self.rows('favorites')[0]['note'], '恢复后仍需显示此收藏备注。')
        self.lifecycle(job_id, 'restore')
        restored = self.assert_state(job_id, True, False, True)
        self.assertEqual(restored['raw'], original['raw'])
        self.assertEqual(restored['payload'], original['payload'])
        self.assertEqual(self.rows('revisions'), history)
        visible(1)
        events = self.rows('audit_events')
        self.assertEqual([event['action'] for event in events[-2:]], ['deactivate_job', 'restore_job'])
        self.assertTrue(all(event['target'] == str(job_id) for event in events[-2:]))
        self.assertIn('岗位已关闭', json.dumps(events[-2], ensure_ascii=False))

    def test_admin_management_lists_all_and_inactive_with_search_dataset_and_pagination(self):
        records = [self.raw(str(i)) for i in range(32)]
        self.store.import_records(records, 'test:lifecycle', 'pagination fixture')
        ids = [row['id'] for row in self.rows('jobs')]
        self.lifecycle(ids[0])
        self.lifecycle(ids[1])
        demo_id = self.public_job('demo-only', demo=True)
        enterprise_id = self.enterprise_job()
        pages = [self.request('/api/admin/jobs?dataset=real&status=all&page='+str(page)) for page in (1, 2, 3)]
        self.assertEqual([result['total'] for result in pages], [32]*3)
        self.assertEqual([result['page'] for result in pages], [1, 2, 3])
        self.assertEqual([len(result['items']) for result in pages], [15, 15, 2])
        returned = [item for page in pages for item in page['items']]
        self.assertEqual({item['id'] for item in returned}, set(ids))
        self.assertEqual(len({item['id'] for item in returned}), 32)
        owners = {row['id']: row['owner_org_id'] for row in self.rows('jobs')}
        for item in returned:
            self.assertIsNone(owners[item['id']])
            for key in ('source_active', 'manual_inactive', 'active'):
                self.assertIsInstance(item[key], bool)
            self.assertEqual(bool(item['active']), bool(item['source_active']) and not bool(item['manual_inactive']))
            self.assertRegex(item['edit_token'], r'^[0-9a-f]{64}$')
        self.assertNotIn(enterprise_id, {item['id'] for item in returned})
        inactive = self.request('/api/admin/jobs?dataset=real&status=inactive')
        self.assertEqual((inactive['total'], {item['id'] for item in inactive['items']}), (2, set(ids[:2])))
        self.assertEqual(self.request('/api/admin/jobs?dataset=real&status=active')['total'], 30)
        self.assertEqual([item['id'] for item in self.request('/api/admin/jobs?dataset=demo&status=all')['items']], [demo_id])
        searched = self.request('/api/admin/jobs?dataset=real&status=all&q=工程师 31')
        self.assertEqual([item['id'] for item in searched['items']], [ids[31]])

    def test_management_list_requires_admin_and_valid_query(self):
        self.public_job()
        for user in ('candidate_a', 'candidate_b', 'boss_a', 'boss_b', 'hr_a', 'hr_b'):
            with self.subTest(user=user):
                self.request('/api/admin/jobs?status=all', user, status=403)
        self.request('/api/admin/jobs?status=all', None, status=401)
        for query in ('status=deleted', 'dataset=all', 'page=not-a-page'):
            with self.subTest(query=query):
                self.request('/api/admin/jobs?'+query, status=400)

    def test_source_reimport_preserves_manual_withdrawal_without_phantom_revisions(self):
        job_id = self.public_job()
        self.lifecycle(job_id)
        old = self.detail(job_id)
        history = self.rows('revisions')
        for _ in range(2):
            result = self.store.import_records([self.raw()], old['source'], 'same content from source')
            self.assertEqual((result['unchanged'], result['updated']), (1, 0))
            current = self.assert_state(job_id, True, True, False)
            self.assertEqual(current['lifecycle_reason'], old['lifecycle_reason'])
            self.assertEqual(current['lifecycle_updated_at'], old['lifecycle_updated_at'])
            self.assertEqual(self.rows('revisions'), history)
        changed = self.raw(title='来源更新后的岗位标题')
        result = self.store.import_records([changed], old['source'], 'source content update')
        self.assertEqual(result['updated'], 1)
        self.assert_state(job_id, True, True, False)
        self.assertEqual(len(self.rows('revisions')), len(history)+1)
        self.lifecycle(job_id, 'restore')
        self.assertEqual(self.detail(job_id, 'candidate_a')['payload']['title'], changed['title'])

    def test_source_disappearance_is_independent_of_manual_restore(self):
        self.store.import_records([self.raw(), self.raw('other')], 'test:lifecycle', 'initial snapshot', authoritative=True)
        job_id = self.rows('jobs')[0]['id']
        self.lifecycle(job_id)
        result = self.store.import_records([self.raw('other')], 'test:lifecycle', 'source disappearance', authoritative=True)
        self.assertEqual(result['deactivated'], 1)
        self.assert_state(job_id, False, True, False)
        self.lifecycle(job_id, 'restore')
        self.assert_state(job_id, False, False, False)
        self.request('/api/jobs/'+str(job_id), 'candidate_a', status=404)
        inactive = self.request('/api/admin/jobs?dataset=real&status=inactive')
        self.assertEqual([item['id'] for item in inactive['items']], [job_id])
        self.assertFalse(inactive['items'][0]['source_active'])
        self.assertFalse(inactive['items'][0]['manual_inactive'])
        self.store.import_records([self.raw(), self.raw('other')], 'test:lifecycle', 'source returns', authoritative=True)
        self.assert_state(job_id, True, False, True)
        self.detail(job_id, 'candidate_a')

    def test_public_sync_keeps_manual_decision_without_network_access(self):
        payload = {'jobs': [dict(id=101, title='Software Engineer', location={'name': '杭州'},
                                 content='Python 开发，本科及以上学历。', absolute_url='https://example.org/101'),
                            dict(id=102, title='Software Engineer', location={'name': '杭州'}, content='Python 接口测试。')]}
        self.store.sync(payload)
        job_id = next(row['id'] for row in self.rows('jobs') if row['external_id'] == '101')
        self.lifecycle(job_id)
        history = self.rows('revisions')
        with patch.object(self.store.importer.source, 'fetch', return_value=payload) as fetch:
            self.request('/api/sync', body={})
        fetch.assert_called_once_with()
        self.assert_state(job_id, True, True, False)
        self.assertEqual(self.rows('revisions'), history)
        self.lifecycle(job_id, 'restore')
        self.detail(job_id, 'candidate_a')

    def test_existing_company_checkbox_withdrawal_survives_reimport(self):
        job_id = self.enterprise_job(active=False)
        old = self.assert_state(job_id, True, True, False, 'boss_a')
        history = self.rows('revisions')
        result = self.store.import_records([old['raw']], old['source'], 'enterprise repeated import',
                                          owner_org_id=old['owner_org_id'])
        self.assertEqual((result['unchanged'], result['updated']), (1, 0))
        self.assert_state(job_id, True, True, False, 'boss_a')
        self.assertEqual(self.rows('revisions'), history)
        self.request('/api/jobs/'+str(job_id), 'candidate_a', status=404)
        self.lifecycle(job_id, 'restore', 'hr_a')
        self.detail(job_id, 'candidate_a')

    def test_boss_and_authorized_hr_can_change_only_their_own_company_jobs(self):
        job_id = self.enterprise_job()
        self.lifecycle(job_id, user='boss_a')
        self.assert_state(job_id, True, True, False, 'hr_a')
        self.lifecycle(job_id, 'restore', 'hr_a')
        self.assert_state(job_id, True, False, True, 'boss_a')
        self.accounts.grant(self.actors['boss_a'], self.actors['hr_a']['id'], False)
        body = dict(id=job_id, action='deactivate', reason='越权操作',
                    expected_edit_token=self.detail(job_id, 'boss_a')['edit_token'])
        before = self.business_rows()
        for user in ('candidate_a', 'candidate_b', 'hr_a', 'boss_b', 'hr_b', 'admin'):
            with self.subTest(user=user):
                self.request('/api/jobs/lifecycle', user, body, status=403)
        self.request('/api/jobs/lifecycle', None, body, status=401)
        self.assertEqual(self.business_rows(), before)

    def test_restore_keeps_company_draft_unpublished_until_explicit_publish(self):
        job_id = self.enterprise_job(published=False)
        self.lifecycle(job_id, user='boss_a')
        self.lifecycle(job_id, 'restore', 'hr_a')
        draft = self.assert_state(job_id, True, False, True, 'boss_a')
        self.assertFalse(draft['published'])
        self.request('/api/jobs/'+str(job_id), 'candidate_a', status=404)
        self.assertEqual(self.request('/api/jobs?dataset=real', 'candidate_a')['total'], 0)
        self.request('/api/company/jobs', 'boss_a', dict(id=job_id, expected_edit_token=draft['edit_token'], published=True))
        self.detail(job_id, 'candidate_a')

    def test_company_manager_cannot_change_public_job_even_with_real_edit_token(self):
        job_id = self.public_job()
        body = dict(id=job_id, action='deactivate', reason='越权操作', expected_edit_token=self.detail(job_id)['edit_token'])
        before = self.business_rows()
        for user in ('boss_a', 'hr_a', 'boss_b', 'hr_b', 'candidate_a'):
            with self.subTest(user=user):
                self.request('/api/jobs/lifecycle', user, body, status=403)
        self.assertEqual(self.business_rows(), before)
        for user in ('candidate_a', 'boss_a'):
            public = self.detail(job_id, user)
            self.assertFalse({'edit_token', 'source_active', 'manual_inactive', 'lifecycle_reason',
                              'lifecycle_updated_at'} & public.keys())

    def test_revoked_permissions_and_changed_roles_reject_existing_sessions_and_stale_actors(self):
        job_id = self.enterprise_job()
        body = dict(id=job_id, action='deactivate', reason='撤权旧会话',
                    expected_edit_token=self.detail(job_id, 'hr_a')['edit_token'])
        self.accounts.grant(self.actors['boss_a'], self.actors['hr_a']['id'], False)
        before = self.business_rows()
        self.request('/api/jobs/lifecycle', 'hr_a', body, status=403)
        with self.assertRaises(PermissionError):
            self.store.set_lifecycle(self.actors['hr_a'], body)
        with self.store.connect() as db:
            db.execute("UPDATE users SET org_id=? WHERE id=?", (self.actors['boss_b']['org_id'], self.actors['boss_a']['id']))
        self.request('/api/jobs/lifecycle', 'boss_a', body, status=403)
        with self.assertRaises(PermissionError):
            self.store.set_lifecycle(self.actors['boss_a'], body)
        self.assertEqual(self.business_rows(), before)
        public_id = self.public_job()
        admin_body = dict(id=public_id, action='deactivate', reason='管理员角色已撤销',
                          expected_edit_token=self.detail(public_id)['edit_token'])
        with self.store.connect() as db:
            db.execute("UPDATE users SET role='candidate' WHERE id=?", (self.actors['admin']['id'],))
        self.request('/api/admin/jobs?status=all', 'admin', status=403)
        self.request('/api/jobs/lifecycle', 'admin', admin_body, status=403)
        with self.assertRaises(PermissionError):
            self.store.set_lifecycle(self.actors['admin'], admin_body)

    def test_reason_action_id_and_token_validation_make_no_partial_changes(self):
        job_id = self.public_job()
        valid = dict(id=job_id, action='deactivate', reason='合法理由', expected_edit_token=self.detail(job_id)['edit_token'])
        before = self.business_rows()
        invalid = [dict(valid, reason=value) for value in ('', '   ', None, 123, '理'*501)]
        invalid += [dict(valid, action=value) for value in ('delete', '', None, True)]
        invalid += [dict(valid, id=value) for value in (True, 1.5, 'bad')]
        for field in ('id', 'action', 'reason'):
            body = dict(valid)
            body.pop(field)
            invalid.append(body)
        for body in invalid:
            with self.subTest(body=body):
                self.request('/api/jobs/lifecycle', body=body, status=400)
        for value in (None, '', 123, 'stale'):
            body = dict(valid)
            if value is None:
                body.pop('expected_edit_token')
            else:
                body['expected_edit_token'] = value
            with self.subTest(token=value):
                self.request('/api/jobs/lifecycle', body=body, status=409)
        self.assertEqual(self.business_rows(), before)
        self.request('/api/jobs/lifecycle', body=dict(valid, reason='理'*500))
        self.assertEqual(self.detail(job_id)['lifecycle_reason'], '理'*500)
        self.lifecycle(job_id, 'restore', reason='')
        self.lifecycle(job_id, reason='理')
        self.assertEqual(self.detail(job_id)['lifecycle_reason'], '理')

    def test_old_token_cannot_undo_lifecycle_or_source_content_changes(self):
        job_id = self.public_job()
        old = self.detail(job_id)
        self.lifecycle(job_id)
        before = self.business_rows()
        stale = dict(id=job_id, action='restore', reason='', expected_edit_token=old['edit_token'])
        self.request('/api/jobs/lifecycle', body=stale, status=409)
        self.assertEqual(self.business_rows(), before)
        refreshed = self.detail(job_id)
        self.store.import_records([self.raw(title='来源已更正的岗位名称')], refreshed['source'], 'new content')
        before = self.business_rows()
        self.request('/api/jobs/lifecycle', body=dict(stale, expected_edit_token=refreshed['edit_token']), status=409)
        self.assertEqual(self.business_rows(), before)
        self.lifecycle(job_id, 'restore', reason='')
        self.assertEqual(self.detail(job_id, 'candidate_a')['payload']['title'], '来源已更正的岗位名称')

    def test_same_time_withdraw_restore_withdraw_never_reuses_an_old_token(self):
        job_id = self.public_job()
        with patch('recruitment_insights.application.jobs.now', return_value='2026-10-02T08:00:00'):
            self.lifecycle(job_id)
            first_withdrawal = self.detail(job_id)
            self.lifecycle(job_id, 'restore')
            self.lifecycle(job_id)
        current = self.detail(job_id)
        self.assertEqual(current['lifecycle_updated_at'], first_withdrawal['lifecycle_updated_at'])
        self.assertEqual(current['lifecycle_reason'], first_withdrawal['lifecycle_reason'])
        self.assertNotEqual(current['edit_token'], first_withdrawal['edit_token'])
        before = self.business_rows()
        self.request('/api/jobs/lifecycle', body=dict(id=job_id, action='restore', reason='',
                     expected_edit_token=first_withdrawal['edit_token']), status=409)
        self.assertEqual(self.business_rows(), before)
        self.request('/api/jobs/'+str(job_id), 'candidate_a', status=404)

    def test_audit_failure_rolls_back_state_reason_token_and_history(self):
        public_id = self.public_job()
        enterprise_id = self.enterprise_job()
        for job_id, user in ((public_id, 'admin'), (enterprise_id, 'boss_a')):
            with self.subTest(user=user):
                detail = self.detail(job_id, user)
                before = self.business_rows()
                body = dict(id=job_id, action='deactivate', reason='事务失败时不应保留此理由',
                            expected_edit_token=detail['edit_token'])
                with patch.object(Accounts, 'audit', side_effect=RuntimeError('injected lifecycle audit failure')):
                    with self.assertRaisesRegex(RuntimeError, 'injected lifecycle audit failure'):
                        self.store.set_lifecycle(self.actors[user], body)
                self.assertEqual(self.business_rows(), before)
                self.assertEqual(self.detail(job_id, user), detail)
                self.lifecycle(job_id, user=user)
                detail = self.detail(job_id, user)
                before = self.business_rows()
                with patch.object(Accounts, 'audit', side_effect=RuntimeError('injected restore audit failure')):
                    with self.assertRaisesRegex(RuntimeError, 'injected restore audit failure'):
                        self.store.set_lifecycle(self.actors[user], dict(body, action='restore', expected_edit_token=detail['edit_token']))
                self.assertEqual(self.business_rows(), before)
                self.assertEqual(self.detail(job_id, user), detail)

    def test_restart_preserves_withdrawal_and_record_identity_until_restore(self):
        job_id = self.public_job()
        self.request('/api/favorites', 'candidate_a', dict(job_id=job_id, note='持久收藏'))
        self.lifecycle(job_id)
        before = self.business_rows()
        expected = self.detail(job_id)
        reopened = Store(self.path)
        self.assertEqual(reopened.detail(job_id, self.actors['admin']), expected)
        self.assertEqual(reopened.jobs({'dataset': 'real'}, self.actors['candidate_a']), [])
        self.assertEqual(reopened.accounts.favorites(self.actors['candidate_a']), [])
        self.assertEqual(self.business_rows(), before)
        reopened.set_lifecycle(self.actors['admin'], dict(id=job_id, action='restore', reason='',
                                                       expected_edit_token=expected['edit_token']))
        again = Store(self.path)
        self.assertEqual([row['id'] for row in again.jobs({'dataset': 'real'}, self.actors['candidate_a'])], [job_id])
        self.assertEqual(again.accounts.favorites(self.actors['candidate_a'])[0]['note'], '持久收藏')
        self.assertEqual(len(self.rows('jobs')), 1)
        self.assertEqual(self.rows('revisions'), before['revisions'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
