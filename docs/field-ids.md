# Finding custom field IDs

The settings file needs the Jira id of each custom field (for example `customfield_12345`). Test run custom fields are the exception: they are matched by name.

## Without API access

- **Jira admin pages.** Jira settings, Issues, Custom fields. Open the field's actions menu and choose Edit (or Configure); the id is the number at the end of the URL, so `.../customfield/12345` means `customfield_12345`.
- **Browser developer tools.** Open the Create issue screen, inspect the field's input, and read its `id` or `name` attribute (for example `customfield_12345`). The Network tab shows the same ids in the create request.
- **Ask a Jira admin.** Ask for the id of the field on the Test, Test Execution, Test Plan or Test Set issue type in your project.
- **Test run custom fields.** These are configured in Xray, not Jira: project settings, Xray, Test Run Custom Fields. Put the field's exact name in `name:` under `testRun` in the settings file; no id is needed.

System fields use their plain id, for example `fixVersions` or `components`.

## With API access

- **Atlassian (Rovo) MCP.** Use the Atlassian MCP's issue-type metadata tool (find it via its `discover` tool) to get the create metadata of the issue type; it lists every field id, type and allowed values.
- **Optional Jira token.** Once the token is configured, call the `get_create_fields` tool with the project key and issue type (for example `PROJ` and `Test`).

Then add the ids to `.xray/settings.yml`; see `.xray/settings.example.yml`.
