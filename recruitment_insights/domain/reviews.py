"""Evidence projection and duplicate comparison, independent of persistence."""
import json
from functools import lru_cache
from threading import Lock

from . import requirements
from .quality import duplicate_key
from .requirements import candidates, field_fingerprint

_extraction_lock = Lock()

def source_input(row, payload=None):
    raw = json.loads(row['raw'])
    raw['description'] = (json.loads(row['payload']) if payload is None else payload)['description']
    return raw



@lru_cache(maxsize=8192)
def cached_candidates(encoded, rule_version, description=None):
    # Bound repeated bulk scans and updates without depending on demo size.
    # within a fixed bound. Cache only pure extraction: actor/review/source state
    # is projected afresh on every read, and changed input gets a different key.
    # Fingerprints depend only on these same inputs and the rule version too.
    raw = json.loads(encoded)
    if description is not None:
        raw['description'] = description
    return candidates(raw), {field: field_fingerprint(raw, field) for field in ('education', 'experience')}



def _proposal(raw):
    # Keep a textual description as its own immutable cache key. Warm reads
    # need not JSON-encode the whole document just to look up an existing item.
    description = raw.get('description')
    separate_description = isinstance(description, str)
    fields = ('education', 'experience') if separate_description else ('education', 'experience', 'description')
    encoded = json.dumps({key: raw.get(key) for key in fields}, ensure_ascii=False, sort_keys=True)
    # lru_cache can calculate the same cold key simultaneously. Concurrent
    # readers share pure extraction work under this small lock.
    with _extraction_lock:
        return cached_candidates(encoded, requirements.RULE_VERSION,
                                 description if separate_description else None)



def propose(raw):
    return _proposal(raw)[0]



def effective_fields(row, saved, payload=None):
    raw = source_input(row, payload)
    proposed, fingerprints = _proposal(raw)
    result = {}
    for field in ('education', 'experience'):
        item = dict(proposed[field])
        source_hash = fingerprints[field]
        review = saved.get((row['id'], field))
        item.update(source_hash=source_hash, version=review['version'] if review else 0, reviewed_at=None)
        if review and review['state'] == 'confirmed':
            item.update(json.loads(review['conclusion']))
            item.update(status='confirmed' if review['source_hash'] == source_hash else 'stale',
                        source_hash=source_hash, version=review['version'], reviewed_at=review['reviewed_at'])
        result[field] = item
    return result



def apply_fields(job, fields):
    for field, item in fields.items():
        if item['status'] == 'stale':
            value = '待复核'
        elif item['status'] in ('source', 'confirmed'):
            value = '未发现明确要求' if item['value'] == '未知' else item['value']
        else:
            value = '待核验'
        job[field] = value
        job[field+'_status'] = item['status']
        job[field+'_condition'] = item['condition']
        if field == 'experience':
            for bound in ('min_years','max_years'):
                job['experience_'+bound] = item.get(bound) if item['status'] in ('source','confirmed') else None
        else:
            job['education_alternatives'] = ' / '.join(item.get('alternatives',[])) if item['status'] in ('source','confirmed') else ''
    return job



def integer(value, label):
    if type(value) is not int or not 0 <= value < 2**63-1:
        raise ValueError(label+'必须为受支持范围内的非负整数')
    return value



def duplicate_status(previous, left, right):
    if previous and (previous['left_hash'] != left['fingerprint'] or previous['right_hash'] != right['fingerprint']):
        return 'stale'
    return previous['decision'] if previous else 'pending'
