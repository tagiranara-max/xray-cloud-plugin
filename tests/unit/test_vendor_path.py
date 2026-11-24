"""Vendored packages always win over site-packages (ruling R13)."""
from __future__ import annotations

import os
import sys
import types

import pytest

from xray_cloud import _vendor_path
from xray_cloud._vendor_path import VENDOR_DIR, ensure_vendor_first, import_vendored


def under_vendor(module) -> bool:
    return os.path.abspath(module.__file__).startswith(VENDOR_DIR + os.sep)


def test_yaml_and_gherkin_resolve_under_vendor():
    import xray_cloud.config  # noqa: F401
    import xray_cloud.gherkin.parse  # noqa: F401
    import xray_cloud.gherkin.serialize  # noqa: F401
    import gherkin
    import gherkin.parser
    import yaml
    assert under_vendor(yaml), yaml.__file__
    assert under_vendor(gherkin), gherkin.__file__
    assert under_vendor(gherkin.parser), gherkin.parser.__file__


def test_ensure_vendor_first_moves_vendor_to_front(monkeypatch):
    monkeypatch.setattr(sys, "path", ["/somewhere", VENDOR_DIR, "/else", VENDOR_DIR])
    ensure_vendor_first()
    assert sys.path == [VENDOR_DIR, "/somewhere", "/else"]


def test_shadowing_module_already_imported_is_reported(monkeypatch):
    fake = types.ModuleType("yaml")
    fake.__file__ = "/site-packages/yaml/__init__.py"
    monkeypatch.setitem(sys.modules, "yaml", fake)
    with pytest.raises(ImportError) as e:
        import_vendored("yaml")
    assert "'yaml'" in str(e.value) and "/site-packages/yaml/__init__.py" in str(e.value)


def test_is_vendored_rejects_module_without_file():
    assert not _vendor_path.is_vendored(types.ModuleType("x"))
