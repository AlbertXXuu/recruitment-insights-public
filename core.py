"""Compatibility imports for existing course scripts; implementation is in recruitment_insights."""
from recruitment_insights.config import ROOT
from recruitment_insights.bootstrap import Store
from recruitment_insights.domain.jobs import (SKILLS, CATEGORIES, PlainText, now, edit_token,
    plain_text, extract_skills, category, salary, normalize, analyze)
from recruitment_insights.infrastructure.sources import SOURCE_URL, greenhouse_records
