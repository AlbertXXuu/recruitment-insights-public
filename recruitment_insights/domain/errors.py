"""Errors shared by domain use cases and HTTP presentation."""


class ConflictError(ValueError):
    """The source or a saved decision changed after the editor was opened."""
