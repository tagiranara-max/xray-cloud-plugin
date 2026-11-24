---
name: import-results
description: Import automated test results into Xray from a result file (JUnit, TestNG, NUnit, Robot, Cucumber JSON, Behave JSON or Xray JSON). Use when the user wants to upload, import or publish test results, create a test execution from a report file, or check for flaky tests after an import.
---

# Import results

A guide over `import_execution_results`. Collect everything first; call the tool once.

## 1. Find the file and detect the format

Ask for the result file path if not given. Read the start of the file to choose `format`:

- JUnit (`junit`): root `<testsuites>` or `<testsuite>`.
- TestNG (`testng`): root `<testng-results>`.
- NUnit (`nunit`): root `<test-run>` (NUnit 3) or `<test-results>` (NUnit 2).
- Robot (`robot`): root `<robot`.
- Cucumber JSON (`cucumber`): an array of features, each with `elements`.
- Behave JSON (`behave`): the same shape as Cucumber. If you cannot tell which it is, ask the user.
- Xray JSON (`xray`): an object with a `tests` array.

If the format is unclear, ask. Pass the file with `filePath` (sent unchanged, 25 MB maximum), or `content` when the user pastes it; give exactly one.

## 2. Choose the target

Ask which one:

- an existing test execution: pass `testExecKey`;
- a test plan: pass `testPlanKey` (XML formats; JSON formats only together with a new execution created with `fields`);
- a new test execution: omit `testExecKey`, and ask for a `summary`. The project's `testExecution` fields from the settings file apply. Call `get_create_fields` with issue type `Test Execution` to see which are required. A required field with no default must be given in `fields` (friendly names from the settings file) or `rawFields`; the import is refused before any upload if one is missing.

## 3. Environments

Ask whether the results belong to a test environment. Pass names in `testEnvironments`. They must be known environments; the tool's own errors say if not.

## 4. Import and summarise

Call `import_execution_results` with the project key, format, file and target. Report the execution key and the counts in `byStatus` (for example PASSED, FAILED, TODO). Do not repeat the raw response unless asked; `detail` adds it.

## 5. Failures

If there are failures, offer to check for flaky tests: call `get_test_runs` for each failed test key (ask which ones, or start with a few). A test is flaky when `flaky` is true, meaning both PASSED and FAILED appear in its recent runs. Report the flaky ones separately from consistent failures.
