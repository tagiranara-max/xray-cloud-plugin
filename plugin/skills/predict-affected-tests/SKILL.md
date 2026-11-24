---
name: predict-affected-tests
description: Predict which Xray tests a code change affects. Use when the user gives a branch, diff or pull request and wants to know which tests to run, which are at risk, or wants a test execution created for the at-risk tests.
---

# Predict affected tests

Map a change to candidate Xray tests, judge each one from its steps and run history, and report a table. Evidence only: never invent a test key, and say UNCERTAIN when the evidence is thin.

## 1. Collect the change

Ask for the base branch (default: the repository's main branch) and anything else the user wants considered. Choose the Xray project key as described under "Choosing the project" below.

- Changed files: `git diff --name-only <base>...HEAD`. For a pull request, use its changed-files list or the diff the user gives.
- Issue keys: match `[A-Z][A-Z0-9]+-\d+` in the branch name and in commit messages (`git log <base>..HEAD --format=%s%n%b`). Deduplicate. The pattern also matches things that are not issue keys, such as `UTF-8` or `SHA-256`: keep only keys whose project prefix is a real Jira project you saw in the branch or commit context (for example the prefix of the branch name or of the ticket the user mentioned).

Show the user the files and keys you found, and let them correct either.

## 2. Gather candidates

1. Call `find_related_tests` once with:
   - `issueKeys`: the keys found;
   - `terms`: words taken from changed path segments and module names, at most 10, most specific first (skip generic ones such as `src`, `index` or `test`);
   - `labels` and `components`: only those the user names.

   At least one criterion is required. If `find_related_tests` errors on an issue key, drop that key and call it again. Each test appears once, with `matchedBy` listing why it matched, its `lastStatus` and the `truncated` flag. If `truncated` is true, say so: the list is incomplete.
2. For each issue key call `get_requirement_coverage`. It returns the covering `tests`, the `counts` (including `neverRun`) and `truncated`. A requirement with no covering tests is itself a finding: report it as a coverage gap.

Rank candidates: tests matched by an issue key first, then by number of criteria in `matchedBy`.

## 3. Analyse

- For the top 20 candidates call `get_test` with `detail: true` and read the steps. Judge whether the changed code is exercised by what the steps do.
- Call `get_test_runs` for those candidates (or fewer if time is short). Look at `summary.flaky`, `lastStatus`, `lastRunOn` and `byStatus`. Treat a test as higher risk when:
  - its last status is FAILED;
  - it is flaky (both PASSED and FAILED among recent runs);
  - it never ran (`lastStatus` null), or has not run for a long time.

## 4. Report

A table, one row per analysed test:

| Test | Verdict | Risk | Reason |
|---|---|---|---|

- Verdict is the expected outcome on this change: PASS, FAIL or UNCERTAIN. Use UNCERTAIN when the steps do not clearly touch the change, when there is no run history, or when the match was only by a loose term.
- Risk is high, medium or low: how much the change threatens the test, combined with how reliable its history is.
- The reason is one sentence citing the evidence: the matched criterion, the step, the run history.

Below the table, list candidates beyond the top 20 that you did not analyse, requirement coverage gaps, and any truncation.

If the Atlassian (Rovo) MCP tools are in your tool list, you may read the linked stories for context on intent. If they are not, work from Xray alone and do not mention them.

## 5. Optional: create an execution

Ask the user whether to create a test execution with the at-risk tests. Do nothing without a yes. Then call `create_test_execution` with `summary` (for example "Regression for <branch>"), `testKeys` (the high and medium risk tests, taken only from the results above) and optional `environments` (see `get_test_environments`). Report the new execution key.

## Choosing the project

Every call needs a project key. Take it, in this order:

1. from the user's words;
2. from keys in the input (a `@TEST_PROJ-12` tag, a story key, the branch name);
3. if only one project is approved or listed in the settings file, that one (`get_status` shows them);
4. otherwise ask the user. When working autonomously and you cannot ask, choose the most likely project and state the choice in your result.

The first write to a project that is not in a settings file and not yet approved asks the user once (see below).

## Questions from the tool (`needs_answer`)

When a tool returns `status: needs_answer`, nothing was done. Relay the question to the user in plain words and wait for their answer. Never answer it yourself, and never pass `allowProject`, `allowCreateTests` or any other answer on your own. Then call the tool again with the answer. Some clients show the question to the user directly (an approval prompt with "All projects" pre-selected); in that case just wait for the result.
