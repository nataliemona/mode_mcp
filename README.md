# Mode Analytics MCP Server

An MCP server that lets you browse reports, manage queries, and fetch results from [Mode Analytics](https://mode.com) from any MCP-compatible AI assistant or agent.

## Tools

| Tool | Description |
|---|---|
| `list_data_sources` | List all database connections in the workspace |
| `list_spaces` | List all spaces (collections) |
| `list_reports` | List reports in a given space |
| `search_reports` | Search for reports by name or description across all spaces (client-side) |
| `get_report` | Get report details including its SQL queries |
| `list_report_runs` | List recent runs for a report (state, timestamps) |
| `get_run_results` | Fetch results from a specific or latest report run |
| `create_report` | Create a new report in a space, optionally with initial queries |
| `update_report` | Update a report's name, description, or move it to a different space |
| `archive_report` | Archive (hide) a report — reversible |
| `unarchive_report` | Restore an archived report |
| `delete_report` | Permanently delete a report (preview first, then confirm). Off unless `MODE_ENABLE_DELETES=true` |
| `add_query` | Add a SQL query to an existing report |
| `update_query` | Update a query's SQL, name, or data source |
| `delete_query` | Permanently delete a query (preview first, then confirm). Off unless `MODE_ENABLE_DELETES=true` |

## Setup

### 1. Get Mode API credentials

Go to **Mode > Settings > API Tokens** and create a token/secret pair.

Mode no longer allows creating new *personal* API tokens (existing ones keep working). New tokens are **Workspace API tokens**, which only Workspace admins can create and which act with admin access to the whole Workspace. Treat them accordingly — see [Limitations / Safety](#limitations--safety).

### 2. Add the server to your MCP client

The server runs over stdio via `uvx mode-mcp`. Add it to your MCP client's config; most clients use this `mcpServers` shape:

```json
{
  "mcpServers": {
    "mode-analytics": {
      "command": "uvx",
      "args": ["mode-mcp"],
      "env": {
        "MODE_WORKSPACE": "your-workspace-name",
        "MODE_API_TOKEN": "${MODE_API_TOKEN}",
        "MODE_API_SECRET": "${MODE_API_SECRET}"
      }
    }
  }
}
```

`uvx` will install the package from PyPI on first run.

The `${MODE_API_TOKEN}` / `${MODE_API_SECRET}` references are expanded from your shell environment by clients that support it, so no secrets land in the config file. Set them wherever you keep shell secrets, e.g. in `~/.zshrc`:

```bash
export MODE_API_TOKEN="..."
export MODE_API_SECRET="..."
```

### Env var syntax differs by client

Some clients expand `${VAR}`, some use another syntax (e.g. `${env:VAR}`), and some don't expand references at all; check your client's docs. If a reference isn't expanded, the server stops at startup with an error naming the variable, rather than failing later with a confusing authentication error.

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `MODE_WORKSPACE` | Yes | Mode workspace/account name |
| `MODE_API_TOKEN` | Yes | API token |
| `MODE_API_SECRET` | Yes | API secret |
| `MODE_ENABLE_DELETES` | No | Set to `true` to enable `delete_report` and `delete_query`. Off by default |
| `MODE_READ_ONLY` | No | Set to `true` to disable every tool that writes to Mode (overrides `MODE_ENABLE_DELETES`) |
| `MODE_DISABLED_TOOLS` | No | Comma-separated tool names to disable, e.g. `create_report,update_query` |

### Choosing which tools are available

Disabled tools are never registered, so the agent doesn't see them at all.

- **Default:** everything except the two delete tools. The agent can browse, author, and archive, but can't permanently delete anything.
- **Allow deletes:** add `"MODE_ENABLE_DELETES": "true"` to `env`.
- **Read-only:** add `"MODE_READ_ONLY": "true"` to only allow browsing and fetching results.
- **Anything else:** list tool names in `MODE_DISABLED_TOOLS`.

A misspelled tool name in `MODE_DISABLED_TOOLS` stops the server with an error rather than silently leaving the tool enabled.

## Limitations / Safety

**No query execution.** The server deliberately does not expose a "run this query" tool. The Mode API has no endpoint for running a single query — the only way to execute SQL is `POST /reports/{token}/runs`, which runs **every** query in the report at once. On reports with many or expensive queries, that's slow, costly, and impossible to predict from the agent's side. Instead, the server focuses on **browsing**, **authoring**, and **fetching results**:
- Write and manage SQL queries through your agent, then run them in Mode's UI where costs and side effects are visible.
- Use `get_run_results` to pull results from completed runs back into the conversation.

**Deletes are permission-scoped, not owner-scoped.** The server does not check who created a report or query; what you can delete is whatever your API token is allowed to delete in Mode. With a personal token that's limited to your own permissions, but a Workspace API token has admin access and can delete **any** report or query in the Workspace. That's why the delete tools are off unless you set `MODE_ENABLE_DELETES=true`.

**Deletes can't skip the preview.** When enabled, `delete_report` and `delete_query` are always two calls:
1. The first call deletes nothing. It returns a preview (report name, creator and queries, or the query's name and SQL) plus a short `confirm_code`.
2. The delete only happens when the same tool is called again with that `confirm_code`. The code is a fingerprint of what the preview showed, so it can't be guessed, and if the report or query changed after the preview (renamed, SQL edited, queries added), the code no longer matches and the delete is refused.

**Archive instead of delete.** `archive_report` hides a report and `unarchive_report` brings it back. The delete tool tells the agent to suggest archiving first.

**Credentials never touch the committed config.** The `${MODE_API_TOKEN}` / `${MODE_API_SECRET}` references in your MCP config are expanded from your shell environment at launch (in clients that support it), so the file is safe to commit. Keep the raw secrets in env vars and don't commit them.

## Development

The source repo is private; the package is published to PyPI.

```bash
git clone git@github.com:nataliemona/mode_mcp.git
cd mode_mcp
uv sync --dev
uv run pytest
```
