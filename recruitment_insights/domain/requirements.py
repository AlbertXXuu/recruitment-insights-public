"""Conservative, evidence-preserving education and experience rules (stdlib only).

Source fields and description candidates are deliberately distinct. A candidate is
never an approval; callers decide whether a current human review can take effect.
"""
from __future__ import annotations

import hashlib
import json
import math
import re

RULE_VERSION = 'requirements-3'
EDUCATION_VALUES = ('高中及以下', '中专中技', '大专', '本科', '硕士', '博士', '不限', '未知', '多种学历选择')
EXPERIENCE_VALUES = ('不限', '应届', '1年以内', '1–3年', '3–5年', '5–10年', '10年以上', '未知', '年限范围')
CONDITIONS = ('硬性要求', '优先考虑', '等价经验可替代', '条件不明确', '未发现明确要求')
FIELDS = ('education', 'experience')
_MISSING = {'', '未披露', '未提供', '未说明', '未知', '不详', 'n/a', 'na', 'none', 'null', 'unknown', '-'}
# Each hint group contains a necessary literal for every branch of its pattern.
# Hints only skip impossible scans; matching and evidence still use original text.
_EDUCATION_PATTERNS = (
    ('高中及以下', ('高中', '初中', '小学', 'high'), r'高中(?:及以下)?|初中|小学|\bhigh\s+school(?:\s+(?:diploma|degree))?\b'),
    ('中专中技', ('中专', '中技', '技校', 'vocational'), r'中专|中技|技校|\bvocational\s+(?:school|diploma)\b'),
    ('大专', ('大专', '专科', 'associate'), r'大专|专科|\bassociate(?:[’\x27]s)?\s+degree\b'),
    ('本科', ('本科', '学士', 'bachelor', 'undergraduate'), r'本科|学士|\bbachelor(?:[’\x27]s|s)?\b|\bundergraduate(?=\s+(?:degree|or\s+graduate\s+degree))'),
    ('硕士', ('硕士', 'master'), r'硕士|\bmaster(?:[’\x27]s|s)\b|\bmaster\s+(?:degree|of)\b'),
    ('博士', ('博士', 'ph', 'doctor'), r'博士|\bph\.?d\.?\b|\bdoctor(?:al|ate)(?:\s+degree)?\b'),
)
_GRADUATE = re.compile(r'(?<!under)\bgraduate\s+degree\b', re.I)
_DEGREE_ABBREVIATION = re.compile(r'\b(?:ph\.?d|m\.?s|b\.?s)$', re.I)
_EDU_ANY = re.compile(r'学历(?:不作要求|不做要求|不限|无要求)|不限学历|无学历要求|'
                      r'\bno\s+(?:formal\s+)?(?:education|degree)\s+(?:is\s+)?required\b|'
                      r'\bdegree\s+(?:is\s+)?not\s+required\b', re.I)
_EXP_ANY = re.compile(r'(?:工作)?经验(?:不限|无要求|不作要求)|不限(?:工作)?经验|'
                      r'(?:无需|不要求)(?:相关|工作)?经验|无(?:相关|工作)?经验要求|'
                      r'\bno\s+(?:prior\s+|work\s+)?experience\s+(?:is\s+)?required\b', re.I)
_NEW_GRAD = re.compile(r'应届(?:毕业生)?|\b(?:new|recent)\s+graduates?\b|\bfresh\s+graduates?\b', re.I)
_PREFERRED = re.compile(r'优先|加分|更佳|\bprefer(?:red|ably)?\b|\bnice\s+to\s+have\b|\bdesirable\b', re.I)
_EQUIVALENT = re.compile(r'等(?:价|同)(?:的)?(?:工作|实践|专业)?经验|同等(?:工作|实践|专业)?经验|'
                         r'\bequivalent\s+(?:(?:work|practical|professional)\s+)?experience\b', re.I)
_REQUIRED = re.compile(r'必须|须具备|要求|至少|及以上|不低于|\brequir(?:ed|ement)\b|\bmust\b|\bminimum\b|\bat\s+least\b', re.I)
_YEARS = re.compile(r'(?<![\d.])(?P<low>\d{1,2}(?:\.\d+)?)\s*(?:[-–—~～至到]|\bto\b)\s*'
                    r'(?P<high>\d{1,2}(?:\.\d+)?)\s*(?:年|years?\b)|'
                    r'(?<![\d.])(?P<single>\d{1,2}(?:\.\d+)?)\s*(?P<plus>\+)?\s*(?:年|years?\b)(?P<suffix>以上|以内|以下)?', re.I)
# A contiguous ASCII token never needs splitting to reach the Chinese suffix.
# Possessive repetition avoids enumerating its equivalent token partitions.
_EXP_AFTER = re.compile(r'^\s*(?:以上|以内|以下)?\s*(?:的)?\s*(?:(?:相关|实际|专业|行业|开发|编程|软件|技术|全职|工作|[A-Za-z][A-Za-z0-9+#./-]*+)\s*){0,5}经验|'
                        r'^\s*(?:of\s+)?(?:(?:relevant|professional|practical|work|working|industry|technical|software|development|hands-on|related|full-time|prior)\s+){0,7}experience\b', re.I)
_EXP_BEFORE = re.compile(r'(?:工作|相关|专业|开发)?经验\s*(?:要求|至少|不少于|为|达|:|：)?\s*$|'
                         r'\bexperience\s*(?:of|:|required:|at\s+least|minimum)?\s*$', re.I)
_BUCKETS = {'1年以内': (0, 1), '1–3年': (1, 3), '3–5年': (3, 5), '5–10年': (5, 10), '10年以上': (10, None)}


def _raw_text(raw, field):
    value = raw.get(field)
    return value if isinstance(value, str) else ''


def _check_field(field):
    if field not in FIELDS:
        raise ValueError('只支持核验 education 和 experience')


def _result(field, value='未知', condition='未发现明确要求', evidence='', alternatives=None,
            status='unresolved', min_years=None, max_years=None):
    result = dict(value=value, condition=condition, evidence=evidence,
                  alternatives=alternatives or [], status=status)
    if field == 'experience':
        result.update(min_years=min_years, max_years=max_years)
    return result


def _condition(evidence, explicit=False):
    # Both facts remain visible in the exact source quote; the alternative is
    # more important than suggesting that the degree is an absolute barrier.
    if _EQUIVALENT.search(evidence):
        return '等价经验可替代'
    if _PREFERRED.search(evidence):
        return '优先考虑'
    if explicit or _REQUIRED.search(evidence):
        return '硬性要求'
    return '条件不明确'


def _hint_text(text):
    # re.I accepts dotted/dotless I, long S and Kelvin sign for ASCII letters.
    # casefold handles the latter two; these replacements handle the I variants.
    # This string is never used for match positions or source evidence.
    return text.casefold().replace('ı', 'i').replace('i\u0307', 'i')


def _condition_context(text, start, end):
    """Limit a modifier to its clause, plus an explicitly linked alternative."""
    boundaries = []
    for match in re.finditer(r'[。；;!！?？\n,，]|\.(?=\s|$)', text):
        # Ph.D. is a degree spelling, not an end-of-sentence modifier boundary.
        # The longest abbreviation is four characters; allow one final newline
        # because Python's $ also matches before it. Search the original text so
        # word boundaries still see the preceding character outside this window.
        if match[0] == '.' and _DEGREE_ABBREVIATION.search(text, max(0, match.start()-5), match.start()):
            continue
        boundaries.append((match.start(), match.end()))
    left = max((b for a, b in boundaries if b <= start), default=0)
    right = min((a for a, b in boundaries if a >= end), default=len(text))
    context = text[left:right]
    # "本科优先，可由同等工作经验替代" is one connected qualification.
    # An unrelated next clause about preferred programming skills is not.
    after = next((b for a, b in boundaries if a == right), right)
    next_end = min((a for a, b in boundaries if a >= after), default=len(text))
    following = text[after:next_end].strip()
    if (_EQUIVALENT.search(following) and
            re.match(r'^(?:或(?:者)?|也可|可(?:以)?由|可(?:以)?用|允许|alternatively\b|or\b)', following, re.I)):
        context += ' ' + following
    return context


def _quote(text, start, end):
    # Keep the original bytes/characters: the UI can verify this substring.
    left = max(text.rfind('\n', 0, start), text.rfind('。', 0, start), text.rfind(';', 0, start), text.rfind('；', 0, start)) + 1
    stops = [p for sep in ('\n', '。', ';', '；') if (p := text.find(sep, end)) >= 0]
    right = min(stops) + 1 if stops else len(text)
    if right - left > 2000:
        left, right = max(left, start - 300), min(right, end + 300)
    return text[left:right].strip()


def _education(text, explicit):
    if text.strip().lower() in _MISSING:
        return _result('education')
    status = 'source' if explicit else 'candidate'
    if explicit and text.strip() in ('不限', '无要求'):
        return _result('education', '不限', '条件不明确', text.strip(), status=status)
    hint_text = _hint_text(text)
    unrestricted = _EDU_ANY.search(text) if any(hint in hint_text for hint in ('学历', 'education', 'degree')) else None
    matches = []
    for value, hints, pattern in _EDUCATION_PATTERNS:
        if any(hint in hint_text for hint in hints):
            matches += [(m.start(), m.end(), value) for m in re.finditer(pattern, text, re.I)]
    for match in _GRADUATE.finditer(text) if 'graduate' in hint_text else ():
        # Graduate degree can mean a master's or doctorate, never silently pick one.
        matches.extend([(match.start(), match.end(), value) for value in ('硕士', '博士')])
    if unrestricted:
        evidence = _quote(text, unrestricted.start(), unrestricted.end())
        if not matches:
            return _result('education', '不限', '条件不明确', evidence, status=status)
        matches.append((unrestricted.start(), unrestricted.end(), '不限'))
    if not matches:
        return _result('education')
    matches.sort(key=lambda match: (match[0], match[1]))
    values = list(dict.fromkeys(v for _, _, v in matches))
    evidence = _quote(text, matches[0][0], matches[-1][1])
    if len(evidence) > 2000:
        # Distant, conflicting qualifications need a human to select evidence.
        return _result('education')
    conditions = {_condition(_condition_context(text, start, end), explicit)
                  for start, end, _ in matches}
    condition = conditions.pop() if len(conditions) == 1 else '条件不明确'
    if len(values) > 1:
        return _result('education', '多种学历选择', condition, evidence, values, status)
    return _result('education', values[0], condition, evidence, status=status)


def _number(value):
    value = float(value)
    return int(value) if value.is_integer() else value


def _experience(text, explicit):
    if text.strip().lower() in _MISSING:
        return _result('experience')
    status = 'source' if explicit else 'candidate'
    if explicit and text.strip() in ('不限', '无要求'):
        return _result('experience', '不限', '条件不明确', text.strip(), status=status)
    # Exact structured labels observed in Tencent's public API. Keep the quote
    # unchanged and leave the upper bound open; these are not description guesses.
    source_years = {'一年以上工作经验':1, '两年以上工作经验':2, '三年以上工作经验':3,
                    '五年以上工作经验':5, '八年以上工作经验':8, '十年以上工作经验':10}
    if explicit and text.strip() in source_years:
        low = source_years[text.strip()]
        return _result('experience', '10年以上' if low == 10 else '年限范围',
                       _condition(text, True), text.strip(), status=status, min_years=low)
    hint_text = _hint_text(text)
    any_match = _EXP_ANY.search(text) if '经验' in hint_text or ('no' in hint_text and 'experience' in hint_text) else None
    grad_match = _NEW_GRAD.search(text) if '应届' in hint_text or 'graduate' in hint_text else None
    found = []
    for match in _YEARS.finditer(text) if '年' in hint_text or 'year' in hint_text else ():
        before, after = text[max(0, match.start()-60):match.start()], text[match.end():match.end()+100]
        if not explicit and not (_EXP_AFTER.search(after) or _EXP_BEFORE.search(before)):
            continue
        if not explicit and re.search(r'(?:公司|团队|我们)(?:拥有|具有|已有|具备).{0,12}$|'
                                      r'\b(?:our\s+(?:company|team)|we)\s+(?:has|have|bring)(?:\s+over)?\s*$', before, re.I):
            continue
        if re.search(r'约|大约|\b(?:around|approximately)\s*$', before, re.I):
            continue
        low = _number(match['low'] or match['single'])
        high = _number(match['high']) if match['high'] else None
        if match['suffix'] in ('以内', '以下') or re.search(r'(?:不超过|至多|\bat\s+most|\bup\s+to)\s*$', before, re.I):
            low, high = 0, low
        if low > 60 or (high is not None and (high > 60 or high < low)):
            continue
        found.append((match, low, high))
    if not found:
        match, value = (any_match, '不限') if any_match else (grad_match, '应届')
        if match:
            evidence = _quote(text, match.start(), match.end())
            return _result('experience', value, _condition(evidence, explicit), evidence, status=status)
        return _result('experience')
    # Different sub-skills may require different lengths. Keep them for human
    # reading rather than guessing that the largest number is the job minimum.
    bounds = {(low, high) for _, low, high in found}
    if len(bounds) > 1 or any_match or grad_match:
        return _result('experience')
    match, low, high = found[0]
    evidence = _quote(text, match.start(), match.end())
    value = next((v for v, bound in _BUCKETS.items() if bound == (low, high)), '年限范围')
    return _result('experience', value, _condition(_condition_context(text, match.start(), match.end()), explicit), evidence, status=status,
                   min_years=low, max_years=high)


def candidates(raw):
    """Return source-backed values or unapproved description candidates."""
    if not isinstance(raw, dict):
        raise ValueError('岗位原始内容必须是对象')
    description = _raw_text(raw, 'description')
    result = {}
    for field, parser in (('education', _education), ('experience', _experience)):
        result[field] = parser(_raw_text(raw, field), True)
        if result[field]['status'] == 'unresolved':
            result[field] = parser(description, False)
    return result


def field_fingerprint(raw, field):
    """Invalidate a review only on relevant source text or rule-version changes."""
    _check_field(field)
    if not isinstance(raw, dict):
        raise ValueError('岗位原始内容必须是对象')
    content = [RULE_VERSION, field, raw.get(field), raw.get('description')]
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def validate_review(field, body, raw):
    """Validate a human conclusion without replacing the original source data."""
    _check_field(field)
    if not isinstance(body, dict) or not isinstance(raw, dict):
        raise ValueError('核验内容与岗位原文必须是对象')
    values = EDUCATION_VALUES if field == 'education' else EXPERIENCE_VALUES
    value, condition = body.get('value'), body.get('condition')
    if not isinstance(value, str) or value not in values:
        raise ValueError('请选择支持的规范结论')
    if not isinstance(condition, str) or condition not in CONDITIONS:
        raise ValueError('请选择支持的要求条件')
    evidence = body.get('evidence', '')
    if not isinstance(evidence, str) or len(evidence) > 2000:
        raise ValueError('原文证据必须是 2000 字符以内的文本')
    evidence = evidence.strip()
    if evidence and not any(evidence in _raw_text(raw, key) for key in (field, 'description')):
        raise ValueError('证据必须是当前原始字段或岗位描述中的连续原文')
    alternatives = body.get('alternatives', [])
    if not isinstance(alternatives, list) or len(alternatives) > 7 or any(not isinstance(v, str) for v in alternatives):
        raise ValueError('学历候选必须是规范值列表')
    if len(set(alternatives)) != len(alternatives):
        raise ValueError('学历候选不能重复')
    if value == '多种学历选择':
        if len(alternatives) < 2 or any(v not in EDUCATION_VALUES or v in ('未知', '多种学历选择') for v in alternatives):
            raise ValueError('多种学历选择至少需要两个明确且不同的学历候选')
    elif alternatives:
        raise ValueError('只有多种学历选择可以填写学历候选')
    if value == '未知':
        if condition != '未发现明确要求':
            raise ValueError('未知结论的条件必须为未发现明确要求')
    elif not evidence or evidence.lower() in _MISSING or condition == '未发现明确要求':
        raise ValueError('明确结论必须提供原文证据和相应条件')
    if value == '不限':
        pattern = _EDU_ANY if field == 'education' else _EXP_ANY
        explicit = _raw_text(raw, field).strip()
        if not pattern.search(evidence) and not (evidence == explicit and explicit in ('不限', '无要求')):
            raise ValueError('不限必须由原文明确说明，缺失字段不能视为不限')
    low = high = None
    if field == 'experience':
        low, high = body.get('min_years'), body.get('max_years')
        for bound in (low, high):
            if bound is not None and (isinstance(bound, bool) or not isinstance(bound, (int, float)) or not 0 <= bound <= 60 or not math.isfinite(bound)):
                raise ValueError('经验年限必须是 0–60 范围内的数字或空值')
        if low is not None and high is not None and low > high:
            raise ValueError('经验年限下界不得大于上界')
        if value in _BUCKETS:
            expected_low, expected_high = _BUCKETS[value]
            if (low is not None and low != expected_low) or (high is not None and high != expected_high):
                raise ValueError('经验年限与所选规范类别不一致')
            low, high = expected_low, expected_high
        elif value == '年限范围':
            if low is None and high is None:
                raise ValueError('年限范围至少需要一个明确的上下界')
        elif low is not None or high is not None:
            raise ValueError('未知、不限或应届不能附加年限范围')
    return _result(field, value, condition, evidence, alternatives.copy(), 'reviewed', low, high)
