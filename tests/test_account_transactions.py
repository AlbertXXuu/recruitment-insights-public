"""Account transaction regressions; every case uses an isolated temporary database."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from accounts import Accounts
from core import Store


PASSWORD = 'Transaction-test-2026!'


class AccountTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'accounts.sqlite3')
        self.accounts = Accounts(self.store)
        self.org_a = self.accounts.organization('事务测试企业 A')
        self.org_b = self.accounts.organization('事务测试企业 B')
        definitions = (
            ('owner_a', 'boss', self.org_a, False),
            ('human_a', 'hr', self.org_a, True),
            ('viewer_a', 'candidate', None, False),
        )
        self.actors = {}
        for username, role, org, allowed in definitions:
            self.accounts.create_user(username, PASSWORD, username, role, org, allowed)
            _, self.actors[role] = self.accounts.login(username, PASSWORD)

    def hr_body(self, **overrides):
        return dict(dict(username='created_hr', password=PASSWORD,
                         display_name='新 HR', can_manage=True), **overrides)

    def rows(self, sql, args=()):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute(sql, args)]

    def public_job(self):
        result = self.store.import_records([dict(
            job_id='favorite-one', title='Python 开发', company='公开企业',
            location='杭州', description='负责 Python 接口开发与测试。' * 5)], 'transaction-fixture', 'fixture',
            owner_org_id=self.org_a)
        self.assertEqual(result['inserted'], 1)
        job_id = self.store.jobs({'dataset': 'all'})[0]['id']
        with self.store.connect() as db:
            db.execute('UPDATE jobs SET published=1 WHERE id=?', (job_id,))
        return job_id

    def test_grant_reloads_boss_role_before_mutation(self):
        actor = self.actors['boss']
        hr = self.actors['hr']
        with self.store.connect() as db:
            db.execute("UPDATE users SET role='hr' WHERE id=?", (actor['id'],))
        with self.assertRaises(PermissionError):
            self.accounts.grant(actor, hr['id'], False)
        self.assertEqual(self.rows('SELECT can_manage_jobs FROM users WHERE id=?',
                                   (hr['id'],))[0]['can_manage_jobs'], 1)
        self.assertEqual(self.rows('SELECT * FROM audit_events'), [])

    def test_favorite_reloads_candidate_role_before_mutation(self):
        actor = self.actors['candidate']
        job_id = self.public_job()
        with self.store.connect() as db:
            db.execute("UPDATE users SET role='hr',org_id=? WHERE id=?", (self.org_a, actor['id']))
        with self.assertRaises(PermissionError):
            self.accounts.save_favorite(actor, job_id, '不应写入')
        self.assertEqual(self.rows('SELECT * FROM favorites'), [])

    def test_create_hr_and_audit_roll_back_together(self):
        with patch.object(Accounts, 'audit', side_effect=RuntimeError('audit unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'audit unavailable'):
                self.accounts.create_hr(self.actors['boss'], self.hr_body())
        self.assertEqual(self.rows("SELECT * FROM users WHERE username='created_hr'"), [])
        self.assertEqual(self.rows('SELECT * FROM audit_events'), [])

    def test_create_hr_uses_live_owner_and_ignores_client_role_and_org(self):
        actor = self.actors['boss']
        with self.store.connect() as db:
            db.execute('UPDATE users SET org_id=? WHERE id=?', (self.org_b, actor['id']))
        user_id = self.accounts.create_hr(actor, self.hr_body(role='admin', org_id=self.org_a))
        with self.store.uow() as tx:
            created = tx.accounts.get(user_id)
        self.assertEqual((created['role'], created['org_id'], created['org_name']),
                         ('hr', self.org_b, '事务测试企业 B'))
        self.assertEqual(created['can_manage_jobs'], 1)
        event = self.rows('SELECT * FROM audit_events')[0]
        self.assertEqual((event['user_id'], event['org_id'], event['action'], event['target']),
                         (actor['id'], self.org_b, 'create_hr', str(user_id)))

    def test_create_hr_rejects_revoked_owner_and_nonboolean_grant(self):
        actor = self.actors['boss']
        with self.assertRaises(ValueError):
            self.accounts.create_hr(actor, self.hr_body(can_manage='false'))
        with self.store.connect() as db:
            db.execute("UPDATE users SET role='hr' WHERE id=?", (actor['id'],))
        with self.assertRaises(PermissionError):
            self.accounts.create_hr(actor, self.hr_body())
        self.assertEqual(self.rows("SELECT * FROM users WHERE username='created_hr'"), [])

    def test_grant_and_audit_roll_back_together(self):
        hr = self.actors['hr']
        with patch.object(Accounts, 'audit', side_effect=RuntimeError('audit unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'audit unavailable'):
                self.accounts.grant(self.actors['boss'], hr['id'], False)
        self.assertEqual(self.rows('SELECT can_manage_jobs FROM users WHERE id=?',
                                   (hr['id'],))[0]['can_manage_jobs'], 1)
        self.assertEqual(self.rows('SELECT * FROM audit_events'), [])

    def test_grant_uses_live_organization_and_refuses_former_team(self):
        actor = self.actors['boss']
        with self.store.connect() as db:
            db.execute('UPDATE users SET org_id=? WHERE id=?', (self.org_b, actor['id']))
        with self.assertRaises(PermissionError):
            self.accounts.grant(actor, self.actors['hr']['id'], False)
        self.assertEqual(self.accounts.members(actor), [])
        self.assertEqual(self.rows('SELECT * FROM audit_events'), [])

    def test_favorite_keeps_note_and_allows_removing_withdrawn_job(self):
        actor = self.actors['candidate']
        job_id = self.public_job()
        self.accounts.save_favorite(actor, job_id, '私人备注')
        self.accounts.save_favorite(actor, job_id)
        self.assertEqual(self.accounts.favorites(actor)[0]['note'], '私人备注')
        with self.store.connect() as db:
            db.execute('UPDATE jobs SET published=0 WHERE id=?', (job_id,))
        self.assertEqual(self.accounts.favorites(actor), [])
        with self.assertRaises(PermissionError):
            self.accounts.save_favorite(actor, job_id, '不可覆盖')
        self.assertEqual(self.rows('SELECT note FROM favorites')[0]['note'], '私人备注')
        self.accounts.save_favorite(actor, job_id, remove=True)
        self.assertEqual(self.rows('SELECT * FROM favorites'), [])

    def test_favorites_use_one_snapshot_during_concurrent_withdrawal(self):
        from recruitment_insights.infrastructure.accounts import AccountRepository
        actor = self.actors['candidate']
        job_id = self.public_job()
        self.accounts.save_favorite(actor, job_id, '已有备注')
        original = AccountRepository.favorites

        def withdraw_after_read(repository, user_id):
            saved = original(repository, user_id)
            with self.store.connect() as db:
                db.execute('UPDATE jobs SET published=0 WHERE id=?', (job_id,))
            return saved

        with patch.object(AccountRepository, 'favorites', new=withdraw_after_read):
            saved = self.accounts.favorites(actor)
        self.assertEqual([(job['id'], job['note']) for job in saved], [(job_id, '已有备注')])
        self.assertEqual(self.accounts.favorites(actor), [])

    def test_read_services_reject_revoked_roles(self):
        boss = self.actors['boss']
        candidate = self.actors['candidate']
        with self.store.connect() as db:
            db.execute("UPDATE users SET role='hr' WHERE id=?", (boss['id'],))
            db.execute("UPDATE users SET role='hr',org_id=? WHERE id=?", (self.org_a, candidate['id']))
        with self.assertRaises(PermissionError):
            self.accounts.members(boss)
        with self.assertRaises(PermissionError):
            self.accounts.favorites(candidate)

    def test_sync_rechecks_admin_after_fetch_before_import(self):
        self.accounts.create_user('source_admin', PASSWORD, '同步管理员', role='admin')
        _, actor = self.accounts.login('source_admin', PASSWORD)

        def revoke_during_fetch():
            with self.store.connect() as db:
                db.execute("UPDATE users SET role='candidate' WHERE id=?", (actor['id'],))
            return {'jobs': [dict(id=123, title='Software Engineer',
                                  location={'name': '杭州'}, content='Python 接口开发') ]}

        with patch.object(self.store.importer.source, 'fetch', side_effect=revoke_during_fetch) as fetch:
            with self.assertRaises(PermissionError):
                self.store.sync_for(actor)
        fetch.assert_called_once_with()
        self.assertEqual(self.rows('SELECT * FROM jobs'), [])
        self.assertEqual(self.rows('SELECT * FROM revisions'), [])
        runs = self.rows('SELECT status FROM runs')
        self.assertEqual(runs, [{'status': 'failed'}])

    def test_csv_import_reloads_revoked_hr_permission(self):
        actor = self.actors['hr']
        self.assertTrue(actor['can_manage_jobs'])
        self.accounts.grant(self.actors['boss'], actor['id'], False)
        with self.assertRaises(PermissionError):
            self.store.import_for(actor, dict(source='permission-test',
                csv='job_id,title,company,location\none,Python 开发,伪造企业,杭州\n'))
        self.assertEqual(self.rows('SELECT * FROM jobs'), [])
        self.assertEqual(self.rows('SELECT * FROM runs'), [])
        self.assertEqual(self.rows("SELECT * FROM audit_events WHERE action='import_csv'"), [])

    def test_csv_import_and_audit_roll_back_updates_inserts_and_history(self):
        actor = self.actors['boss']
        self.store.import_for(actor, dict(source='rollback-test',
            csv='job_id,title,company,location\none,原始岗位,输入公司,杭州\n'))
        tables = ('jobs', 'runs', 'revisions', 'audit_events')
        before = {table: self.rows('SELECT * FROM ' + table + ' ORDER BY id') for table in tables}
        with patch.object(Accounts, 'audit', side_effect=RuntimeError('audit unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'audit unavailable'):
                self.store.import_for(actor, dict(source='rollback-test',
                    csv='job_id,title,company,location\none,修改岗位,输入公司,北京\ntwo,新增岗位,输入公司,杭州\n'))
        after = {table: self.rows('SELECT * FROM ' + table + ' ORDER BY id') for table in tables}
        self.assertEqual(after, before)

    def test_compatibility_audit_and_scoped_repository_reads(self):
        from recruitment_insights.application.accounts import Accounts as ServiceAccounts
        self.assertIs(Accounts, ServiceAccounts)
        actor = self.actors['boss']
        with self.store.connect() as db:
            Accounts.audit(db, actor, 'legacy_entry', 'one', {'中文': '保留'})
        with self.store.uow(write=True) as tx:
            Accounts.audit(tx, dict(actor, org_id=self.org_b), 'other_company', 'two')
            own_events = tx.audit.recent(actor)
            all_events = tx.audit.recent(dict(actor, role='admin'))
        self.assertEqual([item['action'] for item in own_events], ['legacy_entry'])
        self.assertEqual([item['action'] for item in all_events], ['other_company', 'legacy_entry'])
        self.assertEqual(own_events[0]['details'], '{"中文": "保留"}')

    def test_constructor_has_no_database_side_effect(self):
        class UnopenedStore:
            queries = object()

            def uow(self, **kwargs):
                raise AssertionError('constructor opened a transaction')

            def __getattr__(self, name):
                raise AssertionError('constructor touched database: ' + name)
        store = UnopenedStore()
        accounts = Accounts(store)
        self.assertEqual(accounts.uow, store.uow)
        self.assertIs(accounts.queries, store.queries)
        self.assertNotIn('store', vars(accounts))


if __name__ == '__main__':
    unittest.main()
