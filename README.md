# xray-cloud-plugin

A Claude Code plugin that lets Claude manage Xray Cloud: tests, test sets, test plans, test executions, runs, folders and environments. The server is pure Python (standard library only), so there is nothing to install besides the plugin.

What it does that other Xray integrations do not:

- Full step editing, including insert at a position and move, with attachments recovered when steps are rewritten.
- Test Repository folders: create, rename, move, delete, and file tests into them.
- Test environments.
- Friendly custom fields: you declare them once in a settings file and use names like `component` instead of `customfield_12345`.
- Full Gherkin import: every valid `.feature` file, with Backgrounds as Pre-Conditions, Features as Test Sets, tags as labels, Rules, Outlines with all Examples, DataTables and DocStrings preserved.
- Safety: a project allowlist, duplicate checks before creating, explicit confirmation before deletes, a read-only mode, and region support (global/US, EU, AU).

## Requirements

- `python3` 3.9 or newer on your PATH.
- macOS or Linux. Windows is untested.
- An Xray Cloud API key (server/data center Xray is not supported).

## Install

```
/plugin marketplace add <owner>/xray-cloud-plugin
/plugin install xray-cloud@xray-cloud-plugin
```

## Credentials

Create an API key in Jira: Apps, Xray, API Keys. Claude Code asks for the client ID and secret when you enable the plugin. Alternatively leave those prompts empty and set the `XRAY_CLIENT_ID` and `XRAY_CLIENT_SECRET` environment variables. Credentials are never exposed to the model and never stored in the settings file.

## Region

Pick the region of your Xray instance when enabling the plugin: `global` (US, the default), `eu` or `au`. It can also be set with `XRAY_REGION`.

## Settings

The settings file lists the projects Claude may use and their custom fields. It is YAML (JSON is also accepted). The first file found wins; files are not merged:

1. the file chosen in the plugin's "Settings file" prompt (`XRAY_SETTINGS_FILE`); if that file does not exist, tools fail with an error instead of falling back to the other locations;
2. `.xray/settings.yml`, `.xray/settings.yaml` or `.xray/settings.json` in Claude Code's project directory, or the current directory when that is not set (per repository, so different repositories can target different projects);
3. `settings.yml`, `settings.yaml` or `settings.json` in `~/.config/xray-cloud/`.

Without a settings file the server still starts and read tools work, but write tools refuse. The settings are re-read whenever the file changes. A complete example is in [`.xray/settings.example.yml`](.xray/settings.example.yml):

```yaml
projects:
  PROJ:
    fields:
      test:
        component:
          id: customfield_12345
          type: option
          required: true
          default: Backend
      testExecution:
        fixVersion:
          id: fixVersions
          type: versions
          required: true
      testPlan: {}
      testSet: {}
      testRun:
        version:
          name: Version
          required: false
lifecycleLabels: [ai-draft, needs-refinement, automation-candidate, automated, obsolete]
duplicateCheck: true
```

- `projects`: the keys are an allowlist. Anything on an unlisted project is refused.
- Item types under `fields`: `test`, `testExecution`, `testPlan`, `testSet`, `precondition`, `testRun`.
- Field `type`: `option`, `options`, `string` (default), `number`, `version`, `versions`, `user`, `raw`.
- `required`: when true and there is no `default`, a create is refused before any API call until the value is given. `default` fills in the value when it is omitted.
- `id` is the Jira field id. Not sure how to find it? See [docs/field-ids.md](docs/field-ids.md).
- `testRun` fields are Xray test-run custom fields, not Jira fields: give the Xray field `name` (or an `id`).
- Escape hatch: create tools accept `rawFields` (`{"customfield_12345": <Jira JSON>}`), passed through untouched for fields not in the settings. `update_test_run` takes `rawFields` keyed by Xray run field name or id.
- `lifecycleLabels` (default shown above) are the labels `set_lifecycle_label` may apply. `duplicateCheck` turns the pre-create duplicate search on or off.

## Optional Jira token

Xray's API cannot edit Jira fields. With a Jira API token the plugin can also edit a test's summary, description, labels, priority and assignee, set lifecycle labels, list a project's versions (`list_project_versions`, which strictly needs the token), and read live create-field metadata for `get_create_fields` (without the token it answers from your settings file).

To enable it, create a token at https://id.atlassian.com/manage-profile/security/api-tokens, then fill in the plugin prompts "Jira site URL" (for example `https://yourcompany.atlassian.net`), "Jira email" and "Jira API token". The token acts as you. Some organizations block API tokens, which is why it is optional; the Atlassian (Rovo) MCP can do the same Jira edits.

## Read-only mode

Turn on "Read-only mode" in the plugin prompts (or set `XRAY_READ_ONLY=true`) and every tool that changes anything refuses to run.

## Permissions

Tools act with the permissions of the user behind the Xray API key (and, for Jira-field edits, the Jira token's user). The delete tools (`delete_test`, `delete_test_set`, `delete_test_plan`, `delete_test_execution`) need the Jira project permission **Delete Issues**, which many users don't have. Without it the tool returns a clear error with `reason: "permission_denied"` and the original Xray message in `xrayMessage`; ask a Jira admin for "Delete Issues" or the project Administrator role. Other missing write permissions are reported the same way. With the optional Jira token, delete previews (without `confirm: true`) also show `canDelete`.

## Tools

| Tool | What it does |
|---|---|
| `search_tests` | Search tests in a project by summary text or issue key (query) and/or a JQL filter (jql) |
| `get_test` | Get one test by key |
| `create_test` | Create a test (Manual, Cucumber or Generic) in a project |
| `update_test_type` | Change a test's type (Manual, Cucumber or Generic) |
| `edit_test` | Edit a test (Gherkin through Xray; Jira fields need the Jira token) |
| `delete_test` | Delete a test |
| `set_lifecycle_label` | Set a lifecycle label on a test, replacing any previous lifecycle label |
| `get_test_steps` | Get the steps of a test |
| `add_test_steps` | Add steps to a Manual test at the end, start, or after step N |
| `move_test_step` | Move a step of a Manual test from one position to another |
| `edit_test_step` | Update a step of a Manual test |
| `remove_test_step` | Delete a step of a Manual test |
| `create_test_set` | Create a test set, optionally adding existing or new tests |
| `search_test_sets` | Search test sets in a project |
| `get_tests_in_test_set` | List the tests in a test set |
| `add_tests_to_test_set` | Add tests to a test set |
| `remove_tests_from_test_set` | Remove tests from a test set |
| `delete_test_set` | Delete a test set (the tests are kept) |
| `create_test_plan` | Create a test plan, optionally adding existing or new tests |
| `search_test_plans` | Search test plans in a project |
| `get_tests_in_test_plan` | List the tests in a test plan |
| `add_tests_to_test_plan` | Add tests (or all tests of test sets) to a test plan |
| `remove_tests_from_test_plan` | Remove tests from a test plan |
| `delete_test_plan` | Delete a test plan (the tests are kept) |
| `create_test_execution` | Create a test execution |
| `add_test_executions_to_test_plan` | Link existing test executions to a test plan |
| `add_tests_to_test_execution` | Add tests, tests of test sets, or tests of a plan to an execution |
| `remove_tests_from_test_execution` | Remove tests from a test execution |
| `delete_test_execution` | Delete a test execution and its runs (irreversible) |
| `search_test_executions` | Search test executions in a project |
| `get_test_execution` | Get a test execution and its test runs |
| `update_test_run` | Update a run: status, steps, defects, evidence, run custom fields |
| `get_test_environments` | List the test environments known in a project |
| `add_test_environments_to_test_execution` | Add environments to a test execution (new names are created) |
| `remove_test_environments_from_test_execution` | Remove environments from a test execution (Xray has no API to delete an environment) |
| `create_folder` | Create a Test Repository folder (and missing parents) |
| `add_tests_to_folder` | Move tests into a folder |
| `get_tests_in_folder` | List the tests directly inside a folder |
| `list_folders` | List the folder tree below a path |
| `rename_folder` | Rename a folder |
| `move_folder` | Move a folder with its contents under another folder |
| `delete_folder` | Delete a folder and its subfolders (tests are kept) |
| `create_tests_from_feature` | Import a Gherkin `.feature` file (preview, then apply) |
| `import_execution_results` | Import test results (xray, cucumber, behave, junit, testng, nunit, robot) into a new or existing test execution |
| `import_feature_file` | Import a `.feature` file or a `.zip` of them with Xray's native importer (no duplicate check, choices, tag rules or AI label) |
| `export_cucumber_features` | Export Cucumber tests as `.feature` files into a local directory (never overwrites unless asked) |
| `get_test_runs` | Run history of one test: statuses, counts, last status and flaky flag |
| `get_requirement_coverage` | Tests covering a requirement, each with the status of its latest run |
| `find_related_tests` | Find tests related to an area by terms, requirement keys, labels, components or folder |
| `get_create_fields` | List an issue type's fields: from Jira with the token, otherwise from your settings file |
| `list_project_versions` | List a Jira project's versions (needs the Jira token) |
| `analyze_feature_tags` | Summarise the tags in `.feature` files and show which tag rules match (read-only) |
| `set_preferences` | Change the remembered import choices and tag rules of a project (writes only the plugin's own file) |
| `get_status` | Show the plugin's state: region, credentials configured (never their values), settings file, projects, Jira, and the effective import choices with their source |

## Results and native feature import/export

- **`import_execution_results`** uploads a result file (JUnit, TestNG, NUnit, Robot, Cucumber JSON, Behave JSON or Xray JSON) into a new or existing test execution. The file is sent byte for byte (max 25 MB); give either `filePath` or `content`. Exceptions: Xray JSON for an existing execution, plan or environments gets `testExecutionKey`/`info` set; Cucumber JSON for an existing execution needs a `@TEST_<KEY>` tag on every scenario and is sent as Xray JSON (one status per test), because Xray's Cucumber import always creates a new execution; Behave JSON always creates a new execution. Every key in an Xray JSON report (`testExecutionKey`, `info.testPlanKey`, `info.project`, each `testKey`) must belong to `projectKey`, and `info.project` is set when missing; new Cucumber/Behave executions always go through the multipart endpoint so the project is set. For JUnit, TestNG, NUnit, Robot and Cucumber, Xray may create Test issues for results it cannot match; those tests get no AI label (use the `import-tests` skill or `create_tests_from_feature` for labelled creation).
- **`import_feature_file`** uses Xray's native importer for a `.feature` file or a `.zip` of them. It is faster and a good fit for bulk loads, but it does no duplicate check, asks no questions, applies no tag rules, adds no AI label and has no preview.
- **`create_tests_from_feature`** is the plugin's own import (see Feature import below): duplicate check, import choices, tag rules, the AI label and a preview before anything is written. Use it for everyday imports; use `import_feature_file` when you want Xray's native result as is.
- **`export_cucumber_features`** writes Cucumber tests (by key or saved filter) as `.feature` files into a local directory and refuses to overwrite existing files unless you ask.

## Analysis

Read-only tools for questions about test history and impact:

- **`get_test_runs`**: the run history of one test, with counts by status, the last status and a flaky flag (both PASSED and FAILED among the runs).
- **`get_requirement_coverage`**: the tests that cover a requirement and the latest status of each, with counts and never-run tests.
- **`find_related_tests`**: tests related to an area by text, requirement keys, labels, components or folder; each test appears once with the criteria it matched (at most 200 results).

## Skills

Skills guide Claude through multi-step workflows on top of the tools:

- **`xray-setup`**: create or update the settings file (projects, field IDs, defaults, import choices, tag rules) with you.
- **`import-results`**: import a test result file into Xray as a test execution and check for flaky tests.
- **`import-tests`**: import tests from `.feature` files and from tabular or markdown sources.
- **`predict-affected-tests`**: predict which tests a code change affects and optionally create an execution for the at-risk ones.

## Feature import

`create_tests_from_feature` follows Xray's own import semantics:

| Gherkin | Xray |
|---|---|
| Feature | Test Set (reused by summary, if you choose so; see Choices and preferences); feature tags go through the tag rules, plain tags become labels |
| Background (feature or rule level) | Pre-Condition (or inline steps, your choice) linked to every test it applies to; reused by `#@PRECOND_<KEY>` comment, then feature path label, then summary |
| Rule | Its scenarios import normally; its Background combines with the feature's |
| Scenario / Example | Cucumber test (Scenario) whose definition is its steps |
| Scenario Outline / Template | Cucumber test (Scenario Outline) with its steps and all Examples blocks |
| Plain tags | Labels |
| `@TEST_<KEY>` or `@<KEY>` on a scenario | Updates that existing test |
| `@REQ_<KEY>` (or a plain key on the Feature/Rule) | Links new tests to that requirement |
| DocStrings and DataTables | Preserved exactly, with correct escaping |

By default (`apply=false`) the tool only previews what would be created, updated and linked, with possible duplicates flagged. Call it again with `apply=true` to execute; scenarios with possible duplicates are skipped unless their index is listed in `createDuplicates`. A file that fails to parse creates nothing and reports the line and column.

### Choices and preferences

A few import choices are yours to make, and the plugin asks about them instead of guessing:

- `createTestSet`: turn the Feature into a Test Set?
- `backgrounds`: Backgrounds as Pre-Conditions (`preconditions`) or copied into each test (`inline`)?
- `tagsAsLabels`: turn plain tags into labels?
- `aiLabel` and `aiLabelScope`: mark what the AI creates (see "AI label" below).

Each is asked once per project. When a tool answers `status: needs_answer` (or an import preview returns `questions`), Claude asks you and calls again with your answer, and the plugin remembers it for that project. Nothing is written to Xray until every applicable question is answered.

Where a value comes from, first match wins: the call itself, then your settings file (the project's `featureImport` / `aiCreated`, then the top-level ones), then the remembered answer, then unanswered. A value in the settings file always wins, so a team can fix choices for everyone. A call value that differs from the remembered one applies to that call only, unless `remember: true` is passed.

To change a remembered choice, tell Claude "change my import preferences" (it calls `set_preferences`), or edit the settings file. Remembered answers live in `${CLAUDE_PLUGIN_DATA}/preferences.json`, or `~/.config/xray-cloud/preferences.json` when that variable is not set; they are per user and survive plugin updates.

```yaml
# Optional, in your settings file; every key is optional (absent = asked or remembered)
featureImport:
  createTestSet: true
  backgrounds: preconditions   # or inline
  tagsAsLabels: true
  unmatchedTags: keep          # keep (default) | skip
aiCreated:
  label: my-ai-label           # your own label; null = don't mark
  scope: tests                 # tests | all
projects:
  PROJ:
    featureImport: {}          # overrides the top level key by key; tagRules replace, not merge
```

### Backgrounds

With `preconditions` (the Xray way), each Background becomes a Pre-Condition linked to the tests it applies to, and is reused on re-import. With `inline`, no Pre-Conditions are created: each test's steps start with the applicable Background steps (feature level, then rule level). `#@PRECOND_<KEY>` comments are ignored with a warning when inline.

### AI label

You can mark what the AI creates with a label of your own choosing. There is no default label, and it has nothing to do with lifecycle labels. Pass `aiLabel` (or set `aiCreated.label`) to a label without whitespace, or `""` to not mark. It is added only to newly created items, never to existing ones. On `create_test_set` and `create_test_plan` it applies to new tests only. For feature import, `aiLabelScope` is `tests` or `all` (`all` also marks new Pre-Conditions and Test Sets).

### Tag rules

Tag rules turn Gherkin tags into labels, Test Set membership and field values. **There are no built-in conventions**: with no rules, plain tags simply become labels (`unmatchedTags: keep`, the default) and nothing else happens. Issue-key tags (`@TEST_<KEY>`, `@REQ_<KEY>`, plain keys) are never subject to rules. The rules below are **examples only**, with made-up tag names; write your own, or ask Claude to run `analyze_feature_tags` on your files and propose rules (it explains each one and applies nothing until you agree, then stores them with `set_preferences`).

```yaml
# EXAMPLE ONLY: tag names here are made up
featureImport:
  tagRules:
    - tag: group                 # key form: matches @group=alpha and @group:alpha (key is case-insensitive)
      split: ","                 # @group=alpha,beta gives the values alpha and beta
      testSet: "{value}"         # one Test Set per value (reused by exact name, else created)
    - tag: team
      field: { component: "{value}" }   # friendly field name from projects.<KEY>.fields.test
      label: "team-{value}"
    - match: "(wip|draft)"       # regex form: the whole tag (without @) must match
      label: "to-review"         # only {tag} and $1..$9 are available here
    - tag: internal
      skip: true                 # drop the tag; skip cannot be combined with other actions
```

- Key form (`tag`, a string or a list): matches `key=value` and `key:value`. Actions: `label`, `testSet` and `field` take templates (`{value}`, `{key}`, `{tag}`); `split` splits the value into several.
- Regex form (`match`): matches the whole tag; templates may use only `{tag}` and `$1`..`$9` (`{value}` and `{key}` are refused).
- The first matching rule wins. Project `tagRules` replace the top-level list.
- `tagsAsLabels: false` ignores `label` actions and drops unmatched tags; `testSet` and `field` actions still apply.
- A rule that would produce an invalid label or a conflicting field value refuses the whole import before anything is written.
- Tags no rule covers appear in the preview's `tagSummary.unmatched`.

### notDone

Some things cannot be done without Jira: labels, fields or requirement links on existing tests, when there is no Jira token. A field that already has a different value is never overwritten either (an equal value is left alone silently), and a field whose current value could not be read is never written. The import still does everything else and lists the rest in `notDone: [{item, what, reason, fix}]`. When the reason is "no Jira token or Atlassian MCP", Claude can complete the items with the Atlassian MCP if it is connected, or you can set the Jira token (see "Optional Jira token"). Tools that need the Jira token for the whole operation (`edit_test` Jira fields, `set_lifecycle_label`) return an error with the same `reason` and `fix`.

## Changing your setup

- **Plugin prompts:** `/plugin`, then configure the plugin: region, credentials, Jira, read-only mode and the settings file path.
- **Settings file:** your own file, found in this order: the path from the plugin prompt; `.xray/settings.yml` in the project Claude Code is working in; `~/.config/xray-cloud/settings.yml`. The plugin ships no settings. A settings file committed in your team's project makes the choices team-wide.
- **Import choices and tag rules:** ask Claude to "change my import preferences" (`set_preferences`), or edit the settings file. A settings-file value always wins over a remembered one.
- **`get_status`:** shows the plugin's state without secrets: region, whether credentials are configured, the settings file in use and the locations searched, projects, Jira, and each project's effective choices with where each came from (call, settings, remembered, default or unanswered).
- **Guided setup:** the `xray-setup` skill walks through all of this: it finds or creates the settings file, discovers field IDs (Jira token, Atlassian MCP, or manual steps), offers import choices, the AI-created label and tag rules, shows the diff, asks before saving and ends with `get_status`.

## Limitations

- Lifecycle labels and editing a test's summary, description or labels need the optional Jira token or the Atlassian (Rovo) MCP, because Xray's API cannot edit Jira fields.
- Xray Cloud only; Xray Server and Data Center are not supported.
- Windows is untested.

## Development

```sh
python3 -m pip install pytest
python3 -m pytest -q
```

See [CLAUDE.md](CLAUDE.md) for conventions and the server smoke test.
