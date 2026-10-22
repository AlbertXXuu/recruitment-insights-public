"""Enterprise job lifecycle, including permission and edit-version checks."""
import json
import uuid

from recruitment_insights.domain.access import can_manage, may_manage_lifecycle
from recruitment_insights.domain.errors import ConflictError
from recruitment_insights.domain.jobs import edit_token, lifecycle_state, normalize, now
from recruitment_insights.domain.reviews import integer
from .accounts import Accounts
from .identity import live_actor
from .ports import UnitOfWork


class JobService:
    def __init__(self, uow: UnitOfWork, importer):
        self.uow, self.importer = uow, importer

    def set_lifecycle(self, actor, body):
        if not isinstance(body, dict):
            raise ValueError('岗位状态请求必须为对象')
        job_id = integer(body.get('id'), '岗位 ID')
        action = body.get('action')
        if action not in ('deactivate', 'restore'):
            raise ValueError('岗位状态操作无效')
        reason = body.get('reason', '')
        if action == 'deactivate':
            if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
                raise ValueError('下架理由须为 1–500 个字符')
            reason = reason.strip()
        else:
            reason = ''
        with self.uow(write=True) as tx:
            current = live_actor(tx, actor)
            previous = tx.jobs.get(job_id)
            if not previous or not may_manage_lifecycle(previous, current):
                raise PermissionError('没有该岗位的下架或恢复权限')
            if body.get('expected_edit_token') != edit_token(previous):
                raise ConflictError('岗位内容或状态已更新，请重新载入岗位后操作；当前操作未覆盖已有状态')
            before = lifecycle_state(previous)
            manual_inactive = action == 'deactivate'
            # A repeat preserves the change time but advances the state version,
            # so an accepted operation cannot reuse a previously issued token.
            changed = manual_inactive != before['manual_inactive'] or reason != before['lifecycle_reason']
            stamp = now() if changed else before['lifecycle_updated_at']
            tx.jobs.lifecycle(job_id, manual_inactive, reason, stamp)
            row = tx.jobs.get(job_id)
            after = lifecycle_state(row)
            Accounts.audit(tx, current, action+'_job', job_id,
                           dict(reason=reason, before=before, after=after))
            return dict(id=job_id, **after, edit_token=edit_token(row))

    def manage_job(self, actor, body):
        job_id = body.get('id')
        if job_id is not None:
            job_id = integer(job_id, '岗位 ID')
        record = body.get('record', {})
        if not isinstance(record, dict):
            raise ValueError('record 必须是对象')
        allowed = {'title','location','description','salary_text','salary_min','salary_max',
                   'currency','salary_period','education','experience','source_url'}
        with self.uow(write=True) as tx:
            current = live_actor(tx, actor)
            if not current.get('org_id') or not can_manage(current):
                raise PermissionError('没有本企业岗位维护权限')
            previous = tx.jobs.get(job_id) if job_id is not None else None
            if job_id is not None and (not previous or previous['owner_org_id'] != current['org_id']):
                raise PermissionError('岗位不属于本企业')
            if previous and body.get('expected_edit_token') != edit_token(previous):
                raise ConflictError('岗位内容或发布状态已更新，请重新打开岗位后保存；当前表单未覆盖已有内容')
            raw = json.loads(previous['raw']) if previous else dict(job_id='ORG-'+uuid.uuid4().hex[:12])
            raw.update({key:value for key,value in record.items() if key in allowed})
            raw['company'] = tx.accounts.org_name(current['org_id'])
            source = previous['source'] if previous else 'org:'+str(current['org_id'])+':manual'
            demo = body.get('is_demo', bool(previous['is_demo']) if previous else True)
            published = body.get('published', bool(previous['published']) if previous else False)
            active = body.get('active', not bool(previous['manual_inactive']) if previous else True)
            if any(not isinstance(value,bool) for value in (demo,published,active)):
                raise ValueError('发布、启用和模拟状态必须为布尔值')
            normalized = normalize(raw)
            result = self.importer.apply(tx, [raw], source, '企业岗位维护', demo, owner_org_id=current['org_id'])
            job_id = tx.jobs.by_source(source, normalized['job_id'])['id']
            tx.jobs.flags(job_id, published, active)
            if (not active) != (bool(previous['manual_inactive']) if previous else False):
                reason = '企业岗位维护：取消启用' if not active else ''
                tx.jobs.lifecycle(job_id, not active, reason, now())
            row = tx.jobs.get(job_id)
            after = lifecycle_state(row)
            Accounts.audit(tx, current, 'manage_job', job_id,
                           dict(reason=after['lifecycle_reason'],
                                before=lifecycle_state(previous) if previous else None, after=after))
            return dict(result, id=job_id, published=published, **after, edit_token=edit_token(row))
