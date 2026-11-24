from __future__ import annotations

from .containers import Kind, register

PLAN = Kind("TestPlan", "test plan")
register(PLAN, with_sets=True)
