"""kship: validate, gate and ship Kaggle submissions straight from your pipeline."""

__version__ = "0.1.1"


class KshipError(Exception):
    """An expected failure with a message meant for the user (no traceback)."""
