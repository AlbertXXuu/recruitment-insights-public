"""Compatibility alias for the canonical domain module."""
import sys
from recruitment_insights.domain import requirements as _implementation

sys.modules[__name__] = _implementation
