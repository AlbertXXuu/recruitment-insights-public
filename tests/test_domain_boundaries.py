"""Checks for pure domain imports and preserved compatibility contracts."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from recruitment_insights.domain.access import can_manage, can_read_job, may_review, may_read_duplicate_reason
from recruitment_insights.domain.jobs import normalize, analyze
from recruitment_insights.domain.reviews import apply_fields, effective_fields


class DomainBoundaryTests(unittest.TestCase):
    def test_domain_imports_without_storage_web_or_application(self):
        script = '''
import sys
before = set(sys.modules)  # Host TLS/site initialization may already load urllib.request.
from recruitment_insights.domain import jobs, access, errors, reviews, requirements, quality
import requirements as old_requirements
import quality as old_quality
assert old_requirements is requirements
assert old_quality is quality
for name in set(sys.modules) - before:
    assert not name.startswith(('recruitment_insights.application', 'recruitment_insights.infrastructure', 'recruitment_insights.web'))
    assert name not in ('core', 'accounts', 'app', 'sqlite3', 'urllib.request', 'http.server')
assert jobs.analyze([])['total'] == 0
'''
        result = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_rule_alias_preserves_version_patch(self):
        import requirements as legacy
        from recruitment_insights.domain import requirements
        self.assertIs(legacy, requirements)
        raw = {'description': '本科', 'education': ''}
        before = requirements.field_fingerprint(raw, 'education')
        with patch('requirements.RULE_VERSION', 'boundary-test-version'):
            self.assertNotEqual(before, requirements.field_fingerprint(raw, 'education'))

    def test_visibility_role_matrix_and_inactive_boundary(self):
        actors = [None, {'role': 'admin'}, {'role': 'boss', 'org_id': 1},
                  {'role': 'hr', 'org_id': 1, 'can_manage_jobs': 0},
                  {'role': 'boss', 'org_id': 2}, {'role': 'candidate'}]
        cases = [
            ({'owner_org_id': None, 'published': 0, 'active': 1}, [True]*6, [True]*6),
            ({'owner_org_id': 1, 'published': 0, 'active': 1}, [True, True, True, True, False, False], [True, True, True, True, False, False]),
            ({'owner_org_id': 1, 'published': 1, 'active': 1}, [True]*6, [True]*6),
            ({'owner_org_id': None, 'published': 1, 'active': 0}, [False]*6, [True, True, False, False, False, False]),
            ({'owner_org_id': 1, 'published': 1, 'active': 0}, [False]*6, [True, True, True, True, False, False]),
        ]
        for row, default, details in cases:
            with self.subTest(row=row):
                self.assertEqual([can_read_job(row, actor) for actor in actors], default)
                self.assertEqual([can_read_job(row, actor, include_inactive=True) for actor in actors], details)

    def test_read_only_hr_can_read_own_reason_but_cannot_review(self):
        actor = {'role': 'hr', 'org_id': 1, 'can_manage_jobs': 0}
        own, other = {'owner_org_id': 1}, {'owner_org_id': 2}
        self.assertFalse(can_manage(actor))
        self.assertFalse(may_review(own, actor))
        self.assertTrue(may_read_duplicate_reason(own, own, actor))
        self.assertFalse(may_read_duplicate_reason(own, other, actor))
        self.assertFalse(may_read_duplicate_reason(own, own, None))

    def test_projection_keeps_pending_out_of_confirmed_statistics(self):
        raw = dict(job_id='pure-1', title='Python 工程师', company='测试公司', location='北京',
                   description='要求本科及以上学历，至少三年相关经验。', education='', experience='')
        job = normalize(raw)
        row = dict(id=1, raw=json.dumps(raw), payload=json.dumps(job))
        projected = apply_fields(dict(job, source='fixture', is_demo=True, last_seen='2026-10-02'),
                                 effective_fields(row, {}))
        stats = analyze([projected])
        self.assertEqual(projected['education'], '待核验')
        self.assertEqual(projected['experience'], '待核验')
        self.assertEqual(stats['requirement_coverage']['education']['confirmed'], 0)
        self.assertEqual(stats['requirement_coverage']['education']['pending'], 1)
        self.assertIsNone(stats['salaries'][0]['median'])


if __name__ == '__main__':
    unittest.main()
