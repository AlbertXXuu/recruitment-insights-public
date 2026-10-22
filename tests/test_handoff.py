"""Handoff rules use fixed synthetic fixtures, independent of team progress."""
import unittest

from scripts.check_handoff import inspect_reviews


def snapshot(**updates):
    return dict(dict(job_id='sample-1', title='测试岗位',
                     source_url='https://example.com/jobs/sample-1',
                     description='本科及以上学历。要求 3–5 年开发经验。'), **updates)


def review_row(**updates):
    return dict(dict(A='greenhouse:cloudflare', B='sample-1', C='测试岗位',
                     D='本科及以上学历', E='本科及以上（硬性要求）',
                     F='要求 3–5 年开发经验', G='3–5 年（硬性要求）',
                     H='合成测试核验人', I='2026-09-28',
                     J='https://example.com/jobs/sample-1', K=''), **updates)


class HandoffReviewTests(unittest.TestCase):
    def inspect(self, row, raw=None):
        return inspect_reviews([('synthetic-review.xlsx', 7, row)], [raw or snapshot()])

    def test_thirty_prelisted_rows_do_not_count_as_human_reviews(self):
        originals = [snapshot(job_id=f'sample-{n}') for n in range(30)]
        rows = [('synthetic-review.xlsx', n+7, {'A':'greenhouse:cloudflare', 'B':raw['job_id'],
                                              'C':raw['title'], 'J':raw['source_url']})
                for n,raw in enumerate(originals)]
        report = inspect_reviews(rows, originals)
        self.assertEqual(len(rows), 30)
        self.assertEqual(report['expected_samples'], 30)
        self.assertEqual(report['authored_rows'], 0)
        self.assertEqual(report['filled_field_cells'], 0)
        self.assertEqual(report['issue_counts'], {'not_returned': 30})
        self.assertEqual(report['status'], '等待真实人审回交')

    def test_missing_reviewer_or_date_blocks_completeness(self):
        for column in ('H', 'I'):
            with self.subTest(column=column):
                report = self.inspect(review_row(**{column: ''}))
                self.assertEqual(report['authored_rows'], 1)
                self.assertEqual(report['structurally_complete_rows_before_duplicate_adjudication'], 0)
                self.assertEqual(report['issue_counts']['missing_reviewer'], 1)

    def test_missing_or_mismatched_evidence_is_reported_per_field(self):
        for column in ('D', 'F'):
            for evidence, code in (('', 'missing_evidence'), ('这句证据不在岗位快照中', 'evidence_mismatch')):
                with self.subTest(column=column, evidence=evidence):
                    report = self.inspect(review_row(**{column: evidence}))
                    self.assertEqual(report['structurally_complete_rows_before_duplicate_adjudication'], 0)
                    self.assertEqual(report['issue_counts'][code], 1)
                    issue = next(item for item in report['issues'] if item['code'] == code)
                    self.assertEqual((issue['file'], issue['row'], issue['job_id']),
                                     ('synthetic-review.xlsx', 7, 'sample-1'))

    def test_missing_requirement_phrase_inside_label_cannot_bypass_evidence(self):
        for conclusion in ('本科（原文未提及优先条件）', '本科（未发现明确要求为全日制）'):
            with self.subTest(conclusion=conclusion):
                report = self.inspect(review_row(D='这句证据不在岗位快照中', E=conclusion))
                self.assertEqual(report['issue_counts']['evidence_mismatch'], 1)
                self.assertEqual(report['structurally_complete_rows_before_duplicate_adjudication'], 0)
        for conclusion in ('原文未提及', '未发现明确要求。'):
            with self.subTest(conclusion=conclusion):
                report = self.inspect(review_row(D='已完整阅读岗位描述，未找到明确学历要求。', E=conclusion))
                self.assertNotIn('evidence_mismatch', report['issue_counts'])
                self.assertEqual(report['structurally_complete_rows_before_duplicate_adjudication'], 1)
                self.assertIsNone(report['accuracy'])

    def test_conflicting_conclusions_across_copies_are_not_adjudicated(self):
        entries = [('member-a.xlsx', 7, review_row()),
                   ('member-b.xlsx', 12, review_row(E='本科优先', H='另一合成核验人'))]
        report = inspect_reviews(entries, [snapshot()])
        self.assertEqual(report['expected_samples'], 1)
        self.assertEqual(report['authored_rows'], 2)
        self.assertEqual(report['issue_counts']['conflicting_labels'], 1)
        self.assertIn('裁定分歧', report['status'])
        self.assertIsNone(report['accuracy'])
        self.assertNotIn('adjudicated_labels', report)

    def test_identical_copies_are_detected_as_duplicates(self):
        report = inspect_reviews([('a.xlsx', 7, review_row()), ('b.xlsx', 8, review_row())], [snapshot()])
        self.assertEqual(report['issue_counts']['duplicate_review'], 1)
        self.assertNotIn('conflicting_labels', report['issue_counts'])
        self.assertEqual(report['expected_fields'], 2)

    def test_missing_description_and_unresolved_labels_are_incomplete(self):
        report = self.inspect(review_row(), snapshot(description=''))
        self.assertEqual(report['issue_counts']['missing_description'], 1)
        self.assertEqual(report['structurally_complete_rows_before_duplicate_adjudication'], 0)
        for column in ('E', 'G'):
            with self.subTest(column=column):
                report = self.inspect(review_row(**{column: '无法判断，需要原始网页佐证'}))
                self.assertEqual(report['issue_counts']['unresolved'], 1)
                self.assertEqual(report['structurally_complete_rows_before_duplicate_adjudication'], 0)

    def test_structural_completeness_never_becomes_accuracy_or_truth(self):
        report = self.inspect(review_row())
        self.assertEqual(report['structurally_complete_rows_before_duplicate_adjudication'], 1)
        self.assertEqual(report['issue_counts'], {})
        for metric in ('accuracy', 'precision', 'recall'):
            self.assertIsNone(report[metric])
        self.assertIn('等待人工', report['status'])
        self.assertIn('格式完整不代表结论正确', report['qualification'])
        self.assertNotIn('ground_truth', report)

    def test_missing_source_and_formula_input_cannot_be_complete(self):
        report = self.inspect(review_row(A='unrecognised-source'))
        self.assertEqual(report['issue_counts']['unknown_key'], 1)
        self.assertEqual(report['issue_counts']['missing_sample'], 1)
        self.assertEqual(report['structurally_complete_rows_before_duplicate_adjudication'], 0)
        report = self.inspect(review_row(H='[公式：请改为可追溯的人工填写值]'))
        self.assertEqual(report['issue_counts']['formula_input'], 1)
        self.assertEqual(report['structurally_complete_rows_before_duplicate_adjudication'], 0)


if __name__ == '__main__':
    unittest.main()
