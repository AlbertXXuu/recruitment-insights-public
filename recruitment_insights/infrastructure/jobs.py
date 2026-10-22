"""SQLite job and import-run repositories. No repository commits a transaction."""
LIST_COLUMNS = 'id,source,payload,first_seen,last_seen,run_id,is_demo,owner_org_id,published,active'


class JobRepository:
    def __init__(self, connection):
        self.db = connection

    def get(self, job_id):
        row = self.db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        return dict(row) if row else None

    def by_source(self, source, external_id):
        row = self.db.execute('SELECT * FROM jobs WHERE source=? AND external_id=?', (source, external_id)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _active_where(*, is_demo=None, source=None, visibility='all', org_id=None):
        clauses, args = ['active=1'], []
        if is_demo is not None:
            clauses.append('is_demo=?')
            args.append(int(is_demo))
        if source:
            clauses.append('source=?')
            args.append(source)
        public = '(owner_org_id IS NULL OR published<>0)'
        if visibility == 'public':
            clauses.append(public)
        elif visibility == 'company':
            clauses.append('owner_org_id=?')
            args.append(org_id)
        elif visibility == 'public_or_company':
            clauses.append('(' + public + ' OR owner_org_id=?)')
            args.append(org_id)
        elif visibility != 'all':
            raise ValueError('岗位读取范围无效')
        return ' FROM jobs WHERE ' + ' AND '.join(clauses), args

    def active(self, **scope):
        clause, args = self._active_where(**scope)
        query = 'SELECT ' + LIST_COLUMNS + clause + ' ORDER BY id DESC'
        return [dict(row) for row in self.db.execute(query, args)]

    def iter_active(self, *, requirement_evidence_only=False, **scope):
        """Stream evidence inside the caller's read snapshot, without a full list."""
        clause, args = self._active_where(**scope)
        # Aggregates need original education/experience values, while the
        # description already comes from payload. Keep complete raw evidence
        # for ordinary repository callers and detail/review/edit operations.
        # -> plus json() preserves JSON booleans, arrays, objects and null;
        # json_extract alone would turn true/false into 1/0.
        raw = ("CASE WHEN json_valid(raw) THEN "
               "json_object('education',json(raw -> '$.education'),"
               "'experience',json(raw -> '$.experience')) ELSE raw END AS raw"
               if requirement_evidence_only else 'raw')
        cursor = self.db.execute('SELECT ' + LIST_COLUMNS + ',' + raw + clause + ' ORDER BY id DESC', args)
        try:
            for row in cursor:
                yield dict(row)
        finally:
            cursor.close()

    def active_page(self, *, offset, limit, **scope):
        """Only for filters fully covered by the shared relational predicates."""
        clause, args = self._active_where(**scope)
        total = self.db.execute('SELECT count(*)' + clause, args).fetchone()[0]
        if offset >= total:
            return [], total
        query = 'SELECT ' + LIST_COLUMNS + clause + ' ORDER BY id DESC LIMIT ? OFFSET ?'
        return [dict(row) for row in self.db.execute(query, [*args, limit, offset])], total

    def raw_inputs(self, ids):
        """Fetch original evidence only for content-filtered jobs in this UOW."""
        result = {}
        for start in range(0, len(ids), 800):
            chunk = ids[start:start+800]
            query = 'SELECT id,raw FROM jobs WHERE id IN (' + ','.join('?' for _ in chunk) + ')'
            result.update((row['id'], row['raw']) for row in self.db.execute(query, chunk))
        return result

    def company(self, org_id):
        return [dict(row) for row in self.db.execute('SELECT * FROM jobs WHERE owner_org_id=? ORDER BY id DESC', (org_id,))]

    def public(self, is_demo, status):
        clause = 'owner_org_id IS NULL AND is_demo=?'
        args = [int(is_demo)]
        if status != 'all':
            clause += ' AND active=?'
            args.append(int(status == 'active'))
        return [dict(row) for row in self.db.execute('SELECT * FROM jobs WHERE '+clause+' ORDER BY id DESC', args)]

    def metadata(self, ids):
        result = {}
        for start in range(0, len(ids), 800):
            chunk = ids[start:start+800]
            query = 'SELECT id,owner_org_id,fingerprint,payload,source FROM jobs WHERE id IN (' + ','.join('?' for _ in chunk) + ')'
            result.update((row['id'], dict(row)) for row in self.db.execute(query, chunk))
        return result

    def active_source(self, source):
        # A manually hidden job still belongs to the source snapshot.
        return [dict(row) for row in self.db.execute('SELECT id,external_id FROM jobs WHERE source=? AND source_active=1', (source,))]

    def insert(self, values):
        return self.db.execute('''INSERT INTO jobs(source,external_id,payload,fingerprint,raw,
            first_seen,last_seen,run_id,is_demo,owner_org_id,published) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            tuple(values[key] for key in ('source','external_id','payload','fingerprint','raw',
                                         'first_seen','last_seen','run_id','is_demo','owner_org_id','published'))).lastrowid

    def update(self, job_id, values):
        self.db.execute('''UPDATE jobs SET payload=?,fingerprint=?,raw=?,last_seen=?,run_id=?,
            is_demo=?,lifecycle_updated_at=CASE WHEN source_active=0 THEN ? ELSE lifecycle_updated_at END,
            source_active=1,active=CASE WHEN manual_inactive=0 THEN 1 ELSE 0 END WHERE id=?''',
            tuple(values[key] for key in ('payload','fingerprint','raw','last_seen','run_id','is_demo')) +
            (values['last_seen'], job_id))

    def flags(self, job_id, published, active):
        self.db.execute('''UPDATE jobs SET published=?,manual_inactive=?,
            active=CASE WHEN source_active=1 AND ?=1 THEN 1 ELSE 0 END WHERE id=?''',
                        (int(published), int(not active), int(active), job_id))

    def lifecycle(self, job_id, manual_inactive, reason, stamp):
        self.db.execute('''UPDATE jobs SET manual_inactive=?,lifecycle_reason=?,lifecycle_updated_at=?,
            lifecycle_version=lifecycle_version+1,
            active=CASE WHEN source_active=1 AND ?=0 THEN 1 ELSE 0 END WHERE id=?''',
                        (int(manual_inactive), reason, stamp, int(manual_inactive), job_id))

    def deactivate(self, job_id, run_id, stamp):
        self.db.execute('''UPDATE jobs SET source_active=0,active=0,run_id=?,
            lifecycle_updated_at=? WHERE id=?''', (run_id, stamp, job_id))

    def add_revision(self, job_id, run_id, stamp, payload, raw):
        self.db.execute('INSERT INTO revisions(job_id,run_id,observed_at,payload,raw) VALUES(?,?,?,?,?)',
                        (job_id, run_id, stamp, payload, raw))

    def revisions(self, job_id):
        return [dict(row) for row in self.db.execute('SELECT run_id,observed_at,payload FROM revisions WHERE job_id=? ORDER BY id DESC', (job_id,))]


class RunRepository:
    def __init__(self, connection):
        self.db = connection

    def create(self, stamp, source, label, status, result, org_id=None):
        return self.db.execute('INSERT INTO runs(started_at,source,label,status,result,owner_org_id) VALUES(?,?,?,?,?,?)',
                               (stamp, source, label, status, result, org_id)).lastrowid

    def finish(self, run_id, status, result):
        self.db.execute('UPDATE runs SET status=?,result=? WHERE id=?', (status, result, run_id))

    def recent(self, actor):
        clause, args = ('1=1', []) if actor is None or actor['role'] == 'admin' else ('owner_org_id=?', [actor['org_id']])
        return [dict(row) for row in self.db.execute('SELECT * FROM runs WHERE '+clause+' ORDER BY id DESC LIMIT 50', args)]
