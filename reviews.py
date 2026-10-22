"""Compatibility exports for pure review rules and conflict errors.

Transactional review use cases live in recruitment_insights.application.reviews.
"""
from recruitment_insights.domain.access import may_review, may_read_duplicate_reason
from recruitment_insights.domain.errors import ConflictError
from recruitment_insights.domain.reviews import (
    source_input, cached_candidates, propose, effective_fields, apply_fields,
    integer, duplicate_key, duplicate_status,
)
