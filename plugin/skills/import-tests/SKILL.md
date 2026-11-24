---
name: import-tests
description: Create Xray tests from a .feature file, CSV, spreadsheet export, Markdown or test code. Use when the user wants to import, bulk-create or migrate test cases into Xray, turn a document or test code into Xray tests, or add the imported tests to a test set or plan.
---

# Import tests

Parse the source, show a preview, get sign-off, then create. Nothing is written to Xray before the user approves.

## 1. Find the source and the project

Ask for the file path (or pasted content) and the Xray project key (for example `PROJ`) if not given. Every call needs `projectKey`.

## 2. `.feature` files: use `create_tests_from_feature`

Do not parse Gherkin yourself.

1. Call `create_tests_from_feature` with `featureFilePath` and the default `apply: false`. This is a preview and writes nothing. It returns the planned actions, `perTest` details, `testSets`, `notDone` and `choices`.
2. If the preview has non-empty `questions`, ask the user each one (the Test Set, Backgrounds as Pre-Conditions or inline, tags as labels, the AI label, and its scope). Ask once, then call again with the answers (`createTestSet`, `backgrounds`, `tagsAsLabels`, `aiLabel`, `aiLabelScope`). Answers are remembered per project, so later imports do not ask again. `aiLabel: ""` means do not mark. To change a remembered answer for good, pass `remember: true`.
3. Show the preview: tests to create or update, Test Sets, Pre-Conditions, and possible duplicates. Scenarios with possible duplicates are skipped unless their index is listed in `createDuplicates`; ask the user which, if any, to create anyway.
4. After sign-off, call again with `apply: true` and the same answers. `apply: true` is refused while any question is unanswered.
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

For each summary call `search_tests` with `query` set to the summary. Mark a row as a possible duplicate when an existing test has the same or a near-identical summary, and show the existing key. Never invent keys.

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
