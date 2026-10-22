"""Generate labelled fixtures and initialize data without overwriting existing records."""
import csv
import json
import random
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import ROOT, Store

FIELDS = ['job_id', 'title', 'company', 'location', 'salary_text', 'salary_min', 'salary_max',
          'currency', 'salary_period', 'education', 'experience', 'description', 'source_url', 'updated_at']


def write_csv(path, records):
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(records)


def fixtures(seed=20261004, output_dir=None):
    """Weighted, repeatable synthetic data; never alter a real source record."""
    rng = random.Random(seed)
    destination = Path(output_dir) if output_dir is not None else ROOT/'data'
    destination.mkdir(parents=True, exist_ok=True)
    templates = [
        ('Python 后端工程师', 'Python SQL Docker Git'),
        ('Java 后端工程师', 'Java SQL Linux Git'),
        ('前端开发工程师', 'JavaScript TypeScript React Vue Git'),
        ('数据算法工程师', 'Python SQL PyTorch TensorFlow Linux'),
        ('云基础设施运维工程师', 'Go Rust Docker Kubernetes AWS Linux'),
        ('自动化测试工程师', 'Python JavaScript Docker SQL'),
    ]
    rows = []
    for i in range(180):
        title, skills = rng.choices(templates, weights=[25, 22, 18, 15, 13, 7])[0]
        low = round(rng.triangular(6000, 42000, 16000) / 500) * 500
        missing = rng.random() < 0.17
        rows.append(dict(job_id=f'DEMO-{i+1:04d}', title=title,
                         company=f'示例科技 {rng.randint(1,24):02d}（模拟）',
                         location=rng.choices(['北京', '上海', '深圳', '杭州', '成都', '南京', '广州', '武汉'],
                                              weights=[24,21,18,13,8,5,7,4])[0],
                         salary_text='面议' if missing else f'{low/1000:g}–{(low+7000)/1000:g}K/月',
                         salary_min='' if missing else low, salary_max='' if missing else low+7000,
                         currency='CNY', salary_period='month',
                         education=rng.choices(['本科', '硕士', '大专', '不限', ''], weights=[52,16,10,8,14])[0],
                         experience=rng.choices(['应届', '1–3年', '3–5年', '5年以上', '不限', ''],
                                                weights=[14,29,28,12,7,10])[0],
                         description=f'【课程模拟岗位，不是真实招聘】\n岗位职责：参与需求分析、模块实现和测试。\n技能要求：{skills}。\n此条目用于验证筛选、薪资统计、技能追溯和更新流程。',
                         source_url='', updated_at='2026-09-24T00:00:00Z'))
    write_csv(destination/'demo_jobs.csv', rows)
    updates = [dict(r) for r in rows[:5]]
    for row in updates:
        row.update(salary_min=22000, salary_max=32000, salary_text='22–32K/月', updated_at='2026-09-25T00:00:00Z')
    for i in range(3):
        updates.append(dict(rows[10+i], job_id=f'DEMO-NEW-{i+1}'))
    updates.extend([dict(rows[20]), dict(rows[21], job_id='BAD-1', title='')])
    write_csv(destination/'demo_update.csv', updates)
    write_csv(destination/'import_template.csv', [dict(rows[0], job_id='YOUR-001')])
    return rows


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed', type=int, default=20261004, help='Change only the labelled synthetic distribution')
    parser.add_argument('--fixtures-only', action='store_true', help='Write CSV files without opening any database')
    parser.add_argument('--output-dir', type=Path, default=ROOT/'data')
    parser.add_argument('--db', type=Path, default=ROOT/'data/recruitment.sqlite3',
                        help='Destination database; use an independent path for a new demo')
    parser.add_argument('--demo-only', action='store_true',
                        help='Import existing synthetic CSV only; do not load public snapshots or rewrite fixtures')
    args = parser.parse_args()
    if args.demo_only and args.fixtures_only:
        parser.error('--demo-only and --fixtures-only cannot be combined')
    if not args.demo_only:
        rows = fixtures(args.seed, args.output_dir)
    if args.fixtures_only:
        print(json.dumps(dict(seed=args.seed, rows=len(rows), synthetic=True), ensure_ascii=False))
        sys.exit(0)
    if args.output_dir.resolve() != (ROOT/'data').resolve():
        parser.error('Custom output directories require --fixtures-only')
    store = Store(args.db)
    snapshot = ROOT / 'data' / 'cloudflare_snapshot.json'
    if snapshot.exists() and not args.demo_only:
        print('Public snapshot:', json.dumps(store.sync(json.loads(snapshot.read_text(encoding='utf-8'))), ensure_ascii=False))
    print('Demo fixtures:', json.dumps(store.import_csv((ROOT / 'data' / 'demo_jobs.csv').read_text(encoding='utf-8-sig'),
                                                       '课程模拟数据', True), ensure_ascii=False))
