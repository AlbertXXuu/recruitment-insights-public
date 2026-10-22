"""Shared read models. Each public query uses one consistent read snapshot."""
import csv
import io
import json
from itertools import islice
from threading import BoundedSemaphore

from recruitment_insights.domain.access import can_read_job, may_manage_lifecycle, may_review
from recruitment_insights.domain.jobs import analyze, edit_token, lifecycle_state
from recruitment_insights.domain.quality import issues, quality_report
from recruitment_insights.domain.reviews import apply_fields, effective_fields, propose, source_input
from .identity import live_actor
from .ports import UnitOfWork
from .reviews import report as duplicate_report

DETAIL_FIELDS = ('id','source','external_id','first_seen','last_seen','run_id',
                 'is_demo','active','owner_org_id','published')
EXPORT_FIELDS = ('job_id','title','company','location','category','salary_text',
    'salary_monthly_min','salary_monthly_max','currency','salary_status',
    'salary_normalized_period','salary_source_period','education','education_status',
    'education_condition','education_alternatives','experience','experience_status',
    'experience_condition','experience_min_years','experience_max_years','source','source_url','is_demo')


class Queries:
    def __init__(self, uow: UnitOfWork):
        self.uow = uow
        # Full aggregates compete for Python projection and SQLite row decoding.
        # One per instance avoids the measured thread contention. Acquire before
        # opening a snapshot, so queued callers get current data and permissions.
        # Detail, pagination and writes deliberately do not use this semaphore.
        self._aggregate_slots = BoundedSemaphore(1)

    @staticmethod
    def current(tx, actor):
        # None is retained solely for trusted local scripts, never HTTP requests.
        return live_actor(tx, actor) if actor is not None else None

    @staticmethod
    def read_scope(filters, actor):
        scope = filters.get('scope')
        org_id = actor.get('org_id') if actor else None
        if actor is None or actor['role'] == 'admin':
            visibility = 'all'
        elif actor['role'] in ('boss', 'hr'):
            visibility = 'public_or_company'
        elif actor['role'] == 'candidate':
            visibility = 'public'
        else:
            return None
        if scope == 'company':
            if not org_id:
                return None
            visibility = 'company'
        elif scope == 'public':
            visibility = 'public'
        dataset = filters.get('dataset', 'real')
        is_demo = {'real': False, 'demo': True}.get(dataset)
        return dict(is_demo=is_demo, source=filters.get('source'), visibility=visibility, org_id=org_id)

    @staticmethod
    def visible(row, filters, actor):
        if not can_read_job(row, actor):
            return False
        if filters.get('scope') == 'company' and row['owner_org_id'] != (actor or {}).get('org_id'):
            return False
        return not (filters.get('scope') == 'public' and row['owner_org_id'] is not None and not row['published'])

    @staticmethod
    def payload(row):
        job = json.loads(row['payload'])
        job.update(id=row['id'], source=row['source'], is_demo=bool(row['is_demo']),
                   last_seen=row['last_seen'], first_seen=row['first_seen'], run_id=row['run_id'],
                   owner_org_id=row['owner_org_id'], published=bool(row['published']), active=bool(row['active']))
        return job

    @staticmethod
    def matches_content(job, filters):
        if filters.get('location') and filters['location'] not in job['locations']:
            return False
        if any(filters.get(key) and job[key] != filters[key] for key in ('category', 'source')):
            return False
        if filters.get('skill') and filters['skill'] not in job['skills']:
            return False
        query = filters.get('q', '').casefold()
        return not query or query in (job['title']+'\n'+job['company']+'\n'+job['description']).casefold()

    @staticmethod
    def matches_requirements(job, filters):
        return not any(filters.get(key) and job[key] != filters[key] for key in ('education', 'experience'))

    @staticmethod
    def project(tx, matched):
        ids = [row['id'] for row, job in matched]
        raw = tx.jobs.raw_inputs(ids)
        saved = tx.reviews.fields(ids)
        return [apply_fields(job, effective_fields(dict(row, raw=raw[row['id']]), saved, payload=job))
                for row, job in matched]

    def jobs_in(self, tx, filters, actor):
        filters = filters or {}
        scope = self.read_scope(filters, actor)
        if scope is None:
            return []
        matched = []
        for row in tx.jobs.active(**scope):
            # Keep the domain guard after the SQL prefilter. The actor was
            # refreshed inside this same read snapshot, never taken from filters.
            if not self.visible(row, filters, actor):
                continue
            job = self.payload(row)
            if not self.matches_content(job, filters):
                continue
            matched.append((row, job))
        if not matched:
            return []
        # Raw evidence and reviews share the initial query's read snapshot.
        # Only matching content reaches the expensive requirement projection.
        return [job for job in self.project(tx, matched) if self.matches_requirements(job, filters)]

    def iter_jobs_in(self, tx, filters, actor):
        """Bound transient evidence and review maps to one batch in this snapshot."""
        scope = self.read_scope(filters, actor)
        if scope is None:
            return
        rows = tx.jobs.iter_active(requirement_evidence_only=True, **scope)
        try:
            while batch := list(islice(rows, 128)):
                matched = []
                for row in batch:
                    if self.visible(row, filters, actor):
                        job = self.payload(row)
                        if self.matches_content(job, filters):
                            matched.append((row, job))
                saved = tx.reviews.fields([row['id'] for row, job in matched])
                for row, job in matched:
                    apply_fields(job, effective_fields(row, saved, payload=job))
                    if self.matches_requirements(job, filters):
                        yield job
        finally:
            rows.close()

    def jobs(self, filters=None, actor=None):
        with self.uow() as tx:
            return self.jobs_in(tx, filters, self.current(tx, actor))

    def detail_in(self, tx, job_id, actor):
        row = tx.jobs.get(job_id)
        if row is None or not can_read_job(row, actor, include_inactive=True):
            return None
        result = {key:row[key] for key in DETAIL_FIELDS}
        result['payload'] = json.loads(row['payload'])
        result['requirements'] = effective_fields(row, tx.reviews.fields([row['id']]), payload=result['payload'])
        result['can_review'] = may_review(row, actor)
        private = actor is None or actor['role'] == 'admin' or (
            actor['role'] in ('boss','hr') and row['owner_org_id'] is not None and row['owner_org_id'] == actor.get('org_id'))
        if private:
            result['raw'] = json.loads(row['raw'])
            result['fingerprint'] = row['fingerprint']
            result['revisions'] = [dict(revision, payload=json.loads(revision['payload'])) for revision in tx.jobs.revisions(job_id)]
        else:
            for field, item in result['requirements'].items():
                if item['status'] == 'stale':
                    result['requirements'][field] = dict(propose(source_input(row, result['payload']))[field], status='stale')
        own = bool(actor and actor['role'] in ('boss', 'hr') and row['owner_org_id'] is not None
                   and row['owner_org_id'] == actor.get('org_id'))
        if own or may_manage_lifecycle(row, actor):
            result.update(lifecycle_state(row))
        if may_manage_lifecycle(row, actor):
            result['edit_token'] = edit_token(row)
        apply_fields(result['payload'], result['requirements'])
        result['quality_issues'] = issues(dict(result['payload'], is_demo=bool(row['is_demo'])))
        return result

    def detail(self, job_id, actor=None):
        with self.uow() as tx:
            return self.detail_in(tx, job_id, self.current(tx, actor))

    def company_jobs(self, actor):
        with self.uow() as tx:
            actor = self.current(tx, actor)
            if not actor or actor['role'] not in ('boss','hr'):
                raise PermissionError('仅企业账号可查看本企业岗位')
            rows = tx.jobs.company(actor['org_id'])
            saved = tx.reviews.fields([row['id'] for row in rows])
            result = []
            for row in rows:
                job = dict(json.loads(row['payload']), id=row['id'], published=bool(row['published']),
                           is_demo=bool(row['is_demo']), **lifecycle_state(row))
                if may_manage_lifecycle(row, actor):
                    job['edit_token'] = edit_token(row)
                result.append(apply_fields(job, effective_fields(row, saved, payload=job)))
            return result

    def admin_jobs(self, filters, actor):
        with self.uow() as tx:
            actor = self.current(tx, actor)
            if not actor or actor['role'] != 'admin':
                raise PermissionError('仅数据管理员可查看公开源岗位维护列表')
            dataset = filters.get('dataset', 'real')
            status = filters.get('status', 'all')
            if dataset not in ('real', 'demo'):
                raise ValueError('数据范围无效')
            if status not in ('all', 'active', 'inactive'):
                raise ValueError('岗位状态筛选无效')
            query = filters.get('q', '')
            if not isinstance(query, str):
                raise ValueError('搜索条件必须为文字')
            query = query.casefold()
            page = max(1, int(filters.get('page', '1')))
            result = []
            for row in tx.jobs.public(dataset == 'demo', status):
                payload = json.loads(row['payload'])
                searchable = '\n'.join(payload[key] for key in ('title', 'company', 'location', 'description'))+'\n'+row['source']
                if query not in searchable.casefold():
                    continue
                result.append(dict(id=row['id'], title=payload['title'], company=payload['company'],
                                   location=payload['location'], source=row['source'],
                                   is_demo=bool(row['is_demo']), last_seen=row['last_seen'],
                                   **lifecycle_state(row), edit_token=edit_token(row)))
            start = (page-1)*15
            return dict(items=result[start:start+15], total=len(result), page=page)

    def quality(self, filters, actor):
        with self.uow() as tx:
            actor = self.current(tx, actor)
            if not actor or actor['role'] not in ('admin','boss','hr'):
                raise PermissionError('当前账号没有质量检查权限')
            jobs = self.jobs_in(tx, filters, actor)
            duplicates = duplicate_report(tx, jobs, actor, filters.get('duplicate_page',1))
            return dict(quality_report(jobs, duplicates.pop('duplicate_pending_ids')), **duplicates)

    def runs(self, actor=None):
        with self.uow() as tx:
            actor = self.current(tx, actor)
            if actor is not None and actor['role'] not in ('admin','boss','hr'):
                raise PermissionError('当前账号没有同步记录权限')
            return [dict(row, result=json.loads(row['result'])) for row in tx.runs.recent(actor)]

    def audit(self, actor):
        with self.uow() as tx:
            actor = self.current(tx, actor)
            if not actor or actor['role'] not in ('admin','boss','hr'):
                raise PermissionError('当前账号没有操作记录权限')
            return tx.audit.recent(actor)

    def paginated(self, filters, actor):
        page = max(1, int(filters.get('page','1')))
        start = (page-1)*15
        with self.uow() as tx:
            actor = self.current(tx, actor)
            scope = self.read_scope(filters, actor)
            items, total = [], 0
            if scope is not None:
                if any(filters.get(key) for key in ('education', 'experience')):
                    for job in self.iter_jobs_in(tx, filters, actor):
                        if start <= total < start+15:
                            items.append(job)
                        total += 1
                elif (any(filters.get(key) for key in ('q', 'location', 'category', 'skill'))
                      or (filters.get('scope') == 'company' and actor and actor['role'] == 'candidate')):
                    # A downgraded account may still carry an organization ID.
                    # Its company scope also needs the domain's public-only guard
                    # before counting, so drafts cannot leak through page totals.
                    matched = []
                    for row in tx.jobs.active(**scope):
                        if not self.visible(row, filters, actor):
                            continue
                        job = self.payload(row)
                        if self.matches_content(job, filters):
                            if start <= total < start+15:
                                matched.append((row, job))
                            total += 1
                    items = self.project(tx, matched)
                else:
                    rows, total = tx.jobs.active_page(offset=start, limit=15, **scope)
                    items = self.project(tx, [(row, self.payload(row)) for row in rows
                                              if self.visible(row, filters, actor)])
            return dict(items=items, total=total, page=page, page_size=15)

    def stats(self, filters, actor):
        with self._aggregate_slots, self.uow() as tx:
            return analyze(self.iter_jobs_in(tx, filters, self.current(tx, actor)))

    @staticmethod
    def option_sets():
        return {key: set() for key in ('category', 'education', 'experience', 'source', 'location')}

    @staticmethod
    def add_options(options, job):
        for key in ('category', 'education', 'experience', 'source'):
            options[key].add(job[key])
        options['location'].update(job['locations'])

    def options(self, filters, actor):
        base = {'dataset':filters.get('dataset','real'), 'scope':filters.get('scope','all')}
        options = self.option_sets()
        with self._aggregate_slots, self.uow() as tx:
            for job in self.iter_jobs_in(tx, base, self.current(tx, actor)):
                self.add_options(options, job)
        return {key: sorted(values) for key, values in options.items()}

    def overview(self, filters, actor, include_options=True):
        """One scan and one authorization snapshot for the whole first screen."""
        page = max(1, int(filters.get('page', '1')))
        start, items, options = (page-1)*15, [], self.option_sets()
        scan = ({'dataset': filters.get('dataset', 'real'), 'scope': filters.get('scope', 'all')}
                if include_options else filters)
        with self._aggregate_slots, self.uow() as tx:
            actor = self.current(tx, actor)

            def matching():
                total = 0
                for job in self.iter_jobs_in(tx, scan, actor):
                    if include_options:
                        self.add_options(options, job)
                        if not self.matches_content(job, filters) or not self.matches_requirements(job, filters):
                            continue
                    if start <= total < start+15:
                        items.append(job)
                    total += 1
                    yield job

            stats = analyze(matching())
        result = dict(stats=stats, jobs=dict(items=items, total=stats['total'], page=page, page_size=15))
        if include_options:
            result['options'] = {key: sorted(values) for key, values in options.items()}
        return result

    def export(self, filters, actor):
        out = io.StringIO(newline='')
        writer = csv.DictWriter(out, fieldnames=EXPORT_FIELDS, extrasaction='ignore')
        writer.writeheader()
        for job in self.jobs(filters, actor):
            row = dict(job, salary_monthly_min=job['salary_low'], salary_monthly_max=job['salary_high'],
                       salary_normalized_period='month' if job['salary_status']=='有效' else '', salary_source_period=job['salary_period'])
            writer.writerow({key:("'"+value if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')) else value)
                             for key,value in row.items()})
        return out.getvalue().encode('utf-8-sig')
