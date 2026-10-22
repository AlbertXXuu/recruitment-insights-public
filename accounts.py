"""Compatibility imports for local scripts and existing acceptance tests."""
from recruitment_insights.application.accounts import Accounts
from recruitment_insights.domain.access import can_manage

__all__ = ['Accounts', 'can_manage']
