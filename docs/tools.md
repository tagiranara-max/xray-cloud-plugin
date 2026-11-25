# Tools and workflows

The tools the server exposes, and how result import, analysis and feature import behave. Setup is in [configuration.md](configuration.md).

## Tools

| Tool | What it does |
|---|---|
| `search_tests` | Search tests in a project by summary text or issue key (query) and/or a JQL filter (jql) |
| `get_test` | Get one test by key |
| `create_test` | Create a test (Manual, Cucumber or Generic) in a project; a test with the same summary (trimmed, whitespace collapsed, any case) blocks it unless `allowDuplicates`, similar summaries are only listed |
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

## Skills

Skills guide Claude through multi-step workflows on top of the tools:

- **`xray-setup`**: check your setup and, optionally, build a team settings file (field IDs, defaults, import choices, tag rules) from your answers.
- **`import-results`**: import a test result file into Xray as a test execution and check for flaky tests.
- **`import-tests`**: import tests from `.feature` files and from tabular or markdown sources.
- **`predict-affected-tests`**: predict which tests a code change affects and optionally create an execution for the at-risk ones.

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

## Feature import

`create_tests_from_feature` follows Xray's own import semantics:

| Gherkin | Xray |
|---|---|
| Feature | Test Set (reused by summary, if you choose so; see Choices and preferences); feature tags go through the tag rules, plain tags become labels |
| Background (feature or rule level) | Pre-Condition (or inline steps, your choice) linked to every test it applies to; reused by `#@PRECOND_<KEY>` comment, then feature path label, then summary |
| Rule | Its scenarios import normally; its Background combines with the feature's |
| Scenario / Example | Cucumber test (Scenario) whose definition is its steps; its summary is the scenario name. Only when names collide within the file (for example "Sign in" under two Rules) the summary becomes `<Rule name>: <Scenario name>` (outside a Rule `<Feature name>: <Scenario name>`; still colliding: ` (2)`, ` (3)` ...). The summary of an existing test is never changed. Such names are also searched under the new summary, so a test imported earlier under the bare scenario name is not matched by name |
| Scenario Outline / Template | Cucumber test (Scenario Outline) with its steps and all Examples blocks |
| Plain tags | Labels |
| `@<KEY>` on a scenario | Looked up in Xray first, read-only, before anything is written (the Jira token is not needed). A test of the import's project: the scenario maps to that test (a non-Cucumber test leaves the scenario out, with a `notDone` entry). A test of another project: the scenario becomes a new test, with a `notDone` note. A requirement Xray knows: a requirement link. Anything else (Xray cannot tell an issue that is not a test or requirement from a key of no issue, such as `@ISO-8601`): asked, see `keyTag.<KEY>` below |
| `@<KEY>` on the Feature/Rule | Looked up the same way: a requirement Xray knows is a link for every test below it; a test is an ordinary tag; anything else is asked |
| `@TEST_<KEY>` on a scenario | Maps the scenario to that test when it is a test of the import's project; otherwise the scenario is left out, with the reason in `notDone` |
| `@REQ_<KEY>` | Links new tests to that issue. Links on existing tests need the Jira token, else they are in `notDone`. If Jira refuses a link Xray could not confirm (the issue may not exist), the test is still created without it and the link is listed in `notDone` |
| DocStrings and DataTables | Preserved exactly, with correct escaping |

By default (`apply=false`) the tool only previews what would be created, updated and linked. A scenario without a `@TEST_<KEY>` tag whose summary is exactly that of one existing Cucumber test of the project (compared trimmed, with whitespace runs collapsed, ignoring case) maps to that test: if the import would change it, it is part of the `existingTests` question (marked `matchedBy: name`, with `create` as an extra answer), otherwise it is only mapped. Several existing tests with that summary, or a non-Cucumber one, are possible duplicates: listed in `possibleDuplicates` and skipped on apply unless their index is in `createDuplicates`. Tests whose summaries are only similar are listed as `similar` and never cause a skip. `createDuplicates` indexes also answer `create` for tests matched by name. Call it again with `apply=true` to execute.

`summaries: {"<scenario index or name>": "New summary"}` renames tests the import creates (a name repeated in the file needs the index; a summary must be one line of at most 255 characters and differ from the other tests of the file). The preview shows the summaries (`perTest[].scenarioName` holds the scenario's own name when it differs); entries for scenarios that map to existing tests are ignored with a warning. A file that fails to parse creates nothing and reports the line and column. A tag never refuses the file: an unusable one (an invalid key, `@TEST_` on a Feature or Rule, `@PRECOND_` outside a Background comment, several test keys on one scenario, one test named by two scenarios) is ignored or leaves only its scenario out, with a `notDone` entry. If a lookup itself fails (an Xray error other than "no such issue"), the import stops before anything is written. `tagSummary.keyTags` in the preview shows what each issue-key tag turned out to be (`test`, `otherTest`, `requirement`, `plain`, `notImported`, `missingRequirement`), with a note.

### Choices and preferences

A few import choices are yours to make, and the plugin asks about them instead of guessing:

- `createTestSet`: turn the Feature into a Test Set?
- `backgrounds`: Backgrounds as Pre-Conditions (`preconditions`) or copied into each test (`inline`)?
- `tagsAsLabels`: turn plain tags (such as `@smoke`) into labels?
- `tagRule.<key>`: what a `key:value` or `key=value` tag means when no tag rule covers its key (see "Tag rules").
- `tagValue.<field>.<value>`: asked when a tag rule's `field` action would send a value the plugin cannot convert itself (see "Tag rules"): "What is `checkout` for Team?". Free text, nothing pre-selected: the id or the exact value Jira expects. Pass `tagValueAnswers: {"Team": {"checkout": "<id>"}}`. Remembered for the project (per field and tag value), for every rule.
- `keyTag.<KEY>`: asked for an issue-key tag Xray knows neither as a test nor as a requirement (for example `@ISO-8601`, a bug's key, or a key of no issue). The question says where the tag sits (feature, rule or scenario, with names). Answers: `link` (link the tests to that issue as a related/requirement issue; if Jira refuses the link, the test is created without it and the link is in `notDone`), `label` (keep the tag as a label, through `tagsAsLabels` and the tag rules) or `ignore`. Pass `keyTagAnswers: {"PROJ-12": "link" | "label" | "ignore"}`. Asked in the same round as the other questions and never remembered.
- `existingTests`: asked when a scenario maps to an existing test of the project (`@<KEY>`, `@TEST_<KEY>`, or the same summary as one existing test: `matchedBy: name`) and the import would change it. The question lists each such test with what would change (steps, with a short diff in `stepsDiff` {added, removed, lines}, labels added, fields, requirement links, Test Sets joined, Pre-Conditions, tags still awaiting a tag-rule answer); the preview has the same list in `existingTests`. Answers: `update` (change the test from the file), `keep` (the scenario only maps to the test; nothing is written to it, and it is not added to Test Sets or Pre-Conditions), `create` (tests matched by name only: create a new test after all; `createDuplicates` means the same) or `skip` (leave those scenarios out). `existingTestOverrides: {"PROJ-12": "skip"}` answers single tests; `create` is refused for a test named by a key tag. It is asked in the same round as the `tagRule.<key>` questions, is never remembered, and is not asked for a test the import would not change (that test is only mapped).
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

Tag rules turn Gherkin tags into labels, Test Set membership and field values. **There are no built-in conventions**: with no rules, plain tags simply become labels (`unmatchedTags: keep`, the default), and a `key:value` or `key=value` tag is never turned into anything silently: `create_tests_from_feature` asks once per key (question `tagRule.<key>`, both separators count as the same key) whether it is a label as written (`suite:regression`), a label with only the value (`regression`), a Test Set (name template, default `Suite {value}`), a Jira field (a `fields.test` name, a `customfield_<n>` id, or a system field: `priority`, `components`, `fixVersions`, `versions`, `labels`, `duedate`, `environment`, `assignee`, `reporter`; see "Field values" below) or ignored. Answer with `tagRuleAnswers: {"suite": {"action": "label_as_is" | "label_value" | "test_set" | "field" | "skip", "testSet": "...", "field": "..."}}`; the answer is remembered as a tag rule of the project, so later values of the key are not asked about. This applies to new and existing tests, and nothing is written until every such question is answered. With `unmatchedTags: skip` uncovered tags are dropped and nothing is asked. Issue-key tags (`@TEST_<KEY>`, `@REQ_<KEY>`, plain keys) are never subject to rules. The rules below are **examples only**, with made-up tag names; write your own, or ask Claude to run `analyze_feature_tags` on your files and propose rules (it explains each one and applies nothing until you agree, then stores them with `set_preferences`).

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
- The first matching rule wins. Project `tagRules` replace the top-level list; remembered rules (answers to `tagRule.<key>` questions or `set_preferences`) come after the settings rules; a settings `tagRules: []` does not switch them off.
- `tagsAsLabels` only concerns plain tags no rule covers: `false` drops them. A rule's `label`, `testSet` and `field` actions always apply.
- A rule that would produce an invalid label or a conflicting field value refuses the whole import before anything is written.
- Tags no rule covers appear in the preview's `tagSummary.unmatched`.
- A value the call passes for the same field (`fields` or `rawFields`) always wins over a rule's value.

#### Field values

A rule's `field` action sends the tag's value; how depends on the field:

- Text, number, date and `raw` fields: as written.
- Select lists (single or multi), priority, components and versions: matched by name, ignoring case, against Jira's allowed values when the Jira token gives them (`@sev=high` becomes the option `High`), and sent in the shape Jira expects (`{"value": ...}`, `{"name": ...}`, lists for multi-value fields). Without the token they are shaped by the field's type (system fields need no token).
- Anything the plugin cannot convert itself (an Atlassian Team, a user picker, a cascading select, a value no option has, or a `customfield_<n>` id whose type is unknown without the Jira token) is never guessed: the import asks once per field and value (`tagValue.<field>.<value>`), and the answer is remembered for the project (`tagValues` in `preferences.json`, per field and tag value), consulted by every rule, from the settings file or remembered. Later imports with the same value do not ask; a new value asks again.

```yaml
# EXAMPLE ONLY
featureImport:
  tagRules:
    - tag: team
      field: { customfield_12345: "{value}" }   # an Atlassian Team field
      values: { backend: "<team id>" }          # tag value -> id or exact value (keys ignore case)
```

`values` only goes with `field` and wins over a remembered answer; a value may also be a JSON object or list in the shape Jira expects. The plugin never edits your settings file.

### notDone

Some things cannot be done without Jira: labels, fields or requirement links on existing tests, when there is no Jira token. A field that already has a different value is never overwritten either (an equal value is left alone silently), and a field whose current value could not be read is never written. The import still does everything else and lists the rest in `notDone: [{item, what, reason, fix}]`. When the reason says Jira access is missing ("no Jira token or Atlassian MCP") or not configured ("Jira access not configured: ... missing", when only some of the site URL, email and token are set), Claude can complete the items with the Atlassian MCP if it is connected, or you can set the Jira token (see "Optional Jira token" in [configuration.md](configuration.md#optional-jira-token)). Tools that need the Jira token for the whole operation (`edit_test` Jira fields, `set_lifecycle_label`) return an error with the same `reason` and `fix`.
