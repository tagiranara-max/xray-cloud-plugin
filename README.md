# xray-cloud-plugin

A Claude Code plugin that lets Claude manage Xray Cloud: tests, test sets, test plans, test executions, runs, folders and environments. The server is pure Python (standard library only), so there is nothing to install besides the plugin.

What it does that other Xray integrations do not:

- Full step editing, including insert at a position and move, with attachments recovered when steps are rewritten.
- Test Repository folders: create, rename, move, delete, and file tests into them.
- Test environments.
- Friendly custom fields: you can declare them once in an optional settings file and use names like `component` instead of `customfield_12345`.
- Full Gherkin import: every valid `.feature` file, with Backgrounds as Pre-Conditions, Features as Test Sets, tags as labels, Rules, Outlines with all Examples, DataTables and DocStrings preserved.
- Safety: a one-time approval per project (or an optional team project list), duplicate checks before creating, explicit confirmation before deletes, a read-only mode, and region support (global/US, EU, AU).

## Requirements

- `python3` 3.9 or newer on your PATH.
- macOS or Linux. Windows is untested.
- An Xray Cloud API key (server/data center Xray is not supported).

## Install

```
/plugin marketplace add <owner>/xray-cloud-plugin
/plugin install xray-cloud@xray-cloud-plugin
```

## Use with other MCP clients

The server speaks plain MCP over stdio, so it also works in Codex, Cursor, Gemini CLI, VS Code and Claude Desktop. It needs Python 3.9 or newer. In the examples below `/path/to/xray-cloud-plugin` is your clone of this repository (`git clone https://github.com/<owner>/xray-cloud-plugin`). On Windows the command is usually `python` or `py` instead of `python3`, and paths use backslashes (escape them in JSON and double-quoted TOML).

Put credentials in your environment or your client's secret mechanism, never in a committed config file. The examples only reference variables.

The `xray-setup`, `import-results`, `import-tests` and `predict-affected-tests` skills are Claude Code skills. For Codex, Cursor and Gemini CLI, copy them into the client's skills folder:

```sh
python3 /path/to/xray-cloud-plugin/scripts/install_skills.py --client codex --scope project
```

`--client` is `codex`, `cursor` or `gemini`; `--scope` is `project` (in the current directory, or `--project DIR`) or `user` (your home directory). Codex targets `.agents/skills`, Cursor `.cursor/skills` and Gemini `.gemini/skills` (user scope: the same paths under `~`). The installer only copies this plugin's skills and never touches other skills; `--force` replaces a differing skill of the same name (the old one is kept if the copy fails). `--dry-run` shows what it would do.

### Codex

In `~/.codex/config.toml` (or a project's `.codex/config.toml`). `env_vars` forwards variables from your shell; set the ones you use.

```toml
[mcp_servers.xray]
command = "python3"
args = ["/path/to/xray-cloud-plugin/plugin/server/main.py"]
env_vars = ["XRAY_CLIENT_ID", "XRAY_CLIENT_SECRET", "XRAY_REGION", "JIRA_SITE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"]

[mcp_servers.xray.env]
XRAY_SETTINGS_FILE = "/path/to/your-project/.xray/settings.yml"
```

Codex may not start the server in your project directory, so set `cwd = "/path/to/your-project"` in the server table or `XRAY_PROJECT_DIR` in `[mcp_servers.xray.env]`; that is where `.xray/settings.yml` is looked up.

### Cursor

In `~/.cursor/mcp.json` or `.cursor/mcp.json`. Cursor expands `${env:NAME}`, `${userHome}` and `${workspaceFolder}`.

```json
{
  "mcpServers": {
    "xray": {
      "command": "python3",
      "args": ["/path/to/xray-cloud-plugin/plugin/server/main.py"],
      "env": {
        "XRAY_CLIENT_ID": "${env:XRAY_CLIENT_ID}",
        "XRAY_CLIENT_SECRET": "${env:XRAY_CLIENT_SECRET}",
        "XRAY_REGION": "${env:XRAY_REGION}",
        "XRAY_PROJECT_DIR": "${workspaceFolder}"
      }
    }
  }
}
```

### Gemini CLI

```sh
gemini extensions install https://github.com/<owner>/xray-cloud-plugin
```

The extension (`gemini-extension.json`) asks for the client ID, secret and the optional values when you install it, and keeps secrets in your system keychain. It sets `XRAY_PROJECT_DIR` to the workspace. The extension version is a placeholder (`0.0.0`); it is versioned by git commit, so update with `gemini extensions update xray-cloud`.

### VS Code

In `.vscode/mcp.json`. Inputs prompt once and store the value in VS Code's secret storage.

```json
{
  "inputs": [
    {"id": "xray-client-id", "type": "promptString", "description": "Xray client ID", "password": true},
    {"id": "xray-client-secret", "type": "promptString", "description": "Xray client secret", "password": true}
  ],
  "servers": {
    "xray": {
      "type": "stdio",
      "command": "python3",
      "args": ["/path/to/xray-cloud-plugin/plugin/server/main.py"],
      "env": {
        "XRAY_CLIENT_ID": "${input:xray-client-id}",
        "XRAY_CLIENT_SECRET": "${input:xray-client-secret}",
        "XRAY_PROJECT_DIR": "${workspaceFolder}"
      }
    }
  }
}
```

### Claude Desktop

In `claude_desktop_config.json` (Settings, Developer, Edit Config). Claude Desktop does not expand variables, so the credentials have to be set in this file's `env`; keep it out of version control, or prefer one of the clients above.

```json
{
  "mcpServers": {
    "xray": {
      "command": "python3",
      "args": ["/path/to/xray-cloud-plugin/plugin/server/main.py"],
      "env": {
        "XRAY_CLIENT_ID": "<your client id>",
        "XRAY_CLIENT_SECRET": "<your client secret>",
        "XRAY_PROJECT_DIR": "/path/to/your-project"
      }
    }
  }
}
```

### Any other stdio client

Run `python3 /path/to/xray-cloud-plugin/plugin/server/main.py` with these environment variables. A value that is entirely an unsubstituted `${...}` placeholder is ignored, so clients that leave unset variables unexpanded still work.

| Variable | Meaning | Claude Code prompt |
| --- | --- | --- |
| `XRAY_CLIENT_ID` | Xray Cloud API key client ID (required). | Xray client ID |
| `XRAY_CLIENT_SECRET` | Xray Cloud API key client secret (required). | Xray client secret |
| `XRAY_REGION` | `global` (default), `eu` or `au`. | Xray region |
| `XRAY_SETTINGS_FILE` | Explicit path to the settings file. | Settings file |
| `XRAY_READ_ONLY` | `true` makes every write tool refuse. | Read-only mode |
| `XRAY_PROJECT_DIR` | Project directory for `.xray/settings.yml` and relative paths. Falls back to `CLAUDE_PROJECT_DIR`, then the current directory. | none (Claude Code sets `CLAUDE_PROJECT_DIR`) |
| `XRAY_DATA_DIR` | Where remembered preferences live. Falls back to `CLAUDE_PLUGIN_DATA`, then `~/.config/xray-cloud`. | none (Claude Code sets `CLAUDE_PLUGIN_DATA`) |
| `XRAY_TOOLS` | Comma-separated tool groups to expose; empty or unset means all. | none |
| `JIRA_SITE_URL` | Jira site URL, enables Jira tools. | Jira site URL |
| `JIRA_EMAIL` | Email of the Jira account. | Jira email |
| `JIRA_API_TOKEN` | Jira API token. | Jira API token |

### Notes

- **Tool limits:** some clients cap how many tools a server may expose. Set `XRAY_TOOLS` to a comma-separated list of groups: `tests`, `containers`, `folders`, `import`, `analysis`, `jira`, `setup`. The `setup` group is always on. An unknown group stops the server with an error that lists the valid ones.
- **Preferences:** remembered import choices are stored in `preferences.json` inside `XRAY_DATA_DIR`. Without it, `~/.config/xray-cloud` is used (or `CLAUDE_PLUGIN_DATA` under Claude Code).
- **Secrets:** use the environment or the client's secret store (Gemini keychain, VS Code inputs, Cursor `${env:...}`), not committed files.

## Credentials

Create an API key in Jira: Apps, Xray, API Keys. Claude Code asks for the client ID and secret when you enable the plugin. Alternatively leave those prompts empty and set the `XRAY_CLIENT_ID` and `XRAY_CLIENT_SECRET` environment variables. Credentials are never exposed to the model and never stored in the settings file.

## Region

Pick the region of your Xray instance when enabling the plugin: `global` (US, the default), `eu` or `au`. It can also be set with `XRAY_REGION`.

## Settings

No setup is needed: install the plugin, add your credentials and start. A settings file is optional and is for teams (shared projects, field IDs and defaults, import choices). Use it when you want everyone on a repository to get the same choices.

### What gets asked

- **First write to a project.** If the project is not listed in a settings file and you have not approved it yet, you are asked once: "All projects" (every project on this Xray site, pre-selected), "This project only" or "No". The answer is remembered for your user and for that Xray site, so switching to a different Xray site asks again. Read-only mode still blocks every write.
- **Required Jira fields.** With the optional Jira token, required fields are discovered from Jira and asked once, then remembered per field; created items list `appliedDefaults`. Without the token, the plugin tries the create; when Jira refuses it for missing required fields, nothing is created and the plugin asks for those fields (by field id; give values in the shape Jira expects for custom fields, see [docs/field-ids.md](docs/field-ids.md)), then creates the item and remembers the answers. Results import (`import_execution_results`) and `import_feature_file` do not ask: they pass Jira's error through.
- **Who asks.** In clients that support MCP elicitation (see the table below) the client asks you directly with a form. In other clients the tool returns `needs_answer` and the agent asks you, then repeats the call with your answer. The agent never answers for you.
- **Reads** work on approved projects, on projects listed in a settings file, and on any project when there is no settings file (or it lists no projects).

| Client | Who asks | Basis |
|---|---|---|
| Claude Code | asks you directly in interactive sessions (headless runs fall back to the agent asking) | official docs describe an interactive elicitation dialog |
| Cursor | asks you directly | official docs list Elicitation as supported |
| VS Code (Copilot agent) | asks you directly | official docs: form and URL elicitation during a turn |
| Codex | unknown: the agent asks you | official docs do not say |
| Gemini CLI | unknown: the agent asks you | official docs do not say |

Sources and dates are in [docs/coverage.md](docs/coverage.md). If your client is not listed, or its behaviour changed, the question still reaches you through the agent.

### Where answers are remembered, and how to revoke

Approvals and remembered field values live in `preferences.json` (`${CLAUDE_PLUGIN_DATA}`, or `XRAY_DATA_DIR`, or `~/.config/xray-cloud`), per user and per Xray site. `get_status` shows them. To take them back, ask the agent to call `set_preferences` with `revokeProject` (one project) or `approveAll: false` (the "All projects" approval); `clearFieldValues` forgets remembered field values.

### The settings file

The settings file lists projects and their custom fields. It is YAML (JSON is also accepted). Projects are optional: projects listed here write without asking, and a file with no projects behaves like no file. The first file found wins; files are not merged:

1. the file chosen in the plugin's "Settings file" prompt (`XRAY_SETTINGS_FILE`); if that file does not exist, tools fail with an error instead of falling back to the other locations;
2. `.xray/settings.yml`, `.xray/settings.yaml` or `.xray/settings.json` in Claude Code's project directory, or the current directory when that is not set (per repository, so different repositories can target different projects);
3. `settings.yml`, `settings.yaml` or `settings.json` in `~/.config/xray-cloud/`.

Without a settings file the server works: reads are open, and writes ask as described above. The settings are re-read whenever the file changes. A complete example is in [`.xray/settings.example.yml`](.xray/settings.example.yml):

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

- `projects` (optional): projects listed here are written to without asking. Other projects ask once for approval (see above).
- Item types under `fields`: `test`, `testExecution`, `testPlan`, `testSet`, `precondition`, `testRun`.
- Field `type`: `option`, `options`, `string` (default), `number`, `version`, `versions`, `user`, `raw`.
- `required`: when true and there is no `default`, a create is refused before any API call until the value is given. `default` fills in the value when it is omitted.
- `id` is the Jira field id. Not sure how to find it? See [docs/field-ids.md](docs/field-ids.md).
- `testRun` fields are Xray test-run custom fields, not Jira fields: give the Xray field `name` (or an `id`).
- Escape hatch: create tools accept `rawFields` (`{"customfield_12345": <Jira JSON>}`), passed through untouched for fields not in the settings. `update_test_run` takes `rawFields` keyed by Xray run field name or id.
- `lifecycleLabels` (default shown above) are the labels `set_lifecycle_label` may apply. `duplicateCheck` turns the pre-create duplicate search on or off.
- `projects.<KEY>.testKeyTag` (optional): a scenario tag that names the scenario's test in result imports, besides `@TEST_<KEY>`. With `testKeyTag: issue`, `@issue=PROJ-12`, `@issue:PROJ-12` and `@issue_PROJ-12` all mean PROJ-12.

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

- **`import_execution_results`** uploads a result file (JUnit, TestNG, NUnit, Robot, Cucumber JSON, Behave JSON or Xray JSON) into a new or existing test execution (max 25 MB; give either `filePath` or `content`). It never lets tests be created without your consent (see Result matching below). Every key (`testExecKey`, `testPlanKey`, `testKeys`, and every key in the report) must belong to `projectKey`; an Xray JSON report gets `info.project` set when missing. Cucumber and Behave results for an existing execution, or with tests to create, are sent as Xray JSON (one status per test, failures as the run comment), because Xray's Cucumber and Behave imports always create a new execution and never create tests. New Cucumber/Behave executions go through the multipart endpoint so the project is set.
- **`import_feature_file`** uses Xray's native importer for a `.feature` file or a `.zip` of them. It is faster and a good fit for bulk loads, but it does no duplicate check, asks no questions, applies no tag rules, adds no AI label and has no preview.
- **`create_tests_from_feature`** is the plugin's own import (see Feature import below): duplicate check, import choices, tag rules, the AI label and a preview before anything is written. Use it for everyday imports; use `import_feature_file` when you want Xray's native result as is.
- **`export_cucumber_features`** writes Cucumber tests (by key or saved filter) as `.feature` files into a local directory and refuses to overwrite existing files unless you ask.

### Result matching

Before any upload, `import_execution_results` works out which test of the project each result goes to:

| Format | Matched by |
|---|---|
| Cucumber, Behave | a `@TEST_<KEY>` tag or the project's `testKeyTag` tag (if they name different tests, the tool asks), else the test whose summary is exactly the scenario name (trimmed, case-sensitive; outline rows use the outline's name). No tags needed |
| JUnit, TestNG, NUnit, Robot | Xray's own rule: the key the file names (JUnit property `test_key`, TestNG attribute `test`, NUnit property `Test`, Robot tag equal to a Test key), else the Generic test with the same definition (JUnit `classname.name`, TestNG `class.method`, NUnit `fullname`, Robot `Suite.Test`). Xray does not match these by summary |
| Xray JSON | `testKey`; an entry with only `testInfo` matches the Generic test with the same definition (Generic type only) |

When a result matches several tests (`ambiguous`) or none (`missing`), nothing is uploaded: the tool answers `needs_answer` with both lists (50 each at most, with counts). Call it again with:

- `testKeys: {name: KEY}` to send results to chosen tests (for XML formats the key is written into the file the way Xray reads it);
- `skipMissing: true` to leave unmatched results out;
- `allowCreateTests: true`, only with the user's consent, to let new tests be created: Xray creates a Generic test per JUnit/TestNG/NUnit/Robot case (summary = the case name), and a Cucumber test named after each unmatched scenario. They are listed in `createdTests` and get no AI label (label them afterwards with `edit_test` or `set_lifecycle_label`, or create labelled tests first with `create_tests_from_feature`).

Existing tests are never changed (except requirement links named in the file: JUnit `requirements`, Robot requirement-key tags and the like are kept, so Xray may link the test to those requirements). Xray would change them in these cases (verified live), so the tool prevents it:

- A Robot result renames the test it maps to (by key tag or by definition) to the Robot test's name, and adds its plain tags as labels. For each Robot test matched to an existing test, the uploaded copy (never your file) carries that test's key tag and its current summary as the name, and drops plain tags (key-shaped tags stay), so the test stays as it is.
- `testInfo` overwrites a test's summary and definition, even next to a `testKey`. Entries whose test is known are sent with `testKey` only.
- JUnit `test_summary` and `tags` properties overwrite the summary and labels of a test matched by key or definition. The tool removes them (and `test_description`; for TestNG and NUnit the similar names, as a precaution) from matched cases and says so in `warnings`. Cases for new tests keep them. TestNG and NUnit behaviour on matched tests was not verified live; their stripping is a precaution.
- Plain Cucumber/Behave tags (for example `@smoke`) stay in the uploaded copy; only the `@TEST_<KEY>` tag is added or replaced.

`testKeys` values and the keys in `@TEST_<KEY>` / `testKeyTag` tags must be existing Tests of the project. A Robot tag counts as a test key only when the issue is a Test (requirement links and tags such as `ISO-8601` are left alone); a Test of another project is refused. Names that occur more than once are told apart: `Feature: Scenario` for Cucumber/Behave, `summary (definition)` for Xray JSON. Tests created from Cucumber hold only the scenario's steps (no Background, no Examples); outline rows with filled-in names show up as missing, so map them with `testKeys`.

## Analysis

Read-only tools for questions about test history and impact:

- **`get_test_runs`**: the run history of one test, with counts by status, the last status and a flaky flag (both PASSED and FAILED among the runs).
- **`get_requirement_coverage`**: the tests that cover a requirement and the latest status of each, with counts and never-run tests.
- **`find_related_tests`**: tests related to an area by text, requirement keys, labels, components or folder; each test appears once with the criteria it matched (at most 200 results).

## Skills

Skills guide Claude through multi-step workflows on top of the tools:

- **`xray-setup`**: check your setup and, optionally, build a team settings file (field IDs, defaults, import choices, tag rules) from your answers.
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

To change a remembered choice, tell Claude "change my import preferences" (it calls `set_preferences`), or edit the settings file. Remembered answers live in `preferences.json` inside `XRAY_DATA_DIR` (which wins), else `${CLAUDE_PLUGIN_DATA}`, else `~/.config/xray-cloud`; they are per user and survive plugin updates.

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
- **Guided setup:** the `xray-setup` skill walks through all of this: it checks credentials, explains the first-use questions, and optionally builds a team settings file: discovers field IDs (Jira token, Atlassian MCP, or manual steps), offers import choices, the AI-created label and tag rules, shows the diff, asks before saving and ends with `get_status`.

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
