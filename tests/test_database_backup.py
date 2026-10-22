"""Real SQLite backup/recovery; every database and subprocess is isolated."""
import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core import Store
from recruitment_insights.infrastructure.database import REQUIRED_COLUMNS
from scripts import database_backup as backup


PASSWORD = 'private-backup-fixture-2026!'


class DatabaseBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='recruitment-backup-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source.sqlite3'
        self.target = self.root / 'backup.sqlite3'
        self.store = Store(self.source)

    @staticmethod
    def file_hash(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def rows(path):
        # Independent record comparison, including hashes, sessions and audits.
        with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            result = {}
            for table in sorted(set(REQUIRED_COLUMNS) | {'schema_migrations'}):
                rows = db.execute('SELECT * FROM ' + table).fetchall()
                result[table] = sorted(rows, key=repr)
            return result

    def assert_no_temporary(self, directory=None):
        self.assertEqual(list((directory or self.root).glob('.*.tmp*')), [])

    def populate(self):
        accounts = self.store.accounts
        org_a = accounts.organization('备份隔离企业 A')
        org_b = accounts.organization('备份隔离企业 B')
        definitions = (
            ('bk_admin', 'admin', None, False), ('bk_boss_a', 'boss', org_a, False),
            ('bk_boss_b', 'boss', org_b, False), ('bk_hr_a', 'hr', org_a, True),
            ('bk_candidate_a', 'candidate', None, False), ('bk_candidate_b', 'candidate', None, False),
        )
        self.tokens, self.actors = {}, {}
        for username, role, org_id, allowed in definitions:
            accounts.create_user(username, PASSWORD + username, username, role, org_id, allowed)
            self.tokens[username], self.actors[username] = accounts.login(username, PASSWORD + username)
        raw = dict(job_id='left', title='备份 Python 岗位', company='公开快照样本', location='杭州',
                   description='负责 Python 接口开发，要求本科及以上学历。', education='本科', experience='3年',
                   salary_min='10000', salary_max='20000', currency='CNY', salary_period='month')
        self.store.import_records([raw, dict(raw, job_id='right')], 'test:backup-public', '备份样本')
        self.store.import_records([dict(raw, salary_max='22000')], 'test:backup-public', '保留内容版本')
        with self.store.connect() as db:
            left_id, right_id = [row[0] for row in db.execute('SELECT id FROM jobs ORDER BY id')]
        self.left_id, self.right_id = left_id, right_id
        admin = self.actors['bk_admin']
        field = self.store.detail(left_id, admin)['requirements']['education']
        self.store.review_field(admin, dict(job_id=left_id, field='education', action='confirm',
            source_hash=field['source_hash'], version=field['version'], value='本科',
            condition='硬性要求', evidence='本科及以上学历', alternatives=[]))
        pair = self.store.quality({'dataset': 'real'}, admin)['duplicates'][0]
        self.store.decide_duplicate(admin, dict(left_id=pair['left']['id'], right_id=pair['right']['id'],
            left_hash=pair['left_hash'], right_hash=pair['right_hash'], version=pair['version'],
            decision='same', reason='备份验收人工判断'))
        accounts.save_favorite(self.actors['bk_candidate_a'], left_id, '仅 A 可见的恢复备注')
        accounts.save_favorite(self.actors['bk_candidate_b'], right_id, '仅 B 可见的恢复备注')
        self.store.set_lifecycle(admin, dict(id=left_id, action='deactivate', reason='快照保留下架原因',
            expected_edit_token=self.store.detail(left_id, admin)['edit_token']))
        saved = self.store.manage_job(self.actors['bk_boss_a'], dict(
            record=dict(title='企业 A 私有恢复岗位', location='杭州', description='Python 接口开发'),
            published=False, is_demo=False))
        self.private_id = saved['id']
        accounts.grant(self.actors['bk_boss_a'], self.actors['bk_hr_a']['id'], False)

    def test_round_trip_preserves_all_records_roles_sessions_reviews_and_privacy(self):
        self.populate()
        original = self.rows(self.source)
        source_hash = self.file_hash(self.source)
        manifest = backup.copy_database(self.source, self.target)
        self.assertEqual(self.rows(self.target), original)
        self.assertEqual(self.file_hash(self.source), source_hash)
        self.assertEqual(manifest['file_sha256'], self.file_hash(self.target))
        self.assertEqual({name: item['rows'] for name, item in manifest['tables'].items()},
                         {name: len(rows) for name, rows in original.items()})
        restored_path = self.root / 'restored' / 'runtime.sqlite3'
        restored_manifest = backup.copy_database(self.target, restored_path, mode='restore')
        self.assertEqual(restored_manifest['tables'], manifest['tables'])
        self.assertEqual(restored_manifest['schema_sha256'], manifest['schema_sha256'])
        self.assertEqual(self.rows(restored_path), original)
        self.assertEqual(self.rows(self.target), original)
        self.assertEqual(self.rows(self.source), original)
        self.assert_no_temporary()
        self.assert_no_temporary(restored_path.parent)
        restored = Store(restored_path)
        actors = {name: restored.accounts.user(token) for name, token in self.tokens.items()}
        self.assertEqual({actor['role'] for actor in actors.values()}, {'admin', 'boss', 'hr', 'candidate'})
        for name, actor in actors.items():
            self.assertEqual(actor['id'], self.actors[name]['id'])
        self.assertFalse(actors['bk_hr_a']['can_manage_jobs'])
        with self.assertRaises(PermissionError):
            restored.manage_job(actors['bk_hr_a'], {'record': {}})
        self.assertIsNone(restored.detail(self.private_id, actors['bk_boss_b']))
        self.assertIsNone(restored.detail(self.private_id, actors['bk_candidate_a']))
        self.assertIsNotNone(restored.detail(self.private_id, actors['bk_boss_a']))
        detail = restored.detail(self.left_id, actors['bk_admin'])
        self.assertTrue(detail['manual_inactive'])
        self.assertEqual(detail['lifecycle_reason'], '快照保留下架原因')
        self.assertEqual(detail['requirements']['education']['status'], 'confirmed')
        self.assertGreaterEqual(len(detail['revisions']), 2)
        self.assertEqual(restored.accounts.favorites(actors['bk_candidate_a']), [])
        self.assertEqual(restored.accounts.favorites(actors['bk_candidate_b'])[0]['note'], '仅 B 可见的恢复备注')
        restored.set_lifecycle(actors['bk_admin'], dict(id=self.left_id, action='restore',
            expected_edit_token=detail['edit_token']))
        self.assertEqual(restored.accounts.favorites(actors['bk_candidate_a'])[0]['note'], '仅 A 可见的恢复备注')
        restored.accounts.logout(self.tokens['bk_candidate_a'])
        self.assertIsNone(restored.accounts.user(self.tokens['bk_candidate_a']))
        self.assertIsNotNone(self.store.accounts.user(self.tokens['bk_candidate_a']))

    def test_committed_wal_not_checkpointed_is_included(self):
        with self.store.connect() as db:
            db.execute('INSERT INTO organizations(name) VALUES(?)', ('已提交但仅存在 WAL',))
            db.commit()
            self.assertGreater(Path(str(self.source) + '-wal').stat().st_size, 0)
            before_main = self.file_hash(self.source)
            manifest = backup.copy_database(self.source, self.target)
            self.assertEqual(self.rows(self.target)['organizations'][0][1], '已提交但仅存在 WAL')
            self.assertEqual(manifest['tables']['organizations']['rows'], 1)
            self.assertEqual(self.file_hash(self.source), before_main)

    def test_transaction_in_progress_never_produces_half_transaction(self):
        with self.store.connect() as db:
            db.executemany('INSERT INTO organizations(name) VALUES(?)', [('before-A',), ('before-B',)])
        with self.store.connect() as writer:
            writer.execute('BEGIN IMMEDIATE')
            writer.execute("UPDATE organizations SET name='after-A' WHERE id=1")
            backup.copy_database(self.source, self.target)
            self.assertEqual([row[1] for row in self.rows(self.target)['organizations']], ['before-A', 'before-B'])
            writer.execute("UPDATE organizations SET name='after-B' WHERE id=2")
            writer.commit()
            second = self.root / 'after.sqlite3'
            backup.copy_database(self.source, second)
            self.assertEqual([row[1] for row in self.rows(second)['organizations']], ['after-A', 'after-B'])

    def test_commit_during_copy_does_not_change_pinned_snapshot(self):
        with self.store.connect() as db:
            db.executemany('INSERT INTO organizations(name) VALUES(?)',
                           [(str(index) + '-' + '填充' * 1000,) for index in range(400)])
        committed = []
        with self.store.connect() as writer:
            def progress(status, remaining, total):
                if not committed:
                    self.assertGreater(remaining, 0, 'Fixture must require multiple backup batches')
                    writer.execute("UPDATE organizations SET name='concurrent-commit' WHERE id=1")
                    writer.commit()
                    committed.append(True)
            manifest = backup.copy_database(self.source, self.target, progress=progress)
        self.assertEqual(committed, [True])
        self.assertTrue(self.rows(self.target)['organizations'][0][1].startswith('0-'))
        self.assertEqual(manifest['tables']['organizations']['rows'], 400)
        later = self.root / 'later.sqlite3'
        backup.copy_database(self.source, later)
        self.assertEqual(self.rows(later)['organizations'][0][1], 'concurrent-commit')

    def test_backup_interruption_cleans_only_its_temporary_file(self):
        marker = self.root / 'unrelated.tmp'
        marker.write_bytes(b'keep this file')
        before_main = self.file_hash(self.source)
        def interrupt(*args):
            raise KeyboardInterrupt('simulated backup interruption')
        with self.assertRaises(KeyboardInterrupt):
            backup.copy_database(self.source, self.target, progress=interrupt)
        self.assertFalse(self.target.exists())
        self.assertEqual(marker.read_bytes(), b'keep this file')
        self.assertEqual(self.file_hash(self.source), before_main)
        self.assert_no_temporary()

    def test_timeout_during_progress_does_not_publish_target(self):
        with self.assertRaises(TimeoutError):
            backup.copy_database(self.source, self.target, timeout=0.03,
                                 progress=lambda *args: time.sleep(0.06))
        self.assertFalse(self.target.exists())
        self.assert_no_temporary()

    def test_exclusive_source_lock_has_bounded_failure_and_cleans_temp(self):
        with self.store.connect() as writer:
            writer.execute('PRAGMA journal_mode=DELETE')
            writer.execute('BEGIN EXCLUSIVE')
            started = time.monotonic()
            with self.assertRaises((ValueError, TimeoutError)):
                backup.copy_database(self.source, self.target, timeout=0.1)
            self.assertLess(time.monotonic() - started, 2)
            writer.rollback()
        self.assertFalse(self.target.exists())
        self.assert_no_temporary()

    def test_corrupt_unknown_and_wrong_version_sources_are_rejected(self):
        cases = ('corrupt', 'unknown', 'version', 'view')
        for kind in cases:
            with self.subTest(kind=kind):
                source = self.root / (kind + '.sqlite3')
                if kind == 'corrupt':
                    source.write_bytes(b'not a SQLite database')
                elif kind == 'unknown':
                    with contextlib.closing(sqlite3.connect(source)) as db, db:
                        db.execute('CREATE TABLE unknown(value TEXT)')
                else:
                    store = Store(source)
                    with store.connect() as db:
                        if kind == 'version':
                            db.execute('DELETE FROM schema_migrations WHERE version=2')
                        else:
                            db.execute('CREATE VIEW misleading AS SELECT * FROM users')
                target = self.root / (kind + '-restored.sqlite3')
                source_hash = self.file_hash(source)
                with self.assertRaises(ValueError):
                    backup.copy_database(source, target, mode='restore')
                self.assertFalse(target.exists())
                self.assertEqual(self.file_hash(source), source_hash)
                self.assert_no_temporary()

    def test_foreign_key_corruption_is_rejected_without_source_migration(self):
        with contextlib.closing(sqlite3.connect(self.source)) as db, db:
            db.execute('INSERT INTO favorites VALUES(?,?,?)', (123, 456, 'broken reference'))
        source_hash = self.file_hash(self.source)
        with self.assertRaises(ValueError):
            backup.copy_database(self.source, self.target, mode='restore')
        self.assertEqual(self.file_hash(self.source), source_hash)
        self.assertFalse(self.target.exists())
        self.assert_no_temporary()

    def test_existing_target_is_never_replaced_even_for_bad_source(self):
        self.target.write_bytes(b'existing target must survive')
        corrupt = self.root / 'corrupt.sqlite3'
        corrupt.write_bytes(b'corrupt')
        for source in (self.source, corrupt):
            for mode in ('backup', 'restore'):
                with self.subTest(source=source.name, mode=mode), self.assertRaises(ValueError):
                    backup.copy_database(source, self.target, mode=mode)
                self.assertEqual(self.target.read_bytes(), b'existing target must survive')
        self.assert_no_temporary()

    def test_successful_backup_and_restore_cannot_be_repeated_on_existing_target(self):
        backup.copy_database(self.source, self.target)
        restored = self.root / 'restore.sqlite3'
        backup.copy_database(self.target, restored, mode='restore')
        for source, target, mode in ((self.source, self.target, 'backup'), (self.target, restored, 'restore')):
            before = self.file_hash(target)
            with self.assertRaises(ValueError):
                backup.copy_database(source, target, mode=mode)
            self.assertEqual(self.file_hash(target), before)

    def test_publication_race_never_overwrites_newly_created_target(self):
        link = os.link
        def racing_link(source, target):
            Path(target).write_bytes(b'concurrent target contents')
            return link(source, target)
        with patch.object(backup.os, 'link', side_effect=racing_link):
            with self.assertRaises(FileExistsError):
                backup.copy_database(self.source, self.target)
        self.assertEqual(self.target.read_bytes(), b'concurrent target contents')
        self.assert_no_temporary()

    def test_same_file_relative_missing_and_protected_targets_are_rejected(self):
        before = self.file_hash(self.source)
        with patch.object(backup, 'ROOT', self.root):
            cases = (
                (self.source, self.source), (self.source, Path('relative.sqlite3')),
                (Path('relative.sqlite3'), self.target), (self.root / 'missing.sqlite3', self.target),
                (self.source, self.root / 'data/recruitment.sqlite3'),
                (self.source, self.root / 'deliverables/team-handoff/04_组员测试版/data/recruitment.sqlite3'),
            )
            for source, target in cases:
                with self.subTest(source=source, target=target), self.assertRaises(ValueError):
                    backup.copy_database(source, target)
        self.assertEqual(self.file_hash(self.source), before)
        self.assertFalse(self.target.exists())

    def test_existing_target_sidecars_are_preserved_and_block_publication(self):
        for suffix in backup.SIDECARS:
            path = Path(str(self.target) + suffix)
            path.write_bytes(b'keep target sidecar')
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                backup.copy_database(self.source, self.target)
            self.assertEqual(path.read_bytes(), b'keep target sidecar')
            path.unlink()
        self.assertFalse(self.target.exists())

    def test_source_sidecar_names_are_reserved_even_when_absent(self):
        with self.store.connect() as db:
            db.execute('PRAGMA journal_mode=DELETE')
        before = self.file_hash(self.source)
        for suffix in backup.SIDECARS:
            target = Path(str(self.source) + suffix)
            for mode in ('backup', 'restore'):
                with self.subTest(suffix=suffix, mode=mode):
                    try:
                        with self.assertRaises(ValueError):
                            backup.copy_database(self.source, target, mode=mode)
                        self.assertFalse(target.exists())
                        self.assertEqual(self.file_hash(self.source), before)
                    finally:
                        # A failing regression may publish this isolated fixture
                        # name. Remove only that exact path owned by this test.
                        target.unlink(missing_ok=True)
        self.assert_no_temporary()

    def test_protected_database_sidecar_names_are_reserved_without_creating_parents(self):
        before = self.file_hash(self.source)
        protected = (self.root / 'data/recruitment.sqlite3',
                     self.root / 'deliverables/team-handoff/04_组员测试版/data/recruitment.sqlite3')
        with patch.object(backup, 'ROOT', self.root):
            for database in protected:
                for suffix in backup.SIDECARS:
                    target = Path(str(database) + suffix)
                    for mode in ('backup', 'restore'):
                        with self.subTest(database=database, suffix=suffix, mode=mode):
                            try:
                                with self.assertRaises(ValueError):
                                    backup.copy_database(self.source, target, mode=mode)
                                self.assertFalse(target.parent.exists())
                                self.assertEqual(self.file_hash(self.source), before)
                            finally:
                                target.unlink(missing_ok=True)

    def test_current_column_names_do_not_substitute_for_real_schema_constraints(self):
        variations = (
            ('primary-key', 'sessions', 'token_hash TEXT PRIMARY KEY', 'token_hash TEXT'),
            ('foreign-key', 'sessions', ' REFERENCES users(id)', ''),
            ('unique', 'organizations', ' NOT NULL UNIQUE', ' NOT NULL'),
            ('not-null', 'sessions', 'expires REAL NOT NULL', 'expires REAL'),
            ('type', 'sessions', 'expires REAL NOT NULL', 'expires TEXT NOT NULL'),
        )
        with contextlib.closing(sqlite3.connect(self.source.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            definitions = db.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL "
                                     "AND name NOT LIKE 'sqlite_%' ORDER BY type DESC,name").fetchall()
            versions = db.execute('SELECT * FROM schema_migrations').fetchall()
        for kind, altered_table, old, new in variations:
            source = self.root / (kind + '.sqlite3')
            with contextlib.closing(sqlite3.connect(source)) as db, db:
                for table, definition in definitions:
                    if table == altered_table:
                        self.assertIn(old, definition)
                        definition = definition.replace(old, new)
                    db.execute(definition)
                db.executemany('INSERT INTO schema_migrations VALUES(?,?)', versions)
                if kind == 'foreign-key':
                    db.execute('INSERT INTO sessions VALUES(?,?,?)', ('orphan-fixture-token', 999, 2000000000))
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            before = self.file_hash(source)
            for mode in ('backup', 'restore'):
                target = self.root / (kind + '-' + mode + '.sqlite3')
                with self.subTest(kind=kind, mode=mode):
                    with self.assertRaises(ValueError):
                        backup.copy_database(source, target, mode=mode)
                    self.assertFalse(target.exists())
                    self.assertEqual(self.file_hash(source), before)
                    self.assert_no_temporary()

    def test_missing_checks_reject_invalid_lifecycle_review_and_duplicate_records(self):
        self.populate()
        original = self.rows(self.source)
        with contextlib.closing(sqlite3.connect(self.source.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            definitions = db.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL "
                                     "AND name NOT LIKE 'sqlite_%' ORDER BY type DESC,name").fetchall()
        variations = (
            ('source-active', 'jobs', ' CHECK(source_active IN (0,1))',
             'UPDATE jobs SET source_active=2'),
            ('manual-inactive', 'jobs', ' CHECK(manual_inactive IN (0,1))',
             'UPDATE jobs SET manual_inactive=2'),
            ('field-review', 'field_reviews', ", CHECK(field IN ('education','experience'))",
             "UPDATE field_reviews SET field='unknown-fixture-field'"),
            ('duplicate-order', 'duplicate_decisions', ', CHECK(left_id<right_id)',
             'UPDATE duplicate_decisions SET right_id=left_id'),
        )
        for kind, altered_table, removed, mutation in variations:
            source = self.root / (kind + '.sqlite3')
            with contextlib.closing(sqlite3.connect(source)) as db, db:
                for table, definition in definitions:
                    if table == altered_table:
                        self.assertIn(removed, definition)
                        definition = definition.replace(removed, '')
                    db.execute(definition)
                for table, rows in original.items():
                    if rows:
                        placeholders = ','.join('?' for value in rows[0])
                        db.executemany('INSERT INTO ' + table + ' VALUES(' + placeholders + ')', rows)
                db.execute(mutation)
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            before = self.file_hash(source)
            for mode in ('backup', 'restore'):
                target = self.root / (kind + '-' + mode + '.sqlite3')
                with self.subTest(kind=kind, mode=mode):
                    with self.assertRaises(ValueError):
                        backup.copy_database(source, target, mode=mode)
                    self.assertFalse(target.exists())
                    self.assertEqual(self.file_hash(source), before)
                    self.assert_no_temporary()

    def test_missing_current_named_index_is_rejected_without_changing_source(self):
        with self.store.connect() as db:
            db.execute('DROP INDEX jobs_source_active')
            db.execute('PRAGMA journal_mode=DELETE')
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())
        before = self.file_hash(self.source)
        for mode in ('backup', 'restore'):
            target = self.root / (mode + '-missing-index.sqlite3')
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError):
                    backup.copy_database(self.source, target, mode=mode)
                self.assertFalse(target.exists())
                self.assertEqual(self.file_hash(self.source), before)
                self.assert_no_temporary()

    def test_restore_rejects_live_wal_and_journal_sources(self):
        with self.store.connect() as writer:
            writer.execute('INSERT INTO organizations(name) VALUES(?)', ('live WAL',))
            writer.commit()
            with self.assertRaises(ValueError):
                backup.copy_database(self.source, self.target, mode='restore')
        with self.store.connect() as writer:
            writer.execute('PRAGMA journal_mode=DELETE')
            writer.execute('BEGIN IMMEDIATE')
            writer.execute("UPDATE organizations SET name='uncommitted'")
            for mode in ('backup', 'restore'):
                with self.subTest(mode=mode), self.assertRaises(ValueError):
                    backup.copy_database(self.source, self.target, mode=mode)
            writer.rollback()
        self.assertFalse(self.target.exists())

    def test_source_hardlinks_and_target_hardlinks_are_rejected(self):
        alias = self.root / 'source-alias.sqlite3'
        os.link(self.source, alias)
        for source in (self.source, alias):
            with self.subTest(source=source), self.assertRaises(ValueError):
                backup.copy_database(source, self.target)
        alias.unlink()
        self.target.write_bytes(b'keep linked target')
        os.link(self.target, self.root / 'target-alias.sqlite3')
        with self.assertRaises(ValueError):
            backup.copy_database(self.source, self.target)
        self.assertEqual(self.target.read_bytes(), b'keep linked target')

    def test_symbolic_file_parent_and_sidecar_links_are_rejected(self):
        alias = self.root / 'source-symlink.sqlite3'
        directory = self.root / 'real-directory'
        directory.mkdir()
        linked = self.root / 'linked-directory'
        try:
            os.symlink(self.source, alias)
            os.symlink(directory, linked, target_is_directory=True)
        except OSError as exc:
            self.skipTest('This account cannot create symbolic links: ' + str(exc))
        for source, target in ((alias, self.target), (self.source, linked / 'fresh.sqlite3')):
            with self.subTest(source=source, target=target), self.assertRaises(ValueError):
                backup.copy_database(source, target)
        sidecar = Path(str(self.source) + '-wal')
        os.symlink(self.source, sidecar)
        with self.assertRaises(ValueError):
            backup.copy_database(self.source, self.target)
        self.assertFalse(self.target.exists())

    def test_invalid_timeout_rejected_without_output(self):
        for timeout in (0, -1, True, float('nan'), float('inf'), '3'):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                backup.copy_database(self.source, self.target, timeout=timeout)
        self.assertFalse(self.target.exists())

    def test_real_cli_backup_restore_from_other_directory_emits_verified_manifest(self):
        script = str(Path(backup.__file__).resolve())
        backup_result = subprocess.run(
            [sys.executable, '-X', 'utf8', script, 'backup', '--source', str(self.source), '--target', str(self.target)],
            cwd=self.root, capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(backup_result.returncode, 0, backup_result.stderr)
        manifest = json.loads(backup_result.stdout)
        self.assertEqual(manifest['file_sha256'], self.file_hash(self.target))
        restored = self.root / 'new-directory' / 'restored.sqlite3'
        restore_result = subprocess.run(
            [sys.executable, '-X', 'utf8', script, 'restore', '--source', str(self.target), '--target', str(restored)],
            cwd=self.root, capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(restore_result.returncode, 0, restore_result.stderr)
        self.assertEqual(json.loads(restore_result.stdout)['tables'], manifest['tables'])
        self.assertEqual(self.rows(restored), self.rows(self.target))


if __name__ == '__main__':
    unittest.main()
