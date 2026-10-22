"""Real SQLite races around account password computation and write rechecks."""
import hashlib
import tempfile
import threading
import unittest
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from accounts import Accounts
from core import Store
from recruitment_insights.infrastructure.accounts import AccountRepository


PASSWORD = 'Account-concurrency-2026!'
WAIT = 5
JOIN_WAIT = 35  # SQLite's existing busy timeout is 30 seconds.


class AccountConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='recruitment_insights-account-concurrency-')
        self.workers = []
        self.releases = []
        self.addCleanup(self.cleanup_database)
        self.store = Store(Path(self.temporary.name) / 'isolated.sqlite3')
        self.accounts = Accounts(self.store)
        self.org_a = self.accounts.organization('并发企业 A')
        self.org_b = self.accounts.organization('并发企业 B')
        self.boss = self.create('owner_race', role='boss', org=self.org_a)
        self.candidate = self.create('candidate_race')

    def cleanup_database(self):
        # Release every controlled pause before joining or deleting the database,
        # including assertion, timeout and unexpected application failure paths.
        for release in self.releases:
            release.set()
        for worker in self.workers:
            worker.join(JOIN_WAIT)
        self.assertFalse(any(worker.is_alive() for worker in self.workers),
                         'Account worker did not stop; keep its database until it exits')
        self.temporary.cleanup()

    def create(self, username, *, role='candidate', org=None):
        user_id = self.accounts.create_user(username, PASSWORD, username, role, org)
        with self.store.uow() as tx:
            return tx.accounts.get(user_id)

    def rows(self, sql, args=()):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute(sql, args)]

    def start(self, name, operation):
        result = Future()

        def run():
            try:
                result.set_result(operation())
            except BaseException as exc:
                result.set_exception(exc)

        worker = threading.Thread(name=name, target=run, daemon=True)
        self.workers.append(worker)
        worker.start()
        return result, worker

    def write(self, mutate):
        def operation():
            with self.store.uow(write=True) as tx:
                return mutate(tx.accounts.db)

        pending, _ = self.start('independent-account-writer', operation)
        return pending.result(WAIT)

    @contextmanager
    def paused_hash(self, operation):
        """Observe real UOW lifetimes while pausing, not replacing, PBKDF2."""
        entered = threading.Event()
        release = threading.Event()
        self.releases.append(release)
        observed = {'transactions': [], 'active': 0, 'hashes': []}
        original_uow = self.accounts.uow
        original_hash = hashlib.pbkdf2_hmac
        name = 'paused-password-' + str(len(self.releases))

        @contextmanager
        def tracked_uow(*, write=False):
            with original_uow(write=write) as tx:
                tracked = threading.current_thread().name == name
                if tracked:
                    observed['transactions'].append(write)
                    observed['active'] += 1
                try:
                    yield tx
                finally:
                    if tracked:
                        observed['active'] -= 1

        def real_hash(algorithm, password, salt, iterations, *args, **kwargs):
            if threading.current_thread().name == name:
                observed['hashes'].append((algorithm, iterations, observed['active']))
                entered.set()
                if not release.wait(JOIN_WAIT):
                    raise TimeoutError('Test did not release password computation')
            return original_hash(algorithm, password, salt, iterations, *args, **kwargs)

        with patch.object(self.accounts, 'uow', new=tracked_uow), \
             patch.object(hashlib, 'pbkdf2_hmac', new=real_hash):
            pending, worker = self.start(name, operation)
            try:
                self.assertTrue(entered.wait(WAIT), 'Password computation was not reached')
                self.assertFalse(pending.done())
                self.assertEqual(observed['hashes'], [('sha256', 240000, 0)],
                                 'The real password calculation must run outside every UOW')
                self.assertEqual(observed['transactions'], [False],
                                 'Only the completed read snapshot may precede password work')
                yield pending, release, observed
            finally:
                release.set()
                worker.join(JOIN_WAIT)
                self.assertFalse(worker.is_alive(), 'Paused account operation did not stop')

    def body(self, username='created_hr'):
        return dict(username=username, password=PASSWORD, display_name='新 HR', can_manage=True)

    def test_login_hash_releases_snapshot_and_writer_then_returns_current_identity(self):
        with self.paused_hash(lambda: self.accounts.login('candidate_race', PASSWORD)) as paused:
            pending, release, observed = paused
            self.write(lambda db: db.execute('''UPDATE users SET display_name=?,role='hr',
                org_id=?,can_manage_jobs=1 WHERE id=?''',
                ('最新身份', self.org_b, self.candidate['id'])))
            self.assertFalse(pending.done(), 'Independent write must finish while hashing is paused')
            release.set()
            token, actor = pending.result(WAIT)
        self.assertEqual(observed['transactions'], [False, True])
        self.assertEqual((actor['display_name'], actor['role'], actor['org_id'],
                          actor['org_name'], actor['can_manage_jobs']),
                         ('最新身份', 'hr', self.org_b, '并发企业 B', 1))
        self.assertEqual(self.accounts.user(token), actor)
        self.assertEqual(len(self.rows('SELECT * FROM sessions')), 1)

    def test_login_password_change_identity_replacement_and_deletion_fail_closed(self):
        changed_hash = Accounts._user_values('replacement_hash', 'Changed-password-2026!',
                                            'replacement', 'candidate', None, False)['password_hash']
        for change in ('password', 'identity', 'delete'):
            with self.subTest(change=change):
                original = self.create('login_' + change)

                def mutate(db):
                    if change == 'password':
                        db.execute('UPDATE users SET password_hash=? WHERE id=?',
                                   (changed_hash, original['id']))
                    elif change == 'identity':
                        db.execute('DELETE FROM users WHERE id=?', (original['id'],))
                        # Identical username and credential, but a distinct identity.
                        db.execute('''INSERT INTO users(id,username,display_name,password_hash,
                            role,org_id,can_manage_jobs) VALUES(?,?,?,?,?,?,?)''',
                            (original['id'] + 100, original['username'], original['display_name'],
                             original['password_hash'], 'candidate', None, 0))
                    else:
                        db.execute('DELETE FROM users WHERE id=?', (original['id'],))

                with self.paused_hash(lambda: self.accounts.login(original['username'], PASSWORD)) as paused:
                    pending, release, observed = paused
                    self.write(mutate)
                    release.set()
                    with self.assertRaises(ValueError):
                        pending.result(WAIT)
                self.assertEqual(observed['transactions'], [False, True])
                self.assertEqual(self.rows('SELECT * FROM sessions'), [])

    def test_wrong_and_missing_accounts_keep_real_hashing_outside_transactions(self):
        for username in ('candidate_race', 'missing_account'):
            with self.subTest(username=username):
                with self.paused_hash(lambda: self.accounts.login(username, 'Wrong-password-2026!')) as paused:
                    pending, release, observed = paused
                    self.write(lambda db: db.execute('UPDATE organizations SET name=name WHERE id=?',
                                                     (self.org_a,)))
                    release.set()
                    with self.assertRaises(ValueError):
                        pending.result(WAIT)
                self.assertEqual(observed['transactions'], [False])
                self.assertEqual(self.rows('SELECT * FROM sessions'), [])

    def test_hr_hash_releases_snapshot_and_writer_then_creates_with_atomic_audit(self):
        with self.paused_hash(lambda: self.accounts.create_hr(self.boss, self.body())) as paused:
            pending, release, observed = paused
            self.write(lambda db: db.execute('UPDATE users SET display_name=? WHERE id=?',
                                             ('更新后的负责人', self.boss['id'])))
            self.assertFalse(pending.done(), 'Independent write must finish while hashing is paused')
            release.set()
            user_id = pending.result(WAIT)
        self.assertEqual(observed['transactions'], [False, True])
        with self.store.uow() as tx:
            created = tx.accounts.get(user_id)
        self.assertEqual((created['role'], created['org_id'], created['can_manage_jobs']),
                         ('hr', self.org_a, 1))
        event = self.rows('SELECT * FROM audit_events')[0]
        self.assertEqual((event['user_id'], event['org_id'], event['action'], event['target']),
                         (self.boss['id'], self.org_a, 'create_hr', str(user_id)))
        # A successful independent login proves the paused wrapper retained the
        # actual salted password calculation instead of substituting a digest.
        self.assertEqual(self.accounts.login('created_hr', PASSWORD)[1]['id'], user_id)

    def test_hr_creation_rejects_revocation_organization_change_and_owner_deletion(self):
        for change in ('role', 'organization', 'delete'):
            with self.subTest(change=change):
                boss = self.create('owner_' + change, role='boss', org=self.org_a)
                username = 'hr_' + change

                def mutate(db):
                    if change == 'role':
                        db.execute("UPDATE users SET role='hr' WHERE id=?", (boss['id'],))
                    elif change == 'organization':
                        db.execute('UPDATE users SET org_id=? WHERE id=?', (self.org_b, boss['id']))
                    else:
                        db.execute('DELETE FROM users WHERE id=?', (boss['id'],))

                with self.paused_hash(lambda: self.accounts.create_hr(boss, self.body(username))) as paused:
                    pending, release, observed = paused
                    self.write(mutate)
                    release.set()
                    with self.assertRaises(PermissionError):
                        pending.result(WAIT)
                self.assertEqual(observed['transactions'], [False, True])
                self.assertEqual(self.rows('SELECT * FROM users WHERE username=?', (username,)), [])
                self.assertEqual(self.rows('SELECT * FROM audit_events'), [])

    def test_hr_username_races_with_registration_and_another_hr_create(self):
        for rival in ('registration', 'hr'):
            with self.subTest(rival=rival):
                username = 'shared_' + rival
                before_events = len(self.rows('SELECT * FROM audit_events'))
                with self.paused_hash(lambda: self.accounts.create_hr(self.boss, self.body(username))) as paused:
                    pending, release, observed = paused
                    operation = (lambda: self.accounts.create_user(username, PASSWORD, '注册用户')) \
                        if rival == 'registration' else \
                        (lambda: self.accounts.create_hr(self.boss, self.body(username)))
                    winner, _ = self.start('competing-' + rival, operation)
                    winner_id = winner.result(WAIT)
                    self.assertFalse(pending.done())
                    release.set()
                    with self.assertRaises(ValueError):
                        pending.result(WAIT)
                self.assertEqual(observed['transactions'], [False, True])
                users = self.rows('SELECT * FROM users WHERE username=?', (username,))
                self.assertEqual(len(users), 1)
                self.assertEqual((users[0]['id'], users[0]['role']),
                                 (winner_id, 'candidate' if rival == 'registration' else 'hr'))
                self.assertEqual(len(self.rows('SELECT * FROM audit_events')),
                                 before_events + int(rival == 'hr'))

    def test_login_session_failure_rolls_back_expired_session_cleanup(self):
        with self.store.uow(write=True) as tx:
            tx.accounts.create_session('expired-sentinel', self.candidate['id'], 0)
        before = self.rows('SELECT * FROM sessions')
        with patch.object(AccountRepository, 'create_session', side_effect=RuntimeError('session storage failed')):
            with self.assertRaisesRegex(RuntimeError, 'session storage failed'):
                self.accounts.login('candidate_race', PASSWORD)
        self.assertEqual(self.rows('SELECT * FROM sessions'), before)


if __name__ == '__main__':
    unittest.main()
