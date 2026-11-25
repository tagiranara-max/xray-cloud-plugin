---
name: xray-setup
description: Check the Xray plugin setup and, if the user wants, create or update the optional settings file (~/.config/xray-cloud/settings.yml, or the file XRAY_SETTINGS_FILE names). Use when the user asks to set up or configure the Xray plugin, check credentials, see what the plugin remembers, add field IDs or default field values, set feature-import choices, the AI-created label or Gherkin tag rules, or share project choices with a team.
---

# Xray setup

The plugin works without any setup: it only needs credentials. A settings file is optional and mainly for teams. This skill checks the credentials, explains what the plugin asks on first use, and, if the user wants, builds a settings file from the answers. Never write a file without showing the diff and getting a yes.

## 0. Check and explain

Call `get_status`: it shows whether credentials are configured, the region, the settings file in use (if any), the projects the user approved, and the field values the plugin remembers. If credentials are missing, point the user to the README section "Credentials".

Explain what happens on first use, so nothing comes as a surprise:

- Reads work on any project when there is no settings file (or it lists no projects), and on approved or listed projects otherwise.
- The first write to a project that is not listed in a settings file and not approved asks the user once: "all" (every project on this Xray site, pre-selected), "project" (only that one) or "no". Clients that support it show this question directly; otherwise the tool returns `needs_answer` and you relay it. The answer is remembered per user and per Xray site.
- Required Jira fields are asked once (from the live create-field metadata when a Jira token is configured, or when Jira reports a missing required field) and remembered per field; created items list `appliedDefaults`.
- To take an approval back: `set_preferences` with `revokeProject` (one project) or `approveAll: false`. To forget a remembered field value: `set_preferences` with `clearFieldValues`.

Then offer the settings file, and stop if the user declines it.

## 1. Find the settings file

`get_status` shows the settings file in use and the locations searched. The settings file is the user's own file, found in this order:

1. the path from the plugin prompt in Claude Code, or the `XRAY_SETTINGS_FILE` environment variable in any other client;
2. `~/.config/xray-cloud/settings.yml`.

The project folder is never searched. The plugin ships no settings. If none exists, propose `~/.config/xray-cloud/settings.yml` (personal, found with no further step), or, for a team, a file committed in the team's repository whose path every user then sets in `XRAY_SETTINGS_FILE` (Claude Code's "Settings file" prompt, Cursor's Configure, Gemini's extension setting, or the environment variable). Say that this extra step is needed; the server does not find a repository file on its own. Never put credentials in it. A minimal skeleton to start from:

```yaml
projects:                          # optional: projects here write without asking
  PROJ:
    fields:
      test:
        component:                 # friendly name used in `fields`
          id: customfield_12345    # Jira field id (or a system id such as fixVersions)
          type: option             # option | options | string | number | version | versions | user | raw
          required: true
          default: Backend
      testExecution:
        fixVersion:
          id: fixVersions
          type: versions
          required: true           # no default: asked on every create
      testRun:                     # Xray test-run custom fields, matched by name
        version:
          name: Version
          required: false
    # testKeyTag: issue            # optional: @issue=PROJ-12 names a scenario's test in result imports
```

The README of the plugin's repository has more detail, including every optional key.

## 2. Projects and fields

Ask for the Xray project keys (for example `PROJ`). Projects listed under `projects` are allowed to write without asking; projects left out are still usable after the user approves them, so the list is optional. For each project, find which Jira fields are required on the Test, Test Execution, Test Plan, Test Set and Precondition issue types. Discover them in this order:

1. **Jira token configured** (`get_status` shows Jira as configured): call `get_create_fields` with the project key and each issue type. It lists field IDs, types, required flags and allowed values.
2. **Atlassian (Rovo) MCP tools in your tool list**: use its issue-type metadata (create-meta) tools for the same information. They may be reached through the MCP's discover tool and its executeRead tool rather than listed directly.
3. **Neither**: walk the user through finding each field ID by hand, then ask them to paste the IDs:
   - Jira settings, Issues, Custom fields: open the field's actions menu and choose Edit or Configure; the number at the end of the URL is the ID (`.../customfield/12345` means `customfield_12345`).
   - While signed in to Jira, open `/rest/api/3/issue/<KEY>?expand=names` for an existing issue of that type in the browser: the JSON lists each field ID with its name. `/rest/api/3/field` lists every field.
   - On the Create issue screen, inspect the field's input and read its `id` or `name` attribute.
   - Ask a Jira admin for the ID (and allowed values) of the field on that issue type in the project.
   - System fields use their plain ID (`fixVersions`, `components`). Test run custom fields are configured in Xray (project settings, Test Run Custom Fields); use the exact field name under `testRun`, no ID needed.

For each field to declare, write it under `projects.<KEY>.fields.<issue type>` with a friendly name, its `id` (for example `customfield_12345`), a `type` (option, options, string, number, version, versions, user or raw), `required`, and a `default` only when the user names one (for example `Backend`). A required field with no default is asked for on every create. Test run custom fields are matched by `name`, not id.

## 3. Optional choices

Offer each one; skip what the user declines.

- `featureImport`: `createTestSet`, `backgrounds` (preconditions or inline), `tagsAsLabels`, `unmatchedTags` (keep or skip) and `tagRules`. It can be set at the top level and overridden per project.
- `aiCreated`: the user's own label for what the AI creates (`label`, or null for none) and its `scope` (tests or all). There is no default label; never invent one.
- `tagRules`: if the user has `.feature` files, call `analyze_feature_tags` on them (absolute paths), explain each proposed rule in plain words with its effect on a few real scenarios, and add a rule only after the user agrees. No rules are built in. Without a rule, `create_tests_from_feature` asks per key what a `key:value` tag means (`tagRule.<key>`); relay that question, never answer it.

## 4. Team-wide settings or personal preferences

Explain the difference. Values in the settings file are team-wide when the file is committed in the project, and a settings value always wins over a remembered one. Personal choices and approvals are remembered on the user's machine (the first-use questions, or `set_preferences` for import choices; no file edit). Ask where each choice belongs and use the matching route.

## 5. Save

Show the full diff (or the whole new file) and ask before saving. Write the file only after an explicit yes, and keep any existing keys the user did not ask to change.

## 6. Verify

Call `get_status` again. Confirm that the file is found, the projects are listed and the effective choices show the expected sources. Report anything it flags.

## Questions from the tool (`needs_answer`)

When a tool returns `status: needs_answer`, nothing was done. Relay the question to the user in plain words and wait for their answer. Never answer it yourself, and never pass `allowProject` on your own. Then call the tool again with the answer.
