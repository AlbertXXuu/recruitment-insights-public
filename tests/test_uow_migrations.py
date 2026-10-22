"""Real legacy migration and transaction checks; every database is temporary.

The legacy comparison runs the preserved 2.2 implementation in a separate Python
process. It does not simulate an old database by deleting a new version marker.
If that optional checkpoint is absent, only the two legacy-fixture tests skip.
"""
import hashlib
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from core import Store, analyze, normalize
from recruitment_insights.infrastructure import database
from recruitment_insights.infrastructure.jobs import JobRepository
from recruitment_insights.domain.jobs import edit_token


ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / 'outputs/backups/checkpoint-20261002-before-architecture'
LIFECYCLE_FIELDS = {'source_active', 'manual_inactive', 'lifecycle_reason', 'lifecycle_updated_at'}
V2_JOB_FIELDS = LIFECYCLE_FIELDS | {'lifecycle_version'}
V1_JOB_COLUMNS = ('id','source','external_id','payload','fingerprint','raw','first_seen','last_seen',
                  'run_id','is_demo','active','owner_org_id','published')
LEGACY_FIXTURE = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from core import Store, analyze
from accounts import Accounts

store=Store(Path(sys.argv[2]))
accounts=Accounts(store)
org=accounts.organization('旧库企业 A')
password='Temporary-migration-2026!'
for name,role,owner,manage in [('old_admin','admin',None,False),('old_boss','boss',org,True),
                              ('old_hr','hr',org,False),('old_candidate','candidate',None,False)]:
    accounts.create_user(name,password,name,role,owner,manage)
admin_token,admin=accounts.login('old_admin',password)
candidate_token,candidate=accounts.login('old_candidate',password)
boss_token,boss=accounts.login('old_boss',password)

def raw(i, title):
    return dict(job_id=str(i),title=title,company='旧库样本公司',location='杭州',
                description='岗位职责为 Python 开发与代码审查。要求本科及以上学历，至少 3 年软件开发经验。',
                education='',experience='',salary_min='120000',salary_max='240000',
                currency='CNY',salary_period='year',source_url='https://example.org/job/'+str(i),
                original_note='保留原文\n与自定义字段 '+str(i))
records=[raw(1,'Python 后端岗位 A'),raw(2,'Python 后端岗位 A'),
         raw(3,'Python 后端岗位 B'),raw(4,'Python 后端岗位 B'),
         raw(5,'Python 后端岗位 C'),raw(6,'Python 后端岗位 C')]
store.import_records(records,'legacy:public','旧版本公开批次')
ids={int(row['external_id']):row['id'] for row in [store.detail(j['id'],admin) for j in store.jobs({'dataset':'all'},admin)]}

def review(i, action='confirm'):
    item=store.detail(ids[i],admin)['requirements']['education']
    body=dict(job_id=ids[i],field='education',action=action,source_hash=item['source_hash'],version=item['version'])
    if action=='confirm':
        body.update(value='本科',condition='硬性要求',evidence='本科及以上学历',alternatives=[])
    return store.review_field(admin,body)

def decide(a,b,decision):
    left,right=store.detail(ids[a],admin),store.detail(ids[b],admin)
    with store.connect() as db:
        saved=db.execute('SELECT version FROM duplicate_decisions WHERE left_id=? AND right_id=?',(ids[a],ids[b])).fetchone()
    store.decide_duplicate(admin,dict(left_id=ids[a],right_id=ids[b],left_hash=left['fingerprint'],
        right_hash=right['fingerprint'],version=saved['version'] if saved else 0,
        decision=decision,reason='旧版本人工判断依据：保留来源记录'))

for i in (1,3,5): review(i)
review(5,'revoke')
decide(1,2,'same')
decide(3,4,'same')
decide(5,6,'different')
decide(5,6,'revoke')
changed=dict(records[2],description=records[2]['description']+' 岗位原文后来补充了远程协作要求。')
store.import_records([changed],'legacy:public','旧版本后续更新')
store.import_records([raw(7,'企业私有草稿'),raw(8,'已停用企业岗位')],
                     'legacy:org','旧版本企业批次',owner_org_id=org)
with store.connect() as db:
    private=db.execute("SELECT id FROM jobs WHERE source='legacy:org' AND external_id='7'").fetchone()['id']
    inactive=db.execute("SELECT id FROM jobs WHERE source='legacy:org' AND external_id='8'").fetchone()['id']
    db.execute('UPDATE jobs SET published=1,active=0 WHERE id=?',(inactive,))
accounts.save_favorite(candidate,ids[1],note='旧版收藏备注，必须逐字保留。')
jobs=store.jobs({'dataset':'all'},candidate)
output=dict(admin=admin,boss=boss,candidate=candidate,candidate_token=candidate_token,
            ids=ids,private_id=private,inactive_id=inactive,
            public_jobs=jobs,public_stats=analyze(jobs),
            public_details={str(i):store.detail(ids[i],candidate) for i in (1,3,5)},
            favorites=accounts.favorites(candidate),quality=store.quality({'dataset':'all'},admin),
            runs=store.runs(admin),company_jobs=store.company_jobs(boss))
print(json.dumps(output,ensure_ascii=False))
'''


def snapshot(path):
    """Capture every business column/value plus schema, excluding only the new marker."""
    with closing(sqlite3.connect(path)) as db:
        objects = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND name!='schema_migrations' AND tbl_name!='schema_migrations' ORDER BY type,name").fetchall()
        tables = [name for kind, name, _, _ in objects if kind == 'table']
        rows = {name: sorted(db.execute('SELECT * FROM "'+name.replace('"','""')+'"').fetchall(), key=repr)
                for name in tables}
        return objects, rows


class UnitOfWorkMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='recruitment_insights-uow-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'temporary.sqlite3'

    def legacy_database(self):
        required = ('core.py','accounts.py','reviews.py','requirements.py','quality.py')
        if not all((LEGACY/name).is_file() for name in required):
            self.skipTest('Preserved 2.2 checkpoint absent; real legacy-module comparison was not run.')
        result = subprocess.run([sys.executable, '-B', '-X', 'utf8', '-c', LEGACY_FIXTURE,
                                 str(LEGACY), str(self.path)], cwd=self.temp.name,
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='schema_migrations'").fetchone())
        return json.loads(result.stdout)

    def current_database(self):
        store = Store(self.path)
        actors = {}
        for name, role, org in [('admin','admin',None),('boss_a','boss','企业 A'),
                                ('boss_b','boss','企业 B'),('candidate','candidate',None)]:
            owner = store.accounts.organization(org) if org else None
            user_id = store.accounts.create_user(name, 'Temporary-uow-2026!', name, role, owner, role=='boss')
            with store.uow() as tx:
                actors[name] = tx.accounts.get(user_id)
        return store, actors

    def v1_database(self):
        """Create a real v1 schema, before any v2 columns or application exist."""
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            database.migrate_v1(db)
            db.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            db.execute('INSERT INTO schema_migrations(version) VALUES(1)')
            db.execute("INSERT INTO organizations(id,name) VALUES(1,'旧版本企业')")
            users = [(1,'migration_admin','管理员','preserved-admin-hash','admin',None,0),
                     (2,'migration_boss','负责人','preserved-boss-hash','boss',1,1),
                     (3,'migration_hr','只读 HR','preserved-hr-hash','hr',1,0),
                     (4,'migration_candidate','求职者','preserved-candidate-hash','candidate',None,0)]
            db.executemany('INSERT INTO users VALUES(?,?,?,?,?,?,?)', users)
            db.execute('INSERT INTO sessions VALUES(?,?,?)',
                       (hashlib.sha256(b'v1-preserved-session').hexdigest(), 4, time.time()+3600))
            db.execute("INSERT INTO runs(id,started_at,source,label,status,result,owner_org_id) VALUES(1,'2026-09-30T12:00:00','v1:fixture','旧批次','success','{\"total\":4}',NULL)")
            for job_id, owner, active in ((1,None,1),(2,None,0),(3,1,1),(4,1,0)):
                raw = dict(job_id=str(job_id), title='旧岗位 '+str(job_id), company='旧公司', location='杭州',
                           description='Python 开发，要求本科及以上学历。', salary_text='10-20K/月',
                           original_note='保留原文\n及自定义字段 '+str(job_id))
                payload = json.dumps(normalize(raw), ensure_ascii=False, sort_keys=True)
                fingerprint = hashlib.sha256(payload.encode()).hexdigest()
                original = json.dumps(raw, ensure_ascii=False)
                db.execute('INSERT INTO jobs('+','.join(V1_JOB_COLUMNS)+') VALUES('+','.join('?' for _ in V1_JOB_COLUMNS)+')',
                           (job_id,'v1:fixture',str(job_id),payload,fingerprint,original,
                            '2026-09-29T12:00:00','2026-09-30T12:00:00',1,0,active,owner,1))
                db.execute('INSERT INTO revisions(job_id,run_id,observed_at,payload,raw) VALUES(?,?,?,?,?)',
                           (job_id,1,'2026-09-30T12:00:00',payload,original))
            db.execute('INSERT INTO revisions(job_id,run_id,observed_at,payload,raw) SELECT id,run_id,first_seen,payload,raw FROM jobs WHERE id=1')
            db.execute("INSERT INTO favorites VALUES(4,1,'必须逐字保留的旧收藏备注')")
            for job_id, field, state in ((1,'education','confirmed'),(3,'experience','revoked')):
                conclusion = json.dumps(dict(value='本科' if field=='education' else '1–3年', condition='硬性要求',
                                             evidence='旧证据', alternatives=[]), ensure_ascii=False)
                db.execute('INSERT INTO field_reviews VALUES(?,?,?,?,?,?,?,?)',
                           (job_id,field,conclusion,'a'*64,state,3,1,'2026-09-30T12:00:00'))
            db.execute('INSERT INTO duplicate_decisions VALUES(?,?,?,?,?,?,?,?,?)',
                       (1,2,'same','旧版重复判断依据必须保留','b'*64,'c'*64,2,1,'2026-09-30T12:00:00'))
            db.execute("INSERT INTO audit_events(id,user_id,org_id,action,target,created,details) VALUES(1,1,NULL,'review_confirm','1',12345.5,'{\"中文\":\"保留旧审计\"}')")
            db.commit()
            self.assertEqual([row[0] for row in db.execute('SELECT version FROM schema_migrations')], [1])
            self.assertFalse(V2_JOB_FIELDS & {row['name'] for row in db.execute('PRAGMA table_info(jobs)')})
        return snapshot(self.path)

    def assert_legacy_rows_preserved(self, before):
        """Project only added columns away; compare every original row and object."""
        objects, old_rows = before
        actual_objects, actual_rows = snapshot(self.path)
        self.assertEqual([item for item in actual_objects if item[1] != 'jobs' and item[2] != 'resumes'],
                         [item for item in objects if item[1] != 'jobs'])
        self.assertEqual(set(actual_rows), set(old_rows) | {'resumes'})
        self.assertEqual(actual_rows['resumes'], [], 'A legacy migration must not invent resume data')
        for table in old_rows:
            if table != 'jobs':
                self.assertEqual(actual_rows[table], old_rows[table], table)
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            projected = sorted([tuple(row) for row in db.execute('SELECT '+','.join(V1_JOB_COLUMNS)+' FROM jobs')], key=repr)
            self.assertEqual(projected, old_rows['jobs'])
            self.assertEqual({row['name'] for row in db.execute('PRAGMA table_info(jobs)')},
                             set(V1_JOB_COLUMNS) | V2_JOB_FIELDS)
            for row in db.execute('SELECT * FROM jobs'):
                self.assertEqual((row['source_active'], row['manual_inactive']),
                                 (0 if row['active']==0 and row['owner_org_id'] is None else 1,
                                  1 if row['active']==0 and row['owner_org_id'] is not None else 0))
                self.assertEqual((row['lifecycle_reason'], row['lifecycle_updated_at']), ('',''))
                self.assertEqual(row['lifecycle_version'], 0)
                self.assertEqual(bool(row['active']), bool(row['source_active']) and not bool(row['manual_inactive']))

    def assert_legacy_view_preserved(self, actual, expected):
        # New private metadata is tested separately; all former public/business
        # fields must still compare exactly, including ordering and raw content.
        if isinstance(expected, list):
            self.assertIsInstance(actual, list)
            self.assertEqual(len(actual), len(expected))
            for current, previous in zip(actual, expected):
                self.assert_legacy_view_preserved(current, previous)
        elif isinstance(expected, dict):
            added = set(actual)-set(expected)
            self.assertTrue(added <= LIFECYCLE_FIELDS | {'edit_token'}, added)
            self.assertTrue(set(expected) <= set(actual))
            for key, value in expected.items():
                if key == 'edit_token':
                    # v2 binds lifecycle metadata/version into its token.
                    self.assertRegex(actual[key], r'^[0-9a-f]{64}$')
                    with closing(sqlite3.connect(self.path)) as db:
                        db.row_factory = sqlite3.Row
                        row = db.execute('SELECT * FROM jobs WHERE id=?', (actual['id'],)).fetchone()
                    self.assertEqual(actual[key], edit_token(row))
                else:
                    self.assert_legacy_view_preserved(actual[key], value)
            if LIFECYCLE_FIELDS & added:
                self.assertEqual(LIFECYCLE_FIELDS & added, LIFECYCLE_FIELDS)
                with closing(sqlite3.connect(self.path)) as db:
                    db.row_factory = sqlite3.Row
                    row = db.execute('SELECT * FROM jobs WHERE id=?', (actual['id'],)).fetchone()
                for key in LIFECYCLE_FIELDS:
                    self.assertEqual(actual[key], row[key])
            if 'edit_token' in added:
                self.assertRegex(actual['edit_token'], r'^[0-9a-f]{64}$')
        else:
            self.assertEqual(actual, expected)

    @staticmethod
    def job_body(title='同一公开岗位'):
        return dict(record=dict(title=title,location='上海',description='Python 后端开发，要求本科及以上学历。',
                                education='本科',experience='1–3年'),is_demo=False,published=True,active=True)

    def test_real_22_rich_database_upgrade_and_two_reopens_preserve_every_row(self):
        old = self.legacy_database()
        before = snapshot(self.path)
        states = {row[0]: row[4] for row in before[1]['field_reviews']}
        self.assertEqual(set(states.values()), {'confirmed','revoked'})
        actions = {row[3] for row in before[1]['audit_events']}
        self.assertTrue({'review_confirm','review_revoke','duplicate_same','duplicate_different','duplicate_revoke'} <= actions)
        self.assertEqual(len(before[1]['sessions']), 3)
        self.assertEqual(len(before[1]['favorites']), 1)
        self.assertGreater(len(before[1]['revisions']), len(before[1]['jobs']))
        # First startup applies all current migrations; subsequent startups are idempotent.
        for reopen in range(3):
            # Compare schema migration with the same extraction rule version as
            # the legacy fixture. A rule change intentionally invalidates reviews
            # and is verified independently below, not erased from expectations.
            with self.subTest(reopen=reopen), patch('requirements.RULE_VERSION', 'requirements-2'):
                store = Store(self.path)
                self.assert_legacy_rows_preserved(before)

                with store.connect() as db:
                    self.assertEqual([row[0] for row in db.execute('SELECT version FROM schema_migrations ORDER BY version')],
                                     list(range(1, database.SCHEMA_VERSION + 1)))
                self.assertEqual(store.accounts.user(old['candidate_token']), old['candidate'])
                jobs = store.jobs({'dataset':'all'}, old['candidate'])
                self.assertEqual(jobs, old['public_jobs'])
                self.assertEqual(analyze(jobs), old['public_stats'])
                self.assert_legacy_view_preserved(store.accounts.favorites(old['candidate']), old['favorites'])
                self.assertEqual(store.quality({'dataset':'all'}, old['admin']), old['quality'])
                self.assertEqual(store.runs(old['admin']), old['runs'])
                self.assert_legacy_view_preserved(store.company_jobs(old['boss']), old['company_jobs'])
                for external_id in ('1','3','5'):
                    self.assert_legacy_view_preserved(store.detail(old['ids'][external_id], old['candidate']), old['public_details'][external_id])
                self.assertEqual(store.detail(old['ids']['1'],old['admin'])['requirements']['education']['status'], 'confirmed')
                self.assertEqual(store.detail(old['ids']['3'],old['admin'])['requirements']['education']['status'], 'stale')
                self.assertEqual(store.detail(old['ids']['5'],old['admin'])['requirements']['education']['status'], 'candidate')
                self.assertIsNone(store.detail(old['private_id'],old['candidate']))
                self.assertIsNone(store.detail(old['inactive_id'],old['candidate']))
                self.assert_legacy_rows_preserved(before)

    def test_old_reviews_require_reverification_after_rule_version_changes_without_data_loss(self):
        old=self.legacy_database()
        before=snapshot(self.path)
        store=Store(self.path)
        self.assert_legacy_rows_preserved(before)
        with patch('requirements.RULE_VERSION', 'requirements-2'):
            self.assertEqual(store.detail(old['ids']['1'],old['admin'])['requirements']['education']['status'], 'confirmed')
        with patch('requirements.RULE_VERSION', 'requirements-3'):
            self.assertEqual(store.detail(old['ids']['1'],old['admin'])['requirements']['education']['status'], 'stale')
            self.assertEqual(store.detail(old['ids']['1'],old['candidate'])['payload']['education'], '待复核')
        self.assert_legacy_rows_preserved(before)

    def test_failed_migration_rolls_back_ddl_data_and_version_marker(self):
        self.legacy_database()
        before = snapshot(self.path)
        original = database.MIGRATIONS[1]
        def fail_after_changes(db):
            original(db)
            db.execute('ALTER TABLE jobs ADD COLUMN migration_probe TEXT')
            db.execute('CREATE TABLE migration_probe(value TEXT)')
            db.execute("UPDATE jobs SET raw='temporary migration damage' WHERE id=(SELECT min(id) FROM jobs)")
            raise RuntimeError('injected migration failure')
        with patch.dict(database.MIGRATIONS, {1: fail_after_changes}):
            with self.assertRaisesRegex(RuntimeError, 'injected migration failure'):
                Store(self.path)
        self.assertEqual(snapshot(self.path), before)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='schema_migrations'").fetchone())
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='migration_probe'").fetchone())
        Store(self.path)
        self.assert_legacy_rows_preserved(before)

    def test_real_v1_rich_database_migrates_to_current_and_reopens_idempotently(self):
        before = self.v1_database()
        for reopen in range(3):
            with self.subTest(reopen=reopen):
                store = Store(self.path)
                self.assert_legacy_rows_preserved(before)
                with store.connect() as db:
                    self.assertEqual([row[0] for row in db.execute('SELECT version FROM schema_migrations ORDER BY version')],
                                     list(range(1, database.SCHEMA_VERSION + 1)))
                    self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
                actor = store.accounts.user('v1-preserved-session')
                self.assertEqual(actor['username'], 'migration_candidate')
                self.assertEqual({item['id'] for item in store.jobs({'dataset':'real'}, actor)}, {1,3})
                self.assertEqual(store.accounts.favorites(actor)[0]['note'], '必须逐字保留的旧收藏备注')
                self.assertIsNone(store.detail(2, actor))
                self.assertIsNone(store.detail(4, actor))

    def test_v2_failure_rolls_back_ddl_data_and_marker_in_an_existing_v1_database(self):
        before = self.v1_database()
        with closing(sqlite3.connect(self.path)) as db:
            before_markers = db.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall()
        original = database.MIGRATIONS[2]

        def fail_after_changes(db):
            original(db)
            db.execute('CREATE TABLE migration_v2_probe(value TEXT)')
            db.execute("UPDATE jobs SET raw='injected v2 migration damage' WHERE id=1")
            raise RuntimeError('injected v2 migration failure')

        with patch.dict(database.MIGRATIONS, {2: fail_after_changes}):
            with self.assertRaisesRegex(RuntimeError, 'injected v2 migration failure'):
                Store(self.path)
        self.assertEqual(snapshot(self.path), before)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall(), before_markers)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='migration_v2_probe'").fetchone())
        Store(self.path)
        self.assert_legacy_rows_preserved(before)

    def test_read_only_unit_of_work_rejects_repository_writes(self):
        store, actors = self.current_database()
        saved = store.manage_job(actors['boss_a'], self.job_body())
        before = snapshot(self.path)
        with self.assertRaises(sqlite3.OperationalError):
            with store.uow() as tx:
                tx.jobs.flags(saved['id'], False, False)
        self.assertEqual(snapshot(self.path), before)
        self.assertTrue(store.detail(saved['id'],actors['candidate'])['active'])

    def test_quality_metadata_keeps_same_snapshot_during_private_revision(self):
        store, actors = self.current_database()
        first = store.manage_job(actors['boss_a'], self.job_body())
        second = store.manage_job(actors['boss_a'], self.job_body())
        old = store.detail(first['id'],actors['boss_a'])
        secret = '不应出现在旧读取快照中的私有新岗位标题'
        original = JobRepository.metadata
        calls = []
        def revise_then_read(repository, ids):
            calls.append(tuple(ids))
            self.assertEqual(set(ids), {first['id'],second['id']})
            # A different connection commits while the reader still holds its WAL snapshot.
            store.manage_job(actors['boss_a'], dict(id=first['id'],expected_edit_token=old['edit_token'],
                             record=dict(title=secret,description='刚撤回后的私有新描述'),published=False))
            return original(repository, ids)
        with patch.object(JobRepository, 'metadata', revise_then_read):
            result = store.quality({'dataset':'real'}, actors['boss_b'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['duplicate_pairs'], 1)
        self.assertEqual({record['title'] for pair in result['duplicates'] for record in (pair['left'],pair['right'])}, {'同一公开岗位'})
        self.assertNotIn(secret, json.dumps(result,ensure_ascii=False))
        self.assertIsNone(store.detail(first['id'],actors['boss_b']))
        self.assertEqual(store.detail(first['id'],actors['boss_a'])['payload']['title'], secret)
        self.assertEqual(store.quality({'dataset':'real'},actors['boss_b'])['total'], 1)

    def test_detail_uses_explicit_fields_even_after_database_adds_secret_column(self):
        store, actors = self.current_database()
        saved = store.manage_job(actors['boss_a'], self.job_body())
        secret = 'database-only-secret-must-never-be-exported'
        with store.connect() as db:
            db.execute('ALTER TABLE jobs ADD COLUMN secret_column TEXT')
            db.execute('UPDATE jobs SET secret_column=? WHERE id=?',(secret,saved['id']))
        common = {'id','source','external_id','first_seen','last_seen','run_id','is_demo','active',
                  'owner_org_id','published','payload','requirements','can_review','quality_issues'}
        for name in ('candidate','boss_b','admin','boss_a'):
            with self.subTest(role=name):
                detail = store.detail(saved['id'], actors[name])
                private = name in ('admin','boss_a')
                allowed = common | ({'raw','fingerprint','revisions'} if private else set())
                if name == 'boss_a':
                    allowed.add('edit_token')
                    allowed.update(LIFECYCLE_FIELDS)
                self.assertEqual(set(detail), allowed)
                self.assertNotIn('secret_column', detail)
                self.assertNotIn(secret, json.dumps(detail,ensure_ascii=False))


if __name__ == '__main__':
    unittest.main(verbosity=2)
