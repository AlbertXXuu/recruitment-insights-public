"""End-to-end HTTP acceptance against disposable databases, never the public site."""
import json
import secrets
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

from core import Store
from recruitment_insights.config import WebPolicy
from recruitment_insights.web.server import make_server


class EnterpriseHttpAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'isolated.sqlite3')
        self.password = secrets.token_urlsafe(24)
        self.store.accounts.create_user('review_admin', self.password, '独立审核员', 'admin')
        self.cookies = {}
        self.server = make_server(self.store, 0, policy=WebPolicy(
            test_origin='https://acceptance.example.test', allow_candidate_registration=True))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.login('review_admin')

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def request(self, path, body=None, user=None, expected=200, marker=True):
        headers = {'Host': 'acceptance.example.test', 'Origin': 'https://acceptance.example.test',
                   'Content-Type': 'application/json'}
        if marker:
            headers['X-Local-Request'] = '1'
        if user:
            headers['Cookie'] = self.cookies[user]
        conn = HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            conn.request('POST' if body is not None else 'GET', path,
                         json.dumps(body) if body is not None else None, headers)
            response = conn.getresponse()
            raw = response.read()
            try:
                data = json.loads(raw)
            except ValueError:
                data = raw.decode('utf-8')
            self.assertEqual(response.status, expected, (path, data))
            if path == '/api/auth/login' and expected == 200:
                self.cookies[body['username']] = response.getheader('Set-Cookie').split(';', 1)[0]
            return data
        finally:
            conn.close()

    def login(self, name):
        return self.request('/api/auth/login', {'username': name, 'password': self.password})

    def fields(self, name, company):
        return dict(username=name, password=self.password, display_name='验收申请人',
                    company_name=company, contact_name='测试联系人', contact='qa@example.test',
                    website='', description='只在临时数据库中的企业流程验收申请。', dedicated_account=True)

    def apply(self, name, company):
        data = self.fields(name, company)
        data.update(role='admin', org_id=999, can_manage_jobs=True)
        self.request('/api/auth/register-enterprise', data, expected=201)
        self.login(name)
        actor = self.request('/api/auth/me', user=name)
        self.assertEqual((actor['role'], actor['org_id'], actor['can_manage_jobs']), ('candidate', None, 0))
        return self.request('/api/enterprise-application', user=name)

    def review(self, application, decision='approved', reason='', expected=200):
        return self.request('/api/admin/enterprise-applications/review', dict(
            application_id=application['id'], version=application['version'], decision=decision,
            reason=reason), 'review_admin', expected)

    def test_registration_rejection_resubmit_approval_and_stale_sessions(self):
        app = self.apply('boss_one', '验收企业甲')
        for endpoint in ['/api/team', '/api/company/jobs', '/api/admin/enterprise-applications']:
            self.request(endpoint, user='boss_one', expected=403)
        self.request('/api/admin/enterprise-applications/review', dict(
            application_id=app['id'], version=1, decision='approved'), 'boss_one', 403)
        self.request('/api/auth/register-enterprise', self.fields('duplicate_one', '验收 企业甲'), expected=409)
        with self.store.connect() as db:
            self.assertIsNone(db.execute("SELECT id FROM users WHERE username='duplicate_one'").fetchone())
        self.review(app, 'rejected', expected=400)
        self.review(app, 'rejected', '请补充企业介绍')
        rejected = self.request('/api/enterprise-application', user='boss_one')
        self.assertEqual((rejected['status'], rejected['version']), ('rejected', 2))
        self.assertEqual(rejected['review_reason'], '请补充企业介绍')
        data = self.fields('boss_one', '验收企业甲')
        data.update(action='resubmit', version=1)
        self.request('/api/enterprise-application', data, 'boss_one', 409)
        data['version'] = 2
        self.request('/api/enterprise-application', data, 'boss_one')
        current = self.request('/api/enterprise-application', user='boss_one')
        self.review(app, expected=409)
        self.review(current)
        self.review(current, expected=409)
        actor = self.request('/api/auth/me', user='boss_one')
        self.assertEqual(actor['role'], 'boss')
        self.assertIsNotNone(actor['org_id'])
        approved = self.request('/api/enterprise-application', user='boss_one')
        self.assertEqual([json.loads(e['snapshot'])['status'] for e in approved['history']],
                         ['pending', 'rejected', 'pending', 'approved'])
        self.request('/api/resume', user='boss_one', expected=403)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM organizations').fetchone()[0], 1)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_hr_grants_revocation_and_company_isolation(self):
        a = self.apply('boss_one', '权限验收企业甲')
        b = self.apply('boss_two', '权限验收企业乙')
        self.review(a)
        self.review(b)
        for boss, hr in [('boss_one', 'hr_one'), ('boss_two', 'hr_two')]:
            self.request('/api/team', dict(username=hr, password=self.password,
                display_name='测试 HR', can_manage=False, role='admin', org_id=999), boss)
            self.login(hr)
        members = self.request('/api/team', user='boss_one')
        hr_id = members[0]['id']
        self.assertEqual([x['username'] for x in members], ['hr_one'])
        self.assertEqual([x['username'] for x in self.request('/api/team', user='boss_two')], ['hr_two'])
        record = dict(title='Python 工程师', location='北京', description='Python SQL 数据处理与系统开发岗位。')
        create = dict(record=record, published=False, active=True, is_demo=True, org_id=999)
        self.request('/api/company/jobs', create, 'hr_one', 403)
        self.request('/api/team', {'id': hr_id, 'can_manage': True}, 'boss_two', 403)
        self.request('/api/team', {'id': hr_id, 'can_manage': True}, 'hr_one', 403)
        self.request('/api/team', {'id': hr_id, 'can_manage': True}, 'boss_one')
        job = self.request('/api/company/jobs', create, 'hr_one')
        self.assertTrue(self.request('/api/auth/me', user='hr_one')['can_manage_jobs'])
        self.request('/api/jobs/'+str(job['id']), user='boss_two', expected=404)
        self.request('/api/jobs/'+str(job['id']), user='hr_two', expected=404)
        edit = dict(create, id=job['id'], expected_edit_token=job['edit_token'])
        self.request('/api/company/jobs', edit, 'boss_two', 403)
        lifecycle = dict(id=job['id'], action='deactivate', reason='独立验收', expected_edit_token=job['edit_token'])
        self.request('/api/jobs/lifecycle', lifecycle, 'boss_two', 403)
        self.request('/api/team', {'id': hr_id, 'can_manage': False}, 'boss_one')
        self.assertFalse(self.request('/api/auth/me', user='hr_one')['can_manage_jobs'])
        self.request('/api/company/jobs', edit, 'hr_one', 403)
        self.request('/api/jobs/lifecycle', lifecycle, 'hr_one', 403)
        self.request('/api/admin/enterprise-applications/'+str(b['id']), user='boss_one', expected=403)
        self.request('/api/admin/enterprise-applications', user='hr_one', expected=403)
        self.request('/api/team', user='hr_one', expected=403)
        edit['published'] = True
        self.request('/api/company/jobs', edit, 'boss_one')
        visible = self.request('/api/jobs/'+str(job['id']), user='boss_two')
        self.assertIn('权限验收企业甲', json.dumps(visible, ensure_ascii=False))
        self.request('/api/company/jobs', edit, 'boss_two', 403)
        self.request('/api/company/jobs', edit, 'review_admin', 403)

    def test_anonymous_csrf_and_withdrawal(self):
        a = self.apply('boss_one', '撤回验收企业')
        self.request('/api/enterprise-application', expected=401)
        self.request('/api/admin/enterprise-applications', expected=401)
        payload = dict(application_id=a['id'], version=1, decision='approved')
        self.request('/api/admin/enterprise-applications/review', payload, 'review_admin', 403, marker=False)
        self.request('/api/enterprise-application', dict(action='withdraw', version=1), 'boss_one')
        self.review(a, expected=409)
        data = self.fields('boss_one', '撤回后更新企业')
        data.update(action='resubmit', version=2)
        self.request('/api/enterprise-application', data, 'boss_one')
        current = self.request('/api/enterprise-application', user='boss_one')
        self.assertEqual((current['status'], current['version']), ('pending', 3))
        self.review(current)


if __name__ == '__main__':
    unittest.main()
