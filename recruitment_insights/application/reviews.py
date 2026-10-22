"""Transactional review use cases; SQL is delegated to repository ports."""
import json

from recruitment_insights.domain.access import may_review, may_read_duplicate_reason
from recruitment_insights.domain.errors import ConflictError
from recruitment_insights.domain.jobs import now
from recruitment_insights.domain.requirements import field_fingerprint, validate_review
from recruitment_insights.domain.reviews import source_input, duplicate_key, duplicate_status, integer
from .identity import live_actor
from .accounts import Accounts

def editable_row(tx, job_id, actor):
    row = tx.jobs.get(job_id)
    if row is None or not may_review(row, actor):
        raise PermissionError('只能核验有维护权限的岗位')
    return row


class ReviewService:
    def __init__(self, uow, detail_reader):
        self.uow = uow
        self.detail_reader = detail_reader

    def review_field(self, actor, body):
        job_id = integer(body.get('job_id'), '岗位 ID')
        field, action = body.get('field'), body.get('action')
        if field not in ('education', 'experience') or action not in ('confirm', 'revoke'):
            raise ValueError('核验字段或操作无效')
        version = integer(body.get('version'), '核验版本')
        with self.uow(write=True) as tx:
            current_actor = live_actor(tx, actor)
            row = editable_row(tx, job_id, current_actor)
            raw = source_input(row)
            source_hash = field_fingerprint(raw, field)
            previous = tx.reviews.field(job_id, field)
            previous_version = previous['version'] if previous else 0
            if body.get('source_hash') != source_hash or version != previous_version:
                raise ConflictError('原文或核验结论已更新，请重新载入后核验')
            if action == 'revoke' and (not previous or previous['state'] != 'confirmed'):
                raise ValueError('没有可撤销的人工结论')
            conclusion = validate_review(field, body, raw) if action == 'confirm' else json.loads(previous['conclusion'])
            current = dict(conclusion=json.dumps(conclusion, ensure_ascii=False), source_hash=source_hash,
                           state='confirmed' if action == 'confirm' else 'revoked', version=version + 1,
                           user_id=current_actor['id'], reviewed_at=now())
            tx.reviews.save_field(job_id, field, current)
            Accounts.audit(tx, current_actor, 'review_' + action, job_id,
                           dict(field=field, before=dict(previous) if previous else None, after=current))
        return self.detail_reader(job_id, actor)

    def decide_duplicate(self, actor, body):
        left_id = integer(body.get('left_id'), '左侧岗位 ID')
        right_id = integer(body.get('right_id'), '右侧岗位 ID')
        if left_id >= right_id:
            raise ValueError('两个岗位 ID 必须按升序排列且不同')
        decision = body.get('decision')
        if decision not in ('same', 'different', 'revoke'):
            raise ValueError('重复判断操作无效')
        reason = body.get('reason', '')
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000:
            raise ValueError('请填写 1–1000 字符的判断或撤销理由')
        version = integer(body.get('version'), '判断版本')
        with self.uow(write=True) as tx:
            current_actor = live_actor(tx, actor)
            left = editable_row(tx, left_id, current_actor)
            right = editable_row(tx, right_id, current_actor)
            previous = tx.reviews.decision(left_id, right_id)
            if (body.get('left_hash') != left['fingerprint'] or body.get('right_hash') != right['fingerprint'] or
                    version != (previous['version'] if previous else 0)):
                raise ConflictError('岗位内容或重复判断已更新，请重新载入')
            same_key = (duplicate_key(dict(json.loads(left['payload']), is_demo=left['is_demo'])) ==
                        duplicate_key(dict(json.loads(right['payload']), is_demo=right['is_demo'])))
            if not same_key and not previous:
                raise ValueError('这两个岗位不属于当前重复候选')
            if decision == 'same' and left['is_demo'] != right['is_demo']:
                raise ValueError('真实和模拟记录不能确认为同一岗位')
            if decision == 'revoke' and (not previous or previous['decision'] == 'revoked'):
                raise ValueError('没有可撤销的判断')
            current = dict(decision='revoked' if decision == 'revoke' else decision, reason=reason.strip(),
                           left_hash=left['fingerprint'], right_hash=right['fingerprint'], version=version + 1,
                           user_id=current_actor['id'], reviewed_at=now())
            tx.reviews.save_decision(left_id, right_id, current)
            Accounts.audit(tx, current_actor, 'duplicate_' + decision, f'{left_id}:{right_id}',
                           dict(before=dict(previous) if previous else None, after=current))
        return {'message': '重复判断已保存；原始来源记录和记录总数保持不变'}


def report(tx, jobs, actor, page=1):
    from collections import Counter, defaultdict
    from itertools import combinations, islice
    page=int(page)
    if page < 1:
        raise ValueError('重复候选页码必须大于零')
    groups = defaultdict(list)
    visible = {j['id']: j for j in jobs}
    keys = {}
    for job in jobs:
        keys[job['id']] = duplicate_key(job)
        groups[keys[job['id']]].append(job['id'])
    saved = {pair: dict(row) for pair, row in tx.reviews.decisions().items()
             if pair[0] in visible and pair[1] in visible}
    # Caller supplies the transaction that produced jobs, preserving one read snapshot.
    meta = tx.jobs.metadata(list(visible))
    total = sum(len(group)*(len(group)-1)//2 for group in groups.values())
    historical = [pair for pair in saved if keys[pair[0]] != keys[pair[1]]]
    total += len(historical)
    statuses = {pair: duplicate_status(decision, meta[pair[0]], meta[pair[1]])
                for pair, decision in saved.items()}
    status_counts = dict.fromkeys(('pending', 'same', 'different', 'stale', 'revoked'), 0)
    status_counts['pending'] = total - len(saved)
    reviewed_neighbors = Counter()
    pending_ids = set()
    for pair, status in statuses.items():
        status_counts[status] += 1
        if keys[pair[0]] == keys[pair[1]]:
            reviewed_neighbors.update(pair)
        if status in ('stale', 'revoked'):
            pending_ids.update(pair)
    # Count unresolved records across every page without materializing all O(n²) pairs.
    for group in groups.values():
        pending_ids.update(job_id for job_id in group if reviewed_neighbors[job_id] < len(group)-1)
    pending_pairs = sum(status_counts[status] for status in ('pending', 'stale', 'revoked'))
    urgent = [pair for pair, status in statuses.items() if status in ('stale', 'revoked')]
    urgent_set=set(urgent)
    def ordered_pairs():
        yield from urgent
        for group in groups.values():
            for pair in combinations(sorted(group),2):
                if pair not in saved:
                    yield pair
        yield from (pair for pair in saved if pair not in urgent_set)
    pages=max(1,(total+99)//100)
    page=min(page,pages)
    pairs=list(islice(ordered_pairs(),(page-1)*100,page*100))
    result = []
    for left_id,right_id in pairs:
        previous = saved.get((left_id,right_id))
        left,right = meta[left_id],meta[right_id]
        decision = duplicate_status(previous, left, right)
        can_read_reason = may_read_duplicate_reason(left, right, actor)
        left_display=dict(json.loads(left['payload']),id=left_id,source=left['source'])
        right_display=dict(json.loads(right['payload']),id=right_id,source=right['source'])
        item = dict(left={k:left_display[k] for k in ('id','title','company','location','source')},
                    right={k:right_display[k] for k in ('id','title','company','location','source')},
                    decision=decision, reason=previous['reason'] if previous and can_read_reason else '',
                    can_view_reason=can_read_reason,
                    version=previous['version'] if previous else 0,
                    left_hash=left['fingerprint'],right_hash=right['fingerprint'],
                    can_review=may_review(left,actor) and may_review(right,actor))
        result.append(item)
    return dict(duplicates=result,duplicate_pairs=total,duplicates_shown=len(result),
                duplicate_page=page,duplicate_pages=pages,
                duplicate_pending_pairs=pending_pairs, duplicate_pending_jobs=len(pending_ids),
                duplicate_status_counts=status_counts, duplicate_pending_ids=sorted(pending_ids))
