import unittest
from unittest.mock import patch

from requirements import candidates, field_fingerprint, validate_review


class RequirementRulesTests(unittest.TestCase):
    def test_explicit_fields_are_source_not_human_reviews(self):
        result = candidates({'education': '本科及以上', 'experience': '1–3年'})
        self.assertEqual(result['education']['value'], '本科')
        self.assertEqual(result['education']['condition'], '硬性要求')
        self.assertEqual(result['education']['status'], 'source')
        self.assertEqual(result['experience']['value'], '1–3年')
        self.assertEqual((result['experience']['min_years'], result['experience']['max_years']), (1, 3))

    def test_missing_is_not_unrestricted(self):
        for raw in ({}, {'education': '未披露', 'experience': '未披露'}, {'description': '欢迎加入团队，福利待遇良好。'}):
            for result in candidates(raw).values():
                self.assertEqual(result['value'], '未知')
                self.assertEqual(result['status'], 'unresolved')
                self.assertEqual(result['evidence'], '')

    def test_unrestricted_requires_explicit_statement(self):
        result = candidates({'description': '学历不限，工作经验不限。'})
        self.assertEqual(result['education']['value'], '不限')
        self.assertEqual(result['experience']['value'], '不限')
        self.assertEqual(result['education']['status'], 'candidate')
        self.assertEqual(candidates({'description': 'No degree required. No experience required.'})['education']['value'], '不限')

    def test_no_experience_applicants_excluded_is_not_unrestricted(self):
        for text in ('无工作经验者请勿投递', '无经验者不予录用', '无经验者勿投'):
            for raw in ({'experience': text}, {'description': text}):
                with self.subTest(raw=raw):
                    self.assertEqual(candidates(raw)['experience']['value'], '未知')
                    with self.assertRaises(ValueError):
                        validate_review('experience', {'value': '不限', 'condition': '条件不明确', 'evidence': text}, raw)
        for text in ('无需工作经验', '不要求相关经验', '无工作经验要求', '经验不限'):
            self.assertEqual(candidates({'experience': text})['experience']['value'], '不限')

    def test_description_preferred_never_auto_confirms(self):
        raw = {'education': '未披露', 'description': '本科优先，熟悉 Python。'}
        result = candidates(raw)['education']
        self.assertEqual((result['value'], result['condition'], result['status']), ('本科', '优先考虑', 'candidate'))
        self.assertIn(result['evidence'], raw['description'])

    def test_english_equivalent_preserves_qualification(self):
        raw = {'description': "Bachelor's degree preferred or equivalent professional experience."}
        result = candidates(raw)['education']
        self.assertEqual(result['value'], '本科')
        self.assertEqual(result['condition'], '等价经验可替代')
        self.assertEqual(result['evidence'], raw['description'])

    def test_degree_condition_does_not_borrow_skill_preference(self):
        for text in ("Bachelor's degree required. Python knowledge preferred.",
                     "Bachelor's degree required. python knowledge preferred.",
                     '本科及以上学历，熟悉 Python 者优先。',
                     '本科及以上学历。具备 Python 经验优先。',
                     'Ph.D. required. Python preferred.'):
            with self.subTest(text=text):
                self.assertEqual(candidates({'description': text})['education']['condition'], '硬性要求')
        mixed = candidates({'description': "Bachelor's degree required. Master's degree preferred."})['education']
        self.assertEqual(mixed['condition'], '条件不明确')

    def test_linked_equivalent_clause_is_preserved(self):
        for text in ('本科优先，可由同等工作经验替代。',
                     "Bachelor's degree preferred, or equivalent professional experience."):
            with self.subTest(text=text):
                result = candidates({'description': text})['education']
                self.assertEqual(result['condition'], '等价经验可替代')
                self.assertIn(result['evidence'], text)
        unrelated = candidates({'description': "Bachelor's degree required. Equivalent work experience in Python preferred."})['education']
        self.assertEqual(unrelated['condition'], '硬性要求')

    def test_multiple_degrees_are_not_compressed_to_minimum(self):
        result = candidates({'description': "Bachelor's or Master's degree required."})['education']
        self.assertEqual(result['value'], '多种学历选择')
        self.assertEqual(result['alternatives'], ['本科', '硕士'])

    def test_graduate_degree_is_ambiguous(self):
        result = candidates({'description': 'Undergraduate or graduate degree in Computer Science.'})['education']
        self.assertEqual(result['value'], '多种学历选择')
        self.assertEqual(result['alternatives'], ['本科', '硕士', '博士'])

    def test_experience_ranges_and_open_bounds(self):
        for text, low, high, value in (
            ('要求 3-5 年开发经验。', 3, 5, '3–5年'),
            ('本科及以上学历，至少 3 年 Python 开发经验。', 3, None, '年限范围'),
            ('At least 5 years of professional experience required.', 5, None, '年限范围'),
            ('2+ years of relevant experience.', 2, None, '年限范围'),
            ('工作经验 1 年以内。', 0, 1, '1年以内'),
            ('10年以上相关工作经验', 10, None, '10年以上'),
        ):
            with self.subTest(text=text):
                result = candidates({'description': text})['experience']
                self.assertEqual((result['min_years'], result['max_years'], result['value']), (low, high, value))
                self.assertEqual(result['status'], 'candidate')

    def test_unrelated_years_and_vague_experience_are_ignored(self):
        for text in ('公司成立 20 年，有丰富经验。', 'We have 10 years of history and offer 5 years of paid benefits.',
                     '公司拥有20年行业经验。', 'Our company has 10 years of industry experience.',
                     'Master your tools. We welcome graduate students.', 'Over 5 years in business. Excellent benefits.',
                     'Approximately 5 years of experience would be useful.'):
            self.assertEqual(candidates({'description': text})['experience']['value'], '未知')
        self.assertEqual(candidates({'description': 'Master your tools. We welcome graduate students.'})['education']['value'], '未知')

    def test_conflicting_experience_is_left_for_review(self):
        result = candidates({'description': '5 years of professional experience; 2 years of relevant experience.'})['experience']
        self.assertEqual(result['value'], '未知')

    def test_new_graduate(self):
        self.assertEqual(candidates({'description': '欢迎应届毕业生申请。'})['experience']['value'], '应届')
        self.assertEqual(candidates({'description': 'Recent graduates are welcome.'})['experience']['value'], '应届')

    def test_source_value_takes_precedence(self):
        result = candidates({'education': '大专', 'description': '本科优先。'})['education']
        self.assertEqual((result['value'], result['status'], result['evidence']), ('大专', 'source', '大专'))

    def test_review_accepts_exact_evidence_and_unknown(self):
        result = validate_review('education', {'value': '本科', 'condition': '优先考虑', 'evidence': '本科优先'}, {'description': '要求：本科优先。'})
        self.assertEqual(result['status'], 'reviewed')
        result = validate_review('education', {'value': '未知', 'condition': '未发现明确要求'}, {})
        self.assertEqual(result['evidence'], '')

    def test_review_rejects_invented_evidence(self):
        for evidence in ('硕士学历', '', '未披露'):
            with self.assertRaises(ValueError):
                validate_review('education', {'value': '硕士', 'condition': '硬性要求', 'evidence': evidence}, {'education': '未披露', 'description': '本科优先。'})

    def test_review_rejects_arbitrary_categories_and_wrong_types(self):
        good = {'value': '本科', 'condition': '硬性要求', 'evidence': '本科'}
        for bad in ({'value': ['本科']}, {'value': '顶尖学历'}, {'condition': {}}, {'condition': '随意条件'},
                    {'evidence': ['本科']}, {'evidence': '本' * 2001}, {'alternatives': '硕士'},
                    {'alternatives': [{}]}, {'alternatives': ['本科', '本科']}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_review('education', good | bad, {'education': '本科'})
        for field, body, raw in (('salary', good, {}), ('education', [], {}), ('education', good, [])):
            with self.assertRaises(ValueError):
                validate_review(field, body, raw)

    def test_multidegree_review_requires_valid_alternatives(self):
        body = {'value': '多种学历选择', 'condition': '硬性要求', 'evidence': '本科或硕士', 'alternatives': ['本科', '硕士']}
        self.assertEqual(validate_review('education', body, {'description': '本科或硕士'})['alternatives'], ['本科', '硕士'])
        for alternatives in ([], ['本科'], ['本科', '未知'], ['本科', '自定义']):
            with self.assertRaises(ValueError):
                validate_review('education', body | {'alternatives': alternatives}, {'description': '本科或硕士'})

    def test_review_unrestricted_cannot_mean_missing(self):
        body = {'value': '不限', 'condition': '条件不明确', 'evidence': '欢迎加入'}
        with self.assertRaises(ValueError):
            validate_review('education', body, {'description': '欢迎加入'})
        self.assertEqual(validate_review('education', body | {'evidence': '不限'}, {'education': '不限'})['value'], '不限')

    def test_review_numeric_bounds(self):
        body = {'value': '年限范围', 'condition': '硬性要求', 'evidence': '至少2年经验', 'min_years': 2, 'max_years': None}
        self.assertEqual(validate_review('experience', body, {'description': '至少2年经验'})['min_years'], 2)
        for bad in ({'min_years': True}, {'min_years': '2'}, {'min_years': float('nan')}, {'min_years': -1},
                    {'min_years': 10**1000}, {'max_years': 10**1000}, {'max_years': float('inf')},
                    {'max_years': 61}, {'max_years': 1}, {'min_years': None}, {'value': '未知'}, {'value': '不限'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_review('experience', body | bad, {'description': '至少2年经验'})
        bucket = {'value': '3–5年', 'condition': '硬性要求', 'evidence': '3-5年经验'}
        result = validate_review('experience', bucket, {'description': '3-5年经验'})
        self.assertEqual((result['min_years'], result['max_years']), (3, 5))

    def test_fingerprint_only_tracks_relevant_source_and_rules(self):
        raw = {'education': '本科', 'experience': '3年', 'description': '原文', 'salary_text': '10k'}
        first = field_fingerprint(raw, 'education')
        self.assertEqual(first, field_fingerprint(raw | {'salary_text': '20k', 'experience': '5年'}, 'education'))
        self.assertNotEqual(first, field_fingerprint(raw | {'education': '硕士'}, 'education'))
        self.assertNotEqual(first, field_fingerprint(raw | {'description': '新原文'}, 'education'))
        with patch('requirements.RULE_VERSION', 'next-rule'):
            self.assertNotEqual(first, field_fingerprint(raw, 'education'))


if __name__ == '__main__':
    unittest.main()
