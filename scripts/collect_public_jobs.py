"""Collect public careers APIs into a traceable, unmodified real-data snapshot.

Run from the project root: python scripts/collect_public_jobs.py
Replay saved responses without the network: add --offline.
This standalone standard-library script never opens or changes a database.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from recruitment_insights.domain.jobs import normalize, plain_text  # noqa: E402

MAX_BYTES = 20 * 1024 * 1024
MAX_PAGES = 40
MAX_RECORDS = 10000
HOSTS = {'greenhouse': 'boards-api.greenhouse.io', 'ashby': 'api.ashbyhq.com',
         'lever': 'api.lever.co', 'tencent': 'careers.tencent.com'}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def source_key(source):
    return re.sub(r'[^a-zA-Z0-9_-]', '_', source)


def fetch_snapshot(source, endpoint, path, offline):
    url = urllib.parse.urlsplit(endpoint)
    if url.scheme != 'https' or url.hostname != HOSTS.get(source['kind']):
        raise ValueError('Source must use its official HTTPS public API host')
    if offline:
        raw = path.read_bytes()
        meta_path = path.with_suffix('.metadata.json')
        if meta_path.exists():
            stored = json.loads(meta_path.read_text(encoding='utf-8'))
            if hashlib.sha256(raw).hexdigest() != stored.get('sha256'):
                raise ValueError('Saved response SHA-256 differs from collection metadata')
            if stored.get('endpoint') != endpoint:
                raise ValueError('Saved response endpoint differs from current manifest')
            fetched_at = stored['fetched_at']
        else:
            fetched_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec='seconds')
    else:
        request = urllib.request.Request(endpoint, headers={
            'User-Agent': 'recruitment-insights-CourseProject/2.5 (public careers data; educational local analysis)',
            'Accept': 'application/json'})
        with urllib.request.urlopen(request, timeout=35) as response:
            raw = response.read(MAX_BYTES + 1)
        fetched_at = utc_now()
    if len(raw) > MAX_BYTES:
        raise ValueError('Source response exceeds 20 MB')
    payload = json.loads(raw)
    digest = hashlib.sha256(raw).hexdigest()
    if not offline:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        write_json(path.with_suffix('.metadata.json'), {
            'endpoint': endpoint, 'fetched_at': fetched_at, 'bytes': len(raw), 'sha256': digest})
    return payload, {'file': str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
                     'endpoint': endpoint, 'fetched_at': fetched_at,
                     'bytes': len(raw), 'sha256': digest}


def explicit_salary(job, kind):
    """Map one explicit range only; never infer currency or flatten location tiers."""
    result = {'salary_text': '', 'salary_min': '', 'salary_max': '',
              'currency': '', 'salary_period': ''}
    if kind == 'ashby':
        pay = job.get('compensation') or {}
        result['salary_text'] = pay.get('scrapeableCompensationSalarySummary') or pay.get('compensationTierSummary') or ''
        ranges = []
        for tier in pay.get('compensationTiers') or []:
            ranges.extend(c for c in tier.get('components') or [] if c.get('compensationType') == 'Salary')
        # Summary is sometimes broad across regions. Exact numeric values require
        # a single unambiguous tier/component (or a summary when no tiers exist).
        if not ranges:
            ranges = [c for c in pay.get('summaryComponents') or [] if c.get('compensationType') == 'Salary']
        unique = {(c.get('minValue'), c.get('maxValue'), c.get('currencyCode'), c.get('interval')) for c in ranges}
        if len(unique) == 1:
            low, high, currency, interval = unique.pop()
            period = {'1 YEAR': 'year', '1 MONTH': 'month'}.get(interval, '')
            if low is not None and high is not None and currency and period:
                result.update(salary_min=low, salary_max=high, currency=currency, salary_period=period)
    elif kind == 'lever':
        pay = job.get('salaryRange') or {}
        result['salary_text'] = job.get('salaryDescriptionPlain') or plain_text(job.get('salaryDescription') or '')
        period = {'per-year-salary': 'year', 'per-month-salary': 'month'}.get(pay.get('interval'), '')
        if pay.get('min') is not None and pay.get('max') is not None and pay.get('currency') and period:
            result.update(salary_min=pay['min'], salary_max=pay['max'], currency=pay['currency'], salary_period=period)
            if not result['salary_text']:
                result['salary_text'] = f"{pay['min']}–{pay['max']} {pay['currency']} ({pay['interval']})"
    return result


def map_job(job, source):
    kind = source['kind']
    if kind == 'greenhouse':
        location = (job.get('location') or {}).get('name') or '未披露'
        offices = [o.get('location') or o.get('name') for o in job.get('offices') or []]
        locations = [location] + [s for s in offices if s]
        result = dict(job_id=str(job['id']), title=job.get('title'), location=location,
                      description=plain_text(job.get('content') or ''),
                      source_url=job.get('absolute_url') or '', updated_at=job.get('updated_at') or '',
                      published_at=job.get('first_published') or '')
    elif kind == 'ashby':
        location = job.get('location') or '未披露'
        locations = [location] + [o.get('location') for o in job.get('secondaryLocations') or [] if o.get('location')]
        result = dict(job_id=str(job['id']), title=job.get('title'), location=location,
                      description=job.get('descriptionPlain') or plain_text(job.get('descriptionHtml') or ''),
                      source_url=job.get('jobUrl') or '', updated_at='', published_at=job.get('publishedAt') or '')
    elif kind == 'lever':
        categories = job.get('categories') or {}
        location = categories.get('location') or '未披露'
        locations = categories.get('allLocations') or [location]
        sections = [job.get('openingPlain') or '', job.get('descriptionPlain') or '']
        for item in job.get('lists') or []:
            sections.extend([item.get('text') or '', plain_text(item.get('content') or '')])
        sections.append(job.get('additionalPlain') or '')
        result = dict(job_id=str(job['id']), title=job.get('text'), location=location,
                      description='\n\n'.join(s for s in sections if s),
                      source_url=job.get('hostedUrl') or '', updated_at='', published_at='')
        # createdAt is creation, not publication. Retain in raw snapshot only.
    else:
        location = job.get('LocationName') or '未披露'
        locations = [location]
        result = dict(job_id=str(job['PostId']), title=job.get('RecruitPostName'), location=location,
                      description=job.get('Responsibility') or '', source_url=job.get('PostURL') or '',
                      updated_at=job.get('LastUpdateTime') or '', published_at='')
        # Work-year labels and qualification fields are preserved as raw source
        # metadata; application rules can propose/verify them with evidence.
    result.update(company=source['company'], locations=list(dict.fromkeys(locations)),
                  education='', experience='', **explicit_salary(job, kind))
    if kind == 'tencent':
        # This explicit source field is already present; do not discard it or
        # replace its original Chinese text with an invented numeric bucket.
        result['experience'] = job.get('RequireWorkYearsName') or ''
    # No full raw duplication in the distribution; trace the original object via
    # snapshot SHA + source ID. Keep small source-specific interpretation fields.
    result['_source_metadata'] = {k: job[k] for k in (
        'department', 'team', 'categories', 'employmentType', 'workplaceType',
        'ComName', 'CountryName', 'BGName', 'ProductName', 'CategoryName', 'RequireWorkYearsName', 'IsValid') if k in job}
    return result


def get_jobs(payload, kind):
    if kind == 'lever':
        jobs = payload
    elif kind == 'tencent':
        if payload.get('Code') not in (0, '0', 200, '200'):
            raise ValueError(f"Tencent API returned Code={payload.get('Code')}")
        jobs = (payload.get('Data') or {}).get('Posts')
    else:
        jobs = payload.get('jobs')
    if not isinstance(jobs, list):
        raise ValueError('Source has no jobs array')
    return jobs


def collect_source(source, snapshot_dir, offline, delay):
    kind = source['kind']
    endpoints = [source['endpoint']]
    fetched = []
    snapshots = []
    seen = set()
    counts = Counter()
    errors = []
    page = 1
    expected_total = None
    while page <= len(endpoints):
        if page > MAX_PAGES:
            raise ValueError('Pagination exceeds the bounded 40-page limit')
        endpoint = endpoints[page - 1]
        suffix = f'-page{page}' if kind == 'tencent' else ''
        path = snapshot_dir / (source_key(source['source']) + suffix + '.json')
        payload, metadata = fetch_snapshot(source, endpoint, path, offline)
        snapshots.append(metadata)
        jobs = get_jobs(payload, kind)
        counts['upstream_rows'] += len(jobs)
        if kind == 'tencent' and page == 1:
            expected_total = int((payload.get('Data') or {}).get('Count') or 0)
            if expected_total <= 0 or not jobs:
                raise ValueError('Tencent query returned no postings')
            # Respect the actual returned page length in case the provider caps it.
            total_pages = math.ceil(expected_total / len(jobs))
            if total_pages > MAX_PAGES:
                raise ValueError(f'Tencent needs {total_pages} pages, exceeds {MAX_PAGES}')
            for next_page in range(2, total_pages + 1):
                url = urllib.parse.urlsplit(endpoint)
                query = urllib.parse.parse_qs(url.query, keep_blank_values=True)
                query['pageIndex'] = [str(next_page)]
                endpoints.append(urllib.parse.urlunsplit(url._replace(query=urllib.parse.urlencode(query, doseq=True))))
        for index, job in enumerate(jobs):
            if not isinstance(job, dict):
                errors.append({'page': page, 'index': index, 'error': 'Job is not an object'})
                continue
            if kind == 'ashby' and job.get('isListed') is False:
                counts['unlisted_excluded'] += 1
                continue
            if kind == 'tencent' and job.get('IsValid') is False:
                counts['invalid_excluded'] += 1
                continue
            try:
                record = map_job(job, source)
                normalized = normalize(record)
                if record['job_id'] in seen:
                    counts['duplicate_ids_excluded'] += 1
                    continue
                seen.add(record['job_id'])
                record['_source_snapshot_sha256'] = metadata['sha256']
                record['_source_observed_at'] = metadata['fetched_at']
                fetched.append(record)
                counts['valid_records'] += 1
                counts['salary_comparable'] += int(normalized['salary_status'] == '有效')
            except (ValueError, KeyError, TypeError) as exc:
                errors.append({'page': page, 'index': index, 'source_id': str(job.get('id') or job.get('PostId') or ''), 'error': str(exc)})
            if len(fetched) > MAX_RECORDS:
                raise ValueError('Source exceeds the bounded 10000-record limit')
        print(f"{source['source']}: page {page}/{len(endpoints)}, valid={len(fetched)}", flush=True)
        page += 1
        if not offline and page <= len(endpoints):
            time.sleep(delay)
    if not fetched:
        raise ValueError('No valid listed records; existing snapshot is not overwritten')
    dataset = {k: source[k] for k in ('source', 'label', 'company', 'endpoint')}
    dataset.update(fetched_at=max(s['fetched_at'] for s in snapshots), records=fetched,
                   snapshots=snapshots, authoritative=False,
                   description_scope=source.get('description_scope', '官方公开岗位正文；保留源站内容。'))
    check = dict(source=source['source'], company=source['company'], endpoint=source['endpoint'],
                 counts=dict(counts), expected_upstream_total=expected_total, normalization_errors=errors,
                 unique_ids=len(seen), snapshots=snapshots,
                 status='ok', replayed=offline,
                 limitation='分页期间可能有源站新增/撤下，不据此自动删除既有库中的岗位。' if kind == 'tencent' else '')
    return dataset, check


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=ROOT / 'data/public_sources.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'data/public_jobs.json')
    parser.add_argument('--report', type=Path, default=ROOT / 'outputs/verification/stage7/data-collection.json')
    parser.add_argument('--snapshot-dir', type=Path, default=ROOT / 'outputs/fixtures/public-source-probes')
    parser.add_argument('--source', action='append', help='Source ID; repeat to select multiple boards')
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--delay', type=float, default=0.4, help='Seconds between requests; minimum 0.25')
    args = parser.parse_args()
    if not 0.25 <= args.delay <= 10:
        parser.error('--delay must be between 0.25 and 10 seconds')
    manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
    sources = manifest['sources']
    if args.source:
        requested = set(args.source)
        available = {s['source'] for s in sources}
        if requested - available:
            parser.error('Unknown source: ' + ', '.join(sorted(requested - available)))
        sources = [s for s in sources if s['source'] in requested]
    datasets = []
    checks = []
    for source in sources:
        try:
            dataset, check = collect_source(source, args.snapshot_dir.resolve(), args.offline, args.delay)
            datasets.append(dataset)
            checks.append(check)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            checks.append({'source': source['source'], 'company': source['company'], 'status': 'failed', 'error': str(exc)})
            print(f"{source['source']}: failed: {exc}", file=sys.stderr, flush=True)
        if not args.offline:
            time.sleep(args.delay)
    report = dict(format_version=1, checked_at=utc_now(), replayed=args.offline,
                  valid_records=sum(len(s['records']) for s in datasets), companies=len(datasets),
                  sources=checks, missing_values_policy='留空/未披露；不随机补全、不把加工结果作为原站事实。',
                  randomization_applied=False, external_llm_used=False,
                  scope='所选企业官网公开招聘快照，覆盖非技术岗位；不是招聘市场代表性抽样。')
    write_json(args.report, report)
    if datasets:
        write_json(args.output, datasets)
    print(json.dumps({k: report[k] for k in ('valid_records', 'companies', 'replayed')}, ensure_ascii=False))
    return 1 if any(s['status'] != 'ok' for s in checks) else 0


if __name__ == '__main__':
    raise SystemExit(main())
