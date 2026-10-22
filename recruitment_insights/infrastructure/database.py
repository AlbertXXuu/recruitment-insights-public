"""SQLite connection lifetime, atomic migrations, and transaction composition."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .accounts import AccountRepository, AuditRepository
from .jobs import JobRepository, RunRepository
from .reviews import ReviewRepository
from .resumes import ResumeRepository

SCHEMA_VERSION = 3
BASE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS organizations(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, source TEXT NOT NULL,
    label TEXT NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
    payload TEXT NOT NULL, fingerprint TEXT NOT NULL, raw TEXT NOT NULL, first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL, run_id INTEGER NOT NULL REFERENCES runs(id), is_demo INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1, UNIQUE(source,external_id));
CREATE INDEX IF NOT EXISTS jobs_source_active ON jobs(source,active);
CREATE TABLE IF NOT EXISTS revisions(id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
    run_id INTEGER NOT NULL REFERENCES runs(id), observed_at TEXT NOT NULL, payload TEXT NOT NULL, raw TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL, role TEXT NOT NULL, org_id INTEGER REFERENCES organizations(id),
    can_manage_jobs INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS favorites(user_id INTEGER NOT NULL REFERENCES users(id), job_id INTEGER NOT NULL REFERENCES jobs(id),
    note TEXT NOT NULL DEFAULT '', PRIMARY KEY(user_id,job_id));
CREATE TABLE IF NOT EXISTS audit_events(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
    org_id INTEGER, action TEXT NOT NULL, target TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS field_reviews(job_id INTEGER NOT NULL REFERENCES jobs(id), field TEXT NOT NULL,
    conclusion TEXT NOT NULL, source_hash TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id), reviewed_at TEXT NOT NULL,
    PRIMARY KEY(job_id,field), CHECK(field IN ('education','experience')));
CREATE TABLE IF NOT EXISTS duplicate_decisions(left_id INTEGER NOT NULL REFERENCES jobs(id),
    right_id INTEGER NOT NULL REFERENCES jobs(id), decision TEXT NOT NULL, reason TEXT NOT NULL,
    left_hash TEXT NOT NULL, right_hash TEXT NOT NULL, version INTEGER NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id), reviewed_at TEXT NOT NULL,
    PRIMARY KEY(left_id,right_id), CHECK(left_id<right_id));
'''
ADDITIVE_COLUMNS = {
    'jobs': [('owner_org_id', 'INTEGER REFERENCES organizations(id)'), ('published', 'INTEGER NOT NULL DEFAULT 1')],
    'runs': [('owner_org_id', 'INTEGER')],
    'audit_events': [('details', "TEXT NOT NULL DEFAULT '{}'")],
}
REQUIRED_COLUMNS = {
    'organizations': {'id','name'},
    'jobs': {'id','source','external_id','payload','fingerprint','raw','first_seen','last_seen',
             'run_id','is_demo','active','owner_org_id','published','source_active',
             'manual_inactive','lifecycle_reason','lifecycle_updated_at','lifecycle_version'},
    'runs': {'id','started_at','source','label','status','result','owner_org_id'},
    'revisions': {'id','job_id','run_id','observed_at','payload','raw'},
    'users': {'id','username','display_name','password_hash','role','org_id','can_manage_jobs'},
    'sessions': {'token_hash','user_id','expires'},
    'favorites': {'user_id','job_id','note'},
    'audit_events': {'id','user_id','org_id','action','target','created','details'},
    'field_reviews': {'job_id','field','conclusion','source_hash','state','version','user_id','reviewed_at'},
    'duplicate_decisions': {'left_id','right_id','decision','reason','left_hash','right_hash','version','user_id','reviewed_at'},
    'resumes': {'user_id','filename','media_type','size_bytes','content','sha256','uploaded_at'},
}


def migrate_v1(db):
    # execute each DDL inside the caller's transaction: executescript commits implicitly.
    for statement in BASE_SCHEMA.split(';'):
        if statement.strip():
            db.execute(statement)
    for table, columns in ADDITIVE_COLUMNS.items():
        known = {row['name'] for row in db.execute('PRAGMA table_info('+table+')')}
        for column, definition in columns:
            if column not in known:
                db.execute('ALTER TABLE '+table+' ADD COLUMN '+column+' '+definition)


def migrate_v2(db):
    # Preserve every existing business column. Legacy public inactivity came
    # from an authoritative source; legacy company inactivity was manual.
    columns = (
        ('source_active', 'INTEGER NOT NULL DEFAULT 1 CHECK(source_active IN (0,1))'),
        ('manual_inactive', 'INTEGER NOT NULL DEFAULT 0 CHECK(manual_inactive IN (0,1))'),
        ('lifecycle_reason', "TEXT NOT NULL DEFAULT ''"),
        ('lifecycle_updated_at', "TEXT NOT NULL DEFAULT ''"),
        ('lifecycle_version', 'INTEGER NOT NULL DEFAULT 0'),
    )
    known = {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}
    for column, definition in columns:
        if column not in known:
            db.execute('ALTER TABLE jobs ADD COLUMN '+column+' '+definition)
    if 'source_active' not in known or 'manual_inactive' not in known:
        db.execute('''UPDATE jobs SET
            source_active=CASE WHEN active=0 AND owner_org_id IS NULL THEN 0 ELSE 1 END,
            manual_inactive=CASE WHEN active=0 AND owner_org_id IS NOT NULL THEN 1 ELSE 0 END''')


def migrate_v3(db):
    # New private content only. Existing users, sessions and business rows stay intact.
    db.execute('''CREATE TABLE IF NOT EXISTS resumes(
        user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        filename TEXT NOT NULL,
        media_type TEXT NOT NULL CHECK(media_type IN
            ('application/pdf','application/vnd.openxmlformats-officedocument.wordprocessingml.document')),
        size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 1 AND 2097152),
        content BLOB NOT NULL CHECK(typeof(content)='blob' AND length(content)=size_bytes),
        sha256 TEXT NOT NULL CHECK(length(sha256)=64),
        uploaded_at TEXT NOT NULL)''')


MIGRATIONS = {1: migrate_v1, 2: migrate_v2, 3: migrate_v3}


class Connection(sqlite3.Connection):
    @property
    def audit(self):
        # Legacy local tools pass Store.connect() to Accounts.audit(). Keep that
        # structural contract at this boundary, outside the application services.
        return AuditRepository(self)


class Transaction:
    def __init__(self, connection):
        self.jobs = JobRepository(connection)
        self.runs = RunRepository(connection)
        self.accounts = AccountRepository(connection)
        self.reviews = ReviewRepository(connection)
        self.audit = AuditRepository(connection)
        self.resumes = ResumeRepository(connection)


class Database:
    def __init__(self, path):
        self.path = Path(path)

    def _open(self):
        db = sqlite3.connect(self.path, timeout=30, factory=Connection)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            versions = [row[0] for row in db.execute('SELECT version FROM schema_migrations ORDER BY version')]
            if versions != list(range(1, len(versions)+1)) or (versions and versions[-1] > SCHEMA_VERSION):
                raise ValueError('数据库版本不受当前程序支持，请使用匹配版本，已有数据未修改')
            for version in range(len(versions)+1, SCHEMA_VERSION+1):
                MIGRATIONS[version](db)
                db.execute('INSERT INTO schema_migrations(version) VALUES(?)', (version,))
            # A version marker is not evidence that an externally modified schema is valid.
            for table, columns in REQUIRED_COLUMNS.items():
                actual = {row['name'] for row in db.execute('PRAGMA table_info('+table+')')}
                if not columns <= actual:
                    raise ValueError('数据库结构与版本不符：'+table+'，请恢复匹配的数据库备份')
            if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise ValueError('数据库存在无关联的记录，请核对或恢复数据库备份，已有数据未修改')

    @contextmanager
    def connect(self):
        """Compatibility connection for trusted maintenance tools only."""
        db = self._open()
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def uow(self, *, write=False):
        db = self._open()
        try:
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            if not write:
                db.execute('PRAGMA query_only=ON')
            yield Transaction(db)
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
