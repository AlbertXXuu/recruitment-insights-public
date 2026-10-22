import csv
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from app import make_server
from core import Store, analyze, extract_skills, greenhouse_records, normalize, salary
from recruitment_insights.domain.jobs import category
from recruitment_insights.domain.requirements import candidates


def record(**overrides):
    raw = dict(job_id='001', title='Python 后端工程师', company='测试公司', location='杭州',
               salary_min='10000', salary_max='20000', currency='CNY', salary_period='month',
               description='Python python JavaScript C++ Golang Docker Docker', education='本科', experience='1–3年')
    return dict(raw, **overrides)


class DomainTests(unittest.TestCase):
    def test_explicit_chinese_source_experience_keeps_quote_and_open_upper_bound(self):
        for text, years in [('一年以上工作经验',1),('两年以上工作经验',2),('三年以上工作经验',3),
                            ('五年以上工作经验',5),('八年以上工作经验',8),('十年以上工作经验',10)]:
            with self.subTest(text=text):
                item=candidates({'experience':text,'description':'要求20年相关经验。'})['experience']
                self.assertEqual((item['status'],item['evidence'],item['min_years'],item['max_years']),
                                 ('source',text,years,None))
        missing=candidates({'experience':'','description':''})['experience']
        self.assertEqual(missing['value'],'未知')
        self.assertNotEqual(missing['value'],'不限')

    def test_nontechnical_titles_are_not_counted_as_technical_jobs(self):
        for title, expected in [('Sales Engineer','销售与市场'),('客户成功经理','客户服务与支持'),
                                ('Product Designer','产品与设计'),('财务经理','职能与运营'),
                                ('Office Assistant','其他岗位'),('研发工程师','其他技术岗位')]:
            with self.subTest(title=title):
                self.assertEqual(category(title), expected)

    def test_skill_boundaries_and_dedup(self):
        self.assertEqual(extract_skills('JavaScript C++ python PYTHON go Golang'), ['C++', 'Go', 'JavaScript', 'Python'])

    def test_missing_is_not_unrestricted(self):
        job = normalize(record(education='', experience=''))
        self.assertEqual(job['education'], '未披露')
        self.assertEqual(job['experience'], '未披露')
        self.assertEqual(normalize(record(education='不限'))['education'], '不限')

    def test_annual_monthly_conversion(self):
        self.assertEqual(salary(record(salary_min='120000', salary_max='240000', currency='USD', salary_period='year'))[:2], (10000, 20000))

    def test_unsupported_currency_and_period(self):
        for fields in (dict(currency='EUR'), dict(salary_period='hour'), dict(currency='')):
            self.assertIsNone(salary(record(**fields))[0])

    def test_invalid_salary_ranges(self):
        for low, high in [('NaN', '20000'), ('-1', '10000'), ('20000', '10000'), ('Inf', 'Inf'), ('abc', '123')]:
            self.assertIsNone(salary(record(salary_min=low, salary_max=high))[0])

    def test_salary_text_and_negotiable(self):
        self.assertEqual(salary(record(salary_min='', salary_max='', salary_text='10-20K/月'))[:2], (10000, 20000))
        self.assertIsNone(salary(record(salary_min='', salary_max='', salary_text='面议'))[0])

    def test_invalid_required_field_and_url(self):
        for fields in (dict(title=' '), dict(company=None), dict(source_url='javascript:alert(1)')):
            with self.assertRaises(ValueError):
                normalize(record(**fields))

    def test_greenhouse_html_and_filter(self):
        payload = {'jobs':[dict(id=1,title='Software Engineer',location={'name':'Remote'},content='&lt;p&gt;Python&lt;/p&gt;',absolute_url='https://example.com/1'),dict(id=2,title='Sales representative',location={'name':'Remote'})]}
        jobs = greenhouse_records(payload)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['description'], 'Python')
        self.assertEqual(jobs[0]['education'], '未披露')


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'test.sqlite3')

    def tearDown(self):
        self.temp.cleanup()

    def insert(self, rows=None, **kwargs):
        return self.store.import_records(rows or [record()], 'test:source', 'fixture', **kwargs)

    def test_duplicate_import_is_idempotent(self):
        self.insert()
        result = self.insert()
        self.assertEqual((result['inserted'], result['unchanged']), (0, 1))
        self.assertEqual(len(self.store.jobs()), 1)
        self.assertEqual(len(self.store.detail(1)['revisions']), 1)

    def test_update_retains_identity_and_history(self):
        self.insert()
        result = self.insert([record(title='Senior Python Engineer')])
        self.assertEqual(result['updated'], 1)
        detail = self.store.detail(1)
        self.assertEqual(len(detail['revisions']), 2)
        self.assertEqual(detail['revisions'][-1]['payload']['title'], 'Python 后端工程师')

    def test_source_namespace_keeps_distinct_jobs(self):
        self.insert()
        self.store.import_records([record()], 'other:source', 'fixture')
        self.assertEqual(len(self.store.jobs()), 2)

    def test_partial_rejection_with_row_evidence(self):
        result = self.insert([record(),record(job_id='2', title='')])
        self.assertEqual((result['inserted'],result['rejected']), (1,1))
        self.assertEqual(result['errors'][0]['row'], 3)
        self.assertEqual(self.store.runs()[0]['status'], 'partial')

    def test_demo_separation(self):
        self.insert(is_demo=True)
        self.assertEqual(self.store.jobs(), [])
        self.assertEqual(len(self.store.jobs({'dataset':'demo'})), 1)

    def test_filters_match_analysis_population(self):
        self.insert([record(),record(job_id='2',location='北京',description='Java',title='Java 工程师')])
        rows = self.store.jobs({'location':'杭州','skill':'Python','q':'后端'})
        self.assertEqual(analyze(rows)['total'], 1)
        self.assertEqual(analyze(rows)['locations'], [{'name':'杭州','count':1}])

    def test_salary_missing_excluded_currency_separated(self):
        self.insert([record(),record(job_id='2',salary_min='',salary_max='',salary_text='面议'),record(job_id='3',currency='USD',salary_min=20000,salary_max=40000)])
        stats = analyze(self.store.jobs())
        self.assertEqual(stats['invalid_salary'],1)
        self.assertEqual(stats['salaries'][0]['mean'],15000)
        self.assertEqual(stats['salaries'][1]['mean'],30000)

    def test_multi_office_filter_and_counts(self):
        self.insert([record(locations=['杭州','北京','北京'])])
        self.assertEqual(len(self.store.jobs({'location':'北京'})),1)
        self.assertEqual(analyze(self.store.jobs())['locations'],[{'name':'北京','count':1},{'name':'杭州','count':1}])

    def test_empty_statistics(self):
        self.assertIsNone(analyze([])['salaries'][0]['median'])
        self.assertEqual(analyze([])['total'],0)

    def test_csv_bom_quotes_and_duplicates(self):
        content = '\ufeffjob_id,title,company,location\n1,"Engineer, Python",ACME,Remote\n'
        self.assertEqual(self.store.import_csv(content,'样本')['inserted'],1)
        self.assertEqual(self.store.import_csv(content,'样本')['unchanged'],1)

    def test_bad_csv_headers_and_empty(self):
        for content in ('hello\nworld', 'job_id,title,company,location\n', 'job_id,title,company,location\n1,a,b,c,extra'):
            with self.assertRaises(ValueError):
                self.store.import_csv(content,'test')
        self.assertEqual(self.store.runs(), [])

    def test_persistence_across_reopen(self):
        self.insert()
        self.assertEqual(len(Store(self.store.path).jobs()), 1)

    def test_authoritative_deactivation_and_reactivation(self):
        self.insert([record(),record(job_id='2')],authoritative=True)
        self.assertEqual(self.insert(authoritative=True)['deactivated'],1)
        self.assertEqual(len(self.store.jobs()),1)
        self.assertEqual(self.insert([record(job_id='2')])['updated'],1)
        self.assertEqual(len(self.store.jobs()),2)

    def test_partial_snapshot_does_not_deactivate(self):
        self.insert([record(),record(job_id='2')])
        self.insert([record(),record(job_id='3',title='')],authoritative=True)
        self.assertEqual(len(self.store.jobs()),2)

    def test_source_failure_retains_data_and_logs_failure(self):
        self.insert()
        with self.assertRaises(ValueError):
            self.store.sync({'jobs':[]})
        self.assertEqual(len(self.store.jobs()),1)
        self.assertEqual(self.store.runs()[0]['status'],'failed')

    def test_concurrent_import_idempotence(self):
        errors=[]
        def work():
            try:self.insert()
            except Exception as exc:errors.append(exc)
        threads=[threading.Thread(target=work) for _ in range(4)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(errors,[])
        self.assertEqual(len(self.store.jobs()),1)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.store=Store(Path(cls.temp.name)/'http.sqlite3')
        cls.store.import_records([record(title='=HYPERLINK("bad")')], 'test:source','HTTP fixture')
        cls.server=make_server(cls.store,0)
        cls.server.accounts.create_user('test_admin','test-password-2026','Test admin','admin')
        token,_=cls.server.accounts.login('test_admin','test-password-2026')
        cls.cookie='recruitment_insights_session='+token
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.url=f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.temp.cleanup()

    def get(self,path,headers=None):
        return urlopen(Request(self.url+path,headers={'Cookie':self.cookie,**(headers or {})}),timeout=5)

    def test_health_and_ui(self):
        with self.get('/api/health') as response:self.assertEqual(json.load(response)['status'],'ok')
        with self.get('/') as response:
            self.assertIn('recruitment-insights',response.read().decode())
            self.assertIn("frame-ancestors 'none'",response.headers['Content-Security-Policy'])

    def test_query_pagination_validation(self):
        with self.get('/api/jobs?page=1') as response:self.assertEqual(json.load(response)['total'],1)
        with self.assertRaises(HTTPError) as context:self.get('/api/jobs?page=nope')
        self.assertEqual(context.exception.code,400)

    def test_not_found_and_path_traversal(self):
        for path in ('/api/jobs/99999','/../core.py','/data/recruitment.sqlite3'):
            with self.assertRaises(HTTPError) as context:self.get(path)
            self.assertEqual(context.exception.code,404)

    def test_host_and_cross_origin_rejected(self):
        with self.assertRaises(HTTPError) as context:self.get('/api/health',{'Host':'evil.example'})
        self.assertEqual(context.exception.code,403)
        req=Request(self.url+'/api/import',data=b'{}',headers={'Origin':'https://evil.example','Content-Type':'application/json','X-Local-Request':'1'})
        with self.assertRaises(HTTPError) as context:urlopen(req)
        self.assertEqual(context.exception.code,403)

    def test_export_formula_is_neutralized(self):
        with self.get('/api/export') as response:
            rows=list(csv.DictReader(io.StringIO(response.read().decode('utf-8-sig'))))
        self.assertTrue(rows[0]['title'].startswith("'="))

    def test_import_endpoint_validation_and_update(self):
        body=dict(csv='job_id,title,company,location\nHTTP-1,Python,Test,Remote',source='http',is_demo=True)
        req=Request(self.url+'/api/import',data=json.dumps(body).encode(),headers={'Cookie':self.cookie,'Content-Type':'application/json','X-Local-Request':'1'})
        with urlopen(req) as response:self.assertEqual(json.load(response)['inserted'],1)
        with urlopen(req) as response:self.assertEqual(json.load(response)['unchanged'],1)
        bad=Request(self.url+'/api/import',data=b'[]',headers={'Cookie':self.cookie,'Content-Type':'application/json','X-Local-Request':'1'})
        with self.assertRaises(HTTPError) as context:urlopen(bad)
        self.assertEqual(context.exception.code,400)


if __name__=='__main__':
    unittest.main(verbosity=2)
