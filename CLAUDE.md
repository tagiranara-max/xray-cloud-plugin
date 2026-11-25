# xray-cloud-plugin

Claude Code plugin `xray-cloud`: a stdlib-only Python 3.9 MCP server (stdio) for Xray Cloud. It exposes tests, test sets, plans, executions, runs, environments, folders and Gherkin import as tools, with an optional Jira token for Jira-field edits.

## Layout

- `plugin/` is the plugin: `.claude-plugin/plugin.json` (userConfig prompts, MCP server definition) and `server/` (`main.py`, package `xray_cloud/`: config, registry, tools, vendored libs under `_vendor/`).
- `plugin/skills/` holds the four skills (`xray-setup`, `import-results`, `import-tests`, `predict-affected-tests`), each a `SKILL.md`.
- `scripts/install_skills.py` copies the skills into Codex, Cursor or Gemini CLI skills folders (`--client`, `--scope`, `--force`, `--dry-run`).
- `gemini-extension.json` is the Gemini CLI extension manifest (versioned by git commit; its version is a placeholder).
- Codex: `.agents/plugins/marketplace.json` (repo marketplace) and `plugin/.codex-plugin/plugin.json` with the server in `plugin/codex-mcp.json` (credentials forwarded via `env_vars`). Cursor: `.cursor-plugin/marketplace.json` (repo root) and `plugin/.cursor-plugin/plugin.json` (`variables`) with the server in `plugin/mcp.json`. None carries a `version`. Field sources are in `docs/coverage.md`.
- `README.md` is the short install guide; `docs/configuration.md` holds credentials, env vars, manual client setup and the settings file; `docs/tools.md` the tool reference.
- `dev/` holds dev-only scripts (`parity_audit.py`: lists the live GraphQL schema's mutations and queries, read-only, needs credentials; compare with `docs/coverage.md`).
- `.claude-plugin/marketplace.json` makes the repo root a one-plugin marketplace (`xray-cloud-plugin`).
- `.xray/settings.example.yml` is the settings example; `docs/field-ids.md` the field-id guide.
- `tests/unit/` unit tests, `tests/fixtures/` Gherkin fixtures.

## Tests

```sh
python3 -m pip install pytest
python3 -m pytest -q                 # everything except live tests
python3 -m pytest -m live -q         # live tests; need XRAY_CLIENT_ID, XRAY_CLIENT_SECRET (write tests also XRAY_TEST_PROJECT, XRAY_REGION; tag-rule field checks also XRAY_TEST_DATE_FIELD, a date field id)
```

## Smoke test

```sh
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"s","version":"0"}}}' | python3 plugin/server/main.py
claude plugin validate ./plugin
```

The first prints one JSON line with `serverInfo.name == "xray-cloud"`; the second must report `Validation passed`.

## Conventions

- Runtime code is Python 3.9 standard library only; pure-Python vendored libraries live under `_vendor/` and always win over installed copies. pytest is dev-only.
- No company-specific values anywhere (names, project keys, cloud IDs, field IDs). Examples use `PROJ`, `customfield_12345`, `Backend`. `tests/unit/test_repo_guards.py` enforces this.
- Conventional commits (`feat:`, `fix:`, `docs:`, `chore:` ...) with no AI attribution trailers. Plans, specs and other working notes stay out of the repo.
- `plugin.json` and the marketplace entry carry no `"version"` (the plugin is versioned by git commit); `tests/unit/test_repo_guards.py` enforces it.
- Never print, log or commit secrets (client ID/secret, Jira token); the settings loader rejects credential-like keys.
