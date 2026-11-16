# Mode Analytics MCP Server

An MCP server that lets you browse reports, manage queries, and fetch results from [Mode Analytics](https://mode.com) from any MCP-compatible AI assistant or agent.

## Tools

| Tool | Description |
|---|---|
| `list_data_sources` | List all database connections in the workspace |
| `list_spaces` | List all spaces (collections) |
| `list_reports` | List reports in a given space (archived ones hidden unless `include_archived=true`) |
| `search_reports` | Search for reports by name or description across all spaces (client-side; archived ones hidden unless `include_archived=true`) |
| `get_report` | Get report details including its SQL queries, who wrote each one, each query's charts, and the report's registered form `parameters` |
| `list_report_runs` | List recent runs for a report (state, timestamps) |
| `get_run_results` | Fetch results from a specific or latest report run. Failed queries return their warehouse error and `query_token`; pass `include_failed=true` to read the latest run even if it failed. `columns` returns only the named columns |
| `run_report` | Run a report and return per-query results or errors. Runs **every** query in the report (warehouse cost). Reports `applied_parameters` and warns when parameters were probably ignored. Off with `MODE_READ_ONLY` |
| `list_charts` | List the charts in a report (or on one query): token, type, title |
| `get_chart` | Get one chart's type, title and what it plots; `include_vegas=true` adds the raw settings |
| `create_chart` | Add a line, bar, area, pie, table or big-number chart to a query. **Undocumented endpoint** |
| `update_chart` | Change a chart's type, columns, title or raw settings, keeping the rest of its formatting. **Undocumented endpoint** |
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

Use a **Member API key** (recommended). Create your own under **Workspace Settings > Personal > My API Keys** and save the token and secret. Member keys respect your own Mode permissions, so the agent can only see and change what you can. If you can't create one, a Workspace admin needs to enable them under **Workspace Settings > Features > API Keys > Member keys**.

Mode also offers **Workspace API tokens**, created by admins under **Workspace Settings > Privacy & Security > API**. These act with admin access to the whole Workspace, so only use one if you need that — see **Limitations / Safety** below. (Older *personal* API tokens can no longer be created, but existing ones keep working.)

Both kinds of key expire after 90 days by default. When yours expires, the server reports "Authentication failed"; create a new key and update your MCP config.

### 2. Install uv

The server is launched with `uvx`, which comes with [uv](https://docs.astral.sh/uv/getting-started/installation/). Install it with `brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`.

### 3. Add the server to your MCP client

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

`uvx` installs the package from PyPI on first run and reuses that version afterwards. To pick up new releases automatically, use `"args": ["mode-mcp@latest"]` instead.

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
| `MODE_WORKSPACE` | Yes | Your Workspace's username, as in `app.mode.com/<workspace>/...` |
| `MODE_API_TOKEN` | Yes | API token (from a Member API key, ideally) |
| `MODE_API_SECRET` | Yes | API secret |
| `MODE_ENABLE_DELETES` | No | Set to `true` to enable `delete_report` and `delete_query`. Off by default |
| `MODE_READ_ONLY` | No | Set to `true` to disable every tool that writes to Mode (overrides `MODE_ENABLE_DELETES`) |
| `MODE_DISABLED_TOOLS` | No | Comma-separated tool names to disable, e.g. `create_report,update_query` |

### Choosing which tools are available

Disabled tools are never registered, so the agent doesn't see them at all.

- **Default:** everything except the two delete tools. The agent can browse, author, run, chart, and archive, but can't permanently delete anything.
- **Allow deletes:** add `"MODE_ENABLE_DELETES": "true"` to `env`.
- **Read-only:** add `"MODE_READ_ONLY": "true"` to only allow browsing and fetching results. This also disables `run_report`, since running a report spends warehouse compute and adds run history.
- **Anything else:** list tool names in `MODE_DISABLED_TOOLS`.

A misspelled tool name in `MODE_DISABLED_TOOLS` stops the server with an error rather than silently leaving the tool enabled.

## Limitations / Safety

**Running reports.** The Mode API has no endpoint for running a single query. `run_report` calls `POST /reports/{token}/runs`, which runs **every** query in the report at once, spends warehouse compute, and adds to the report's run history.
- Keep reports small (or split them) while an agent iterates on SQL.
- Rows are truncated to 50 per query by default, since the usual goal is checking that the SQL works. A failed query returns its warehouse error and `query_token`, which maps straight to `update_query`.
- Parameters only take effect when the report has registered form fields: a `{% form %}` block in a query's SQL, saved in Mode. Queries created through the API aren't registered until someone saves them in the editor, and Mode then runs with the form defaults. `get_report` lists the registered `parameters`, and `run_report` warns when it sent parameters to a report with none.
- `columns` keeps wide results (emails, impersonation links) out of the transcript.
- If a run outlasts `timeout_seconds`, the result has `timed_out: true`; fetch it later with `get_run_results`.
- To turn it off, set `MODE_READ_ONLY=true` or `MODE_DISABLED_TOOLS=run_report`.

**Charts use undocumented endpoints.** Mode's public Charts API is read-only (`list_charts`, `get_chart`). `create_chart` and `update_chart` call the same endpoints Mode's own editor does (create a chart on a query, then patch its `view_vegas`), with your normal API token. They can break without notice; turn them off with `MODE_DISABLED_TOOLS=create_chart,update_chart` (or `MODE_READ_ONLY=true`).
- Columns are checked against the query's latest succeeded run (run the report first), ignoring case. Each y column is plotted as `SUM(column)`.
- `update_chart` only changes what you pass and keeps the chart's colors and formats. If you change the type or columns of a chart built in Mode's editor, and it has a shape the server can't read back, it asks for the full set of fields or for the change through `view`.
- This server can't render charts, so check what it makes. Smoke test after upgrading Mode-facing code, on a scratch report with one succeeded run:
  1. `create_chart` a bar chart, then open its `web_path` under `app.mode.com` and confirm it renders with the right axes.
  2. `update_chart` with a new `title` and confirm the title changed and the colors didn't.
  3. `update_chart` with `type="line"` and confirm the chart is rebuilt as a line.
  4. `get_chart` with `include_vegas=true` and check `view_vegas` is still version 2.
  5. Delete the chart in Mode's UI. There is no delete-chart tool.

**Deletes are permission-scoped, not owner-scoped.** The server does not check who created a report or query; what you can delete is whatever your API token is allowed to delete in Mode. With a Member API key that's limited to your own permissions, but a Workspace API token has admin access and can delete **any** report or query in the Workspace. That's why the delete tools are off unless you set `MODE_ENABLE_DELETES=true`.

**Deletes can't skip the preview.** When enabled, `delete_report` and `delete_query` are always two calls:
1. The first call deletes nothing. It returns a preview (the report's name, whether it's already archived, and its queries with who wrote each one; or a query's name, author, and SQL) plus a short `confirm_code`. Mode's API doesn't say who created a report, only who wrote each query.
2. The delete only happens when the same tool is called again with that `confirm_code`. The code is a fingerprint of what the preview showed, so it can't be guessed, and if the report or query changed after the preview (renamed, SQL edited, queries added), the code no longer matches and the delete is refused.

**Archive instead of delete.** `archive_report` hides a report and `unarchive_report` brings it back. The delete tool tells the agent to suggest archiving first.

**Credentials never touch the committed config.** The `${MODE_API_TOKEN}` / `${MODE_API_SECRET}` references in your MCP config are expanded from your shell environment at launch (in clients that support it), so the file is safe to commit. Keep the raw secrets in env vars and don't commit them.

## Development

```bash
git clone https://github.com/nataliemona/mode_mcp.git
cd mode_mcp
uv sync --dev
uv run pytest
```
