"""Read the existing handoff workbook; never treat completeness as human truth.

Uses only the standard library. Trial CSV imports always use a temporary database.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import posixpath
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import Store
from requirements import RULE_VERSION

HANDOFF = ROOT / 'deliverables/team-handoff'
NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def read_workbook(path):
    """Read text/numeric cells from the existing three-sheet xlsx template."""
    with ZipFile(path) as archive:
        strings = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            strings = [''.join(si.itertext()) for si in
                       ET.fromstring(archive.read('xl/sharedStrings.xml'))]
        relationships = {r.attrib['Id']: r.attrib['Target'] for r in
                         ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))}
        result = {}
        for sheet in ET.fromstring(archive.read('xl/workbook.xml')).findall('s:sheets/s:sheet', NS):
            target = relationships[sheet.attrib[f'{{{REL}}}id']]
            member = target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/' + target)
            rows = {}
            for row in ET.fromstring(archive.read(member)).findall('s:sheetData/s:row', NS):
                values = {}
                for cell in row.findall('s:c', NS):
                    ref = cell.attrib['r']
                    column = ''.join(c for c in ref if c.isalpha())
                    if cell.find('s:f', NS) is not None:
                        value = '[公式：请改为可追溯的人工填写值]'
                    elif cell.attrib.get('t') == 'inlineStr':
                        value = ''.join(t.text or '' for t in cell.findall('.//s:t', NS))
                    else:
                        value = cell.findtext('s:v', '', NS)
                        if cell.attrib.get('t') == 's':
                            value = strings[int(value)]
                    values[column] = value.strip()
                rows[int(row.attrib['r'])] = values
            result[sheet.attrib['name']] = rows
        return result


def inspect_reviews(entries, snapshot, source='greenhouse:cloudflare'):
    """Validate structure and evidence, without interpreting human label semantics."""
    expected = {(source, row['job_id']): row for row in snapshot}
    issues, groups = [], defaultdict(list)
    for filename, number, row in entries:
        if not any(row.values()):
            continue
        key = (row.get('A', ''), row.get('B', ''))
        groups[key].append((filename, number, row))
    populated, complete, fields = 0, 0, 0
    for key, rows in groups.items():
        raw = expected.get(key)
        for filename, number, row in rows:
            ref = {'file': filename, 'row': number, 'source': key[0], 'job_id': key[1]}
            add = lambda code, detail: issues.append(dict(ref, code=code, detail=detail))
            if raw is None:
                add('unknown_key', '来源与 job_id 无法对应 03 快照；先核对原始编号。')
                continue
            authored = any(row.get(c) for c in ('D', 'E', 'F', 'G', 'H', 'I', 'K'))
            if not authored:
                add('not_returned', '没有人工填写结果。')
                continue
            populated += 1
            before = len(issues)
            if not row.get('H') or not row.get('I'):
                add('missing_reviewer', '缺核验人或核验日期。')
            if row.get('J') != raw.get('source_url'):
                add('source_url_changed', '链接与快照不同；需先解释来源变更。')
            if any(str(v).startswith('[公式：') for v in row.values()):
                add('formula_input', '标注表输入含公式，不能作为独立人工填写结果。')
            description = ' '.join(raw.get('description', '').split())
            if not description:
                add('missing_description', '快照缺完整原文，无法核对证据。')
            for evidence_col, label_col, label in [('D', 'E', '学历'), ('F', 'G', '经验')]:
                conclusion, evidence = row.get(label_col, ''), row.get(evidence_col, '')
                if not conclusion:
                    add('missing_label', label + '结论空白。')
                    continue
                fields += 1
                if '无法判断' in conclusion:
                    add('unresolved', label + '仍为无法判断，不纳入确定结论分母。')
                elif conclusion.strip(' 。.：:') in ('原文未提及', '未发现明确要求'):
                    if not evidence:
                        add('missing_evidence', label + '缺完整阅读后的说明。')
                elif not evidence:
                    add('missing_evidence', label + '缺原文证据。')
                elif not description or ' '.join(evidence.split()) not in description:
                    add('evidence_mismatch', label + '证据不是快照中的连续原文，需人工核对。')
            if len(issues) == before:
                complete += 1
        authored_rows = [r for _, _, r in rows if any(r.get(c) for c in ('D', 'E', 'F', 'G', 'H', 'I', 'K'))]
        if len(authored_rows) > 1:
            conclusions = {(r.get('E', ''), r.get('G', '')) for r in authored_rows}
            issues.append({'source': key[0], 'job_id': key[1],
                           'code': 'conflicting_labels' if len(conclusions) > 1 else 'duplicate_review',
                           'detail': '同一来源和编号有多份标注；保留各人原表，去重或解决分歧后再计数。'})
    missing = sorted(set(expected) - set(groups))
    for key in missing:
        issues.append({'source': key[0], 'job_id': key[1], 'code': 'missing_sample', 'detail': '回交表缺少该样本行。'})
    return {
        'expected_samples': len(expected), 'expected_fields': len(expected) * 2,
        'authored_rows': populated, 'filled_field_cells': fields,
        'structurally_complete_rows_before_duplicate_adjudication': complete,
        'issue_counts': dict(Counter(issue['code'] for issue in issues)), 'issues': issues,
        'accuracy': None, 'precision': None, 'recall': None,
        'status': '等待真实人审回交' if populated == 0 else '等待人工核对证据、统一条件并裁定分歧',
        'qualification': '格式完整不代表结论正确或已经完成真人核验；本检查不自动生成真值或准确率。',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workbook', type=Path, action='append', help='可重复指定组员副本，检查重复与冲突')
    parser.add_argument('--snapshot', type=Path, default=HANDOFF / '03_现有真实岗位30条_仅供核验.csv')
    parser.add_argument('--output', type=Path, default=ROOT / 'outputs/verification/handoff/handoff-review-check.json')
    args = parser.parse_args()
    paths = args.workbook or [HANDOFF / '02_采集核验与测试.xlsx']
    content = args.snapshot.read_text(encoding='utf-8-sig')
    snapshot = list(csv.DictReader(content.splitlines(keepends=True)))
    if not snapshot or len({r['job_id'] for r in snapshot}) != len(snapshot):
        raise ValueError('03 快照为空或 job_id 重复；先核对快照，不能静默覆盖。')
    entries, inventories = [], []
    for path in paths:
        workbook = read_workbook(path)
        review = workbook['字段核验']
        if review.get(6, {}).get('E') != '学历结论及条件' or review.get(6, {}).get('G') != '经验结论及条件':
            raise ValueError('字段核验表头已改变，请使用原工作簿结构。')
        entries += [(str(path.resolve()), n, row) for n, row in review.items() if n >= 7]
        cases = [r for n, r in workbook['功能测试'].items() if n >= 7 and r.get('A')]
        inventories.append({'file': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                            'collection_rows': sum(any(r.values()) for n, r in workbook['采集登记'].items() if n >= 7),
                            'functional_cases': len(cases),
                            'functional_statuses': dict(Counter(r.get('H', '') for r in cases)),
                            'functional_results_filled': sum(bool(r.get('G')) for r in cases)})
    report = inspect_reviews(entries, snapshot)
    with tempfile.TemporaryDirectory(prefix='recruitment_insights-handoff-') as directory:
        store = Store(Path(directory) / 'trial.sqlite3')
        first = store.import_csv(content, 'handoff_snapshot_trial')
        second = store.import_csv(content, 'handoff_snapshot_trial')
        with store.connect() as db:
            counts = {table: db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                      for table in ('jobs', 'field_reviews', 'duplicate_decisions')}
    report.update(rule_version=RULE_VERSION, snapshot=str(args.snapshot.resolve()),
                  snapshot_sha256=hashlib.sha256(args.snapshot.read_bytes()).hexdigest(),
                  workbooks=inventories, trial_import={'first': first, 'second': second, 'counts': counts,
                  'database': 'temporary; removed after validation', 'formal_database_touched': False})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('status', 'expected_samples', 'authored_rows', 'issue_counts')}, ensure_ascii=False))
    print(args.output)


if __name__ == '__main__':
    main()
