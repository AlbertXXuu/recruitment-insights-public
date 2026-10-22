"""Preserve requirement semantics while bounding ambiguous regex computation."""
from pathlib import Path
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from recruitment_insights.domain import requirements, reviews
from recruitment_insights.domain.requirements import candidates, _condition_context


class RequirementComputationTests(unittest.TestCase):
    def setUp(self):
        reviews.cached_candidates.cache_clear()

    def tearDown(self):
        reviews.cached_candidates.cache_clear()

    def test_chinese_experience_keeps_ascii_tokens_unicode_casefold_and_five_token_limit(self):
        accepted = (
            '至少 3 年 Python 开发经验。',
            '至少 3 年 Python开发经验。',
            '至少 3 年 C++经验。',
            '至少 3 年 C#经验。',
            '至少 3 年 Node.js/.NET/API-v2经验。',
            '至少 3 年 İıſK经验。',
            '至少 3 年 A B C D E 经验。',
            '至少 3 年 A\tB\nC\u00a0D\u3000E 经验。',
            '至少 3 年 相关实际专业行业开发经验。',
            '至少 3 年 PythonPythonPythonPythonPythonPython经验。',
            '至少 3 年 以上 的 软件 全职 工作 经验。',
            '至少 3 年 Python经验丰富。',
        )
        for text in accepted:
            with self.subTest(text=text):
                result = candidates({'description': text})['experience']
                self.assertEqual((result['value'], result['min_years'], result['max_years'], result['status']),
                                 ('年限范围', 3, None, 'candidate'))
                self.assertIn(result['evidence'], text)
        rejected = (
            '3年 A B C D E F 经验。',
            '3年 相关实际专业行业开发编程经验。',
            '3年 .NET经验。',
            '3年 3D经验。',
            '3年 Python 无关 经验。',
            '3年（Python）经验。',
        )
        for text in rejected:
            with self.subTest(text=text):
                result = candidates({'description': text})['experience']
                self.assertEqual((result['value'], result['status']), ('未知', 'unresolved'))

    def test_english_fallback_keeps_word_boundaries_modifiers_and_numeric_ranges(self):
        accepted = (
            ('At least 3 years experience.', 3, None),
            ('At least 3 years of professional experience.', 3, None),
            ('At least 3 years of relevant professional practical work working industry technical experience.', 3, None),
            ('3 years of hands-on experience required.', 3, None),
            ('3 years of full-time experience required.', 3, None),
            ('At least 3.5 years of software experience.', 3.5, None),
            ('3–5 years of development experience required.', 3, 5),
            ('EXPERIENCE AT LEAST 3 YEARS', 3, None),
        )
        for text, low, high in accepted:
            with self.subTest(text=text):
                result = candidates({'description': text})['experience']
                self.assertEqual((result['min_years'], result['max_years'], result['status']),
                                 (low, high, 'candidate'))
                self.assertEqual(result['condition'], '硬性要求')
                self.assertIn(result['evidence'], text)
        rejected = (
            '3 years of history and extra benefits.',
            '3 years of relevant professional practical work working industry technical prior experience.',
            '3 years of professional experienceextra.',
            '3 years of professional experience_more.',
            'Our company has 3 years of professional experience.',
            'Approximately 3 years of professional experience.',
        )
        for text in rejected:
            with self.subTest(text=text):
                self.assertEqual(candidates({'description': text})['experience']['value'], '未知')

    def test_explicit_source_experience_still_bypasses_description_context_gate(self):
        for text in ('3年', '3 years', '3年 A B C D E F 经验。'):
            with self.subTest(text=text):
                result = candidates({'experience': text, 'description': '经验不限。'})['experience']
                self.assertEqual((result['value'], result['min_years'], result['max_years'],
                                  result['status'], result['condition'], result['evidence']),
                                 ('年限范围', 3, None, 'source', '硬性要求', text))
        for text in ('工作经验至少3年', 'Experience at least 3 years'):
            with self.subTest(text=text):
                result = candidates({'description': text})['experience']
                self.assertEqual((result['min_years'], result['status']), (3, 'candidate'))

    def test_clause_boundaries_preserve_abbreviation_word_boundary_and_punctuation(self):
        cases = (
            ('Ph.D. required. Python preferred.', '硬性要求'),
            ('本科 M.S. required, Python preferred.', '硬性要求'),
            ('本科 B.S. required，Python preferred.', '硬性要求'),
            ('本科 m.s. preferred. Python required.', '优先考虑'),
            ('本科 xM.S. preferred. Python required.', '条件不明确'),
            ('本科 中B.S. preferred. Python required.', '条件不明确'),
            ('本科及以上学历！Python 优先。', '硬性要求'),
            ('本科及以上学历? Python 优先。', '硬性要求'),
            ('本科及以上学历\nPython 优先。', '硬性要求'),
            # A period without following whitespace is deliberately not a
            # clause boundary in the existing rules.
            ('本科及以上学历.Python 优先。', '优先考虑'),
        )
        for text, condition in cases:
            with self.subTest(text=text):
                result = candidates({'description': text})['education']
                self.assertEqual(result['condition'], condition)
                self.assertIn(result['evidence'], text)

    def test_linked_alternative_remains_local_and_evidence_remains_an_exact_quote(self):
        cases = (
            ('本科优先，可由同等工作经验替代。Python 必须熟练。', '等价经验可替代'),
            ("Bachelor's degree preferred, alternatively equivalent professional experience. Python required.",
             '等价经验可替代'),
            ("Bachelor's degree required. Equivalent work experience in Python preferred.", '硬性要求'),
            ('本科及以上学历。熟悉 Python。或同等工作经验。', '硬性要求'),
            ("Bachelor's degree required. Master's degree preferred.", '条件不明确'),
        )
        for text, condition in cases:
            with self.subTest(text=text):
                result = candidates({'description': text})['education']
                self.assertEqual(result['condition'], condition)
                self.assertTrue(result['evidence'])
                self.assertIn(result['evidence'], text)

    def test_abbreviation_suffix_keeps_word_and_end_of_line_boundaries(self):
        cases = (
            ('Ph.D. 本科 required.', 'Ph.D. 本科 required'),
            ('M.S. 本科 required.', 'M.S. 本科 required'),
            ('B.S. 本科 required.', 'B.S. 本科 required'),
            ('underph.d. 本科 required.', ' 本科 required'),
            ('中Ph.D. 本科 required.', ' 本科 required'),
            # The original suffix regex uses $, which can match just before a
            # final newline. The bounded search must keep that exact behavior.
            ('Ph.D\n. 本科 required.', '. 本科 required'),
            ('M.S\n. 本科 required.', '. 本科 required'),
            ('B.S\n. 本科 required.', '. 本科 required'),
            ('xPh.D\n. 本科 required.', ' 本科 required'),
            ('B.S.\n本科 required.', '本科 required'),
        )
        for text, context in cases:
            with self.subTest(text=text):
                start = text.index('本科')
                self.assertEqual(_condition_context(text, start, start + 2), context)

    def test_hint_gates_match_unconditionally_scanning_the_same_deterministic_corpus(self):
        snippets = (
            '高中', '高中及以下', '初中', '小学', 'high school', 'high school diploma', 'high school degree',
            '中专', '中技', '技校', 'vocational school', 'vocational diploma',
            '大专', '专科', 'associate degree', "associate's degree", 'associate’s degree',
            '本科', '学士', 'bachelor', "bachelor's", 'bachelor’s', 'bachelors',
            'undergraduate degree', 'undergraduate or graduate degree',
            '硕士', "master's", 'master’s', 'masters', 'master degree', 'master of science',
            '博士', 'PhD', 'Ph.D.', 'doctor', 'doctoral degree', 'doctorate degree', 'graduate degree',
            '学历不作要求', '学历不做要求', '学历不限', '学历无要求', '不限学历', '无学历要求',
            'No formal education is required', 'No degree required', 'Degree is not required',
            'Degree not required', '工作经验不限', '经验无要求', '经验不作要求',
            '不限工作经验', '不限经验', '无需相关经验', '不要求工作经验', '无相关经验要求', '无经验要求',
            'No experience required', 'No prior experience is required', 'No work experience required',
            '应届', '应届毕业生', 'new graduate', 'recent graduates', 'fresh graduate',
            '3-5年开发经验', '3 to 5 years of work experience', '3+年经验',
            '3年以下经验', '3年以内经验', '3年以上经验', '3 years software experience',
            '一年以上工作经验', '两年以上工作经验', '三年以上工作经验',
            '五年以上工作经验', '八年以上工作经验', '十年以上工作经验',
            '不限', '无要求', 'mastery and higher priorities', 'graduate students discuss education',
            'No paid benefits. Experience preferred.', 'yearly bonuses', '欢迎加入团队。',
            "Bachelor's degree required. Master's degree preferred.",
            '本科优先，可由同等工作经验替代。',
        )
        # Force every existing gate open while retaining the exact same parser.
        # This compares full candidate dictionaries without duplicating rules.
        all_hints = ' '.join(hint for _, hints, _ in requirements._EDUCATION_PATTERNS for hint in hints)
        all_hints += ' 学历 education degree graduate 经验 no experience 应届 年 year'
        unusual_case = str.maketrans({'i': 'ı', 'I': 'İ', 's': 'ſ', 'S': 'ſ', 'k': 'K', 'K': 'K'})
        for snippet in snippets:
            for text in dict.fromkeys((snippet, snippet.upper(), snippet.translate(unusual_case))):
                for raw in ({'description': text}, {'education': text, 'experience': text},
                            {'education': '未披露', 'experience': '未知', 'description': text}):
                    with self.subTest(raw=raw):
                        gated = candidates(raw)
                        with patch.object(requirements, '_hint_text', return_value=all_hints):
                            ungated = candidates(raw)
                        self.assertEqual(gated, ungated)

    def test_hint_gates_preserve_unicode_literal_branches_and_original_evidence(self):
        cases = (
            ('hİgh school', 'education', '高中及以下'),
            ('hıgh school', 'education', '高中及以下'),
            ('vocatİonal dıploma', 'education', '中专中技'),
            ('aſſocıate’ſ degree', 'education', '大专'),
            ('bachelor’ſ', 'education', '本科'),
            ('maſter’ſ', 'education', '硕士'),
            ('maſters', 'education', '硕士'),
            ('DOCTORATE degree', 'education', '博士'),
            ('Ph.D.', 'education', '博士'),
            ('graduate DEGREE', 'education', '多种学历选择'),
            ('No educatİon ıſ requıred', 'education', '不限'),
            ('degree ıſ not requıred', 'education', '不限'),
            ('No worK experİence ıſ requıred', 'experience', '不限'),
            ('freſh graduateſ', 'experience', '应届'),
            ('At leaſt 3 yearſ of worK experİence requıred.', 'experience', '年限范围'),
        )
        for text, field, value in cases:
            with self.subTest(text=text, field=field):
                result = candidates({'description': text})[field]
                self.assertEqual((result['value'], result['status']), (value, 'candidate'))
                self.assertTrue(result['evidence'])
                self.assertIn(result['evidence'], text)
                if value == '多种学历选择':
                    self.assertEqual(result['alternatives'], ['硕士', '博士'])
                if value == '年限范围':
                    self.assertEqual((result['min_years'], result['max_years']), (3, None))

    def test_hint_matches_do_not_become_results_and_explicit_unrestricted_stays_separate(self):
        for text in ('aßociate degree', 'maﬆers', 'hİ\u0307gh school', 'doctored experience',
                     'degree is preferred', 'graduate students', 'yearly bonuses',
                     'No paid benefits. Experience preferred.', '不限', '无要求'):
            with self.subTest(text=text):
                for result in candidates({'description': text}).values():
                    self.assertEqual((result['value'], result['status'], result['evidence']),
                                     ('未知', 'unresolved', ''))
        explicit = candidates({'education': ' 不限 ', 'experience': ' 无要求 ',
                               'description': '本科及以上学历，3年开发经验。'})
        for field, evidence in (('education', '不限'), ('experience', '无要求')):
            self.assertEqual((explicit[field]['value'], explicit[field]['status'], explicit[field]['evidence']),
                             ('不限', 'source', evidence))

    def test_pure_projection_reuses_fingerprints_but_reloads_review_source_and_rule_state(self):
        raw = {'education': '', 'experience': '', 'description': '本科及以上学历。要求3年工作经验。'}
        row = dict(id=1, raw=json.dumps(raw), payload=json.dumps({'description': raw['description']}))
        with patch.object(reviews, 'field_fingerprint', wraps=reviews.field_fingerprint) as fingerprint:
            initial = reviews.effective_fields(row, {})
            self.assertEqual(fingerprint.call_count, 2)
            self.assertEqual(reviews.effective_fields(row, {}), initial)
            self.assertEqual(fingerprint.call_count, 2, 'Warm projection must reuse both pure fingerprints')
            review = dict(state='confirmed', version=1, reviewed_at='2026-10-08T12:00:00Z',
                          source_hash=initial['education']['source_hash'],
                          conclusion=json.dumps(dict(value='本科', condition='硬性要求',
                                                      evidence='本科及以上学历', alternatives=[])))
            confirmed = reviews.effective_fields(row, {(1, 'education'): review})
            self.assertEqual(confirmed['education']['status'], 'confirmed')
            revoked = reviews.effective_fields(row, {(1, 'education'): dict(review, state='revoked', version=2)})
            self.assertEqual((revoked['education']['status'], revoked['education']['version']), ('candidate', 2))
            self.assertEqual(fingerprint.call_count, 2, 'Human review state is projected outside the pure cache')
            changed = dict(row, raw=json.dumps(dict(raw, education='硕士')))
            updated = reviews.effective_fields(changed, {(1, 'education'): review})
            self.assertEqual(updated['education']['status'], 'stale')
            self.assertNotEqual(updated['education']['source_hash'], initial['education']['source_hash'])
            self.assertEqual(fingerprint.call_count, 4)
            with patch.object(requirements, 'RULE_VERSION', requirements.RULE_VERSION + '-computation'):
                versioned = reviews.effective_fields(changed, {(1, 'education'): review})
                self.assertEqual(versioned['education']['status'], 'stale')
                self.assertNotEqual(versioned['education']['source_hash'], updated['education']['source_hash'])
                self.assertEqual(fingerprint.call_count, 6)
                self.assertEqual(reviews.effective_fields(changed, {})['education']['status'], 'source')
                self.assertEqual(fingerprint.call_count, 6)

    def test_many_sentence_boundaries_complete_within_a_bounded_subprocess(self):
        script = '''
from recruitment_insights.domain.requirements import candidates
text = 'A. ' * 6000 + '本科 required. Python preferred.'
for index in range(20):
    result = candidates({'description': text})['education']
    assert result['condition'] == '硬性要求', result
    assert result['value'] == '本科', result
    assert result['evidence'] in text, result
print('checked 20 long texts with 6000 sentence boundaries')
'''
        try:
            result = subprocess.run([sys.executable, '-B', '-X', 'utf8', '-c', script],
                                    cwd=Path(__file__).resolve().parents[1],
                                    capture_output=True, text=True, encoding='utf-8', timeout=5)
        except subprocess.TimeoutExpired:
            self.fail('20 texts with 6000 sentence boundaries exceeded 5 seconds; prefix scanning regressed')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'checked 20 long texts with 6000 sentence boundaries')

    def test_long_nonmatching_suffixes_complete_within_a_bounded_subprocess(self):
        # These are bounded 100-character context windows, not large input.
        # An ambiguous token regex can still try millions of ways to split one
        # ASCII word before failing. A subprocess makes failure safely bounded.
        script = '''
from recruitment_insights.domain.requirements import candidates
suffixes = ('x' * 100, 'x' * 110 + '经验', 'x' * 90 + '无关经验')
for index in range(12):
    for suffix in suffixes:
        result = candidates({'description': '3年 ' + suffix})['experience']
        assert result['value'] == '未知', result
        assert result['status'] == 'unresolved', result
print('checked 36 bounded nonmatching inputs')
'''
        try:
            result = subprocess.run([sys.executable, '-B', '-X', 'utf8', '-c', script],
                                    cwd=Path(__file__).resolve().parents[1],
                                    capture_output=True, text=True, encoding='utf-8', timeout=5)
        except subprocess.TimeoutExpired:
            self.fail('36 bounded requirement inputs exceeded 5 seconds; ambiguous regex work regressed')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'checked 36 bounded nonmatching inputs')


if __name__ == '__main__':
    unittest.main()
