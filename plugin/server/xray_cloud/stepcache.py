from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Hashable, List, Optional

DEFAULT_TTL_S = 5 * 60.0


@dataclass
class CachedTest:
    """A test's step definition as fetched from Xray. `steps` are raw GraphQL step dicts
    (id, action, data, result, attachments [{id, filename, downloadLink}], callTestIssueId)."""
    key: str
    issue_id: str
    steps: List[dict] = field(default_factory=list)
    test_type: Optional[str] = None   # testType.name, e.g. "Manual"
    kind: Optional[str] = None        # testType.kind, e.g. "Steps", "Gherkin", "Unstructured"
    gherkin: Optional[str] = None
    folder_path: Optional[str] = None
    fetched_at: float = 0.0           # wall-clock seconds (time.time())


class TTLCache:
    """Generic in-memory cache with a TTL (monotonic clock). Mutating tools invalidate the
    entry before their first mutation, so a failure can never leave a stale entry behind."""

    def __init__(self, ttl_s: float = DEFAULT_TTL_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_s
        self._clock = clock
        self._entries: Dict[str, tuple] = {}

    def get(self, test_key: Hashable) -> Optional[Any]:
        hit = self._entries.get(test_key)
        if hit is None:
            return None
        stored_at, entry = hit
        if self._clock() - stored_at > self._ttl:
            del self._entries[test_key]
            return None
        return entry

    def set(self, test_key: Hashable, entry: Any) -> None:
        self._entries[test_key] = (self._clock(), entry)

    def invalidate(self, test_key: Hashable) -> None:
        self._entries.pop(test_key, None)

    def clear(self) -> None:
        self._entries.clear()

    def __contains__(self, test_key: Hashable) -> bool:
        return self.get(test_key) is not None


class StepCache(TTLCache):
    """Per-test cache of CachedTest entries keyed by issue key."""
