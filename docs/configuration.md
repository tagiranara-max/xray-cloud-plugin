# Configuration

Everything beyond the quick start in the [README](../README.md): credentials, environment variables, manual setup for MCP clients, the optional settings file, the Jira token and read-only mode.

## Requirements

- `python3` 3.9 or newer on your PATH.
- macOS or Linux. Windows is untested.
- An Xray Cloud API key (server/data center Xray is not supported).

## Credentials

Create an API key in Jira: Apps, Xray, API Keys. Claude Code, Gemini CLI and Cursor (through the plugin's variables) ask for the client ID and secret when you install or configure the plugin. Codex cannot ask for a local server's settings, so the Codex plugin forwards `XRAY_CLIENT_ID`, `XRAY_CLIENT_SECRET` and the other variables below from the environment Codex starts in. In Claude Code you may also leave the prompts empty and set the environment variables instead. Credentials are never exposed to the model and never stored in the settings file.

## Region

Pick the region of your Xray instance: `global` (US, the default), `eu` or `au`. Claude Code, Gemini CLI and Cursor ask for it; elsewhere set `XRAY_REGION`.

## Other MCP clients (manual setup)

The server speaks plain MCP over stdio, so it also works without a plugin system. Codex, Cursor and Gemini CLI can install it as a plugin or extension (see the [README](../README.md#install)); use the manual setups below when you prefer to wire the server yourself, or for VS Code, Claude Desktop and other clients. It needs Python 3.9 or newer. In the examples below `/path/to/xray-cloud-plugin` is your clone of this repository (`git clone https://github.com/tagiranara-max/xray-cloud-plugin`). On Windows the command is usually `python` or `py` instead of `python3`, and paths use backslashes (escape them in JSON and double-quoted TOML).

Put credentials in your environment or your client's secret mechanism, never in a committed config file. The examples only reference variables.

The `xray-setup`, `import-results`, `import-tests` and `predict-affected-tests` skills come with the Claude Code, Codex and Cursor plugins. With a manual setup, copy them into the client's skills folder:

```sh
python3 /path/to/xray-cloud-plugin/scripts/install_skills.py --client codex --scope project
```

`--client` is `codex`, `cursor` or `gemini`; `--scope` is `project` (in the current directory, or `--project DIR`) or `user` (your home directory). Codex targets `.agents/skills`, Cursor `.cursor/skills` and Gemini `.gemini/skills` (user scope: the same paths under `~`). The installer only copies this plugin's skills and never touches other skills; `--force` replaces a differing skill of the same name (the old one is kept if the copy fails). `--dry-run` shows what it would do.

### Codex (manual)

In `~/.codex/config.toml` (or a project's `.codex/config.toml`). `env_vars` forwards variables from your shell; set the ones you use.

```toml
[mcp_servers.xray]
command = "python3"
args = ["/path/to/xray-cloud-plugin/plugin/server/main.py"]
env_vars = ["XRAY_CLIENT_ID", "XRAY_CLIENT_SECRET", "XRAY_REGION", "JIRA_SITE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"]

[mcp_servers.xray.env]
XRAY_SETTINGS_FILE = "/path/to/xray-settings.yml"
```

Codex may not start the server in your project directory, so relative file paths may not resolve: give the agent absolute paths, or set `cwd = "/path/to/your-project"` in the server table (or `XRAY_PROJECT_DIR` in `[mcp_servers.xray.env]`).

### Cursor (manual)

In `~/.cursor/mcp.json` or `.cursor/mcp.json`. Cursor expands `${env:NAME}`, `${userHome}` and `${workspaceFolder}`.

```json
{
  "mcpServers": {
    "xray": {
      "command": "python3",
      "args": ["/path/to/xray-cloud-plugin/plugin/server/main.py"],
      "env": {
        "XRAY_CLIENT_ID": "${env:XRAY_CLIENT_ID}",
        "XRAY_CLIENT_SECRET": "${env:XRAY_CLIENT_SECRET}",
        "XRAY_REGION": "${env:XRAY_REGION}",
        "XRAY_PROJECT_DIR": "${workspaceFolder}"
      }
    }
  }
}
```

### VS Code

In `.vscode/mcp.json`. Inputs prompt once and store the value in VS Code's secret storage.

```json
{
  "inputs": [
    {"id": "xray-client-id", "type": "promptString", "description": "Xray client ID", "password": true},
    {"id": "xray-client-secret", "type": "promptString", "description": "Xray client secret", "password": true}
  ],
  "servers": {
    "xray": {
      "type": "stdio",
      "command": "python3",
      "args": ["/path/to/xray-cloud-plugin/plugin/server/main.py"],
      "env": {
        "XRAY_CLIENT_ID": "${input:xray-client-id}",
        "XRAY_CLIENT_SECRET": "${input:xray-client-secret}",
        "XRAY_PROJECT_DIR": "${workspaceFolder}"
      }
    }
  }
}
```

### Claude Desktop

In `claude_desktop_config.json` (Settings, Developer, Edit Config). Claude Desktop does not expand variables, so the credentials have to be set in this file's `env`; keep it out of version control, or prefer one of the clients above.

```json
{
  "mcpServers": {
    "xray": {
      "command": "python3",
      "args": ["/path/to/xray-cloud-plugin/plugin/server/main.py"],
      "env": {
        "XRAY_CLIENT_ID": "<your client id>",
        "XRAY_CLIENT_SECRET": "<your client secret>",
        "XRAY_PROJECT_DIR": "/path/to/your-project"
      }
    }
  }
}
```

### Any other stdio client

Run `python3 /path/to/xray-cloud-plugin/plugin/server/main.py` with these environment variables. A value that is entirely an unsubstituted `${...}` placeholder is ignored, so clients that leave unset variables unexpanded still work.

Claude Code and Cursor hand their plugin prompts to the server as `NAME_SETTING` (for example `XRAY_CLIENT_ID_SETTING`). A non-empty prompt takes precedence over the environment variable `NAME`; an empty prompt falls back to the environment variable, so credentials exported in the shell that starts Claude Code keep working when you leave the prompts blank.

| Variable | Meaning | Claude Code prompt |
| --- | --- | --- |
| `XRAY_CLIENT_ID` | Xray Cloud API key client ID (required). | Xray client ID |
| `XRAY_CLIENT_SECRET` | Xray Cloud API key client secret (required). | Xray client secret |
| `XRAY_REGION` | `global` (default), `eu` or `au`. | Xray region |
| `XRAY_SETTINGS_FILE` | Explicit path to the settings file. | Settings file |
| `XRAY_READ_ONLY` | `true` makes every write tool refuse. | Read-only mode |
| `XRAY_PROJECT_DIR` | Project directory that relative file paths resolve against. Falls back to `CLAUDE_PROJECT_DIR`, then the current directory. | none (Claude Code sets `CLAUDE_PROJECT_DIR`) |
| `XRAY_DATA_DIR` | Where remembered preferences live. Falls back to `CLAUDE_PLUGIN_DATA`, then `~/.config/xray-cloud`. | none (Claude Code sets `CLAUDE_PLUGIN_DATA`) |
| `XRAY_TOOLS` | Comma-separated tool groups to expose; empty or unset means all. | none |
| `JIRA_SITE_URL` | Jira site URL, enables Jira tools. | Jira site URL |
| `JIRA_EMAIL` | Email of the Jira account. | Jira email |
| `JIRA_API_TOKEN` | Jira API token. | Jira API token |

### Notes

- **Tool limits:** some clients cap how many tools a server may expose. Set `XRAY_TOOLS` to a comma-separated list of groups: `tests`, `containers`, `folders`, `import`, `analysis`, `jira`, `setup`. The `setup` group is always on. An unknown group stops the server with an error that lists the valid ones.
- **Preferences:** remembered import choices are stored in `preferences.json` inside `XRAY_DATA_DIR`. Without it, `~/.config/xray-cloud` is used (or `CLAUDE_PLUGIN_DATA` under Claude Code).
- **Secrets:** use the environment or the client's secret store (Gemini keychain, VS Code inputs, Cursor `${env:...}`), not committed files.

## Codex and Cursor plugin notes

- **Codex** starts the server with the plugin folder as its working directory and passes on only the variables the plugin lists (`env_vars` in `plugin/codex-mcp.json`: the variables in the table above). Export the credentials in your shell profile (for example `~/.zshrc`); the ChatGPT desktop app needs them too (see the README). Because the working directory is the plugin folder, the server does not know your project: it refuses relative file paths, and `get_status` reports `projectDir.known: false`. Give the agent absolute paths, or set the project per launch, not in your profile: `XRAY_PROJECT_DIR="$PWD" codex`.
- **Cursor** substitutes the plugin's variables (`${XRAY_CLIENT_ID}` and the others in `plugin/mcp.json`) with the values set under Plugins, Configure. An optional variable left empty is ignored by the server. The plugin sets `XRAY_PROJECT_DIR` to `${workspaceFolder}`; if Cursor leaves that unexpanded, the server ignores it.

## Settings

No setup is needed: install the plugin, add your credentials and start. A settings file is optional and is for teams (shared projects, field IDs and defaults, import choices). Use it when you want everyone on a repository to get the same choices.

### What gets asked

- **First write to a project.** If the project is not listed in a settings file and you have not approved it yet, you are asked once: "All projects" (every project on this Xray site, pre-selected), "This project only" or "No". The answer is remembered for your user and for that Xray site, so switching to a different Xray site asks again. Read-only mode still blocks every write.
- **Required Jira fields.** With the optional Jira token, required fields are discovered from Jira and asked once, then remembered per field; created items list `appliedDefaults`. Without the token, the plugin tries the create; when Jira refuses it for missing required fields, nothing is created and the plugin asks for those fields (by field id; give values in the shape Jira expects for custom fields, see [docs/field-ids.md](field-ids.md)), then creates the item and remembers the answers. Results import (`import_execution_results`) and `import_feature_file` do not ask: they pass Jira's error through.
- **Who asks.** In clients that support MCP elicitation (see the table below) the client asks you directly with a form. In other clients the tool returns `needs_answer` and the agent asks you, then repeats the call with your answer. The agent never answers for you.
- **Reads** work on approved projects, on projects listed in a settings file, and on any project when there is no settings file (or it lists no projects).

| Client | Who asks | Basis |
|---|---|---|
| Claude Code | asks you directly in interactive sessions (headless runs may fall back to the agent asking) | official docs describe an interactive elicitation dialog |
| Cursor | asks you directly | official docs list Elicitation as supported |
| VS Code (Copilot agent) | asks you directly | official docs: form and URL elicitation during a turn |
| Codex | unknown: the agent asks you | official docs do not say |
| Gemini CLI | unknown: the agent asks you | official docs do not say |

Sources and dates are in [docs/coverage.md](coverage.md). If your client is not listed, or its behaviour changed, the question still reaches you through the agent.

### Where answers are remembered, and how to revoke

Approvals and remembered field values live in `preferences.json` inside `XRAY_DATA_DIR` (which wins), else `${CLAUDE_PLUGIN_DATA}`, else `~/.config/xray-cloud`, per user and per Xray site. `get_status` shows them. To take them back, ask the agent to call `set_preferences` with `revokeProject` (one project) or `approveAll: false` (the "All projects" approval); `clearFieldValues` forgets remembered field values.

### The settings file

The settings file lists projects and their custom fields. It is YAML (JSON is also accepted). Projects are optional: projects listed here write without asking, and a file with no projects behaves like no file. The first file found wins; files are not merged:

1. the file chosen in the plugin's "Settings file" prompt (`XRAY_SETTINGS_FILE`); if that file does not exist, tools fail with an error instead of falling back to the other location;
2. `settings.yml`, `settings.yaml` or `settings.json` in `~/.config/xray-cloud/`.

The project folder is not searched, so the settings do not depend on where your client starts the server. **Teams:** commit the file in your repository (for example `xray-settings.yml`) and have everyone point `XRAY_SETTINGS_FILE` at it (Claude Code's "Settings file" prompt, Cursor's Configure, Gemini's extension setting, or the environment variable).

Without a settings file the server works: reads are open, and writes ask as described above. The settings are re-read whenever the file changes. A complete example is in [`.xray/settings.example.yml`](../.xray/settings.example.yml):

```yaml
projects:
  PROJ:
    fields:
      test:
        component:
          id: customfield_12345
          type: option
          required: true
          default: Backend
      testExecution:
        fixVersion:
          id: fixVersions
          type: versions
          required: true
      testPlan: {}
      testSet: {}
      testRun:
        version:
          name: Version
          required: false
lifecycleLabels: [ai-draft, needs-refinement, automation-candidate, automated, obsolete]
duplicateCheck: true
```

- `projects` (optional): projects listed here are written to without asking. Other projects ask once for approval (see above).
- Item types under `fields`: `test`, `testExecution`, `testPlan`, `testSet`, `precondition`, `testRun`.
- Field `type`: `option`, `options`, `string` (default), `number`, `version`, `versions`, `user`, `raw`.
- `required`: when true and there is no `default`, a create is refused before any API call until the value is given. `default` fills in the value when it is omitted.
- `id` is the Jira field id. Not sure how to find it? See [docs/field-ids.md](field-ids.md).
- `testRun` fields are Xray test-run custom fields, not Jira fields: give the Xray field `name` (or an `id`).
- Escape hatch: create tools accept `rawFields` (`{"customfield_12345": <Jira JSON>}`), passed through untouched for fields not in the settings (an explicit `reporter` included). `summary`, `project` and `issuetype` are set by the tool and refused in `fields`/`rawFields`. Without the Jira token, `fields` also takes Jira field ids (`customfield_12345`, `components`, `priority`, ...) for fields not in the settings. `update_test_run` takes `rawFields` keyed by Xray run field name or id.
- `lifecycleLabels` (default shown above) are the labels `set_lifecycle_label` may apply. `duplicateCheck` turns the pre-create duplicate search on or off.
- `projects.<KEY>.testKeyTag` (optional): a scenario tag that names the scenario's test in result imports, besides `@TEST_<KEY>`. With `testKeyTag: issue`, `@issue=PROJ-12`, `@issue:PROJ-12` and `@issue_PROJ-12` all mean PROJ-12.

## Optional Jira token

Xray's API cannot edit Jira fields. With a Jira API token the plugin can also edit a test's summary, description, labels, priority and assignee, set lifecycle labels, list a project's versions (`list_project_versions`, which strictly needs the token), and read live create-field metadata for `get_create_fields` (without the token it answers from your settings file).

To enable it, create a token at https://id.atlassian.com/manage-profile/security/api-tokens, then fill in the plugin prompts "Jira site URL" (for example `https://yourcompany.atlassian.net`), "Jira email" and "Jira API token". The token acts as you. Some organizations block API tokens, which is why it is optional; the Atlassian (Rovo) MCP can do the same Jira edits.

## Read-only mode

Turn on "Read-only mode" in the plugin prompts (or set `XRAY_READ_ONLY=true`) and every tool that changes anything refuses to run.

## Permissions

Tools act with the permissions of the user behind the Xray API key (and, for Jira-field edits, the Jira token's user). The delete tools (`delete_test`, `delete_test_set`, `delete_test_plan`, `delete_test_execution`) need the Jira project permission **Delete Issues**, which many users don't have. Without it the tool returns a clear error with `reason: "permission_denied"` and the original Xray message in `xrayMessage`; ask a Jira admin for "Delete Issues" or the project Administrator role. Other missing write permissions are reported the same way. With the optional Jira token, delete previews (without `confirm: true`) also show `canDelete`.

## Changing your setup

- **Plugin prompts:** in Claude Code `/plugin`, then configure the plugin (region, credentials, Jira, read-only mode and the settings file path); in Cursor, Plugins, Configure; in Codex, change the environment variables and restart Codex.
- **Settings file:** your own file, found in this order: `XRAY_SETTINGS_FILE` (the plugin prompt); `~/.config/xray-cloud/settings.yml`. The plugin ships no settings. To share choices team-wide, commit a file in the team's repository and point `XRAY_SETTINGS_FILE` at it.
- **Import choices and tag rules:** ask Claude to "change my import preferences" (`set_preferences`), or edit the settings file. A settings-file value always wins over a remembered one.
- **`get_status`:** shows the plugin's state without secrets: region, whether credentials are configured, the settings file in use and the locations searched, projects, Jira, and each project's effective choices with where each came from (call, settings, remembered, default or unanswered).
- **Guided setup:** the `xray-setup` skill walks through all of this: it checks credentials, explains the first-use questions, and optionally builds a team settings file: discovers field IDs (Jira token, Atlassian MCP, or manual steps), offers import choices, the AI-created label and tag rules, shows the diff, asks before saving and ends with `get_status`.

## Limitations

- Lifecycle labels and editing a test's summary, description or labels need the optional Jira token or the Atlassian (Rovo) MCP, because Xray's API cannot edit Jira fields.
- Xray Cloud only; Xray Server and Data Center are not supported.
- Windows is untested.
