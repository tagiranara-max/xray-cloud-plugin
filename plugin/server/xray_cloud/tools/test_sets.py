from __future__ import annotations

from .containers import Kind, register

SET = Kind("TestSet", "test set")
register(SET, with_sets=False)
