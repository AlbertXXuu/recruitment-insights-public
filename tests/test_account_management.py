"""Account administration against temporary databases and a local HTTP server."""
import json
import secrets
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from core import Store
from recruitment_insights.config import WebPolicy
from recruitment_insights.domain.errors import ConflictError
from recruitment_insights.infrastructure import database
from recruitment_insights.web.server import make_server


class AccountManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'accounts.sqlite3'
        self.store = Store(self.path)
        self.pw = secrets.token_urlsafe(24)
        self.store.accounts.create_user('admin_one', self.pw, '管理员', 'admin')
        self.store.accounts.create_user('admin_two', self.pw, '管理员2', 'admin')
        self.org1 = self.store.accounts.organization('合成企业甲')
        self.org2 = self.store.accounts.organization('合成企业乙')
        self.store.accounts.create_user('boss_one', self.pw, '负责人甲', 'boss', self.org1, True)
        self.store.accounts.create_user('boss_two', self.pw, '负责人乙', 'boss', self.org2, True)
        self.store.accounts.create_user('hr_one', self.pw, 'HR甲', 'hr', self.org1, True)
        self.store.accounts.create_user('hr_two', self.pw, 'HR乙', 'hr', self.org2, True)
        self.store.accounts.create_user('candidate_one', self.pw, '求职者', 'candidate')
        self.tokens = {}
        self.server = make_server(self.store, 0, policy=WebPolicy())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        for name in ('admin_one', 'admin_two', 'boss_one', 'boss_two', 'hr_one', 'candidate_one'):
            self.login(name)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def request(self, path, user=None, body=None, expected=200):
        headers = {'Content-Type': 'application/json', 'X-Local-Request': '1'}
        if user:
            headers['Cookie'] = self.tokens[user]
        conn = HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            conn.request('POST' if body is not None else 'GET', path,
                         json.dumps(body) if body is not None else None, headers)
            response = conn.getresponse()
            data = json.loads(response.read())
            self.assertEqual(response.status, expected, (path, data))
            if path == '/api/auth/login' and expected == 200:
                self.tokens[body['username']] = response.getheader('Set-Cookie').split(';', 1)[0]
            return data
        finally:
            conn.close()

    def login(self, username, expected=200):
        return self.request('/api/auth/login', body={'username': username, 'password': self.pw}, expected=expected)

    def listing(self, **params):
        from urllib.parse import urlencode
        return self.request('/api/admin/accounts?' + urlencode(params), user='admin_one')

    def item(self, username):
        return next(item for item in self.listing()['items'] if item['username'] == username)

    def test_admin_list_filters_no_credentials_and_cross_role_denied(self):
        page = self.listing(q='合成企业甲', role='hr', status='active')
        self.assertEqual((page['total'], page['page']), (1, 1))
        self.assertEqual(page['items'][0]['username'], 'hr_one')
        self.assertEqual(set(page['items'][0]), {'id','username','display_name','role','org_id','org_name',
                                                 'can_manage_jobs','active','version'})
        self.request('/api/admin/accounts', user='candidate_one', expected=403)
        self.request('/api/admin/accounts', user='boss_one', expected=403)
        self.request('/api/admin/accounts', user='hr_one', expected=403)
        self.request('/api/admin/accounts?role=nope', user='admin_one', expected=400)

    def test_edit_optimistic_conflict_audit_and_forbidden_fields(self):
        item = self.item('hr_one')
        edit = {'id': item['id'], 'version': item['version'], 'display_name': '更新 HR', 'can_manage_jobs': False}
        self.request('/api/admin/accounts/edit', user='admin_one', body=edit)
        self.request('/api/admin/accounts/edit', user='admin_one', body=edit, expected=409)
        changed = self.item('hr_one')
        self.assertEqual((changed['display_name'], changed['can_manage_jobs']), ('更新 HR', 0))
        edit.update(version=changed['version'], role='admin')
        self.request('/api/admin/accounts/edit', user='admin_one', body=edit, expected=400)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit_events WHERE action='account_edit'").fetchone()[0], 1)

    def test_boss_deactivate_revokes_session_retains_org_restore_as_readonly(self):
        item = self.item('hr_one')
        other = self.item('hr_two')
        self.request('/api/team/deactivate', 'boss_one', {'id': other['id'], 'version': other['version']}, 403)
        self.request('/api/team/deactivate', 'hr_one', {'id': item['id'], 'version': item['version']}, 403)
        self.request('/api/team/deactivate', 'boss_one', {'id': item['id'], 'version': item['version'], 'active': True}, 400)
        self.request('/api/team/deactivate', 'boss_one', {'id': item['id'], 'version': item['version']})
        self.request('/api/auth/me', 'hr_one', expected=401)
        self.login('hr_one', expected=401)
        self.assertFalse(any(r['id'] == item['id'] for r in self.request('/api/team', 'boss_one')))
        disabled = self.item('hr_one')
        self.assertEqual((disabled['org_id'], disabled['active'], disabled['can_manage_jobs']), (self.org1, 0, 0))
        self.request('/api/admin/accounts/status', 'admin_one', {'id': item['id'], 'version': item['version'], 'active': True}, 409)
        self.request('/api/admin/accounts/status', 'admin_one', {'id': item['id'], 'version': disabled['version'], 'active': True})
        self.login('hr_one')
        self.request('/api/company/jobs', 'hr_one', {'record': {'title': '不应创建'}}, 403)
        self.assertEqual(self.item('hr_one')['can_manage_jobs'], 0)

    def test_admin_and_boss_protection_and_personal_data_retained(self):
        admin = self.item('admin_one')
        self.request('/api/admin/accounts/status', 'admin_one', {'id': admin['id'], 'version': admin['version'], 'active': False}, 400)
        boss = self.item('boss_one')
        self.request('/api/admin/accounts/status', 'admin_one', {'id': boss['id'], 'version': boss['version'], 'active': False}, 400)
        second = self.item('admin_two')
        self.request('/api/admin/accounts/status', 'admin_one', {'id': second['id'], 'version': second['version'], 'active': False})
        self.request('/api/auth/me', 'admin_two', expected=401)
        candidate = self.item('candidate_one')
        with self.store.connect() as db:
            # Keep a real foreign-key-bearing row, so accidental hard deletion fails the test.
            db.execute('INSERT INTO audit_events(user_id,org_id,action,target,created,details) VALUES(?,?,?,?,?,?)',
                       (candidate['id'], None, 'test', 'retained', 0, '{}'))
        self.request('/api/admin/accounts/status', 'admin_one', {'id': candidate['id'], 'version': candidate['version'], 'active': False})
        self.request('/api/auth/me', 'candidate_one', expected=401)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM audit_events WHERE user_id=?', (candidate['id'],)).fetchone()[0], 1)

    def test_pending_application_cannot_be_approved_after_deactivation(self):
        body = dict(username='applicant_one', password=self.pw, display_name='企业申请人',
                    company_name='合成申请企业', contact_name='联系人', contact='demo@example.invalid',
                    description='仅用于课程测试的虚构企业申请。', dedicated_account=True)
        self.request('/api/auth/register-enterprise', body=body, expected=201)
        self.login('applicant_one')
        application = self.request('/api/enterprise-application', 'applicant_one')
        item = self.item('applicant_one')
        self.request('/api/admin/accounts/status', 'admin_one',
                     {'id': item['id'], 'version': item['version'], 'active': False})
        self.request('/api/admin/enterprise-applications/review', 'admin_one',
                     {'application_id': application['id'], 'version': application['version'], 'decision': 'approved'}, 409)
        self.assertEqual(self.item('applicant_one')['role'], 'candidate')

    def test_approval_increments_account_version_and_rejects_stale_edit(self):
        body = dict(username='applicant_two', password=self.pw, display_name='企业申请人二',
                    company_name='合成申请企业二', contact_name='联系人', contact='demo2@example.invalid',
                    description='仅用于课程测试的虚构企业申请。', dedicated_account=True)
        self.request('/api/auth/register-enterprise', body=body, expected=201)
        self.login('applicant_two')
        application = self.request('/api/enterprise-application', 'applicant_two')
        old = self.item('applicant_two')
        self.request('/api/admin/enterprise-applications/review', 'admin_one',
                     {'application_id': application['id'], 'version': application['version'], 'decision': 'approved'})
        updated = self.item('applicant_two')
        self.assertEqual((updated['role'], updated['version']), ('boss', old['version'] + 1))
        self.request('/api/admin/accounts/edit', 'admin_one',
                     {'id': old['id'], 'version': old['version'], 'display_name': '旧页面编辑'}, 409)

    def test_disable_between_password_check_and_session_creation(self):
        import hmac
        item = self.item('candidate_one')
        admin = self.store.accounts.login('admin_one', self.pw)[1]
        compare = hmac.compare_digest
        def disable_during_check(actual, expected):
            result = compare(actual, expected)
            self.store.accounts.management_status(admin, {'id': item['id'], 'version': item['version'], 'active': False})
            return result
        with patch('recruitment_insights.application.accounts.hmac.compare_digest', side_effect=disable_during_check):
            with self.assertRaisesRegex(ValueError, '账号或密码错误'):
                self.store.accounts.login('candidate_one', self.pw)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sessions WHERE user_id=?', (item['id'],)).fetchone()[0], 0)

    def test_delete_http_is_admin_only_and_requires_exact_confirmation(self):
        target = self.item('candidate_one')
        body = {'id': target['id'], 'version': target['version'], 'confirm_username': target['username']}
        for user in ('boss_one', 'hr_one', 'candidate_one'):
            self.request('/api/admin/accounts/delete', user, body, 403)
        self.request('/api/admin/accounts/delete', 'admin_one', body, 400)
        self.request('/api/admin/accounts/status', 'admin_one', {'id': target['id'], 'version': target['version'], 'active': False})
        target = self.item('candidate_one')
        body = {'id': target['id'], 'version': target['version'], 'confirm_username': 'other'}
        self.request('/api/admin/accounts/delete', 'admin_one', body, 400)
        body['confirm_username'] = target['username']
        self.assertEqual(self.request('/api/admin/accounts/delete', 'admin_one', body)['message'], '账号已永久删除')
        self.assertFalse(any(item['username'] == target['username'] for item in self.listing()['items']))
        self.request('/api/auth/me', 'candidate_one', expected=401)


class AccountMigrationTests(unittest.TestCase):
    def test_schema4_to5_preserves_rows_and_atomic_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'v4.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
                for version in range(1, 5):
                    database.MIGRATIONS[version](db)
                    db.execute('INSERT INTO schema_migrations(version) VALUES(?)', (version,))
                db.execute("INSERT INTO users(username,display_name,password_hash,role) VALUES('legacy','旧账号','test','candidate')")
                db.execute("INSERT INTO sessions(token_hash,user_id,expires) VALUES('original',1,123)")
                db.commit()
            original = database.MIGRATIONS[5]
            def fail(db):
                original(db)
                raise RuntimeError('simulated migration failure')
            with patch.dict(database.MIGRATIONS, {5: fail}):
                with self.assertRaises(RuntimeError):
                    Store(path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT max(version) FROM schema_migrations').fetchone()[0], 4)
                self.assertNotIn('active', [r[1] for r in db.execute('PRAGMA table_info(users)')])
                self.assertEqual(db.execute('SELECT username,password_hash FROM users').fetchall(), [('legacy','test')])
            Store(path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT username,password_hash,active,account_version FROM users').fetchall(), [('legacy','test',1,1)])
                self.assertEqual(db.execute('SELECT token_hash FROM sessions').fetchall(), [('original',)])
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
