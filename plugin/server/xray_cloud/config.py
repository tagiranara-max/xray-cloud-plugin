from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ._vendor_path import import_vendored
from .errors import ToolError
from .gherkin.tagrules import TagRuleError, parse_rules

# The vendored PyYAML always wins over an installed one (see _vendor/README.md).
yaml = import_vendored("yaml")

REGIONS = {
    "global": "https://xray.cloud.getxray.app",
    "eu": "https://eu.xray.cloud.getxray.app",
    "au": "https://au.xray.cloud.getxray.app",
}
ITEM_TYPES = ("test", "testExecution", "testPlan", "testSet", "precondition", "testRun")
FIELD_TYPES = ("option", "options", "string", "number", "version", "versions", "user", "raw")
FEATURE_IMPORT_KEYS = ("createTestSet", "backgrounds", "tagsAsLabels", "unmatchedTags",
                       "tagRules")
AI_CREATED_KEYS = ("label", "scope")
DEFAULT_LIFECYCLE_LABELS = ["ai-draft", "needs-refinement", "automation-candidate",
                            "automated", "obsolete"]

# Jira project key: uppercase letter, then uppercase letters, digits or underscores.
PROJECT_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*")
_PLACEHOLDER = re.compile(r"^\$\{[A-Za-z0-9_.:]+\}$")
_CREDENTIAL_KEY = re.compile(r"secret|token|password|client_?id", re.IGNORECASE)


class SettingsError(ToolError):
    pass


def _get(env: Mapping[str, str], key: str) -> Optional[str]:
    """Env value, or None when unset, empty, or an entirely unsubstituted ${...} placeholder."""
    value = env.get(key)
    if value is None:
        return None
    value = value.strip()
    if not value or _PLACEHOLDER.match(value):
        return None
    return value


@dataclass
class Credentials:
    client_id: str
    client_secret: str = field(repr=False)
    region: str
    base_url: str


def load_credentials(env: Mapping[str, str]) -> Optional[Credentials]:
    region = (_get(env, "XRAY_REGION") or "global").lower()
    if region not in REGIONS:
        raise SettingsError("Unknown XRAY_REGION %r; expected one of: %s"
                            % (region, ", ".join(REGIONS)))
    client_id = _get(env, "XRAY_CLIENT_ID")
    client_secret = _get(env, "XRAY_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None
    return Credentials(client_id, client_secret, region, REGIONS[region])


@dataclass
class JiraCredentials:
    site_url: str
    email: str = field(repr=False)
    token: str = field(repr=False)


def load_jira_credentials(env: Mapping[str, str]) -> Optional[JiraCredentials]:
    site, email, token = (_get(env, k) for k in ("JIRA_SITE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"))
    if not (site and email and token):
        return None
    return JiraCredentials(site, email, token)


def load_read_only(env: Mapping[str, str]) -> bool:
    value = _get(env, "XRAY_READ_ONLY")
    if value is None:
        return False
    v = value.lower()
    if v in ("true", "1", "yes"):
        return True
    if v in ("false", "0", "no"):
        return False
    raise SettingsError("XRAY_READ_ONLY must be one of true/false/1/0/yes/no")


@dataclass
class FieldSpec:
    name: str
    id: Optional[str]
    xray_name: Optional[str]
    type: str
    required: bool
    default: Any


@dataclass
class ProjectSettings:
    key: str
    fields: Dict[str, Dict[str, FieldSpec]] = field(default_factory=dict)
    feature_import: Dict[str, Any] = field(default_factory=dict)
    ai_created: Dict[str, Any] = field(default_factory=dict)
    # Result import: a scenario tag naming its test (@<tag>=KEY, @<tag>:KEY, @<tag>_KEY).
    test_key_tag: Optional[str] = None


@dataclass
class Settings:
    path: Optional[Path]
    projects: Dict[str, ProjectSettings]
    lifecycle_labels: List[str]
    duplicate_check: bool
    feature_import: Dict[str, Any] = field(default_factory=dict)
    ai_created: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "Settings":
        return cls(None, {}, list(DEFAULT_LIFECYCLE_LABELS), True)


# The plugin's own folder (plugin/): a client that starts the server there (Codex runs plugin
# servers with the plugin folder as cwd) gives no hint where the user's project is.
PLUGIN_DIR = Path(__file__).resolve().parents[2]


def project_dir_info(env: Mapping[str, str]) -> Tuple[Optional[str], Optional[str]]:
    """-> (project directory, source). Source is XRAY_PROJECT_DIR, CLAUDE_PROJECT_DIR or cwd.
    When neither variable is set and the cwd is inside the plugin's own folder, the project
    directory is unknown: (None, None)."""
    for name in ("XRAY_PROJECT_DIR", "CLAUDE_PROJECT_DIR"):
        value = _get(env, name)
        if value:
            return value, name
    cwd = os.getcwd()
    real, plugin = os.path.realpath(cwd), os.path.realpath(str(PLUGIN_DIR))
    if real == plugin or real.startswith(plugin + os.sep):
        return None, None
    return cwd, "cwd"


def project_dir(env: Mapping[str, str]) -> Optional[str]:
    """The project directory: $XRAY_PROJECT_DIR, else Claude Code's $CLAUDE_PROJECT_DIR, else
    the current directory; None when unknown (see project_dir_info)."""
    return project_dir_info(env)[0]


def settings_search_paths(env: Mapping[str, str]) -> List[Path]:
    """Every location a settings file is looked for, in priority order."""
    candidates: List[Path] = []
    explicit = _get(env, "XRAY_SETTINGS_FILE")
    if explicit:
        candidates.append(Path(os.path.expanduser(explicit)))
    root = project_dir(env)
    if root is not None:  # unknown project folder: no per-project settings lookup
        for ext in ("yml", "yaml", "json"):
            candidates.append(Path(root) / ".xray" / ("settings." + ext))
    home = Path(os.path.expanduser("~")) / ".config" / "xray-cloud"
    for ext in ("yml", "yaml", "json"):
        candidates.append(home / ("settings." + ext))
    return candidates


def find_settings_file(env: Mapping[str, str]) -> Optional[Path]:
    """The first existing settings file. An explicit XRAY_SETTINGS_FILE must exist: a wrong
    path is an error rather than a silent fall-through to another file."""
    explicit = _get(env, "XRAY_SETTINGS_FILE")
    if explicit:
        path = Path(os.path.expanduser(explicit))
        if not path.is_file():
            raise SettingsError(
                "The settings file set in XRAY_SETTINGS_FILE (Claude Code plugin setting "
                "\"Settings file\", or the environment variable in your MCP client's server "
                "config) does not exist or is not a file: %s" % path,
                setting="XRAY_SETTINGS_FILE", file=str(path))
        return path
    for c in settings_search_paths(env):
        if c.is_file():
            return c
    return None


def _check_keys(node: Any, where: str, path: Path) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and _CREDENTIAL_KEY.search(k):
                raise SettingsError(
                    "%s: key %r looks like a credential; secrets must not be stored in the "
                    "settings file (%s)" % (path.name, k, where))
            _check_keys(v, "%s.%s" % (where, k), path)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _check_keys(v, "%s[%d]" % (where, i), path)


def _fail(path: Path, msg: str) -> SettingsError:
    return SettingsError("%s: %s" % (path.name, msg), file=str(path))


def _bool(path: Path, where: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise _fail(path, "%s must be true or false" % where)
    return value


def _parse_field(path: Path, where: str, item_type: str, name: str, raw: Any) -> FieldSpec:
    if not isinstance(raw, dict):
        raise _fail(path, "%s.%s must be a mapping" % (where, name))
    ftype = raw.get("type", "string")
    if ftype not in FIELD_TYPES:
        raise _fail(path, "%s.%s: unknown field type %r; expected one of: %s"
                    % (where, name, ftype, ", ".join(FIELD_TYPES)))
    fid = raw.get("id")
    xname = raw.get("name") if item_type == "testRun" else None
    if item_type == "testRun":
        if not fid and not xname:
            raise _fail(path, "%s.%s needs `name` (Xray run field name) or `id`" % (where, name))
    elif not fid:
        raise _fail(path, "%s.%s needs an `id`" % (where, name))
    return FieldSpec(name=name, id=str(fid) if fid else None,
                     xray_name=str(xname) if xname else None, type=ftype,
                     required=_bool(path, "%s.%s.required" % (where, name), raw.get("required", False)), default=raw.get("default"))


def _project_key(path: Path, key: Any) -> str:
    if isinstance(key, bool):
        raise _fail(path, "projects: a project key was read as the boolean %s (YAML treats "
                          "YES/NO/ON/OFF/TRUE/FALSE as booleans); quote it, e.g. \"NO\": {}"
                    % str(key).lower())
    if not isinstance(key, str) or not PROJECT_KEY_RE.fullmatch(key):
        raise _fail(path, "projects: invalid project key %r; use the Jira project key in "
                          "uppercase (letters, digits, underscore; starting with a letter)"
                    % (key,))
    return key


def feature_import_problem(key: str, value: Any) -> Optional[str]:
    """Why `value` is not valid for featureImport.<key> (text to follow the setting's name),
    or None. Shared by the settings loader and set_preferences. Rule contents are checked
    separately (gherkin.tagrules.parse_rules)."""
    if key in ("createTestSet", "tagsAsLabels"):
        return None if isinstance(value, bool) else "must be true or false"
    if key == "backgrounds":
        return None if value in ("preconditions", "inline") else \
            'must be "preconditions" or "inline"'
    if key == "unmatchedTags":
        return None if value in ("keep", "skip") else 'must be "keep" or "skip"'
    if key == "tagRules":
        return None if isinstance(value, list) else "must be a list"
    return "is not a known key"


def ai_created_problem(key: str, value: Any) -> Optional[str]:
    """Why `value` is not valid for aiCreated.<key>, or None (see feature_import_problem)."""
    if key == "label":
        if value is not None and (not isinstance(value, str) or not value
                                  or re.search(r"\s", value)):
            return "must be a non-empty label without whitespace, or null (do not mark)"
        return None
    if key == "scope":
        return None if value in ("tests", "all") else 'must be "tests" or "all"'
    return "is not a known key"


def _parse_feature_import(path: Path, where: str, raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _fail(path, "%s must be a mapping" % where)
    out: Dict[str, Any] = {}
    for k, v in raw.items():
        w = "%s.%s" % (where, k)
        if k not in FEATURE_IMPORT_KEYS:
            raise _fail(path, "%s: unknown key %r; expected one of: %s"
                        % (where, k, ", ".join(FEATURE_IMPORT_KEYS)))
        problem = feature_import_problem(k, v)
        if problem:
            raise _fail(path, "%s %s" % (w, problem))
        out[k] = list(v) if k == "tagRules" else v
    return out


def _parse_tag_rules(path: Path, where: str, fi: Dict[str, Any], names: set) -> None:
    """Replace the raw tagRules list in `fi` with parsed TagRule objects (load-time check)."""
    if "tagRules" not in fi:
        return
    try:
        fi["tagRules"] = parse_rules(fi["tagRules"], names, where + ".tagRules")
    except TagRuleError as exc:
        raise _fail(path, exc.message)


def _parse_ai_created(path: Path, where: str, raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _fail(path, "%s must be a mapping" % where)
    out: Dict[str, Any] = {}
    for k, v in raw.items():
        w = "%s.%s" % (where, k)
        if k not in AI_CREATED_KEYS:
            raise _fail(path, "%s: unknown key %r; expected one of: %s"
                        % (where, k, ", ".join(AI_CREATED_KEYS)))
        problem = ai_created_problem(k, v)
        if problem:
            raise _fail(path, "%s %s" % (w, problem))
        out[k] = v
    return out


_TAG_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")


def _parse_test_key_tag(path: Path, where: str, raw: Any) -> Optional[str]:
    """projects.<KEY>.testKeyTag: a tag name without '@' (letters, digits, '_', '.', '-')."""
    if raw is None:
        return None
    if not isinstance(raw, str) or not _TAG_NAME.match(raw):
        raise _fail(path, "%s must be a tag name without '@', e.g. issue (for @issue=PROJ-12),"
                          " got %r" % (where, raw))
    return raw


def _parse(path: Path, data: Any) -> Settings:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise _fail(path, "top level must be a mapping")
    _check_keys(data, "<root>", path)
    projects: Dict[str, ProjectSettings] = {}
    raw_projects = data.get("projects") or {}
    if not isinstance(raw_projects, dict):
        raise _fail(path, "`projects` must be a mapping")
    for key, praw in raw_projects.items():
        key = _project_key(path, key)
        praw = praw or {}
        if not isinstance(praw, dict):
            raise _fail(path, "projects.%s must be a mapping" % key)
        fields: Dict[str, Dict[str, FieldSpec]] = {}
        raw_fields = praw.get("fields") or {}
        if not isinstance(raw_fields, dict):
            raise _fail(path, "projects.%s.fields must be a mapping" % key)
        for item_type, fmap in raw_fields.items():
            if item_type not in ITEM_TYPES:
                raise _fail(path, "projects.%s: unknown item type %r; expected one of: %s"
                            % (key, item_type, ", ".join(ITEM_TYPES)))
            where = "projects.%s.fields.%s" % (key, item_type)
            if not isinstance(fmap or {}, dict):
                raise _fail(path, "%s must be a mapping" % where)
            fields[item_type] = {str(n): _parse_field(path, where, item_type, str(n), r)
                                 for n, r in (fmap or {}).items()}
        projects[key] = ProjectSettings(
            key=key, fields=fields,
            feature_import=_parse_feature_import(path, "projects.%s.featureImport" % key,
                                                 praw.get("featureImport")),
            ai_created=_parse_ai_created(path, "projects.%s.aiCreated" % key,
                                         praw.get("aiCreated")),
            test_key_tag=_parse_test_key_tag(path, "projects.%s.testKeyTag" % key,
                                             praw.get("testKeyTag")))
    top = _parse_feature_import(path, "featureImport", data.get("featureImport"))
    union: set = set()
    for ps in projects.values():
        union.update(ps.fields.get("test", {}))
        _parse_tag_rules(path, "projects.%s.featureImport" % ps.key, ps.feature_import,
                         set(ps.fields.get("test", {})))
    _parse_tag_rules(path, "featureImport", top, union)
    labels = data.get("lifecycleLabels", list(DEFAULT_LIFECYCLE_LABELS))
    if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
        raise _fail(path, "`lifecycleLabels` must be a list of strings")
    return Settings(path, projects, list(labels),
                    _bool(path, "duplicateCheck", data.get("duplicateCheck", True)),
                    top,
                    _parse_ai_created(path, "aiCreated", data.get("aiCreated")))


def _load_file(path: Path) -> Settings:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise _fail(path, "not valid UTF-8")
    except OSError as exc:
        raise _fail(path, "cannot read file (%s)" % exc.__class__.__name__)
    try:
        if path.suffix.lower() == ".json":
            data = json.loads(text)
        else:
            data = yaml.safe_load(text)
    except json.JSONDecodeError as exc:
        raise _fail(path, "invalid JSON at line %d: %s" % (exc.lineno, exc.msg))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        problem = getattr(exc, "problem", None) or "invalid YAML"
        if mark is not None:
            raise _fail(path, "invalid YAML at line %d: %s" % (mark.line + 1, problem))
        raise _fail(path, "invalid YAML: %s" % problem)
    return _parse(path, data)


class SettingsStore:
    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env
        self._key: Any = None
        self._settings = Settings.empty()

    def get(self) -> Settings:
        path = find_settings_file(self._env)
        if path is None:
            self._key = None
            self._settings = Settings.empty()
            return self._settings
        key = (str(path), path.stat().st_mtime_ns)
        if key != self._key:
            self._settings = _load_file(path)
            self._key = key
        return self._settings
