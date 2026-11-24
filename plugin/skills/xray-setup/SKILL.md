---
name: xray-setup
description: Create or update the Xray settings file (.xray/settings.yml) with the user. Use when the plugin reports "No settings file found", when the user wants to add a project, field IDs, default field values, feature-import choices, the AI-created label or Gherkin tag rules, or asks to set up or configure the Xray plugin.
---

# Xray setup

Create or update the user's settings file together with the user. Never write it without showing the diff and getting a yes.

## 1. Find the settings file

Call `get_status`. It shows the settings file in use and the locations searched. The settings file is the user's own file, found in this order:

1. the path from the plugin prompt;
2. `.xray/settings.yml` in the project Claude Code is working in;
3. `~/.config/xray-cloud/settings.yml`.

The plugin ships no settings. A file committed in the team's own project makes the choices team-wide. If none exists, propose `.xray/settings.yml` in the current project (team-wide) or the home-folder path (personal), and start from `.xray/settings.example.yml` in the plugin repository as a template. Never put credentials in it.

## 2. Projects and fields

Ask for the Xray project keys (for example `PROJ`); the keys under `projects` are the allowlist. For each project, find which Jira fields are required on the Test, Test Execution, Test Plan, Test Set and Precondition issue types. Discover them in this order:

1. **Jira token configured** (`get_status` shows Jira as configured): call `get_create_fields` with the project key and each issue type. It lists field IDs, types, required flags and allowed values.
2. **Atlassian (Rovo) MCP tools in your tool list**: use its issue-type metadata (create-meta) tools for the same information.
3. **Neither**: give the user the step-by-step instructions in `docs/field-ids.md` of the plugin repository (Jira field admin page, the create screen, or asking an admin) and ask them to paste the IDs.

For each field to declare, write it under `projects.<KEY>.fields.<issue type>` with a friendly name, its `id` (for example `customfield_12345`), a `type` (option, options, string, number, version, versions, user or raw), `required`, and a `default` only when the user names one (for example `Backend`). A required field with no default is asked for on every create. Test run custom fields are matched by `name`, not id.

## 3. Optional choices

Offer each one; skip what the user declines.

- `featureImport`: `createTestSet`, `backgrounds` (preconditions or inline), `tagsAsLabels`, `unmatchedTags` (keep or skip) and `tagRules`. It can be set at the top level and overridden per project.
- `aiCreated`: the user's own label for what the AI creates (`label`, or null for none) and its `scope` (tests or all). There is no default label; never invent one.
- `tagRules`: if the user has `.feature` files, call `analyze_feature_tags` on them, explain each proposed rule in plain words with its effect on a few real scenarios, and add a rule only after the user agrees. No rules are built in.

## 4. Team-wide settings or personal preferences

Explain the difference. Values in the settings file are team-wide when the file is committed in the project, and a settings value always wins over a remembered one. Personal choices are remembered on the user's machine with `set_preferences` (per project, no file edit). Ask where each choice belongs and use the matching route.

## 5. Save

Show the full diff (or the whole new file) and ask before saving. Write the file only after an explicit yes, and keep any existing keys the user did not ask to change.

## 6. Verify

Call `get_status` again. Confirm that the file is found, the projects are listed and the effective choices show the expected sources. Report anything it flags.
