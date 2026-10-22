"""Input adapters: source-specific parsing and bounded external network I/O."""
import csv
import io
import json
import re
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

from recruitment_insights.domain.jobs import plain_text

SOURCE_URL = 'https://boards-api.greenhouse.io/v1/boards/cloudflare/jobs?content=true'


class BundledPublicSource:
    """Read a fixed offline snapshot; never accept a path from an HTTP caller."""
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        if not self.path.is_file():
            raise ValueError('本发行版不附第三方招聘快照；请按 docs/DATA_SOURCES.md 准备有权使用的 data/public_jobs.json')
        if self.path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError('真实岗位样本超过 64 MB 限制')
        try:
            datasets = json.loads(self.path.read_text(encoding='utf-8'))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError('真实岗位样本格式损坏') from exc
        if not isinstance(datasets, list) or not 1 <= len(datasets) <= 20:
            raise ValueError('真实岗位样本需要 1–20 个公开来源')
        seen = set()
        total = 0
        for dataset in datasets:
            if not isinstance(dataset, dict):
                raise ValueError('公开来源必须是对象')
            source = dataset.get('source')
            if (not isinstance(source, str) or
                not re.fullmatch(r'(?:greenhouse|ashby|lever|tencent):[a-z0-9_-]{1,60}', source) or
                source in seen):
                raise ValueError('公开来源名称无效或重复')
            seen.add(source)
            if not isinstance(dataset.get('label'), str) or not dataset['label'].strip():
                raise ValueError('公开来源缺少名称')
            if not isinstance(dataset.get('fetched_at'), str) or not dataset['fetched_at'].strip():
                raise ValueError('公开来源缺少采集时间')
            try:
                observed = datetime.fromisoformat(dataset['fetched_at'].replace('Z', '+00:00'))
                if observed.tzinfo is None or observed > datetime.now(timezone.utc):
                    raise ValueError
            except ValueError as exc:
                raise ValueError('公开来源采集时间无效') from exc
            records = dataset.get('records')
            if not isinstance(records, list) or not 1 <= len(records) <= 5000:
                raise ValueError('每个公开来源需要 1–5000 条记录')
            total += len(records)
        if total > 20000:
            raise ValueError('公开样本总量超过 20000 条限制')
        return datasets


def parse_csv(content):
    if not isinstance(content, str) or len(content.encode('utf-8')) > 2 * 1024 * 1024:
        raise ValueError('CSV 内容不得超过 2 MB')
    reader = csv.DictReader(io.StringIO(content.lstrip('\ufeff')), strict=True)
    if not reader.fieldnames or not {'job_id','title','company','location'} <= set(reader.fieldnames):
        raise ValueError('CSV 必须包含 job_id,title,company,location 列')
    if len(set(reader.fieldnames)) != len(reader.fieldnames):
        raise ValueError('CSV 列名不得重复')
    try:
        rows = []
        for row in reader:
            if None in row:
                raise ValueError('CSV 某行列数超过表头，请检查引号')
            rows.append(row)
            if len(rows) > 5000:
                raise ValueError('单次最多 5000 行')
    except csv.Error as exc:
        raise ValueError(f'CSV 格式错误：{exc}') from exc
    if not rows:
        raise ValueError('CSV 没有数据行')
    return rows


def greenhouse_records(payload, company='Cloudflare'):
    if not isinstance(payload, dict) or not isinstance(payload.get('jobs'), list) or not payload['jobs']:
        raise ValueError('上游未返回非空 jobs 列表，保留已有数据')
    records = []
    for job in payload['jobs']:
        title = job.get('title', '')
        departments = ' '.join(d.get('name', '') for d in job.get('departments', []))
        if not re.search(r'engineer|developer|scientist|security|technical|network|research|product design',
                         title + ' ' + departments, re.I):
            continue
        offices = sorted(set(o.get('location') or o.get('name') for o in job.get('offices', [])
                             if o.get('location') or o.get('name')))
        locations = offices or [job.get('location', {}).get('name') or '未披露']
        records.append(dict(job_id=str(job['id']), title=title, company=company,
                            location=' / '.join(locations), locations=locations,
                            description=plain_text(job.get('content','')),
                            source_url=job.get('absolute_url',''), updated_at=job.get('updated_at',''),
                            published_at=job.get('first_published',''), salary_text='未提供结构化薪资',
                            education='未披露', experience='未披露', _source_record=job))
    if not records:
        raise ValueError('技术岗位筛选结果为空，保留已有数据')
    return records


class GreenhouseSource:
    source = 'greenhouse:cloudflare'
    label = 'Cloudflare 公开技术岗位'

    def fetch(self):
        request = urllib.request.Request(SOURCE_URL, headers={'User-Agent':'CourseRecruitmentAnalysis/1.0'})
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read(20 * 1024 * 1024 + 1)
        if len(body) > 20 * 1024 * 1024:
            raise ValueError('上游响应超过限制')
        return json.loads(body)

    def records(self, payload):
        return greenhouse_records(payload)
