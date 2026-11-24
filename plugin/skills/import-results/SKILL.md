---
name: import-results
description: Import automated test results into Xray from a result file (JUnit, TestNG, NUnit, Robot, Cucumber JSON, Behave JSON or Xray JSON). Use when the user wants to upload, import or publish test results, create a test execution from a report file, or check for flaky tests after an import.
---

# Import results

A guide over `import_execution_results`. Collect everything first, then call the tool. Call it again only to answer its questions (step 5).

## 1. Find the file and detect the format

Ask for the result file path if not given. Read the start of the file to choose `format`:

- JUnit (`junit`): root `<testsuites>` or `<testsuite>`.
- TestNG (`testng`): root `<testng-results>`.
- NUnit (`nunit`): root `<test-run>` (NUnit 3) or `<test-results>` (NUnit 2).
- Robot (`robot`): root `<robot`.
- Cucumber JSON (`cucumber`): an array of features, each with `elements`.
- Behave JSON (`behave`): the same shape as Cucumber. If you cannot tell which it is, ask the user.
- Xray JSON (`xray`): an object with a `tests` array.

If the format is unclear, ask. Pass the file with `filePath` (25 MB maximum), or `content` when the user pastes it; give exactly one (an empty file or empty content is refused).

## 2. Choose the target

Ask which one:

- an existing test execution: pass `testExecKey`. Cucumber and Behave results for an existing execution are sent as Xray JSON, one status per test (step details are not kept; failures become the run comment), because Xray's Cucumber and Behave imports always create a new execution. An Xray JSON report's own `testExecutionKey` also targets that execution; every key in an Xray JSON report must belong to the project, or the import is refused;
- a test plan: pass `testPlanKey` (any format);
- a new test execution: omit `testExecKey`, and ask for a `summary`. The project's `testExecution` fields from the settings file apply. Call `get_create_fields` with issue type `Test Execution` to see which are required. A required field with no default must be given in `fields` (friendly names from the settings file) or `rawFields`; the import is refused before any upload if one is missing.

## 3. Environments

Ask whether the results belong to a test environment. Pass names in `testEnvironments` (non-empty, no `;`). The tool does not check the names against Xray, and Xray handles a name it does not know on its own, so use existing names from `get_test_environments` to avoid typos.

## 4. Import

Call `import_execution_results` with the project key, format, file and target. Before uploading anything, the tool matches every result to a test of the project:

- Cucumber and Behave scenarios: a `@TEST_<KEY>` tag or the project's `testKeyTag` tag (for example `@issue=PROJ-12`); if they name different tests, the tool asks. Else the test whose summary is exactly the scenario name (Scenario Outline rows use the outline's name). Scenarios need no tags.
- JUnit, TestNG, NUnit and Robot cases: the way Xray does it. The key the file names (JUnit property `test_key`, TestNG attribute `test`, NUnit property `Test`, a Robot tag equal to the key), else the Generic test whose definition is the case's full name (for example `com.acme.LoginTest.validUser`). Xray does not match these by summary.
- Xray JSON entries without `testKey` (with `testInfo`): a Generic entry matches the Generic test with the same definition; others match nothing.

## 5. Answer the tool's questions

If the result has status `needs_answer`, nothing was uploaded. It lists `matched` (a count), `ambiguous` (results that match several tests, each with its `candidates`) and `missing` (results that match no test), with `ambiguousCount` and `missingCount` when the lists are cut at 50. Show the user both lists, then ask:

- for each ambiguous result: which candidate it belongs to. Pass the answers as `testKeys` (`{name: KEY}`, names exactly as listed).
- for the missing results: whether to map some of them to existing tests (`testKeys`), leave them out (`skipMissing: true`), or create new Test issues for them (`allowCreateTests: true`). For JUnit, TestNG, NUnit and Robot, Xray creates a Generic test per case; for Cucumber and Behave, a Cucumber test named after the scenario.

Pass `allowCreateTests: true` only after the user says yes to creating tests. Never pass it on your own. Then call the tool again with the same arguments plus the answers.

Before the user answers about Cucumber or Behave, tell them what a created test holds: only the scenario's own steps, with no Background and no Examples table. Scenario Outline rows whose names have their values filled in (for example `Login as admin` from `Login as <role>`) do not match the outline's test, so they show up as missing. Map those rows to the outline's test with `testKeys` rather than creating one test per row.

Result names are unique per result: when two features have a scenario with the same name, it is listed as `Feature: Scenario`; Xray JSON entries with the same summary are listed as `summary (definition)`. Use the names exactly as listed.

`testKeys` values must be existing Tests of the project; a story or other issue key is refused.

When two scenarios with the same name in different features both match no test, creating tests makes one Cucumber test for both (named after the scenario). Tell the user, and map one of them with `testKeys` if they should be separate tests.

For Robot results, Xray would rename a matched test to the Robot test's name. The tool always prevents that: for each Robot test matched to an existing test, the uploaded copy uses the test's key tag and its current summary as the name. The user's file is not changed. Jira tests are never renamed.

Existing tests are otherwise left as they are, except for one thing: links to requirements named in the file (JUnit `requirements`, Robot requirement-key tags and the like) are kept, so the import can link a test to those requirements. Otherwise the tool removes `testInfo` from Xray JSON entries that name a test, summary, description and label properties (for example JUnit `test_summary` and `tags`) from cases matched to existing tests, and plain Robot tags (which Xray would add as labels) from Robot tests matched to existing tests; the result's `warnings` say so.

## 6. Summarise

Report the execution key and the counts in `byStatus` (for example PASSED, FAILED, TODO). Do not repeat the raw response unless asked; `detail` adds it. If `skipped` is present, say which results were left out.

If tests were created, `createdTests` lists them. They carry no AI label and no duplicate check was done for them. Tell the user, and offer to label them (`edit_test` with labels, or `set_lifecycle_label`) if they want. If the user wants new tests created with the AI label and a duplicate check instead, create them first with the `import-tests` skill or `create_tests_from_feature`, then import the results.

## 7. Failures

If there are failures, offer to check for flaky tests: call `get_test_runs` for each failed test key (ask which ones, or start with a few). A test is flaky when `flaky` is true, meaning both PASSED and FAILED appear in its recent runs. Report the flaky ones separately from consistent failures.

## Choosing the project

Every call needs a project key. Take it, in this order:

1. from the user's words;
2. from keys in the input (a `@TEST_PROJ-12` tag, a story key, the branch name);
3. if only one project is approved or listed in the settings file, that one (`get_status` shows them);
4. otherwise ask the user. When working autonomously and you cannot ask, choose the most likely project and state the choice in your result.

The first write to a project that is not in a settings file and not yet approved asks the user once (see below).

## Questions from the tool (`needs_answer`)

When a tool returns `status: needs_answer`, nothing was done. Relay the question to the user in plain words and wait for their answer. Never answer it yourself, and never pass `allowProject`, `allowCreateTests` or any other answer on your own. Then call the tool again with the answer. Some clients show the question to the user directly (an approval prompt with "All projects" pre-selected); in that case just wait for the result.
