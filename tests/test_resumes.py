"""Private candidate resume HTTP, migration and backup contracts.

Every account/file is synthetic and every database is temporary. A fixed
Host/Origin represents the external HTTPS proxy; no public TLS or real resume
is used. Runtime listeners are only ephemeral loopback ports created here.
"""
import base64
import hashlib
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import unquote
from unittest.mock import patch

from core import Store, normalize
from recruitment_insights.application.accounts import Accounts
from recruitment_insights.config import INVITE_SESSION_COOKIE, WebPolicy
from recruitment_insights.infrastructure.database import MIGRATIONS
from recruitment_insights.infrastructure.resumes import ResumeRepository
from recruitment_insights.web.server import make_server
from scripts.database_backup import copy_database
from resume_fixtures import make_docx, make_pdf


ORIGIN = 'https://resume.example.test'
HOST = 'resume.example.test'
PASSWORD = 'Synthetic-resume-fixture-2026!'
MAX_BYTES = 2 * 1024 * 1024
WAIT = 10


class ResumeHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='resume-http-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.store = Store(cls.root / 'resumes.sqlite3')
        organization = cls.store.accounts.organization('合成简历测试企业')
        cls.tokens, cls.actors = {}, {}
        for username, role, owner in (
                ('candidate_a', 'candidate', None), ('candidate_b', 'candidate', None),
                ('resume_admin', 'admin', None), ('resume_boss', 'boss', organization),
                ('resume_hr', 'hr', organization)):
            cls.store.accounts.create_user(username, PASSWORD, username, role, owner, role == 'hr')
            cls.tokens[username], cls.actors[username] = cls.store.accounts.login(username, PASSWORD)
        cls.server, cls.listener = cls.start_server(cls.store)
        cls.addClassCleanup(cls.stop_server, cls.server, cls.listener)

    @staticmethod
    def start_server(store):
        server = make_server(store, 0, policy=WebPolicy(test_origin=ORIGIN))
        listener = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01),
                                    name='synthetic-resume-http', daemon=True)
        listener.start()
        return server, listener

    @staticmethod
    def stop_server(server, listener):
        server.shutdown()
        server.server_close()
        listener.join(WAIT)
        if listener.is_alive():
            raise AssertionError('Synthetic resume listener did not stop')

    def setUp(self):
        with self.store.connect() as database:
            database.execute('DELETE FROM resumes')

    def request(self, path='/api/resume', *, body=None, user='candidate_a',
                host=HOST, origin=ORIGIN, marker=True, server=None, raw=None):
        server = server or self.server
        connection = HTTPConnection('127.0.0.1', server.server_port, timeout=WAIT)
        payload = (json.dumps(body, ensure_ascii=False).encode('utf-8')
                   if body is not None else raw)
        headers = {'Host': host}
        if user is not None:
            headers['Cookie'] = INVITE_SESSION_COOKIE + '=' + self.tokens[user]
        if payload is not None:
            headers['Content-Type'] = 'application/json'
            if marker:
                headers['X-Local-Request'] = '1'
            if origin is not None:
                headers['Origin'] = origin
        try:
            connection.request('POST' if payload is not None else 'GET', path,
                               body=payload, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def upload(self, filename='合成测试简历.pdf', content=None, *, user='candidate_a', **extra):
        content = make_pdf() if content is None else content
        body = dict(filename=filename, content_base64=base64.b64encode(content).decode('ascii'))
        body.update(extra)
        return self.request(body=body, user=user)

    def metadata(self, *, user='candidate_a', server=None):
        status, headers, payload = self.request(user=user, server=server)
        self.assertEqual(status, 200, payload)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        result = json.loads(payload)
        self.assertEqual(set(result), {'resume', 'max_bytes', 'allowed_extensions'})
        self.assertEqual(result['max_bytes'], MAX_BYTES)
        self.assertEqual(result['allowed_extensions'], ['.pdf', '.docx'])
        return result

    def assert_download(self, content, *, user='candidate_a', server=None, path='/api/resume/download'):
        status, headers, downloaded = self.request(path, user=user, server=server)
        self.assertEqual(status, 200, downloaded[:100])
        self.assertEqual(hashlib.sha256(downloaded).digest(), hashlib.sha256(content).digest())
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn('attachment;', headers['Content-Disposition'])
        return headers

    def test_empty_metadata_and_missing_download_are_explicit(self):
        self.assertIsNone(self.metadata()['resume'])
        self.assertEqual(self.request('/api/resume/download')[0], 404)

    def test_pdf_upload_metadata_and_unicode_attachment_preserve_original_bytes(self):
        content, filename = make_pdf(), '张三 合成简历.pdf'
        status, _, payload = self.upload(filename, content)
        self.assertEqual(status, 200, payload)
        result = json.loads(payload)
        self.assertEqual(result, self.metadata())
        metadata = result['resume']
        self.assertEqual(set(metadata), {'filename', 'media_type', 'size_bytes', 'uploaded_at'})
        self.assertEqual(metadata['filename'], filename)
        self.assertEqual(metadata['media_type'], 'application/pdf')
        self.assertEqual(metadata['size_bytes'], len(content))
        self.assertTrue(metadata['uploaded_at'])
        self.assertNotIn('content_base64', payload.decode('utf-8'))
        headers = self.assert_download(content)
        self.assertEqual(headers['Content-Type'], 'application/pdf')
        disposition = headers['Content-Disposition']
        self.assertIn("filename*=UTF-8''", disposition)
        self.assertEqual(unquote(disposition.split("filename*=UTF-8''", 1)[1]), filename)

    def test_docx_round_trip_and_store_reopen_keep_file_and_existing_session(self):
        content = make_docx()
        status, _, payload = self.upload('合成测试简历.docx', content)
        self.assertEqual(status, 200, payload)
        expected = self.metadata()
        self.assertEqual(expected['resume']['media_type'],
                         'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
        reopened = Store(self.store.path)
        server, listener = self.start_server(reopened)
        try:
            self.assertEqual(self.metadata(server=server), expected)
            headers = self.assert_download(content, server=server)
            self.assertEqual(headers['Content-Type'], expected['resume']['media_type'])
            self.assertIsNone(self.metadata(user='candidate_b', server=server)['resume'])
            self.assertEqual(self.request('/api/resume/download', user='candidate_b', server=server)[0], 404)
        finally:
            self.stop_server(server, listener)

    def test_valid_replacement_keeps_only_one_resume_per_account(self):
        self.assertEqual(self.upload()[0], 200)
        replacement = make_docx()
        status, _, payload = self.upload('更新后的合成简历.docx', replacement)
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.metadata()['resume']['filename'], '更新后的合成简历.docx')
        self.assert_download(replacement)
        with self.store.connect() as database:
            self.assertEqual(database.execute('SELECT COUNT(*) FROM resumes').fetchone()[0], 1)

    def test_storage_failure_rolls_back_a_replacement_instead_of_losing_original(self):
        original = make_pdf()
        self.assertEqual(self.upload(content=original)[0], 200)
        before = self.metadata()
        original_save = ResumeRepository.save

        def fail_after_write(repository, user_id, values):
            original_save(repository, user_id, values)
            raise sqlite3.OperationalError('synthetic write failure before commit')

        with patch.object(ResumeRepository, 'save', fail_after_write), \
                self.assertLogs('recruitment', level='ERROR'):
            status, _, payload = self.upload('未提交的替换.docx', make_docx())
        self.assertEqual(status, 500, payload)
        self.assertEqual(self.metadata(), before)
        self.assert_download(original)

    def test_invalid_actual_formats_do_not_overwrite_saved_resume(self):
        original = make_pdf()
        self.assertEqual(self.upload(content=original)[0], 200)
        before = self.metadata()
        for filename, content in (('forged.pdf', b'%PDF-1.7\nnot a document\n%%EOF'),
                                  ('forged.docx', b'PK\x03\x04not a document'),
                                  ('mismatch.docx', original),
                                  ('mismatch.pdf', make_docx()),
                                  ('plain.pdf', b'plain text pretending to be PDF'),
                                  ('unsupported.txt', b'plain text')):
            with self.subTest(filename=filename):
                status, _, payload = self.upload(filename, content)
                self.assertEqual(status, 400, payload)
                self.assertIn('error', json.loads(payload))
        self.assertEqual(self.metadata(), before)
        self.assert_download(original)

    def test_bad_filenames_and_nonstring_metadata_are_rejected(self):
        encoded = base64.b64encode(make_pdf()).decode('ascii')
        for filename in (None, [], '', '../resume.pdf', '/tmp/resume.pdf',
                         'C:\\private\\resume.pdf', 'nested/resume.pdf',
                         'bad\nname.pdf', 'bad\x00name.pdf'):
            with self.subTest(filename=repr(filename)):
                status, _, payload = self.request(body=dict(filename=filename, content_base64=encoded))
                self.assertEqual(status, 400, payload)
        self.assertIsNone(self.metadata()['resume'])

    def test_empty_malformed_and_nonstring_base64_are_rejected_without_record(self):
        for encoded in (None, [], '', 'not!base64', 'A', 'YWJj\n'):
            with self.subTest(encoded=repr(encoded)):
                status, _, payload = self.request(body=dict(filename='synthetic.pdf', content_base64=encoded))
                self.assertEqual(status, 400, payload)
        self.assertIsNone(self.metadata()['resume'])

    def test_exact_two_mib_valid_pdf_fits_transport_and_downloads_unchanged(self):
        content = make_pdf(size_bytes=MAX_BYTES)
        self.assertEqual(len(content), MAX_BYTES)
        status, _, payload = self.upload('最大合成文件.pdf', content)
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.metadata()['resume']['size_bytes'], MAX_BYTES)
        self.assert_download(content)

    def test_decoded_oversize_and_transport_oversize_preserve_previous_file(self):
        content = make_pdf()
        self.assertEqual(self.upload(content=content)[0], 200)
        before = self.metadata()
        status, _, payload = self.upload('too-large.pdf', make_pdf(size_bytes=MAX_BYTES + 1))
        self.assertEqual(status, 400, payload)
        raw = b'{"filename":"transport.pdf","content_base64":"' + b'a' * (3 * 1024 * 1024) + b'"}'
        self.assertEqual(self.request(raw=raw)[0], 413)
        self.assertEqual(self.metadata(), before)
        self.assert_download(content)

    def test_client_owner_or_path_fields_cannot_read_or_overwrite_another_candidate(self):
        other = make_docx()
        self.assertEqual(self.upload('仅属于B.docx', other, user='candidate_b')[0], 200)
        other_before = self.metadata(user='candidate_b')
        self.assertIsNone(self.metadata()['resume'])
        self.assertEqual(self.request('/api/resume/download')[0], 404)
        own = make_pdf()
        victim_id = self.actors['candidate_b']['id']
        status, _, payload = self.upload('仅属于A.pdf', own, user_id=victim_id,
                                        path='../another-person.docx', file_path='C:\\private\\other.docx')
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.metadata(user='candidate_b'), other_before)
        self.assert_download(other, user='candidate_b')
        self.assert_download(own, path=f'/api/resume/download?user_id={victim_id}&path=other.docx')
        status, _, payload = self.request(f'/api/resume?user_id={victim_id}')
        self.assertEqual(status, 200, payload)
        self.assertEqual(json.loads(payload)['resume']['filename'], '仅属于A.pdf')

    def test_all_resume_endpoints_require_a_candidate_session(self):
        self.assertEqual(self.upload()[0], 200)
        body = dict(filename='not-allowed.pdf', content_base64=base64.b64encode(make_pdf()).decode('ascii'))
        for user, expected in ((None, 401), ('resume_admin', 403), ('resume_boss', 403), ('resume_hr', 403)):
            for path, posted in (('/api/resume', None), ('/api/resume', body), ('/api/resume/download', None)):
                with self.subTest(user=user, path=path, posting=posted is not None):
                    self.assertEqual(self.request(path, user=user, body=posted)[0], expected)

    def test_upload_keeps_exact_host_origin_and_request_marker_protection(self):
        body = dict(filename='protected.pdf', content_base64=base64.b64encode(make_pdf()).decode('ascii'))
        for options in (dict(host='evil.example.test'), dict(origin=None),
                        dict(origin='https://evil.example.test'), dict(marker=False)):
            with self.subTest(options=options):
                self.assertEqual(self.request(body=body, **options)[0], 403)
        self.assertIsNone(self.metadata()['resume'])

    def test_backup_restore_preserves_blob_bytes_sessions_and_account_isolation(self):
        content_a, content_b = make_pdf(), make_docx()
        self.assertEqual(self.upload('备份A.pdf', content_a)[0], 200)
        self.assertEqual(self.upload('备份B.docx', content_b, user='candidate_b')[0], 200)
        target = self.root / 'round-trip-backup.sqlite3'
        restored = self.root / 'restored' / 'round-trip.sqlite3'
        snapshot = copy_database(self.store.path, target)
        recovered = copy_database(target, restored, mode='restore')
        self.assertEqual(snapshot['tables'], recovered['tables'])
        self.assertEqual(snapshot['tables']['resumes']['rows'], 2)
        original_metadata = {user: self.metadata(user=user) for user in ('candidate_a', 'candidate_b')}
        server, listener = self.start_server(Store(restored))
        try:
            for user, content in (('candidate_a', content_a), ('candidate_b', content_b)):
                self.assertEqual(self.metadata(user=user, server=server), original_metadata[user])
                self.assert_download(content, user=user, server=server)
        finally:
            self.stop_server(server, listener)


class ResumeMigrationTests(unittest.TestCase):
    def test_true_v2_database_upgrades_without_losing_accounts_jobs_favorites_or_sessions(self):
        with tempfile.TemporaryDirectory(prefix='resume-v2-migration-') as temporary:
            path = Path(temporary) / 'old-v2.sqlite3'
            token = 'synthetic-preserved-v2-session'
            with closing(sqlite3.connect(path)) as database:
                database.row_factory = sqlite3.Row
                database.execute('PRAGMA foreign_keys=ON')
                database.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, '
                                 'applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
                for version in (1, 2):
                    MIGRATIONS[version](database)
                    database.execute('INSERT INTO schema_migrations(version) VALUES(?)', (version,))
                values = Accounts._user_values('v2_candidate', PASSWORD, '迁移测试求职者', 'candidate', None, False)
                database.execute('INSERT INTO users(id,username,display_name,password_hash,role,org_id,can_manage_jobs) '
                                 'VALUES(1,:username,:display_name,:password_hash,:role,:org_id,:can_manage_jobs)', values)
                database.execute('INSERT INTO sessions VALUES(?,?,?)',
                                 (Accounts.token_hash(token), 1, time.time() + 3600))
                database.execute("INSERT INTO runs(id,started_at,source,label,status,result,owner_org_id) "
                                 "VALUES(1,'2026-10-15','v2:resume-fixture','旧合成批次','success','{}',NULL)")
                raw = dict(job_id='old-job', title='旧版本合成岗位', company='测试公司',
                           location='杭州', description='Python 开发与测试。', education='本科')
                payload = json.dumps(normalize(raw), ensure_ascii=False, sort_keys=True)
                original = json.dumps(raw, ensure_ascii=False)
                database.execute('INSERT INTO jobs(id,source,external_id,payload,fingerprint,raw,first_seen,last_seen,'
                    'run_id,is_demo,active,owner_org_id,published) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (1, 'v2:resume-fixture', 'old-job', payload, hashlib.sha256(payload.encode()).hexdigest(),
                     original, '2026-10-14', '2026-10-15', 1, 0, 1, None, 1))
                database.execute('INSERT INTO revisions(job_id,run_id,observed_at,payload,raw) VALUES(?,?,?,?,?)',
                                 (1, 1, '2026-10-15', payload, original))
                database.execute("INSERT INTO favorites VALUES(1,1,'必须保留的旧版个人备注')")
                database.execute("INSERT INTO audit_events(id,user_id,org_id,action,target,created,details) "
                                 "VALUES(1,1,NULL,'fixture','1',12345.0,'{}')")
                database.commit()
                old_tables = [row[0] for row in database.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name!='schema_migrations'")]
                before = {name: database.execute('SELECT * FROM ' + name).fetchall() for name in old_tables}
                before = {name: [tuple(row) for row in rows] for name, rows in before.items()}
                self.assertNotIn('resumes', old_tables)
            migrated = Store(path)
            self.assertTrue(hasattr(migrated, 'resumes'))
            self.assertEqual(migrated.accounts.user(token)['username'], 'v2_candidate')
            for _ in range(2):
                with migrated.connect() as database:
                    self.assertEqual([row[0] for row in database.execute(
                        'SELECT version FROM schema_migrations ORDER BY version')], [1, 2, 3])
                    self.assertEqual(database.execute('SELECT COUNT(*) FROM resumes').fetchone()[0], 0)
                    self.assertEqual({name: [tuple(row) for row in database.execute('SELECT * FROM ' + name)]
                                      for name in old_tables}, before)
                    self.assertEqual(database.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                    self.assertEqual(database.execute('PRAGMA foreign_key_check').fetchall(), [])
                migrated = Store(path)


if __name__ == '__main__':
    unittest.main()
