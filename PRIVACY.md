# Privacy

Xray Cloud (this plugin) runs entirely on your machine, as a local server started by your coding agent.

## What it talks to

- Xray Cloud, at the endpoint of your region (global, EU or AU).
- Your own Jira site, only if you configure a Jira site URL, email and API token.

It contacts no other service and has no third parties. It has no telemetry or analytics.

## Credentials

Your Xray client ID and secret (and the optional Jira token) come from the client's plugin settings or keychain, or from environment variables. The plugin does not write them to any file or log.

## What is stored locally

One preferences file, `preferences.json`, in `XRAY_DATA_DIR` if set, else the client's plugin data directory (`CLAUDE_PLUGIN_DATA`), else `~/.config/xray-cloud/`. It holds your write approvals per project, and your remembered import answers (choices, tag rules and tag values). Entries are keyed by region, project and a short hash of your Xray client ID (never the ID itself). It holds no secrets.

To clear it, delete the file, or ask the agent to change your import preferences.

## Your data

Test and run content that you read from or write to Xray passes through your agent client and its model provider, under that provider's terms.

## Contact

Open an issue: https://github.com/tagiranara-max/xray-cloud-plugin/issues
