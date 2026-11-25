"""Optional XRAY_TOOLS filter: expose only selected tool groups (spec: multi-client)."""
from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Set

from .config import SettingsError, _setting

GROUPS: Dict[str, List[str]] = {
    "tests": ["search_tests", "get_test", "create_test", "update_test_type", "edit_test",
              "delete_test", "set_lifecycle_label", "get_test_steps", "add_test_steps",
              "move_test_step", "edit_test_step", "remove_test_step"],
    "containers": [
        "create_test_set", "search_test_sets", "get_tests_in_test_set", "add_tests_to_test_set",
        "remove_tests_from_test_set", "delete_test_set",
        "create_test_plan", "search_test_plans", "get_tests_in_test_plan",
        "add_tests_to_test_plan", "remove_tests_from_test_plan", "delete_test_plan",
        "add_test_executions_to_test_plan",
        "create_test_execution", "add_tests_to_test_execution", "remove_tests_from_test_execution",
        "delete_test_execution", "search_test_executions", "get_test_execution", "update_test_run",
        "get_test_environments", "add_test_environments_to_test_execution",
        "remove_test_environments_from_test_execution"],
    "folders": ["create_folder", "add_tests_to_folder", "get_tests_in_folder", "list_folders",
                "rename_folder", "move_folder", "delete_folder"],
    "import": ["create_tests_from_feature", "analyze_feature_tags", "import_feature_file",
               "export_cucumber_features", "import_execution_results"],
    "analysis": ["get_test_runs", "get_requirement_coverage", "find_related_tests"],
    "jira": ["get_create_fields", "list_project_versions"],
    "setup": ["get_status", "set_preferences"],
}
ALWAYS_ON = "setup"


def active_groups(env: Mapping[str, str]) -> Optional[List[str]]:
    """Selected group names (setup always included), or None when everything is exposed."""
    raw = _setting(env, "XRAY_TOOLS")
    if raw is None:
        return None
    names = [p.strip().lower() for p in raw.split(",") if p.strip()]
    if not names:
        return None
    unknown = [n for n in names if n not in GROUPS]
    if unknown:
        raise SettingsError("Unknown XRAY_TOOLS group(s) %s; valid groups: %s"
                            % (", ".join(repr(n) for n in unknown), ", ".join(GROUPS)))
    chosen = {ALWAYS_ON, *names}
    return [g for g in GROUPS if g in chosen]


def enabled_tools(env: Mapping[str, str]) -> Optional[Set[str]]:
    """Names of the tools to expose; None means all."""
    groups = active_groups(env)
    if groups is None:
        return None
    return {t for g in groups for t in GROUPS[g]}
