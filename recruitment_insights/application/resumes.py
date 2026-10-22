"""Private candidate resume use cases; no database or document-parser imports."""
from datetime import datetime, timezone

from .identity import live_actor
from .ports import UnitOfWork
from recruitment_insights.domain.resumes import (
    ALLOWED_EXTENSIONS, MAX_RESUME_BYTES, prepare_resume,
)


class ResumeService:
    def __init__(self, uow: UnitOfWork, validate_document):
        self.uow = uow
        self.validate_document = validate_document

    @staticmethod
    def _candidate(tx, actor):
        current = live_actor(tx, actor)
        if current['role'] != 'candidate':
            raise PermissionError('简历属于求职者个人资料')
        return current

    @staticmethod
    def _response(metadata):
        return dict(resume=metadata, max_bytes=MAX_RESUME_BYTES,
                    allowed_extensions=list(ALLOWED_EXTENSIONS))

    def info(self, actor):
        with self.uow() as tx:
            current = self._candidate(tx, actor)
            return self._response(tx.resumes.metadata(current['id']))

    def upload(self, actor, body):
        # Check authority before document work, then recheck in the commit.
        with self.uow() as tx:
            self._candidate(tx, actor)
        prepared = prepare_resume(body.get('filename'), body.get('content_base64'))
        self.validate_document(prepared['filename'], prepared['content'])
        prepared['uploaded_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
        with self.uow(write=True) as tx:
            current = self._candidate(tx, actor)
            tx.resumes.save(current['id'], prepared)
            return self._response(tx.resumes.metadata(current['id']))

    def download(self, actor):
        with self.uow() as tx:
            current = self._candidate(tx, actor)
            return tx.resumes.get(current['id'])
