"""Explainable quality checks over normalized, already-scoped job records."""
import re
import unicodedata
from collections import Counter, defaultdict


def clean_text(value):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', str(value))).strip()


def duplicate_key(job):
    """One grouping definition shared by quality warnings and human decisions."""
    return (bool(job['is_demo']), clean_text(job['company']).casefold(),
            clean_text(job['title']).casefold(),
            tuple(sorted({clean_text(value).casefold() for value in job['locations']})))


def issues(job):
    found = []
    if len(job.get('description', '').strip()) < 40:
        found.append(('description_short', '描述信息不足（少于 40 字符，需人工判断）'))
    if job.get('salary_status') != '有效':
        found.append(('salary_unavailable', '薪资不可比较：' + job.get('salary_status', '未披露')))
    for field,label in (('education','学历'),('experience','经验')):
        if job.get(field+'_status') == 'stale':
            found.append((field+'_stale',label+'原文已变化，需要重新核验'))
        elif job.get(field+'_status') in ('candidate','unresolved'):
            found.append((field+'_pending',label+'尚未核验；不能视为原文未披露'))
        elif job.get(field) in ('未披露','未发现明确要求','未知'):
            found.append((field+'_missing',label+'未发现明确要求'))
    if not job.get('source_url') and not job.get('is_demo'):
        found.append(('source_link_missing', '未提供原始链接（可补充自有数据来源说明）'))
    return [dict(code=code, message=message) for code, message in found]


def quality_report(jobs, duplicate_pending_ids=None):
    groups = defaultdict(list)
    for job in jobs:
        groups[duplicate_key(job)].append(job)
    duplicates = [group for group in groups.values() if len(group) > 1]
    duplicate_ids = {job['id'] for group in duplicates for job in group}
    pending_ids = duplicate_ids if duplicate_pending_ids is None else set(duplicate_pending_ids)
    counts = Counter()
    rows = []
    for job in jobs:
        warnings = issues(job)
        if job['id'] in pending_ids:
            warnings.append(dict(code='possible_duplicate', message='关联的相似记录尚待判断或需要复核；保留原记录，不自动合并'))
        counts.update(item['code'] for item in warnings)
        if warnings:
            rows.append(dict(id=job['id'], title=job['title'], company=job['company'],
                             location=job['location'], source=job['source'], issues=warnings))
    labels = dict(description_short='描述不足', salary_unavailable='薪资不可比较',
                  education_missing='学历缺失', experience_missing='经验缺失',
                  source_link_missing='来源链接缺失', possible_duplicate='重复候选待核验 / 复核')
    labels.update(education_pending='学历待核验',experience_pending='经验待核验',
                  education_stale='学历待复核',experience_stale='经验待复核')
    return dict(total=len(jobs), warning_jobs=len(rows), clean_jobs=len(jobs)-len(rows),
                comparable_salary=sum(j['salary_status'] == '有效' for j in jobs),
                duplicate_groups=len(duplicates), duplicate_jobs=len(duplicate_ids),
                issues=[dict(code=k, name=labels[k], count=v) for k,v in counts.most_common()],
                rows=rows[:100], shown=min(100,len(rows)))
