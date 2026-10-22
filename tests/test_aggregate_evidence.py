"""Aggregate projection retains original field types, evidence and privacy."""
import json
import tempfile
import unittest
from pathlib import Path

from core import Store
from recruitment_insights.domain.jobs import analyze
from recruitment_insights.domain.reviews import effective_fields


class AggregateEvidenceTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='aggregate-evidence-')
        self.addCleanup(temp.cleanup)
        self.store=Store(Path(temp.name)/'test.sqlite3')
        self.store.accounts.create_user('projection_admin','Aggregate-Evidence-2026!','投影管理员','admin')
        with self.store.uow() as tx:
            self.actor=tx.accounts.by_username('projection_admin')

    def insert(self, identifier, **values):
        raw=dict(job_id=identifier,title='Python 工程师',company='投影企业',location='上海',
                 description='本科及以上学历，至少3年Python开发经验。',**values)
        self.store.import_records([raw],'test:projection','aggregate projection')
        with self.store.uow() as tx:
            return tx.jobs.by_source('test:projection',identifier)['id']

    def test_full_and_reduced_projections_keep_original_json_types_and_fingerprints(self):
        for index,value in enumerate([None,False,True,0,1,1.5,'', '本科', ['本科','硕士'],{'preferred':'本科'}]):
            self.insert(str(index),education=value,experience=value,_source_record={'large':'无关原始响应'*10000})
        with self.store.uow() as tx:
            full=list(tx.jobs.iter_active())
            reduced=list(tx.jobs.iter_active(requirement_evidence_only=True))
            saved=tx.reviews.fields([r['id'] for r in full])
            for original,projected in zip(full,reduced):
                self.assertEqual(original['id'],projected['id'])
                self.assertEqual(effective_fields(original,saved),effective_fields(projected,saved))
                self.assertNotIn('_source_record',json.loads(projected['raw']))
                self.assertIn('_source_record',json.loads(original['raw']))
        self.assertEqual(self.store.queries.stats({},self.actor),analyze(self.store.jobs({},self.actor)))

    def test_confirmed_and_stale_reviews_match_full_evidence_and_keep_detail_raw(self):
        identifier=self.insert('review',education='',experience='',_source_record={'description':'保留原始响应'})
        original=self.store.detail(identifier,self.actor)
        field=original['requirements']['education']
        self.store.review_field(self.actor,dict(job_id=identifier,field='education',action='confirm',
            source_hash=field['source_hash'],version=field['version'],value='本科',condition='硬性要求',
            evidence='本科及以上学历',alternatives=[]))
        self.assertEqual(self.store.queries.stats({},self.actor),analyze(self.store.jobs({},self.actor)))
        self.assertEqual(self.store.queries.overview({'education':'本科'},self.actor)['stats']['total'],1)
        self.assertEqual(self.store.detail(identifier,self.actor)['raw'],original['raw'])
        with self.store.connect() as db:
            payload=json.loads(db.execute('SELECT payload FROM jobs WHERE id=?',(identifier,)).fetchone()[0])
            payload['description']='硕士学历，5年开发经验。'
            db.execute('UPDATE jobs SET payload=? WHERE id=?',(json.dumps(payload),identifier))
        self.assertEqual(self.store.queries.stats({},self.actor),analyze(self.store.jobs({},self.actor)))
        self.assertEqual(self.store.queries.overview({'education':'待复核'},self.actor)['stats']['total'],1)

    def test_legacy_nonfinite_json_metadata_and_missing_fields_keep_full_projection(self):
        self.insert('missing',_source_record={'retained':'missing original fields'})
        for index,value in enumerate([float('nan'),float('inf'),float('-inf')]):
            self.insert('legacy'+str(index),education='本科',experience='不限',_source_record={'salary':value})
        with self.store.uow() as tx:
            full=list(tx.jobs.iter_active())
            reduced=list(tx.jobs.iter_active(requirement_evidence_only=True))
            for original,projected in zip(full,reduced):
                self.assertEqual(effective_fields(original,{}),effective_fields(projected,{}))
        self.assertEqual(self.store.queries.stats({},self.actor),analyze(self.store.jobs({},self.actor)))


if __name__=='__main__': unittest.main()
