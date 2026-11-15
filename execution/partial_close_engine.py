"""Category-4 placeholder.

The original partial-close algorithm used absolute price movement as positive
R and could close SHORT trades incorrectly. It is not safe to retain it as
unreachable executable code while building the foundation.
"""
from __future__ import annotations


class PartialCloseEngine:
    """Compatibility interface; never mutates broker state in Category 1."""

    def __init__(self, executor):
        self.executor = executor

    def check_and_execute(self):
        raise RuntimeError("Partial-close and break-even require Category 4 reconciliation")
