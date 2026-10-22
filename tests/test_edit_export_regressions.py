"""Regression coverage for concurrent company edits and normalized salary exports."""
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


class EditExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.store = Store(Path(cls.temp.name) / 'regressions.sqlite3')
        seed(cls.store)
        cls.server = make_server(cls.store, 0)
        cls.accounts = cls.server.accounts
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'
        cls.cookies, cls.actors = {}, {}
        for name in ('admin', 'boss_a', 'hr_a', 'boss_b', 'candidate_a'):
            token, actor = cls.accounts.login(name, PASSWORD)
            cls.cookies[name] = 'recruitment_insights_session='+token
            cls.actors[name] = actor

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def request(self, path, user='boss_a', body=None, status=200):
        headers = {'Cookie': self.cookies[user]}
        payload = None
        if body is not None:
            headers.update({'Content-Type': 'application/json', 'X-Local-Request': '1'})
            payload = json.dumps(body).encode()
        try:
            response = urlopen(Request(self.url+quote(path, safe='/?=&'), data=payload,
                                       headers=headers), timeout=10)
        except HTTPError as exc:
            response = exc
        with response:
            content = response.read().decode('utf-8-sig')
            if status is not None:
                self.assertEqual(response.status, status, (path, content))
            result = json.loads(content) if response.headers.get_content_type() == 'application/json' else content
            return (response.status, result) if status is None else result

    def job(self, **flags):
        result = self.request('/api/company/jobs', body=dict(record={
            'title': self._testMethodName, 'location': '杭州',
            'description': '负责 Python 系统开发、维护与自动化测试。'}, **flags))
        return self.request('/api/jobs/'+str(result['id']))

    @staticmethod
    def save_body(detail, **changes):
        # The fallback lets the original bug reproduce as a lost update, not merely a missing key.
        return dict(id=detail['id'], expected_edit_token=detail.get('edit_token', detail.get('fingerprint')), **changes)

    def counts(self):
        with self.store.connect() as db:
            return {table: db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
                    for table in ('jobs', 'runs', 'revisions', 'audit_events')}

    def test_old_form_cannot_overwrite_a_newer_content_edit(self):
        old = self.job()
        saved = self.request('/api/company/jobs', body=self.save_body(old, record={'title': '新版岗位标题'}))
        before = self.counts()
        self.request('/api/company/jobs', 'hr_a', self.save_body(old, record={'title': '旧表单错误覆盖'}), status=409)
        self.assertEqual(self.counts(), before)
        current = self.request('/api/jobs/'+str(old['id']))
        self.assertEqual(current['raw']['title'], '新版岗位标题')
        self.assertEqual(saved['edit_token'], current['edit_token'])
        self.assertNotEqual(current['edit_token'], old['edit_token'])

    def test_old_form_cannot_undo_published_active_or_dataset_changes(self):
        for flag, new_value in (('published', True), ('active', False), ('is_demo', False)):
            with self.subTest(flag=flag):
                old = self.job()
                self.request('/api/company/jobs', body=self.save_body(old, **{flag: new_value}))
                self.request('/api/company/jobs', body=self.save_body(old, record={'title': '旧内容'},
                    published=bool(old['published']), active=bool(old['active']), is_demo=bool(old['is_demo'])), status=409)
                current = self.request('/api/jobs/'+str(old['id']))
                self.assertEqual(bool(current[flag]), new_value)
                self.assertEqual(current['raw']['title'], old['raw']['title'])

    def test_two_simultaneous_edits_accept_only_one_version(self):
        old = self.job()
        start = threading.Barrier(2)
        outcomes, errors = [], []
        def submit(title):
            try:
                start.wait(timeout=5)
                code, result = self.request('/api/company/jobs', body=self.save_body(old, record={'title': title}), status=None)
                outcomes.append((code, title, result))
            except Exception as exc:
                errors.append(exc)
        workers = [threading.Thread(target=submit, args=(title,)) for title in ('并发标题A', '并发标题B')]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=15)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(code for code, _, _ in outcomes), [200, 409])
        winner = next(title for code, title, _ in outcomes if code == 200)
        self.assertEqual(self.request('/api/jobs/'+str(old['id']))['raw']['title'], winner)

    def test_missing_token_is_rejected_and_new_jobs_do_not_require_one(self):
        detail = self.job()
        self.assertRegex(detail['edit_token'], r'^[0-9a-f]{64}$')
        before = self.counts()
        for value in (None, '', 123, 'outdated'):
            body = dict(id=detail['id'], record={'title': '错误更新'})
            if value is not None:
                body['expected_edit_token'] = value
            self.request('/api/company/jobs', body=body, status=409)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.request('/api/jobs/'+str(detail['id']))['raw'], detail['raw'])

    def test_source_import_invalidates_open_form_even_if_normalized_values_are_same(self):
        old = self.job()
        changed_raw = dict(old['raw'], description='<p>'+old['raw']['description']+'</p>')
        self.store.import_records([changed_raw], old['source'], 'updated original source',
                                  is_demo=True, owner_org_id=old['owner_org_id'])
        current = self.request('/api/jobs/'+str(old['id']))
        self.assertEqual(current['fingerprint'], old['fingerprint'])
        self.request('/api/company/jobs', body=self.save_body(old, published=True), status=409)
        self.assertNotEqual(current['edit_token'], old['edit_token'])

    def test_reload_allows_retry_and_repeated_save_invalidates_previous_token(self):
        old = self.job()
        self.request('/api/company/jobs', body=self.save_body(old, published=True))
        current = self.request('/api/jobs/'+str(old['id']))
        self.request('/api/company/jobs', body=self.save_body(current, record={'title': '重新打开后保存'}))
        refreshed = self.request('/api/jobs/'+str(old['id']))
        self.assertTrue(refreshed['published'])
        self.assertEqual(refreshed['raw']['title'], '重新打开后保存')
        self.request('/api/company/jobs', body=self.save_body(refreshed))
        self.request('/api/company/jobs', body=self.save_body(refreshed), status=409)

    def test_edit_token_does_not_expand_permissions_or_leak_to_public_readers(self):
        detail = self.job(published=True)
        for user in ('candidate_a', 'boss_b'):
            self.assertNotIn('edit_token', self.request('/api/jobs/'+str(detail['id']), user))
            self.request('/api/company/jobs', user, self.save_body(detail, published=False), status=403)
        self.accounts.grant(self.actors['boss_a'], self.actors['hr_a']['id'], False)
        try:
            self.request('/api/company/jobs', 'hr_a', self.save_body(detail, published=False), status=403)
            with self.assertRaises(PermissionError):
                self.store.manage_job(self.actors['hr_a'], self.save_body(detail, published=False))
        finally:
            self.accounts.grant(self.actors['boss_a'], self.actors['hr_a']['id'], True)

    def test_audit_failure_preserves_content_flags_and_edit_token(self):
        detail = self.job()
        counts = self.counts()
        with patch.object(Accounts, 'audit', side_effect=RuntimeError('audit failure')):
            with self.assertRaises(RuntimeError):
                self.store.manage_job(self.actors['boss_a'], self.save_body(detail, record={'title': '回滚'}, published=True))
        self.assertEqual(self.store.detail(detail['id'], self.actors['boss_a']), detail)
        self.assertEqual(self.counts(), counts)

    def export_rows(self, records):
        source = self._testMethodName
        self.store.import_records(records, source, 'salary export fixtures')
        return list(csv.DictReader(io.StringIO(self.request('/api/export?dataset=real&source='+source, 'candidate_a'))))

    @staticmethod
    def salary_record(job_id, **overrides):
        return dict(dict(job_id=job_id, title='薪资导出测试', company='薪资样本', location='杭州',
                         description='测试明确币种与周期的规范薪资导出。', salary_text='', salary_min=120000,
                         salary_max=240000, currency='CNY', salary_period='year'), **overrides)

    def test_annual_numeric_salary_exports_monthly_values_once_with_explicit_periods(self):
        rows = self.export_rows([self.salary_record('annual'), self.salary_record('monthly',
            salary_min=10000, salary_max=20000, currency='USD', salary_period='month', salary_text='USD 10,000–20,000/month')])
        indexed = {row['job_id']: row for row in rows}
        annual, monthly = indexed['annual'], indexed['monthly']
        self.assertIn('salary_monthly_min', annual)
        self.assertEqual((float(annual['salary_monthly_min']), float(annual['salary_monthly_max'])), (10000, 20000))
        self.assertEqual((annual['salary_text'], annual['currency'], annual['salary_status']), ('', 'CNY', '有效'))
        self.assertEqual((annual['salary_source_period'], annual['salary_normalized_period']), ('year', 'month'))
        self.assertNotIn('salary_period', annual)
        self.assertEqual((float(monthly['salary_monthly_min']), float(monthly['salary_monthly_max'])), (10000, 20000))
        self.assertEqual(monthly['salary_text'], 'USD 10,000–20,000/month')
        self.assertEqual((monthly['currency'], monthly['salary_normalized_period']), ('USD', 'month'))

    def test_unusable_salaries_export_empty_bounds_and_explicit_reason(self):
        rows = self.export_rows([self.salary_record('negotiable', salary_min='', salary_max='', salary_text='面议'),
                                 self.salary_record('reversed', salary_min=20000, salary_max=10000),
                                 self.salary_record('unsupported', currency='EUR')])
        for row in rows:
            self.assertEqual((row['salary_monthly_min'], row['salary_monthly_max'], row['salary_normalized_period']), ('', '', ''))
            self.assertNotEqual(row['salary_status'], '有效')
        self.assertEqual(next(row['salary_text'] for row in rows if row['job_id']=='negotiable'), '面议')

    def test_new_export_columns_keep_formula_escaping_and_visibility(self):
        rows = self.export_rows([self.salary_record('escape', salary_text='=HYPERLINK("unsafe")')])
        self.assertEqual(rows[0]['salary_text'], '\'=HYPERLINK("unsafe")')
        draft = self.job()
        exported = list(csv.DictReader(io.StringIO(self.request('/api/export?dataset=demo', 'candidate_a'))))
        self.assertNotIn(draft['raw']['job_id'], [row['job_id'] for row in exported])


if __name__ == '__main__':
    unittest.main(verbosity=2)
