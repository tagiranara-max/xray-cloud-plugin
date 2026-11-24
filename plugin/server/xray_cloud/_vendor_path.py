"""Put the vendored third-party packages (``_vendor/``) FIRST on sys.path, so the copies the
server was tested against (PyYAML, the conformance-tested Gherkin parser) always win over
anything installed in site-packages."""
from __future__ import annotations

import importlib
import os
import sys
from types import ModuleType

VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_vendor")


def ensure_vendor_first() -> None:
    """Move (or add) the vendor directory to the front of sys.path."""
    while VENDOR_DIR in sys.path:
        sys.path.remove(VENDOR_DIR)
    sys.path.insert(0, VENDOR_DIR)


def is_vendored(module: ModuleType) -> bool:
    path = getattr(module, "__file__", None) or ""
    return os.path.abspath(path).startswith(VENDOR_DIR + os.sep)


def import_vendored(name: str) -> ModuleType:
    """Import top-level package `name` from _vendor/; raise ImportError if another copy
    (e.g. from site-packages, imported earlier) shadows it."""
    ensure_vendor_first()
    module = importlib.import_module(name)
    if not is_vendored(module):
        raise ImportError(
            "Module %r is shadowed: loaded from %s, expected the vendored copy under %s"
            % (name, getattr(module, "__file__", None), VENDOR_DIR))
    return module
