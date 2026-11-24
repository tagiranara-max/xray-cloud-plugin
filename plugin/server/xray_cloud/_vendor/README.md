# _vendor

Vendored third-party pure-Python packages. The vendored copies always win:
`xray_cloud/_vendor_path.py` (`import_vendored`, used by `config.py` for `yaml` and by
`xray_cloud/gherkin/` for `gherkin`) puts this directory at the *front* of `sys.path` and
raises `ImportError` naming the module if a different copy (e.g. from site-packages) was
already imported. The server therefore always runs the exact versions it was tested
against (for Gherkin: the conformance corpus in `tests/fixtures/gherkin/`).

| Package | Version | License | Source |
|---|---|---|---|
| `yaml` (PyYAML) | 6.0.3 | MIT (`yaml/LICENSE`) | https://github.com/yaml/pyyaml |
| `gherkin` (gherkin-official) | 39.0.0 | MIT (`gherkin/LICENSE`) | PyPI wheel `gherkin_official-39.0.0-py3-none-any.whl`; identical to https://github.com/cucumber/gherkin tag `v39.0.0`, `python/src/gherkin` |

Only the pure-Python modules are vendored; there is no compiled `_yaml` extension, so
`CSafeLoader`/`CSafeDumper` are unavailable (the server only uses `yaml.safe_load`).

To refresh: copy the `.py` files from an installed `pyyaml` package's `yaml/` directory
(skip `__pycache__` and compiled `.so`/`.pyd` files) into `yaml/`, copy the license from its
`dist-info`, and update the version above.

## gherkin (gherkin-official 39.0.0)

39.0.0 is the newest release whose `Requires-Python` allows 3.9 (39.1.0+ need >= 3.10).
`xray_cloud/gherkin/parse.py` and `serialize.py` import it via `import_vendored("gherkin")`. The conformance corpus in
`tests/fixtures/gherkin/{good,bad}/` comes from the same tag's `testdata/`.

Local patch (the only change): upstream declares a runtime dependency on `typing-extensions`,
but uses it only in type annotations. To stay stdlib-only, these imports were moved under
`if TYPE_CHECKING:` (marked `# xray-cloud patch`):

- `gherkin/dialect.py`: `from typing_extensions import Self`
- `gherkin/parser_types.py`: `from typing_extensions import NotRequired`
- `gherkin/pickles/compiler.py`: `from typing_extensions import NotRequired, TypeIs`

All modules use `from __future__ import annotations`, so annotations are never evaluated at
runtime. Verified on Python 3.9.6 with `python3 -I -S` (no site-packages).

To refresh: download the wheel (`python3 -m pip download --no-deps --only-binary :all:
--python-version 3.9 gherkin-official==<ver>`), copy its `gherkin/` package and
`dist-info/licenses/LICENSE` here, re-apply the patch above, then replace the corpus with
`testdata/good/*.feature` and `testdata/bad/*.feature` from the matching tag and update the
expected counts in `tests/unit/gherkin/test_conformance.py`.
