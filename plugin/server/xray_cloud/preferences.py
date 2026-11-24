from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from .config import SettingsError, _get, load_credentials
from .errors import ToolError


def site_id(env: Mapping[str, str]) -> str:
    """First 12 hex characters of sha256(client id); "" without credentials. A hash, never the
    id itself, and never shown to the user."""
    try:
        creds = load_credentials(env)
    except SettingsError:
        return ""
    if creds is None:
        return ""
    return hashlib.sha256(creds.client_id.encode("utf-8")).hexdigest()[:12]


class PreferenceStore:
    """Remembered choices in one JSON file. Per project: "<region>/<site>/<PROJECT>" (choices
    and fieldValues; an older "<region>/<PROJECT>" entry is read once and migrated on the next
    write). Per site: "<region>/<site>" (approveAll, approvedProjects). The file is:
    ${XRAY_DATA_DIR}/preferences.json, else ${CLAUDE_PLUGIN_DATA}/preferences.json, else
    ~/.config/xray-cloud/preferences.json.
    Reads happen on every call (no caching); every change re-reads the file just before the
    atomic write (temp file, then rename), so a stale read never drops another writer's data."""

    def __init__(self, env: Mapping[str, str]) -> None:
        data_dir = _get(env, "XRAY_DATA_DIR") or _get(env, "CLAUDE_PLUGIN_DATA") or ""
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

    def _mutate(self, fn: Callable[[Dict[str, Any]], Any]) -> Any:
        """Re-read, apply fn to the fresh data, write atomically."""
        data = self._load()
        result = fn(data)
        self._save(data)
        return result

    @staticmethod
    def _key(region: str, project: str, site: str = "") -> str:
        return "%s/%s/%s" % (region, site, project) if site else "%s/%s" % (region, project)

    @staticmethod
    def _site_key(region: str, site: str) -> str:
        return "%s/%s" % (region, site)

    def _entry(self, data: Dict[str, Any], region: str, project: str, site: str) -> Dict[str, Any]:
        entry = data.get(self._key(region, project, site))
        if entry is None and site:  # one-time fallback to the pre-site key
            entry = data.get(self._key(region, project))
        return dict(entry or {})

    def _store_entry(self, data: Dict[str, Any], region: str, project: str, site: str,
                     entry: Dict[str, Any]) -> None:
        key = self._key(region, project, site)
        if site:
            data.pop(self._key(region, project), None)  # migrated
        if entry:
            data[key] = entry
        else:
            data.pop(key, None)

    def get(self, region: str, project: str, site: str = "") -> Dict[str, Any]:
        return self._entry(self._load(), region, project, site)

    def update(self, region: str, project: str, values: Dict[str, Any],
               site: str = "") -> Dict[str, Any]:
        def op(data):
            entry = self._entry(data, region, project, site)
            entry.update(values)
            self._store_entry(data, region, project, site, entry)
            return dict(entry)
        return self._mutate(op)

    def clear(self, region: str, project: str, names: List[str],
              site: str = "") -> Dict[str, Any]:
        def op(data):
            entry = {k: v for k, v in self._entry(data, region, project, site).items()
                     if k not in names}
            self._store_entry(data, region, project, site, entry)
            return dict(entry)
        return self._mutate(op)

    # approvals (per site)

    def approval(self, region: str, site: str) -> Dict[str, Any]:
        entry = self._load().get(self._site_key(region, site)) if site else None
        entry = entry or {}
        projects = entry.get("approvedProjects")
        return {"approveAll": entry.get("approveAll") is True,
                "approvedProjects": sorted(p for p in projects if isinstance(p, str))
                if isinstance(projects, list) else []}

    def _write_approval(self, region: str, site: str, change: Callable[[Dict[str, Any]], None]) -> None:
        if not site:
            raise ToolError("Cannot store an approval without Xray credentials")

        def op(data):
            key = self._site_key(region, site)
            entry = dict(data.get(key) or {})
            state = {"all": entry.get("approveAll") is True,
                     "projects": set(p for p in (entry.get("approvedProjects") or [])
                                     if isinstance(p, str))}
            change(state)
            entry["approveAll"] = state["all"]
            entry["approvedProjects"] = sorted(state["projects"])
            data[key] = entry
        self._mutate(op)

    def approve(self, region: str, site: str, project_key: Optional[str], all_: bool) -> None:
        def change(state):
            if all_:
                state["all"] = True
            if project_key:
                state["projects"].add(project_key)
        self._write_approval(region, site, change)

    def revoke(self, region: str, site: str, project_key: Optional[str], all_: bool) -> None:
        if not site:
            return  # nothing can be approved without credentials
        def change(state):
            if all_:
                state["all"] = False
            if project_key:
                state["projects"].discard(project_key)
        self._write_approval(region, site, change)

    # remembered field values: {itemType: {field: value}} under the project entry

    def field_values(self, region: str, site: str, pk: str, item_type: str) -> Dict[str, Any]:
        values = (self.get(region, pk, site).get("fieldValues") or {}).get(item_type)
        return dict(values) if isinstance(values, dict) else {}

    def remember_fields(self, region: str, site: str, pk: str, item_type: str,
                        values: Dict[str, Any]) -> None:
        if not values:
            return

        def op(data):
            entry = self._entry(data, region, pk, site)
            fv = dict(entry.get("fieldValues") or {})
            fv[item_type] = dict(fv.get(item_type) or {}, **values)
            entry["fieldValues"] = fv
            self._store_entry(data, region, pk, site, entry)
        self._mutate(op)

    def clear_fields(self, region: str, site: str, pk: str, item_type: Optional[str]) -> None:
        def op(data):
            entry = self._entry(data, region, pk, site)
            fv = dict(entry.get("fieldValues") or {})
            if item_type is None:
                fv = {}
            else:
                fv.pop(item_type, None)
            if fv:
                entry["fieldValues"] = fv
            else:
                entry.pop("fieldValues", None)
            self._store_entry(data, region, pk, site, entry)
        self._mutate(op)
