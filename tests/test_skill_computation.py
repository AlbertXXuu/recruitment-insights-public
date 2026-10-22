"""Preserve skill matching semantics while avoiding needless full-text scans."""
import hashlib
import json
import random
import re
import unittest
from unittest.mock import patch

from recruitment_insights.domain import jobs


def reference_skills(text):
    """The pre-optimization rules, including Unicode and Go case behavior."""
    found = []
    for skill in jobs.SKILLS:
        pattern = r'(?<![\w+])' + re.escape(skill) + r'(?![\w+])'
        if re.search(pattern, text, 0 if skill == 'Go' else re.I):
            found.append(skill)
    if re.search(r'\bgolang\b', text, re.I) and 'Go' not in found:
        found.append('Go')
    return sorted(found)


class SkillComputationTests(unittest.TestCase):
    def test_all_skills_deduplicate_and_keep_sorted_output(self):
        text = ' / '.join(jobs.SKILLS) + ' / Python python JavaScript C++ Go'
        self.assertEqual(jobs.extract_skills(text), sorted(jobs.SKILLS))

    def test_go_is_case_sensitive_and_golang_alias_is_not(self):
        for text, expected in (
            ('go GO gO ongoing google', []),
            ('Go', ['Go']),
            ('golang GOLANG GoLang', ['Go']),
            ('xgolang golangx _golang golang_ 中文golang golang中文', []),
            ('+golang+', ['Go']),
            ('+Go+ Go+', []),
        ):
            with self.subTest(text=text):
                self.assertEqual(jobs.extract_skills(text), expected)

    def test_unicode_word_and_plus_boundaries_remain_exact(self):
        for skill in jobs.SKILLS:
            for boundary in ('a', 'Z', '0', '_', '中', 'é', 'K', '+'):
                for text in (boundary + skill, skill + boundary):
                    with self.subTest(text=text):
                        self.assertEqual(jobs.extract_skills(text), [])
            for boundary in (' ', '/', '-', '.', '\n', '\u0301', '😀'):
                with self.subTest(skill=skill, boundary=boundary):
                    self.assertEqual(jobs.extract_skills(boundary + skill + boundary), [skill])

    def test_cplusplus_and_overlapping_java_names(self):
        self.assertEqual(jobs.extract_skills('JavaScript C++/Python Java-Script'),
                         ['C++', 'Java', 'JavaScript', 'Python'])
        self.assertEqual(jobs.extract_skills('C+++ C++_ C++17 JavaScriptX'), [])

    def test_unicode_ignorecase_ascii_equivalents(self):
        for text, expected in (
            ('LİNUX', ['Linux']),
            ('LıNUX', ['Linux']),
            ('GİT Gıt', ['Git']),
            ('Javaſcript', ['JavaScript']),
            ('Typeſcript', ['TypeScript']),
            ('ſql', ['SQL']),
            ('DocKer', ['Docker']),
            ('Kubernetes', ['Kubernetes']),
            ('PYTKON', []),
        ):
            with self.subTest(text=text):
                self.assertEqual(jobs.extract_skills(text), expected)

    def test_reference_equivalence_for_varied_unicode_and_boundaries(self):
        rng = random.Random(20261013)
        terms = list(jobs.SKILLS) + [s.lower() for s in jobs.SKILLS]
        terms += ['GOLANG', 'LİNUX', 'Gıt', 'Javaſcript', 'DocKer', 'ß', '中文']
        edges = ['', ' ', '_', '+', '/', '\n', '中', '9', '\u0301', '😀']
        for _ in range(600):
            text = ''.join(rng.choice(edges) + rng.choice(terms) + rng.choice(edges)
                           for _ in range(rng.randint(1, 12)))
            with self.subTest(text=text):
                self.assertEqual(jobs.extract_skills(text), reference_skills(text))

    def test_normalized_payload_and_import_fingerprint_remain_identical(self):
        for description in (
            '<p>Javaſcript / DocKer / LİNUX / Gıt / golang</p>',
            '中文Python PyTorch_ TensorFlow+ C++17 Go GO go',
            'Python, PYTHON; JavaScript / Java / C++ / Go',
        ):
            raw = dict(job_id='same-job', title='Python Engineer', company='Fixture',
                       location='Test', description=description)
            with patch.object(jobs, 'extract_skills', reference_skills):
                expected = jobs.normalize(raw)
            actual = jobs.normalize(raw)
            expected_json = json.dumps(expected, ensure_ascii=False, sort_keys=True)
            actual_json = json.dumps(actual, ensure_ascii=False, sort_keys=True)
            self.assertEqual(actual_json, expected_json)
            self.assertEqual(hashlib.sha256(actual_json.encode()).hexdigest(),
                             hashlib.sha256(expected_json.encode()).hexdigest())


if __name__ == '__main__':
    unittest.main()
