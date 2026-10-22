"""Recruitment normalization and analysis without persistence or network access."""
from __future__ import annotations

import hashlib
import html
import json
import math
import re
import statistics
from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser

from .quality import clean_text

SKILLS = ('Python', 'JavaScript', 'TypeScript', 'Java', 'C++', 'Go', 'Rust', 'SQL',
          'Docker', 'Kubernetes', 'Linux', 'AWS', 'React', 'Vue', 'Git', 'PyTorch', 'TensorFlow')
_SKILL_PATTERNS = {
    skill: re.compile(r'(?<![\w+])' + re.escape(skill) + r'(?![\w+])',
                      0 if skill == 'Go' else re.I)
    for skill in SKILLS
}
_GOLANG_PATTERN = re.compile(r'\bgolang\b', re.I)



CATEGORIES = ('后端开发', '前端开发', '数据与算法', '基础设施与安全', '测试开发', '其他技术岗位',
              '产品与设计', '销售与市场', '客户服务与支持', '职能与运营', '其他岗位')
LIFECYCLE_FIELDS = ('source_active', 'manual_inactive', 'lifecycle_reason', 'lifecycle_updated_at')



def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')



def edit_token(row):
    """Cover original input, import generation, publication and lifecycle state."""
    content = {key: row[key] for key in ('id', 'raw', 'fingerprint', 'run_id',
                                        'owner_org_id', 'is_demo', 'published', 'active',
                                        *LIFECYCLE_FIELDS, 'lifecycle_version')}
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def lifecycle_state(row):
    """Internal projection shared by maintenance reads and mutation responses."""
    return dict(active=bool(row['active']), source_active=bool(row['source_active']),
                manual_inactive=bool(row['manual_inactive']),
                lifecycle_reason=row['lifecycle_reason'],
                lifecycle_updated_at=row['lifecycle_updated_at'])



class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)



def plain_text(value):
    parser = PlainText()
    parser.feed(html.unescape(html.unescape(str(value))))
    return '\n'.join(s.strip() for s in parser.parts if s.strip())



def extract_skills(text):
    # Reject absent literals before scanning their word boundaries. These three
    # substitutions preserve re.I's extra ASCII equivalents; lower() handles K.
    folded = text.replace('İ', 'i').replace('ı', 'i').replace('ſ', 's').lower()
    found = []
    for skill in SKILLS:
        # Go is a programming language here, not the ordinary English verb.
        present = skill in text if skill == 'Go' else skill.lower() in folded
        if present and _SKILL_PATTERNS[skill].search(text):
            found.append(skill)
    if 'Go' not in found and 'golang' in folded and _GOLANG_PATTERN.search(text):
        found.append('Go')
    return sorted(found)



def category(title):
    for label, pattern in (
        ('销售与市场', r'\bsales\b|account executive|account manager|business development|marketing|销售|市场|商务'),
        ('客户服务与支持', r'customer success|customer support|technical support|support engineer|客户|客服|技术支持'),
        ('产品与设计', r'product manager|product designer|\bdesign\b|产品经理|产品策划|设计'),
        ('职能与运营', r'\bfinance\b|\blegal\b|recruit|human resources|people operations|\baccountant\b|\boperations\b|财务|法务|人事|招聘|运营|行政'),
        ('前端开发', r'front.?end|前端|web developer'),
        ('数据与算法', r'data|machine learning|\bai\b|数据|算法'),
        ('基础设施与安全', r'security|network|infrastructure|reliability|devops|安全|运维|网络'),
        ('测试开发', r'\bqa\b|test|quality|测试'),
        ('后端开发', r'back.?end|software engineer|后端|java|python|golang'),
    ):
        if re.search(pattern, title, re.I):
            return label
    return '其他技术岗位' if re.search(r'engineer|developer|architect|scientist|researcher|工程师|研发|开发|研究员', title, re.I) else '其他岗位'



def salary(raw):
    """Normalize only explicit CNY/USD month/year ranges; never guess a currency."""
    currency = str(raw.get('currency') or '').upper().strip()
    period = str(raw.get('salary_period') or '').lower().strip()
    low, high = raw.get('salary_min'), raw.get('salary_max')
    text = str(raw.get('salary_text') or '').strip()
    if low in (None, '') and high in (None, ''):
        match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*[-~～—–]\s*(\d+(?:\.\d+)?)\s*[kK](?:/月)?', text)
        if match and currency == 'CNY':
            low, high, period = float(match[1]) * 1000, float(match[2]) * 1000, 'month'
        else:
            return None, None, currency, period, '面议' if '面议' in text else '未披露或不可解析'
    try:
        low, high = float(low), float(high)
    except (TypeError, ValueError):
        return None, None, currency, period, '无效数值'
    if not all(math.isfinite(v) for v in (low, high)) or low <= 0 or high < low or high > 10000000:
        return None, None, currency, period, '无效区间'
    if currency not in ('CNY', 'USD') or period not in ('month', 'year'):
        return None, None, currency, period, '币种或计薪周期不支持'
    divisor = 12 if period == 'year' else 1
    return round(low / divisor, 2), round(high / divisor, 2), currency, period, '有效'



def normalize(raw):
    if not isinstance(raw, dict):
        raise ValueError('每行必须是对象')
    result = {}
    for key in ('job_id', 'title', 'company', 'location'):
        value = raw.get(key)
        if value is None or isinstance(value, (dict, list)) or not str(value).strip():
            raise ValueError(f'缺少必填字段 {key}')
        value = str(value).strip() if key == 'job_id' else clean_text(value)
        if len(value) > 500:
            raise ValueError(f'{key} 超过 500 字符')
        result[key] = value
    for key in ('description', 'source_url', 'updated_at', 'published_at', 'salary_text'):
        result[key] = str(raw.get(key) or '').strip()
    if re.search(r'</?(?:p|div|br|ul|li|h[1-6])\b', result['description'], re.I):
        result['description'] = plain_text(result['description'])
    if len(result['description']) > 100000:
        raise ValueError('岗位描述过长')
    if result['source_url'] and not re.match(r'^https?://', result['source_url'], re.I):
        raise ValueError('来源链接必须使用 http 或 https')
    result['education'] = str(raw.get('education') or '未披露').strip() or '未披露'
    result['experience'] = str(raw.get('experience') or '未披露').strip() or '未披露'
    locations = raw.get('locations')
    result['locations'] = sorted(set(str(v).strip() for v in locations if str(v).strip())) if isinstance(locations, list) else [result['location']]
    if not result['locations']:
        result['locations'] = [result['location']]
    result['category'] = category(result['title'])
    result['skills'] = extract_skills(result['title'] + '\n' + result['description'])
    low, high, currency, period, status = salary(raw)
    result.update(salary_low=low, salary_high=high, currency=currency,
                  salary_period=period, salary_status=status)
    return result



def analyze(jobs):
    # Consume even a one-shot iterator once; retain only aggregation state.
    counts = {key: Counter() for key in ('source', 'category', 'education', 'experience')}
    skills, locations, companies = Counter(), Counter(), set()
    values_by_currency = {'CNY': [], 'USD': []}
    states = {field: Counter() for field in ('education', 'experience')}
    unknown = dict(education=0, experience=0)
    total = invalid_salary = demos = 0
    last_seen = None
    for job in jobs:
        total += 1
        companies.add(job['company'])
        for key, counter in counts.items():
            counter[job[key]] += 1
        skills.update(job['skills'])
        locations.update(job['locations'])
        for field in states:
            states[field][job.get(field+'_status', 'source')] += 1
            unknown[field] += job[field] in ('未披露', '未发现明确要求', '待核验', '待复核', '未知')
        if job['salary_status'] == '有效' and job['currency'] in values_by_currency:
            values_by_currency[job['currency']].append((job['salary_low'] + job['salary_high']) / 2)
        invalid_salary += job['salary_status'] != '有效'
        demos += job['is_demo']
        last_seen = job['last_seen'] if last_seen is None else max(last_seen, job['last_seen'])

    def ranked(counter):
        return [dict(name=name, count=count) for name, count in counter.most_common()]

    salaries = []
    for currency in ('CNY', 'USD'):
        values = values_by_currency[currency]
        bins = [sum(1 for x in values if a <= x < b) for a, b in
                ((0, 10000), (10000, 20000), (20000, 30000), (30000, float('inf')))]
        salaries.append(dict(currency=currency, valid=len(values),
                             median=round(statistics.median(values), 2) if values else None,
                             mean=round(statistics.mean(values), 2) if values else None,
                             bins=[dict(name=n, count=v) for n, v in zip(('<10k', '10–20k', '20–30k', '≥30k'), bins)]))
    coverage={}
    for field in ('education','experience'):
        status = states[field]
        coverage[field]=dict(source=status['source'],confirmed=status['confirmed'],
                            pending=status['candidate']+status['unresolved'],stale=status['stale'],
                            unknown=unknown[field])
    return dict(total=total, companies=len(companies),
                sources=ranked(counts['source']),requirement_coverage=coverage,
                locations=ranked(locations), categories=ranked(counts['category']),
                education=ranked(counts['education']), experience=ranked(counts['experience']),
                skills=ranked(skills),
                salaries=salaries, invalid_salary=invalid_salary,
                education_missing=coverage['education']['unknown'],
                experience_missing=coverage['experience']['unknown'],
                demos=demos, last_seen=last_seen)
