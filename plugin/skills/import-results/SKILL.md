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

- Cucumber and Behave scenarios: a `@TEST_<KEY>` tag, else the project's `testKeyTag` setting (for example `@issue=PROJ-12`), else the test whose summary is exactly the scenario name (Scenario Outline rows use the outline's name). Scenarios need no tags.
- JUnit, TestNG, NUnit and Robot cases: the way Xray does it. The key the file names (JUnit property `test_key`, TestNG attribute `test`, NUnit property `Test`, a Robot tag equal to the key), else the Generic test whose definition is the case's full name (for example `com.acme.LoginTest.validUser`). Xray does not match these by summary.
- Xray JSON entries without `testKey` (with `testInfo`): a Generic entry matches the Generic test with the same definition; others match nothing.

## 5. Answer the tool's questions

If the result has status `needs_answer`, nothing was uploaded. It lists `matched` (a count), `ambiguous` (results that match several tests, each with its `candidates`) and `missing` (results that match no test), with `ambiguousCount` and `missingCount` when the lists are cut at 50. Show the user both lists, then ask:

- for each ambiguous result: which candidate it belongs to. Pass the answers as `testKeys` (`{name: KEY}`, names exactly as listed).
- for the missing results: whether to map some of them to existing tests (`testKeys`), leave them out (`skipMissing: true`), or create new Test issues for them (`allowCreateTests: true`). For JUnit, TestNG, NUnit and Robot, Xray creates a Generic test per case; for Cucumber and Behave, a Cucumber test named after the scenario.

Pass `allowCreateTests: true` only after the user says yes to creating tests. Never pass it on your own. Then call the tool again with the same arguments plus the answers.

## 6. Summarise

Report the execution key and the counts in `byStatus` (for example PASSED, FAILED, TODO). Do not repeat the raw response unless asked; `detail` adds it. If `skipped` is present, say which results were left out.

If tests were created, `createdTests` lists them. They carry no AI label and no duplicate check was done for them. Tell the user, and offer to label them (`edit_test` with labels, or `set_lifecycle_label`) if they want. If the user wants new tests created with the AI label and a duplicate check instead, create them first with the `import-tests` skill or `create_tests_from_feature`, then import the results.

## 7. Failures

If there are failures, offer to check for flaky tests: call `get_test_runs` for each failed test key (ask which ones, or start with a few). A test is flaky when `flaky` is true, meaning both PASSED and FAILED appear in its recent runs. Report the flaky ones separately from consistent failures.
