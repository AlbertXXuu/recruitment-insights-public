"""Composition root: the only place choosing concrete adapters for services."""
from recruitment_insights.application.accounts import Accounts
from recruitment_insights.application.imports import ImportService
from recruitment_insights.application.jobs import JobService
from recruitment_insights.application.queries import Queries
from recruitment_insights.application.reviews import ReviewService
from recruitment_insights.application.resumes import ResumeService
from recruitment_insights.infrastructure.database import Database, Transaction
from recruitment_insights.infrastructure.sources import BundledPublicSource, GreenhouseSource, parse_csv
from recruitment_insights.infrastructure.resume_documents import validate_document
from recruitment_insights.config import ROOT


class Store:
    """Compatibility facade: no business rules or SQL.

    connect/import_records/import_csv are trusted local maintenance APIs;
    HTTP calls authenticated use cases instead.
    """
    def __init__(self, path):
        self.database = Database(path)
        self.database.initialize()
        self.path = self.database.path
        self.uow = self.database.uow
        self.queries = Queries(self.uow)
        self.accounts = Accounts(self)
        self.importer = ImportService(self.uow, GreenhouseSource(), parse_csv,
                                      BundledPublicSource(ROOT/'data/public_jobs.json').read)
        self.job_service = JobService(self.uow, self.importer)
        self.review_service = ReviewService(self.uow, self.queries.detail)
        self.resumes = ResumeService(self.uow, validate_document)

    def connect(self):
        return self.database.connect()

    def import_records(self, records, source, label, is_demo=False, authoritative=False, owner_org_id=None, connection=None):
        if connection is not None:
            return self.importer.apply(Transaction(connection), records, source, label, is_demo, authoritative, owner_org_id)
        return self.importer.import_records(records, source, label, is_demo, authoritative, owner_org_id)

    def import_csv(self, content, source_name, is_demo=False, owner_org_id=None, company_name=None):
        return self.importer.import_csv(content, source_name, is_demo, owner_org_id, company_name)

    def import_for(self, actor, body):
        return self.importer.import_for(actor, body)

    def sync(self, payload=None):
        return self.importer.sync(payload)

    def sync_for(self, actor):
        return self.importer.sync(actor=actor)

    def import_demo_for(self, actor, body):
        return self.importer.import_for(actor, body, require_admin=True)

    def load_public_sample_for(self, actor):
        return self.importer.load_public_sample(actor)

    def jobs(self, filters=None, actor=None):
        return self.queries.jobs(filters, actor)

    def detail(self, job_id, actor=None):
        return self.queries.detail(job_id, actor)

    def runs(self, actor=None):
        return self.queries.runs(actor)

    def company_jobs(self, actor):
        return self.queries.company_jobs(actor)

    def admin_jobs(self, filters, actor):
        return self.queries.admin_jobs(filters, actor)

    def quality(self, filters, actor):
        return self.queries.quality(filters, actor)

    def review_field(self, actor, body):
        return self.review_service.review_field(actor, body)

    def decide_duplicate(self, actor, body):
        return self.review_service.decide_duplicate(actor, body)

    def manage_job(self, actor, body):
        return self.job_service.manage_job(actor, body)

    def set_lifecycle(self, actor, body):
        return self.job_service.set_lifecycle(actor, body)
