"""Import use cases. Parsing is injected; persistence participates in one UOW."""
import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from recruitment_insights.domain.access import can_manage
from recruitment_insights.domain.jobs import normalize, now
from .accounts import Accounts
from .identity import live_actor
from .ports import UnitOfWork


@dataclass(frozen=True)
class PreparedBatch:
    # No caller-owned raw dictionaries or database state cross this boundary.
    # Rows contain external ID, normalized JSON, fingerprint, and original JSON.
    rows: tuple[tuple[str, str, str, str], ...]
    result_json: str


def prepare_batch(records):
    if len(records) > 5000:
        raise ValueError('单次最多 5000 行')
    result = dict(total=len(records), inserted=0, updated=0, unchanged=0, rejected=0,
                  duplicated=0, conflicts=0, deactivated=0, errors=[])
    grouped = {}
    for row_number, raw in enumerate(records, 2):
        try:
            job = normalize(raw)
        except ValueError as exc:
            result['rejected'] += 1
            result['errors'].append(dict(row=row_number, reason=str(exc), raw=raw))
            continue
        encoded = json.dumps(job, ensure_ascii=False, sort_keys=True)
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        grouped.setdefault(job['job_id'], []).append((row_number,raw,job,encoded,fingerprint))
    prepared = []
    for group in grouped.values():
        if len({json.dumps(item[1], ensure_ascii=False, sort_keys=True) for item in group}) > 1:
            result['rejected'] += len(group)
            result['conflicts'] += len(group)
            lines = ', '.join(str(item[0]) for item in group)
            for row_number,raw,*_ in group:
                result['errors'].append(dict(row=row_number, reason='同批次同岗位 ID 内容冲突（行 '+lines+'），该 ID 未入库',raw=raw))
            continue
        result['duplicated'] += len(group)-1
        _, raw, job, encoded, fingerprint = group[0]
        prepared.append((job['job_id'], encoded, fingerprint, json.dumps(raw, ensure_ascii=False)))
    return PreparedBatch(tuple(prepared), json.dumps(result, ensure_ascii=False))


class ImportService:
    def __init__(self, uow: UnitOfWork, source, csv_parser, sample_reader=None):
        self.uow, self.source, self.csv_parser = uow, source, csv_parser
        self.sample_reader = sample_reader
        self._sample_prepare_lock = threading.Lock()
        self._prepared_sample = None
        self.sync_lock = threading.Lock()
        self.last_sync_attempt = 0.0

    def apply(self, tx, records, source, label, is_demo=False, authoritative=False, owner_org_id=None, observed_at=None):
        """Compose single-job edits or trusted callers' existing transactions."""
        return self.apply_prepared(tx, prepare_batch(records), source, label,
                                   is_demo, authoritative, owner_org_id, observed_at)

    def apply_prepared(self, tx, batch, source, label, is_demo=False, authoritative=False, owner_org_id=None, observed_at=None):
        """Persist prepared input, reading current state in the caller's UOW."""
        # Each application gets fresh counters/errors; a retry cannot mutate its
        # input or reuse counts/run IDs from a previous transaction.
        result = json.loads(batch.result_json)
        stamp = now()
        run_id = tx.runs.create(stamp, source, label, 'running', '{}', owner_org_id)
        seen = set()
        for external_id, encoded, fingerprint, original in batch.rows:
            seen.add(external_id)
            previous = tx.jobs.by_source(source, external_id)
            if previous and previous['owner_org_id'] != owner_org_id:
                raise ValueError('岗位来源归属不匹配')
            if previous and observed_at:
                # Reloading an offline bundle cannot overwrite a newer source
                # observation already collected by the application.
                try:
                    newer = datetime.fromisoformat(previous['last_seen'].replace('Z','+00:00')) > datetime.fromisoformat(observed_at.replace('Z','+00:00'))
                except (ValueError, TypeError):
                    newer = False
                if newer:
                    result['unchanged'] += 1
                    continue
            values = dict(payload=encoded, fingerprint=fingerprint, raw=original,
                          last_seen=observed_at or stamp, run_id=run_id, is_demo=int(is_demo))
            if previous:
                job_id = previous['id']
                changed = fingerprint != previous['fingerprint'] or not previous['source_active']
                tx.jobs.update(job_id, values)
                result['updated' if changed else 'unchanged'] += 1
            else:
                job_id = tx.jobs.insert(dict(values, source=source, external_id=external_id,
                                            first_seen=stamp, owner_org_id=owner_org_id,
                                            published=int(owner_org_id is None)))
                changed = True
                result['inserted'] += 1
            if changed:
                tx.jobs.add_revision(job_id, run_id, stamp, encoded, original)
        if authoritative and seen and not result['rejected']:
            for old in tx.jobs.active_source(source):
                if old['external_id'] not in seen:
                    tx.jobs.deactivate(old['id'], run_id, stamp)
                    result['deactivated'] += 1
        result['run_id'] = run_id
        tx.runs.finish(run_id, 'partial' if result['rejected'] else 'success', json.dumps(result,ensure_ascii=False))
        return result

    def import_records(self, records, source, label, is_demo=False, authoritative=False, owner_org_id=None):
        """Trusted local maintenance entry; HTTP calls import_for instead."""
        batch = prepare_batch(records)
        with self.uow(write=True) as tx:
            return self.apply_prepared(tx, batch, source, label, is_demo, authoritative, owner_org_id)

    @staticmethod
    def source_name(name, is_demo, org_id):
        name = name.strip()
        if not re.fullmatch(r'[\w\-\u4e00-\u9fff]{1,60}', name):
            raise ValueError('数据源名称只支持 1–60 位文字、数字、下划线或横线')
        source = ('demo:' if is_demo else 'csv:') + (f'org-{org_id}:' if org_id else '') + name
        return source, name+'.csv'

    @staticmethod
    def owned_records(rows, company_name):
        if company_name:
            for row in rows:
                row['_imported_company'] = row.get('company')
                row['company'] = company_name
        return rows

    def import_csv(self, content, source_name, is_demo=False, owner_org_id=None, company_name=None):
        """Trusted local maintenance entry, retained for existing course scripts."""
        source, label = self.source_name(source_name, is_demo, owner_org_id)
        rows = self.owned_records(self.csv_parser(content), company_name)
        return self.import_records(rows, source, label, is_demo, owner_org_id=owner_org_id)

    def import_for(self, actor, body, *, require_admin=False):
        # Reject unauthorized calls before parsing their input; check again under
        # the write lock so revocation cannot survive parsing and preparation.
        with self.uow() as tx:
            owner = self.authorized_actor(tx, actor, require_admin)
        if not isinstance(body.get('csv'),str) or not isinstance(body.get('source'),str):
            raise ValueError('需要 csv 文本和 source 名称')
        is_demo = body.get('is_demo', False)
        if not isinstance(is_demo, bool):
            raise ValueError('模拟状态必须为布尔值')
        rows = self.csv_parser(body['csv'])
        source, label = self.source_name(body['source'], is_demo, owner['org_id'])
        batch = prepare_batch(self.owned_records(rows, owner['org_name']))
        with self.uow(write=True) as tx:
            current = self.authorized_actor(tx, actor, require_admin)
            if (current['org_id'], current['org_name']) != (owner['org_id'], owner['org_name']):
                raise PermissionError('企业归属或名称已变化，请刷新后重新导入')
            result = self.apply_prepared(tx, batch, source, label, is_demo, owner_org_id=current['org_id'])
            Accounts.audit(tx, current, 'import_csv', result['run_id'])
            return result

    def load_public_sample(self, actor):
        """Load all bundled sources atomically; reloading preserves manual stops."""
        with self.uow() as tx:
            self.authorized_actor(tx, actor, True)
        if self.sample_reader is None:
            raise ValueError('此部署未提供真实岗位样本')
        # Read and validate on every call. Only the most recent immutable input
        # preparation is reusable; database state and results never enter this
        # cache. Serialize in original order because raw JSON affects edit tokens.
        with self._sample_prepare_lock:
            encoded = json.dumps(self.sample_reader(), ensure_ascii=False)
            key = hashlib.sha256(encoded.encode()).hexdigest()
            cached = self._prepared_sample
            if cached is not None and cached[0] == key and cached[1] is normalize:
                prepared_sources = cached[2]
            else:
                # Only preparation needs a detached copy. On a cache hit the
                # complete validated input already matches frozen batches.
                datasets = json.loads(encoded)
                # Publish only after every source passes preparation. A broken
                # bundle must never fall back to previously cached input.
                prepared_sources = []
                for dataset in datasets:
                    batch = prepare_batch(dataset['records'])
                    checked = json.loads(batch.result_json)
                    if checked['rejected'] or checked['duplicated']:
                        raise ValueError('真实岗位样本含无效或重复记录，未导入任何来源')
                    prepared_sources.append((dataset['source'], dataset['label'], dataset['fetched_at'], batch))
                prepared_sources = tuple(prepared_sources)
                self._prepared_sample = (key, normalize, prepared_sources)
                del dataset, datasets
            # Persistence sees only frozen batches, never caller-owned inputs.
            del encoded, cached
        aggregate = dict(total=0, inserted=0, updated=0, unchanged=0, rejected=0,
                         duplicated=0, conflicts=0, deactivated=0, errors=[], runs=[], sources=[])
        with self.uow(write=True) as tx:
            current = self.authorized_actor(tx, actor, True)
            for source, label, observed_at, batch in prepared_sources:
                result = self.apply_prepared(tx, batch, source, label, observed_at=observed_at)
                aggregate['runs'].append(result)
                for key in ('total','inserted','updated','unchanged','rejected','duplicated','conflicts','deactivated'):
                    aggregate[key] += result[key]
                aggregate['sources'].append(dict(source=source, label=label,
                    count=result['total'], fetched_at=observed_at))
            aggregate.update(run_id=aggregate['runs'][-1]['run_id'], sample_total=aggregate['total'],
                             source_count=len(prepared_sources), captured_at=max(d[2] for d in prepared_sources))
            Accounts.audit(tx, current, 'load_public_sample', aggregate['run_id'])
        return aggregate

    @staticmethod
    def authorized_actor(tx, actor, require_admin):
        current = live_actor(tx, actor)
        if require_admin and current['role'] != 'admin':
            raise PermissionError('仅数据管理员可操作公共样本数据')
        if not can_manage(current):
            raise PermissionError('没有数据导入权限')
        return current

    def sync(self, payload=None, *, actor=None):
        if actor is not None:
            with self.uow() as tx:
                if live_actor(tx, actor)['role'] != 'admin':
                    raise PermissionError('仅数据管理员可同步公开接口')
        if not self.sync_lock.acquire(blocking=False):
            raise ValueError('已有同步任务在运行，请稍后查看记录')
        try:
            if payload is None:
                if time.monotonic() - self.last_sync_attempt < 60:
                    raise ValueError('公开接口同步间隔至少 60 秒，请稍后重试')
                self.last_sync_attempt = time.monotonic()
                payload = self.source.fetch()
            records = self.source.records(payload)
            batch = prepare_batch(records)
            with self.uow(write=True) as tx:
                if actor is not None and live_actor(tx, actor)['role'] != 'admin':
                    raise PermissionError('公开接口同步权限已变化，未导入数据')
                return self.apply_prepared(tx, batch, self.source.source, self.source.label, authoritative=True)
        except Exception as exc:
            # Failure evidence intentionally survives the rollback of a failed import.
            with self.uow(write=True) as tx:
                tx.runs.create(now(), self.source.source, '公开接口同步', 'failed', json.dumps({'error':str(exc)},ensure_ascii=False))
            raise
        finally:
            self.sync_lock.release()
