"""Enterprise admission regression checks. Temporary databases only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from http.client import HTTPConnection
from pathlib import Path
import secrets
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from core import Store
from recruitment_insights.domain.errors import ConflictError
from recruitment_insights.infrastructure.database import Database
from recruitment_insights.config import WebPolicy
from recruitment_insights.web.routes import Routes
from recruitment_insights.web.server import make_server


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'test.sqlite3'
        self.store = Store(self.path)
        self.password = secrets.token_urlsafe(24)
        self.store.accounts.create_user('reviewer', self.password, '审核员', 'admin')
        self.token, self.admin = self.store.accounts.login('reviewer', self.password)

    def body(self, username='company_user', company='新测试企业'):
        return dict(username=username,password=self.password,display_name='申请人',company_name=company,
                    contact_name='联系人',contact='test@example.test',website='',description='课程测试使用的独立模拟企业。',dedicated_account=True)

    def register(self, username='company_user', company='新测试企业'):
        ident = self.store.onboarding.register(self.body(username,company))
        token, actor = self.store.accounts.login(username,self.password)
        return ident, token, actor

    def approve(self, ident, version=1):
        return self.store.onboarding.review(self.admin,dict(application_id=ident,version=version,decision='approved'))

    def test_register_is_unprivileged_and_cannot_reuse_existing_account(self):
        body=self.body();body.update(role='admin',org_id=1,can_manage_jobs=1)
        self.store.onboarding.register(body)
        _,actor=self.store.accounts.login(body['username'],self.password)
        self.assertEqual((actor['role'],actor['org_id'],actor['can_manage_jobs']),('candidate',None,0))
        with self.assertRaises(ValueError):
            self.store.onboarding.register(self.body(company='另一家测试企业'))
        self.assertEqual(self.store.onboarding.queue(self.admin)['total'],1)

    def test_atomic_approval_live_identity_and_hr_reuse(self):
        ident,token,actor=self.register()
        with self.assertRaises(PermissionError):
            self.store.onboarding.review(actor,dict(application_id=ident,version=1,decision='approved'))
        self.approve(ident)
        boss=self.store.accounts.user(token)
        self.assertEqual(boss['role'],'boss')
        self.assertIsNotNone(boss['org_id'])
        hr_id=self.store.accounts.create_hr(boss,dict(username='new_hr',password=self.password,display_name='HR',can_manage=True))
        self.assertTrue(any(x['id']==hr_id for x in self.store.accounts.members(boss)))
        with self.assertRaises(ConflictError): self.approve(ident)

    def test_normalized_existing_or_pending_name_never_grants_membership(self):
        self.store.accounts.organization('ＡＢＣ 企业')
        with self.assertRaises(ConflictError):self.register(company='abc企业')
        self.register()
        with self.assertRaises(ConflictError):self.register('other_user','新 测试 企业')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0],2)

    def test_withdraw_resubmit_reject_history_and_stale_review(self):
        ident,_,actor=self.register()
        self.store.onboarding.change(actor,dict(action='withdraw',version=1))
        with self.assertRaises(ConflictError):self.approve(ident)
        data=self.body();data.update(action='resubmit',version=2)
        self.store.onboarding.change(actor,data)
        self.store.onboarding.review(self.admin,dict(application_id=ident,version=3,decision='rejected',reason='请补充说明'))
        mine=self.store.onboarding.mine(actor)
        self.assertEqual(mine['status'],'rejected')
        self.assertEqual(len(mine['history']),4)
        with self.assertRaises(ConflictError):self.store.onboarding.change(actor,data)

    def test_concurrent_review_creates_exactly_one_company(self):
        ident,_,_=self.register()
        def attempt():
            try:self.approve(ident);return 'approved'
            except ConflictError:return 'conflict'
        with ThreadPoolExecutor(2) as pool:
            self.assertCountEqual(list(pool.map(lambda _:attempt(),range(2))),['approved','conflict'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM organizations').fetchone()[0],1)

    def test_failed_audit_rolls_back_approval_and_registration(self):
        ident,_,actor=self.register()
        with patch('recruitment_insights.infrastructure.accounts.AuditRepository.append',side_effect=RuntimeError('test failure')):
            with self.assertRaises(RuntimeError):self.approve(ident)
            with self.assertRaises(RuntimeError):self.register('another_user','另一家企业')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM organizations').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0],2)
        self.assertEqual(self.store.onboarding.mine(actor)['status'],'pending')

    def test_foreign_applications_and_stale_admin_are_denied(self):
        ident,_,actor=self.register()
        _,_,other=self.register('other_user','其他测试企业')
        self.assertNotEqual(self.store.onboarding.mine(actor)['id'],self.store.onboarding.mine(other)['id'])
        with self.assertRaises(PermissionError):self.store.onboarding.detail(other,ident)
        with self.store.connect() as db:db.execute("UPDATE users SET role='candidate' WHERE id=?",(self.admin['id'],))
        with self.assertRaises(PermissionError):self.approve(ident)

    def test_personal_content_blocks_conversion_and_rejection_requires_reason(self):
        ident,_,actor=self.register()
        with patch('recruitment_insights.infrastructure.onboarding.OnboardingRepository.has_personal_data',return_value=True):
            with self.assertRaises(ConflictError):self.approve(ident)
        with self.assertRaises(ValueError):
            self.store.onboarding.review(self.admin,dict(application_id=ident,version=1,decision='rejected',reason=' '))
        self.assertEqual(self.store.onboarding.mine(actor)['status'],'pending')

    def test_v3_migration_preserves_existing_users_and_sessions(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('DROP TABLE enterprise_application_events')
            db.execute('DROP TABLE enterprise_applications')
            db.execute('DELETE FROM schema_migrations WHERE version=4')
            db.commit()
        Database(self.path).initialize()
        self.assertEqual(self.store.accounts.user(self.token)['username'],'reviewer')

    def test_http_public_flow_csrf_and_closed_registration(self):
        origin='https://onboarding.example.test'
        server=make_server(self.store,0,policy=WebPolicy(test_origin=origin,allow_candidate_registration=True))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(path,body=None,marker=True):
            conn=HTTPConnection('127.0.0.1',server.server_port,timeout=5)
            headers={'Host':'onboarding.example.test','Origin':origin,'Content-Type':'application/json'}
            if marker:headers['X-Local-Request']='1'
            try:
                conn.request('POST' if body else 'GET',path,json.dumps(body) if body else None,headers)
                response=conn.getresponse();return response.status,response.read()
            finally:conn.close()
        try:
            self.assertEqual(request('/join')[0],200)
            self.assertEqual(request('/api/auth/register-enterprise',self.body(),False)[0],403)
            self.assertEqual(request('/api/auth/register-enterprise',self.body())[0],201)
            self.assertEqual(request('/api/admin/enterprise-applications')[0],401)
            self.assertEqual(request('/api/enterprise-application')[0],401)
        finally:server.shutdown();server.server_close();thread.join(5)
        closed=Routes(self.store,WebPolicy(test_origin=origin))
        self.assertEqual(closed.post('/api/auth/register-enterprise',self.body('closed_user','关闭测试企业'),None).status,403)
        self.assertEqual(closed.get('/join','',None).status,403)


if __name__=='__main__':unittest.main()
