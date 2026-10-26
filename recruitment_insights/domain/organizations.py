"""Stable identity comparison for manually reviewed organization names."""
import unicodedata


def organization_key(name):
    return ''.join(unicodedata.normalize('NFKC', name).split()).casefold()
