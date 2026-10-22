import csv
import io
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from accounts import Accounts
from app import make_server
from core import Store, normalize
from quality import quality_report
from seed_demo_accounts import PASSWORD, seed


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.store = Store(Path(cls.temp.name)/'workflow.sqlite3')
        seed(cls.store)
        cls.server = make_server(cls.store, 0)
        cls.accounts = cls.server.accounts
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'
        cls.cookies = {}
        for username in ('admin', 'boss_a', 'hr_a', 'boss_b', 'hr_b', 'candidate_a', 'candidate_b'):
            token, _ = cls.accounts.login(username, PASSWORD)
            cls.cookies[username] = 'recruitment_insights_session='+token

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(); cls.temp.cleanup()

    def request(self, path, user=None, body=None, status=200):
        headers = {'Cookie': self.cookies.get(user, '')}
        payload = None
        if body is not None:
            # Existing workflow cases save the latest form; stale-form rejection has
            # dedicated regression cases in test_edit_export_regressions.py.
            if path == '/api/company/jobs' and body.get('id') and status == 200 and 'expected_edit_token' not in body:
                current = self.request('/api/jobs/'+str(body['id']), user)
                body = dict(body, expected_edit_token=current['edit_token'])
            headers.update({'Content-Type': 'application/json', 'X-Local-Request': '1'})
            payload = json.dumps(body).encode()
        try:
            response = urlopen(Request(self.url+quote(path, safe='/?=&'), data=payload, headers=headers), timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            self.assertEqual(response.status, status, path)
            content = response.read().decode('utf-8-sig')
            return json.loads(content) if response.headers.get_content_type() == 'application/json' else content

    def job(self, user='boss_a', title='测试 Python 后端', **flags):
        return self.request('/api/company/jobs', user, {
            'record': {'title': title, 'location': '测试地区', 'description': 'Python 岗位，负责接口设计、测试和维护。'*4}, **flags})['id']

    def test_anonymous_and_forged_session_cannot_read(self):
        for path in ('/api/jobs', '/api/stats', '/api/options', '/api/export', '/api/quality', '/api/runs', '/api/team', '/api/favorites'):
            self.request(path, status=401)
        self.request('/api/company/jobs', body={}, status=401)
        self.cookies['forged'] = 'recruitment_insights_session=boss_a'
        self.request('/api/auth/me', 'forged', status=401)

    def test_register_cannot_choose_privileged_role(self):
        self.request('/api/auth/register', body=dict(username='new_candidate', password=PASSWORD,
                     display_name='新用户', role='admin', org_id=1, can_manage_jobs=True), status=201)
        user = self.request('/api/auth/login', body=dict(username='new_candidate', password=PASSWORD))
        self.assertEqual(user['role'], 'candidate'); self.assertIsNone(user['org_id'])
        self.assertNotIn('password_hash', user)
        self.request('/api/auth/login', body=dict(username='new_candidate', password='incorrect'), status=401)

    def test_login_cookie_logout_and_expiry(self):
        body = json.dumps(dict(username='candidate_b', password=PASSWORD)).encode()
        with urlopen(Request(self.url+'/api/auth/login', data=body,
                            headers={'Content-Type': 'application/json', 'X-Local-Request': '1'})) as response:
            cookie = response.headers['Set-Cookie']
        self.assertIn('HttpOnly', cookie); self.assertIn('SameSite=Strict', cookie)
        self.cookies['short_session'] = cookie.split(';')[0]
        self.request('/api/auth/me', 'short_session')
        self.request('/api/auth/logout', 'short_session', {})
        self.request('/api/auth/me', 'short_session', status=401)
        token, _ = self.accounts.login('candidate_b', PASSWORD)
        self.cookies['expired'] = 'recruitment_insights_session='+token
        with self.store.connect() as db:
            db.execute('UPDATE sessions SET expires=? WHERE token_hash=?', (time.time()-1, self.accounts.token_hash(token)))
        self.request('/api/auth/me', 'expired', status=401)

    def test_draft_isolation_across_all_read_surfaces(self):
        title = '隔离测试专属标记'
        job_id = self.job(title=title)
        self.assertEqual(self.request('/api/jobs?dataset=demo&q='+title, 'boss_a')['total'], 1)
        for user in ('boss_b', 'candidate_a'):
            # A client-supplied organization cannot extend authorization.
            self.request('/api/jobs/'+str(job_id)+'?org_id=1', user, status=404)
            self.assertEqual(self.request('/api/jobs?dataset=demo&org_id=1&q='+title, user)['total'], 0)
            self.assertEqual(self.request('/api/stats?dataset=demo&q='+title, user)['total'], 0)
            self.assertNotIn(title, self.request('/api/export?dataset=demo', user))
        report = self.request('/api/quality?dataset=demo&q='+title, 'boss_b')
        self.assertEqual(report['total'], 0)
        self.assertFalse(any(j['id'] == job_id for j in self.request('/api/company/jobs', 'boss_b')))
        self.assertTrue(all(r['owner_org_id'] == 2 for r in self.request('/api/runs', 'boss_b')))

    def test_publish_retract_disable_and_cross_company_write(self):
        job_id = self.job(title='发布生命周期')
        self.request('/api/company/jobs', 'boss_b', {'id': job_id, 'published': True}, status=403)
        self.request('/api/company/jobs', 'boss_a', {'id': job_id, 'published': True})
        public = self.request('/api/jobs/'+str(job_id), 'candidate_a')
        self.assertNotIn('raw', public); self.assertNotIn('revisions', public)
        foreign = self.request('/api/jobs/'+str(job_id), 'boss_b')
        self.assertNotIn('raw', foreign)
        self.assertIn('raw', self.request('/api/jobs/'+str(job_id), 'boss_a'))
        self.request('/api/company/jobs', 'boss_a', {'id': job_id, 'published': False})
        self.request('/api/jobs/'+str(job_id), 'candidate_a', status=404)
        self.request('/api/company/jobs', 'boss_a', {'id': job_id, 'published': True, 'active': False})
        for user in ('candidate_a', 'boss_b'):
            self.request('/api/jobs/'+str(job_id), user, status=404)
        self.request('/api/jobs/'+str(job_id), 'boss_a')

    def test_hr_revocation_applies_to_existing_session(self):
        hr = self.request('/api/team', 'boss_a')[0]
        self.request('/api/team', 'boss_a', dict(id=hr['id'], can_manage=False))
        try:
            self.request('/api/company/jobs', 'hr_a', {'record': {}}, status=403)
            self.request('/api/import', 'hr_a', {'csv': '', 'source': 'test'}, status=403)
            self.request('/api/company/jobs', 'hr_a')
            self.request('/api/team', 'hr_a', dict(id=hr['id'], can_manage=True), status=403)
            self.request('/api/team', 'boss_b', dict(id=hr['id'], can_manage=True), status=403)
        finally:
            self.request('/api/team', 'boss_a', dict(id=hr['id'], can_manage=True))
        self.job(user='hr_a', title='恢复权限后创建')

    def test_owner_creates_only_own_hr(self):
        self.request('/api/team', 'boss_a', dict(username='new_hr', password=PASSWORD, display_name='新 HR',
                                               role='admin', org_id=2, can_manage=False))
        user = self.request('/api/auth/login', body=dict(username='new_hr', password=PASSWORD))
        self.assertEqual((user['role'], user['org_id']), ('hr', 1))

    def test_csv_namespace_and_forced_ownership(self):
        body = dict(csv='job_id,title,company,location\n SAME ,Python,伪造企业,隔离城市', source='同名来源', is_demo=True)
        for user in ('boss_a', 'boss_b'):
            self.assertEqual(self.request('/api/import', user, body)['inserted'], 1)
            self.assertEqual(self.request('/api/import', user, body)['unchanged'], 1)
            jobs = self.request('/api/company/jobs', user)
            owned = next(j for j in jobs if j['job_id'] == 'SAME')
            self.assertNotEqual(owned['company'], '伪造企业'); self.assertFalse(owned['published'])
            self.request('/api/company/jobs', user, dict(id=owned['id'], record={'title':'编辑导入岗位'}))
        self.assertNotIn('隔离城市', self.request('/api/options?dataset=demo', 'candidate_a')['location'])

    def test_private_favorites_notes_and_repeat_save(self):
        job_id = self.job(title='收藏演示岗位', published=True)
        self.request('/api/favorites', 'candidate_a', dict(job_id=job_id, note='仅我可见 <script>'))
        self.request('/api/favorites', 'candidate_a', dict(job_id=job_id))
        saved = self.request('/api/favorites', 'candidate_a')
        self.assertEqual(next(j['note'] for j in saved if j['id'] == job_id), '仅我可见 <script>')
        self.assertEqual(self.request('/api/favorites', 'candidate_b'), [])
        self.request('/api/favorites', 'boss_a', dict(job_id=job_id), status=403)
        self.request('/api/company/jobs', 'boss_a', dict(id=job_id, published=False))
        self.assertFalse(any(j['id'] == job_id for j in self.request('/api/favorites', 'candidate_a')))
        self.request('/api/favorites', 'candidate_a', dict(job_id=job_id, remove=True))
        self.request('/api/favorites', 'candidate_a', dict(job_id=job_id), status=403)

    def test_candidate_cannot_use_enterprise_endpoints(self):
        for path in ('/api/team', '/api/company/jobs', '/api/runs', '/api/audit', '/api/quality', '/api/template'):
            self.request(path, 'candidate_a', status=403)
        for path in ('/api/team', '/api/company/jobs', '/api/import', '/api/demo', '/api/sync'):
            self.request(path, 'candidate_a', {}, status=403)
        html = self.request('/enterprise', 'candidate_a')
        self.assertIn('app.js', html)  # Redirected to the authenticated public shell.

    def test_invalid_flags_and_required_fields_do_not_create_jobs(self):
        before = len(self.request('/api/company/jobs', 'boss_a'))
        for payload in ({'record': {}}, {'record': {'title': 'x'}, 'published': 'false'}, {'record': []}):
            self.request('/api/company/jobs', 'boss_a', payload, status=400)
        self.assertEqual(len(self.request('/api/company/jobs', 'boss_a')), before)

    def test_seed_is_idempotent(self):
        with self.store.connect() as db: count = db.execute('SELECT count(*) FROM users').fetchone()[0]
        self.assertEqual(seed(self.store), 0)
        with self.store.connect() as db: self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0], count)

    def test_company_save_rolls_back_content_state_and_audit_together(self):
        job_id = self.job(title='事务测试', published=True, active=False)
        token, actor = self.accounts.login('boss_a', PASSWORD)
        before = self.store.detail(job_id, actor)
        with patch.object(Accounts, 'audit', side_effect=RuntimeError('Simulated audit failure')):
            with self.assertRaises(RuntimeError):
                self.store.manage_job(actor, dict(id=job_id, expected_edit_token=before['edit_token'],
                                                 record={'title':'不应保存'}, active=True))
        self.assertEqual(self.store.detail(job_id, actor), before)


class QualityMigrationTests(unittest.TestCase):
    def test_normalization_quality_and_duplicate_boundaries(self):
        raw = dict(job_id='1', title='  Ｐｙｔｈｏｎ  后端 ', company=' Ａ公司 ', location=' 北京 ',
                   description='<p>Python</p>', salary_text='15–25K/月', currency='CNY')
        job = normalize(raw)
        self.assertEqual(job['title'], 'Python 后端'); self.assertEqual(job['description'], 'Python')
        self.assertEqual(job['salary_low'], 15000)
        jobs = [dict(job, id=1, source='a', is_demo=True), dict(job, id=2, source='b', is_demo=True),
                dict(job, id=3, source='c', is_demo=False)]
        report = quality_report(jobs)
        self.assertEqual((report['duplicate_groups'], report['duplicate_jobs']), (1, 2))
        self.assertEqual(report['warning_jobs'], 3)
        self.assertIn('description_short', [i['code'] for i in report['rows'][0]['issues']])
        self.assertEqual(quality_report([])['total'], 0)

    def test_additive_migration_retains_records_and_revisions(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory)/'legacy.sqlite3')
            store.import_records([dict(job_id='legacy', title='Python', company='旧公司', location='上海')], 'legacy', '旧批次')
            with store.connect() as db:
                db.execute('ALTER TABLE jobs DROP COLUMN owner_org_id')
                db.execute('ALTER TABLE jobs DROP COLUMN published')
                db.execute('ALTER TABLE runs DROP COLUMN owner_org_id')
                # This fixture represents an unversioned legacy database.
                db.execute('DROP TABLE schema_migrations')
                before = dict(db.execute('SELECT * FROM jobs').fetchone())
            migrated = Store(store.path)
            with migrated.connect() as db:
                after = dict(db.execute('SELECT * FROM jobs').fetchone())
                self.assertEqual({k:after[k] for k in before}, before)
                self.assertEqual(db.execute('SELECT count(*) FROM revisions').fetchone()[0], 1)
                self.assertIsNone(after['owner_org_id']); self.assertEqual(after['published'], 1)
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
