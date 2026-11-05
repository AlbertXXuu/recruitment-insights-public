"""Permanent deletion and identity isolation using synthetic temporary data."""
import secrets
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from core import Store
from recruitment_insights.application.accounts import Accounts
from recruitment_insights.infrastructure import database
from scripts.database_backup import copy_database


class AccountDeletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'test.sqlite3'
        self.store = Store(self.path)
        self.pw = secrets.token_urlsafe(24)
        self.service = self.store.accounts
        self.service.create_user('admin_one', self.pw, '管理员', 'admin')
        self.org = self.service.organization('合成企业')
        self.service.create_user('boss_one', self.pw, '负责人', 'boss', self.org)
        self.service.create_user('hr_one', self.pw, 'HR', 'hr', self.org, True)
        self.service.create_user('candidate_one', self.pw, '求职者', 'candidate')
        self.admin = self.service.login('admin_one', self.pw)[1]
        self.boss = self.service.login('boss_one', self.pw)[1]
        self.token, self.candidate = self.service.login('candidate_one', self.pw)

    def row(self, name):
        with self.store.uow() as tx:
            return tx.accounts.by_username(name)

    def disable(self, name):
        item = self.row(name)
        self.service.management_status(self.admin, {'id': item['id'], 'version': item['account_version'], 'active': False})
        return self.row(name)

    @staticmethod
    def body(item):
        return {'id': item['id'], 'version': item['account_version'], 'confirm_username': item['username']}

    def synthetic_jobs(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO runs(id,started_at,source,label,status,result) VALUES(1,'now','demo','demo','success','{}')")
            for ident in (1, 2):
                db.execute("INSERT INTO jobs(id,source,external_id,payload,fingerprint,raw,first_seen,last_seen,run_id,is_demo) VALUES(?, 'demo', ?, '{}', 'hash', '{}', 'now', 'now', 1, 1)", (ident, str(ident)))

    def test_delete_cleans_private_data_and_never_reuses_identity(self):
        self.synthetic_jobs()
        target = self.row('candidate_one')
        with self.store.connect() as db:
            db.execute("INSERT INTO favorites(user_id,job_id,note) VALUES(?,1,'private')", (target['id'],))
            db.execute("INSERT INTO resumes VALUES(?, 'demo.pdf', 'application/pdf', 1, ?, ?, 'now')", (target['id'], b'x', '0' * 64))
        disabled = self.disable('candidate_one')
        with self.store.connect() as db:
            db.execute("INSERT INTO sessions VALUES('synthetic',?,9999999999)", (target['id'],))
        self.service.management_delete(self.admin, self.body(disabled))
        self.assertIsNone(self.row('candidate_one'))
        self.assertIsNone(self.service.user(self.token))
        with self.assertRaises(PermissionError):
            self.service.favorites(self.candidate)
        with self.store.connect() as db:
            for table in ('sessions', 'favorites', 'resumes'):
                self.assertEqual(db.execute('SELECT count(*) FROM '+table+' WHERE user_id=?', (target['id'],)).fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM organizations').fetchone()[0], 1)
            event = db.execute("SELECT user_id,target,details FROM audit_events WHERE action='account_delete'").fetchone()
            self.assertEqual((event['user_id'], event['target']), (self.admin['id'], str(target['id'])))
            self.assertNotIn('candidate_one', event['details'])
        new_id = self.service.create_user('candidate_one', self.pw, '新用户', 'candidate')
        self.assertGreater(new_id, target['id'])
        with self.assertRaises(PermissionError):
            self.service.favorites(self.candidate)

    def test_enforces_authority_status_version_name_and_boss_protection(self):
        active = self.row('candidate_one')
        with self.assertRaises(PermissionError):
            self.service.management_delete(self.boss, self.body(active))
        with self.assertRaisesRegex(ValueError, '先停用'):
            self.service.management_delete(self.admin, self.body(active))
        with self.assertRaisesRegex(ValueError, '当前账号'):
            self.service.management_delete(self.admin, self.body(self.row('admin_one')))
        with self.assertRaisesRegex(ValueError, '交接'):
            self.service.management_delete(self.admin, self.body(self.row('boss_one')))
        target = self.disable('candidate_one')
        with self.assertRaisesRegex(ValueError, '不一致'):
            self.service.management_delete(self.admin, {**self.body(target), 'confirm_username': 'other'})
        from recruitment_insights.domain.errors import ConflictError
        with self.assertRaises(ConflictError):
            self.service.management_delete(self.admin, {**self.body(target), 'version': target['account_version'] - 1})
        with self.assertRaises(ValueError):
            self.service.management_delete(self.admin, {**self.body(target), 'extra': True})
        self.assertIsNotNone(self.row('candidate_one'))

    def test_admin_can_delete_eligible_hr_and_other_inactive_admin(self):
        self.service.create_user('admin_two', self.pw, '第二管理员', 'admin')
        for name in ('hr_one', 'admin_two'):
            target = self.disable(name)
            self.service.management_delete(self.admin, self.body(target))
            self.assertIsNone(self.row(name))
        self.assertIsNotNone(self.row('admin_one'))
        self.assertIsNotNone(self.row('boss_one'))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM organizations WHERE id=?', (self.org,)).fetchone()[0], 1)

    def test_all_shared_history_blocks_deletion_without_data_loss(self):
        self.synthetic_jobs()
        target = self.disable('hr_one')
        checks = [
            ('字段核验', "INSERT INTO field_reviews VALUES(1,'education','本科','hash','confirmed',1,?,'now')", (target['id'],)),
            ('重复岗位', "INSERT INTO duplicate_decisions VALUES(1,2,'same','reason','h','h',1,?,'now')", (target['id'],)),
            ('操作审计', "INSERT INTO audit_events(user_id,action,target,created,details) VALUES(?,'synthetic','1',0,'{}')", (target['id'],)),
        ]
        for label, statement, params in checks:
            with self.subTest(label=label):
                with self.store.connect() as db:
                    db.execute(statement, params)
                with self.assertRaisesRegex(ValueError, label):
                    self.service.management_delete(self.admin, self.body(target))
                with self.store.connect() as db:
                    table = {'字段核验':'field_reviews','重复岗位':'duplicate_decisions','操作审计':'audit_events'}[label]
                    db.execute('DELETE FROM '+table+' WHERE user_id=?', (target['id'],))
        with self.store.connect() as db:
            db.execute("INSERT INTO enterprise_applications(id,applicant_id,company_name,name_key,contact_name,contact,website,description,status,version,reviewer_id,created_at,updated_at) VALUES(1,?,'合成','合成','合成','none','','description','rejected',1,?,0,0)", (self.candidate['id'], target['id']))
        with self.assertRaisesRegex(ValueError, '企业入驻审核'):
            self.service.management_delete(self.admin, self.body(target))
        with self.store.connect() as db:
            db.execute('UPDATE enterprise_applications SET reviewer_id=NULL WHERE id=1')
            db.execute("INSERT INTO enterprise_application_events(application_id,actor_id,created_at,snapshot) VALUES(1,?,0,'{}')", (target['id'],))
        with self.assertRaisesRegex(ValueError, '企业入驻历史'):
            self.service.management_delete(self.admin, self.body(target))
        with self.store.connect() as db:
            db.execute('DELETE FROM enterprise_application_events WHERE actor_id=?', (target['id'],))
        candidate = self.disable('candidate_one')
        with self.assertRaisesRegex(ValueError, '企业入驻申请'):
            self.service.management_delete(self.admin, self.body(candidate))
        self.assertIsNotNone(self.row('hr_one'))
        self.assertIsNotNone(self.row('candidate_one'))

    def test_audit_failure_rolls_back_user_and_private_data(self):
        self.synthetic_jobs()
        target = self.disable('candidate_one')
        with self.store.connect() as db:
            db.execute("INSERT INTO favorites(user_id,job_id,note) VALUES(?,1,'private')", (target['id'],))
            db.execute("INSERT INTO sessions VALUES('synthetic-rollback',?,9999999999)", (target['id'],))
            db.execute("INSERT INTO resumes VALUES(?, 'demo.pdf', 'application/pdf', 1, ?, ?, 'now')", (target['id'], b'x', '0' * 64))
        with patch.object(Accounts, 'audit', side_effect=RuntimeError('simulated')):
            with self.assertRaises(RuntimeError):
                self.service.management_delete(self.admin, self.body(target))
        self.assertIsNotNone(self.row('candidate_one'))
        with self.store.connect() as db:
            for table in ('sessions', 'favorites', 'resumes'):
                self.assertEqual(db.execute('SELECT count(*) FROM '+table+' WHERE user_id=?', (target['id'],)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM audit_events WHERE action='account_delete'").fetchone()[0], 0)

    def test_login_pending_during_delete_and_reregistration_fails_closed(self):
        import hmac
        target = self.row('candidate_one')
        compare = hmac.compare_digest
        def replace(actual, expected):
            result = compare(actual, expected)
            disabled = self.disable(target['username'])
            self.service.management_delete(self.admin, self.body(disabled))
            new_id = self.service.create_user(target['username'], self.pw, '新用户', 'candidate')
            self.assertGreater(new_id, target['id'])
            return result
        with patch('recruitment_insights.application.accounts.hmac.compare_digest', side_effect=replace):
            with self.assertRaisesRegex(ValueError, '账号或密码错误'):
                self.service.login(target['username'], self.pw)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sessions WHERE user_id=?', (target['id'],)).fetchone()[0], 0)

    def test_concurrent_creation_allocates_unique_ids_above_deleted_max(self):
        from concurrent.futures import ThreadPoolExecutor
        target = self.disable('candidate_one')
        self.service.management_delete(self.admin, self.body(target))
        def create(index):
            return self.service.create_user('parallel_'+str(index), self.pw, '并发测试')
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(create, range(4)))
        self.assertEqual(len(set(ids)), 4)
        self.assertTrue(all(ident > target['id'] for ident in ids))

    def test_backup_preserves_id_high_water(self):
        target = self.disable('candidate_one')
        old_id = target['id']
        self.service.management_delete(self.admin, self.body(target))
        backup = Path(self.tmp.name) / 'backup.sqlite3'
        restored = Path(self.tmp.name) / 'restore.sqlite3'
        copy_database(self.path, backup, mode='backup')
        copy_database(backup, restored, mode='restore')
        new_store = Store(restored)
        self.assertGreater(new_store.accounts.create_user('after_restore', self.pw, '普通用户'), old_id)

    def test_creation_at_id_limit_fails_without_changing_sequence_or_users(self):
        with self.store.connect() as db:
            db.execute('UPDATE user_id_sequence SET next_id=? WHERE singleton=1', (2**63 - 1,))
        with self.assertRaisesRegex(ValueError, '上限'):
            self.service.create_user('cannot_create', self.pw, '上限测试')
        self.assertIsNone(self.row('cannot_create'))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT next_id FROM user_id_sequence').fetchone()[0], 2**63 - 1)


class AccountSequenceMigrationTests(unittest.TestCase):
    def test_v5_upgrade_and_failure_are_atomic(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'v5.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
                for version in range(1, 6):
                    database.MIGRATIONS[version](db)
                    db.execute('INSERT INTO schema_migrations(version) VALUES(?)', (version,))
                db.execute("INSERT INTO users(id,username,display_name,password_hash,role) VALUES(99,'old_user','旧用户','hash','candidate')")
                db.commit()
            migration = database.MIGRATIONS[6]
            def fail(db):
                migration(db)
                raise RuntimeError('test')
            with patch.dict(database.MIGRATIONS, {6: fail}), self.assertRaises(RuntimeError):
                Store(path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0], 5)
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='user_id_sequence'").fetchone())
                self.assertEqual(db.execute('SELECT username FROM users').fetchone()[0], 'old_user')
            store = Store(path)
            self.assertGreater(store.accounts.create_user('new_user', secrets.token_urlsafe(24), '新用户'), 99)

    def test_v5_migration_preserves_deleted_account_targets_without_counting_jobs(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'history.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.row_factory = sqlite3.Row
                db.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
                for version in range(1, 6):
                    database.MIGRATIONS[version](db)
                    db.execute('INSERT INTO schema_migrations(version) VALUES(?)', (version,))
                db.execute("INSERT INTO users(id,username,display_name,password_hash,role) VALUES(2,'old_user','旧用户','hash','candidate')")
                db.execute("INSERT INTO audit_events(user_id,action,target,created) VALUES(2,'account_deactivate','3',0)")
                db.execute("INSERT INTO audit_events(user_id,action,target,created) VALUES(2,'manage_job','999999999',0)")
                db.execute("INSERT INTO audit_events(user_id,action,target,created) VALUES(2,'account_edit','not-an-id',0)")
                db.commit()
            store = Store(path)
            self.assertEqual(store.accounts.create_user('new_user', secrets.token_urlsafe(24), '新用户'), 4)

    def test_v5_migration_rejects_account_id_overflow_without_partial_changes(self):
        for target in (str(2**63 - 1), str(2**63), '9'*30):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'overflow.sqlite3'
                with closing(sqlite3.connect(path)) as db:
                    db.row_factory = sqlite3.Row
                    db.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
                    for version in range(1, 6):
                        database.MIGRATIONS[version](db)
                        db.execute('INSERT INTO schema_migrations(version) VALUES(?)', (version,))
                    db.execute("INSERT INTO users(id,username,display_name,password_hash,role) VALUES(1,'old_user','旧用户','hash','candidate')")
                    db.execute("INSERT INTO audit_events(user_id,action,target,created) VALUES(1,'account_deactivate',?,0)", (target,))
                    db.commit()
                with self.assertRaisesRegex(ValueError, '编号'):
                    Store(path)
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0], 5)
                    self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='user_id_sequence'").fetchone())
                    self.assertEqual(db.execute('SELECT target FROM audit_events').fetchone()[0], target)
