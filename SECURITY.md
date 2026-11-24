# Security policy

## Reporting a vulnerability

Please report security issues privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability**. Do not open a public issue for a security problem.

Include what you found, how to reproduce it, and which commit you tested. You will get a reply in the advisory thread.

## Scope

The plugin runs locally as a stdio MCP server. Relevant reports include, for example:

- credentials (Xray client ID/secret, Jira token) appearing in output, logs or files;
- writes to Xray or Jira outside the configured projects, or without the validation the tools promise;
- local file access outside the paths a tool was given (for example zip entries escaping the export directory).

## Supported versions

The plugin is versioned by git commit; fixes land on `main` and reach users on their next plugin update.
