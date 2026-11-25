# xray-cloud-plugin

Lets your coding agent manage Xray Cloud: tests and their steps, test sets, plans, executions, runs, folders, environments, Gherkin import and result import.
One pure-Python MCP server (standard library only, Python 3.9+) plus four skills, packaged for Claude Code, Codex, Cursor and Gemini CLI.

## Install

You need `python3` 3.9 or newer and an Xray Cloud API key (Jira: Apps, Xray, API Keys).

### Claude Code

```
/plugin marketplace add tagiranara-max/xray-cloud-plugin
/plugin install xray-cloud@xray-cloud-plugin
```

Enter your Xray client ID and secret when asked.

### Codex

```sh
codex plugin marketplace add tagiranara-max/xray-cloud-plugin
```

Then type `/plugins` in Codex (or open Plugins in the ChatGPT desktop app), pick the Xray Cloud marketplace, install Xray Cloud and start a new session. Codex cannot ask for a local server's credentials, so set them once in your shell profile (for example `~/.zshrc`):

```sh
export XRAY_CLIENT_ID="<client id>" XRAY_CLIENT_SECRET="<client secret>"
```

That reaches Codex started from a terminal. The ChatGPT desktop app does not read your shell profile. On macOS only, and not yet verified, you can run `launchctl setenv XRAY_CLIENT_ID "<client id>"` and the same for `XRAY_CLIENT_SECRET`, then restart the app (this lasts until you log out). Give the agent absolute file paths in Codex; see [docs/configuration.md](docs/configuration.md#codex-and-cursor-plugin-notes).

### Cursor

Cursor installs plugins from its marketplace, a team marketplace or a local folder. Teams: an admin imports this repository under Dashboard, Plugins & MCPs, Add Marketplace, Import from Repo; then install Xray Cloud from Customize. On your own, copy the plugin folder into Cursor's local plugins (run the same lines again to update), then reload the window:

```sh
git clone https://github.com/tagiranara-max/xray-cloud-plugin ~/.xray-cloud-plugin 2>/dev/null || git -C ~/.xray-cloud-plugin pull
mkdir -p ~/.cursor/plugins/local && rm -rf ~/.cursor/plugins/local/xray-cloud
cp -R ~/.xray-cloud-plugin/plugin ~/.cursor/plugins/local/xray-cloud
```

On Enterprise plans an admin must turn on Allow Local Plugin Imports first. Enter your Xray client ID and secret under Plugins, Configure. If your Cursor offers no Configure for a local plugin, remove it (`rm -rf ~/.cursor/plugins/local/xray-cloud`) and use the manual setup in [docs/configuration.md](docs/configuration.md#cursor-manual) instead, so you do not run two servers.

### Gemini CLI

```sh
gemini extensions install https://github.com/tagiranara-max/xray-cloud-plugin
```

Enter your Xray client ID and secret when asked (they go to your system keychain).

### Other MCP clients

VS Code, Claude Desktop and any other stdio client: see [docs/configuration.md](docs/configuration.md#other-mcp-clients-manual-setup).

## First use

There is nothing else to set up. Ask the agent to find or create tests in a project, for example "list the tests in PROJ".

- **First write to a project:** you are asked once whether the plugin may write to it, with "All projects" pre-selected ("This project only" and "No" are the other answers). The answer is remembered per user and Xray site.
- **Required Jira fields:** when a create needs fields your Jira requires, you are asked for them once and the answers are remembered.
- Clients that support MCP elicitation show a form; otherwise the agent relays the question. It never answers for you.

Optionally, `~/.config/xray-cloud/settings.yml`, or any file named in `XRAY_SETTINGS_FILE` (a team can commit one in its repository), pins projects, field IDs and import choices; see [docs/configuration.md](docs/configuration.md#settings).

## What it can do

- **Tests and steps:** search, create, edit and delete tests; add, edit, move and remove steps; lifecycle labels.
- **Containers:** test sets, test plans and test executions, with their tests; test runs (status, steps, defects, evidence); test environments.
- **Test Repository folders:** create, rename, move, delete and file tests into them.
- **Import and export:** Gherkin `.feature` import with preview and duplicate checks, result import (JUnit, TestNG, NUnit, Robot, Cucumber, Behave, Xray JSON), Cucumber export.
- **Analysis:** run history and flaky tests, requirement coverage, tests related to an area.
- **Safety:** one-time approval per project, duplicate checks, confirmation before deletes, read-only mode, region support (global, EU, AU).
- **Skills:** `xray-setup`, `import-results`, `import-tests` and `predict-affected-tests` guide the agent through multi-step workflows.

## Documentation

- [docs/configuration.md](docs/configuration.md): credentials, environment variables, manual client setup, settings file, Jira token, read-only mode, permissions.
- [docs/tools.md](docs/tools.md): every tool, result import and matching, feature import, choices and tag rules.
- [docs/field-ids.md](docs/field-ids.md): finding Jira field IDs.
- [docs/coverage.md](docs/coverage.md): what was verified live, API parity, client support.

## Development

```sh
python3 -m pip install pytest
python3 -m pytest -q
```

See [CLAUDE.md](CLAUDE.md) for conventions and the server smoke test.
