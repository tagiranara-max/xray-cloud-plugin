from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping

from .errors import ToolError


class PreferenceStore:
    """Remembered import choices, keyed by "<region>/<PROJECT>" in one JSON file:
    ${XRAY_DATA_DIR}/preferences.json, else ${CLAUDE_PLUGIN_DATA}/preferences.json, else
    ~/.config/xray-cloud/preferences.json.
    Reads happen on every call (no caching); writes are atomic (temp file, then rename)."""

    def __init__(self, env: Mapping[str, str]) -> None:
        data_dir = (env.get("XRAY_DATA_DIR") or env.get("CLAUDE_PLUGIN_DATA") or "").strip()
        if data_dir:
            self.path = Path(os.path.expanduser(data_dir)) / "preferences.json"
        else:
            self.path = (Path(os.path.expanduser("~")) / ".config" / "xray-cloud"
                         / "preferences.json")

    def _error(self, what: str, exc: BaseException = None) -> ToolError:
        detail = " (%s)" % (exc.__class__.__name__ if exc else what)
        return ToolError("Cannot %s the preferences file %s%s" % (what, self.path, detail),
                         file=str(self.path))

    def _load(self) -> Dict[str, Any]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError) as exc:
            raise self._error("read", exc)
        try:
            data = json.loads(text)
        except ValueError:
            raise ToolError("The preferences file %s is not valid JSON; fix or delete it" % self.path,
                            file=str(self.path))
        if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
            raise ToolError("The preferences file %s has an unexpected structure; fix or delete it"
                            % self.path, file=str(self.path))
        return data

    def _save(self, data: Dict[str, Any]) -> None:
        tmp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".preferences-",
                                       suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, str(self.path))
            tmp = None
        except OSError as exc:
            raise self._error("write", exc)
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    @staticmethod
    def _key(region: str, project: str) -> str:
        return "%s/%s" % (region, project)

    def get(self, region: str, project: str) -> Dict[str, Any]:
        return dict(self._load().get(self._key(region, project)) or {})

    def update(self, region: str, project: str, values: Dict[str, Any]) -> Dict[str, Any]:
        data = self._load()
        entry = dict(data.get(self._key(region, project)) or {})
        entry.update(values)
        data[self._key(region, project)] = entry
        self._save(data)
        return dict(entry)

    def clear(self, region: str, project: str, names: List[str]) -> Dict[str, Any]:
        data = self._load()
        key = self._key(region, project)
        entry = {k: v for k, v in (data.get(key) or {}).items() if k not in names}
        if entry:
            data[key] = entry
        else:
            data.pop(key, None)
        self._save(data)
        return dict(entry)
