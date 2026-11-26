# Xray Cloud

Xray Cloud lets Claude Code manage your Xray Cloud test management data: tests, test steps, test sets, test plans, test executions and test runs, with duplicate checks, approval before writes and an optional read-only mode. It can also create tests from Gherkin feature files and attach evidence to runs.

## Install

In Claude Code:

```
/plugin marketplace add tagiranara-max/xray-cloud-plugin
/plugin install xray-cloud@xray-cloud-plugin
```

Then enter your Xray Cloud API key (client ID and secret) when prompted, or set `XRAY_CLIENT_ID` and `XRAY_CLIENT_SECRET`.

## What it runs

The plugin starts a local stdio server with `python3` (3.9 or newer) from the plugin folder. It installs no packages: the libraries it needs are vendored and pure Python. The server runs only on your machine, started by Claude Code.

## What it sends and fetches

- Xray Cloud API, at the endpoint of the region you configure (global, EU or AU): your credentials to authenticate, and the test data you ask it to read or write.
- Your own Jira site, only if you configure a Jira site URL, email and API token.
- Local files: only the paths you or the agent pass to a tool (feature files, step attachments, run evidence). Nothing else on disk is read.

It contacts no other service and has no telemetry.

## What it stores locally

One preferences file, `preferences.json`, with your per-project write approvals and remembered import answers. It holds no secrets. See [PRIVACY.md](https://github.com/tagiranara-max/xray-cloud-plugin/blob/main/PRIVACY.md).

## More

Full documentation: https://github.com/tagiranara-max/xray-cloud-plugin#readme
