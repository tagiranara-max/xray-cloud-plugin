---
name: import-tests
description: Create Xray tests from a .feature file, CSV, spreadsheet export, Markdown or test code. Use when the user wants to import, bulk-create or migrate test cases into Xray, turn a document or test code into Xray tests, or add the imported tests to a test set or plan.
---

# Import tests

Parse the source, show a preview, get sign-off, then create. Nothing is written to Xray before the user approves.

## 1. Find the source and the project

Ask for the file path (or pasted content) if not given. Pass file paths to the tools as absolute paths: some clients (Codex, for example) start the server outside the project, and it then refuses relative paths (`get_status` shows `projectDir`). Every call needs `projectKey`; choose it as described under "Choosing the project" below.

## 2. `.feature` files: use `create_tests_from_feature`

Do not parse Gherkin yourself.

1. Call `create_tests_from_feature` with `featureFilePath` and the default `apply: false`. This is a preview and writes nothing. It returns the planned actions, `perTest` details, `testSets`, `notDone` and `choices`.
2. If the preview has non-empty `questions`, ask the user each one (the Test Set, Backgrounds as Pre-Conditions or inline, tags as labels, the AI label, and its scope). Ask once, then call again with the answers (`createTestSet`, `backgrounds`, `tagsAsLabels`, `aiLabel`, `aiLabelScope`). Answers are remembered per project, so later imports do not ask again. `aiLabel: ""` means do not mark. To change a remembered answer for good, pass `remember: true`.
   - A `keyTag.<KEY>` question asks about a tag that looks like an issue key (for example `@ISO-8601`) but that Xray doesn't know as a test or a requirement. Relay where it sits and the three choices: link it as a related/requirement issue, keep it as a label, or ignore it. Never pick one yourself. Pass `keyTagAnswers: {"<KEY>": "link" | "label" | "ignore"}`. It is not remembered. If Jira later refuses a link, the test is still created and the link is listed in `notDone`.
   - An `existingTests` question lists existing tests the scenarios map to (by a key tag, or `matchedBy: name`: an existing test has exactly the scenario's summary) and what the import would change on each (steps with a short diff, labels, fields, links, Test Sets, Pre-Conditions). Relay the list and the choices: update them from the file, keep them unchanged (the scenario only maps to the test), create a new test instead (offered only for tests matched by name), or skip those scenarios. Never pick one yourself. Pass `existingTests: "update" | "keep" | "create" | "skip"`, and `existingTestOverrides: {"<KEY>": ...}` when the user wants a different answer for single tests. It is not remembered: each import asks again when something would change.
   - A `tagRule.<key>` question asks what a `key:value` / `key=value` tag (for example `@suite:regression`) means when no tag rule covers it. Relay it with its examples and the five choices: label as is (`suite:regression`), label with only the value (`regression`), a Test Set (offer the suggested name, e.g. `Suite {value}`, or the user's own template), a Jira field (the user names it), or ignore. Never pick one yourself and do not suggest a default. A field may be a `fields.test` name, a `customfield_<n>` id or a system field (`priority`, `components`, `fixVersions` ...). Pass the answers as `tagRuleAnswers: {"<key>": {"action": "label_as_is" | "label_value" | "test_set" | "field" | "skip", "testSet": "...", "field": "..."}}`; each becomes a remembered tag rule of the project.
   - A `tagValue.<field>.<value>` question ("What is `checkout` for Team?") asks what to send to a field for a tag value the plugin cannot convert itself (a Team, a user, a field whose type is unknown without the Jira token). Relay it as a free-text question; offer no value and never look one up or guess it yourself. Pass `tagValueAnswers: {"<field>": {"<value>": "<the user's answer>"}}`. It is remembered in the tag rule, so the same value is not asked again. It may follow right after a `tagRule.<key>` answered with a Jira field.
3. Show the preview: tests to create or update, what each issue-key tag turned out to be (`tagSummary.keyTags`: a test of this project, a test of another project, a requirement, or a plain tag such as `@ISO-8601`), scenarios left out (`skip_test` actions, with their reason), Test Sets, Pre-Conditions, and possible duplicates (several existing tests, or a non-Cucumber one, with the same summary). `similar` lists tests with similar summaries for information only; they never cause a skip. Scenarios with possible duplicates are skipped unless their index is listed in `createDuplicates`; ask the user which, if any, to create anyway. Scenario names repeated in the file become `<Rule name>: <Scenario name>` (or `<Feature name>: ...`); point these out. Only if the user asks to rename tests the import creates, pass `summaries: {"<scenario index or name>": "<new summary>"}`; existing tests are never renamed (such entries come back as a warning).
4. After sign-off, call again with `apply: true` and the same answers. `apply: true` is refused while any question is unanswered, `keyTag.<KEY>`, `tagValue.<field>.<value>` and `existingTests` included (for these and `tagRule.<key>` the client may show a form instead, or the result is `status: needs_answer` with the same questions: relay them the same way).
5. Report what was created. List every `notDone` item with its reason and fix; do not hide them.

## 3. Other sources: CSV, spreadsheet export, Markdown, test code

### Parse into a preview table

Read the file and extract one candidate test per case. For each, work out:

- summary (required; for test code, derive it from the test name or description);
- type: `Manual` when there are steps, `Cucumber` when there is Gherkin, otherwise `Generic`;
- steps: action, optional data, optional expected result (action is required for each step);
- the number of steps.

Ask the user to map columns when a CSV or spreadsheet is ambiguous. Do not guess at columns that carry meaning (priority, labels, requirement links). Say what you ignored.

### Flag duplicates

For each summary call `search_tests` with `query` set to the summary. Mark a row as a possible duplicate only when an existing test has the same summary (ignoring case, surrounding spaces and repeated spaces), and show the existing key. Mention tests with merely similar summaries, but do not flag them. `create_test` applies the same rule: only a same-summary test returns `possible_duplicates`; similar ones come back in `similar`. Never invent keys.

Show the table: number, summary, type, steps count, duplicate flag. Ask the user to approve it, and to say which duplicates to create anyway and which to skip.

### Create in batches of 10

After sign-off, call `create_test` once per test, in batches of 10. Pass `summary`, `steps` (Manual) or `gherkin` (Cucumber) (never both), and `testType` if not implied. For a flagged duplicate that the user wants anyway, pass `allowDuplicates: true`. Without it, a duplicate returns `possible_duplicates` and nothing is created.

- After each batch, report progress, such as "20 of 45 done", with the keys created.
- Report each failure with the summary and the error, then continue with the rest unless the user says to stop. Do not retry silently.
- Fields the project requires with no default, such as `customfield_12345`, can be given in `fields`. If the first call fails for that reason, ask the user once and reuse the value for the batch.

### The AI label (`needs_answer`)

When `create_test` returns `status: needs_answer` with `questions`, nothing was created. Ask the user once whether to mark AI-created tests with a label of their own (for example `ai-created`), or not to mark. Call again with `aiLabel` set (`""` for none). The answer is remembered for the project, so ask only once for the whole import. Do not make up a label.

## 4. Optional: add the tests to a set or plan

Ask whether the created tests should go into a container.

- A new Test Set: call `create_test_set` with `summary` and `testKeys` (the created keys).
- An existing Test Set: call `add_tests_to_test_set` with `testSetKey` and `testKeys`.
- A Test Plan: `create_test_plan` or `add_tests_to_test_plan` in the same way, with `testPlanKey`.

Finish with a short summary: created, skipped as duplicates, failed, and where the tests were added.

## Choosing the project

Every call needs a project key. Take it, in this order:

1. from the user's words;
2. from keys in the input (a `@TEST_PROJ-12` tag, a story key, the branch name);
3. if exactly one specific project is approved or listed in the settings file, that one (`get_status` shows them; with approve-all and no specific project, go on to the next step);
4. otherwise ask the user. When working autonomously and you cannot ask, choose the most likely project and state the choice in your result.

The first write to a project that is not in a settings file and not yet approved asks the user once (see below).

## Questions from the tool (`needs_answer`)

When a tool returns `status: needs_answer`, nothing was done. Relay the question to the user in plain words and wait for their answer. Never answer it yourself, and never pass `allowProject` or any other answer on your own. Then call the tool again with the answer. Some clients show the question to the user directly (an approval prompt with "All projects" pre-selected); in that case just wait for the result.
