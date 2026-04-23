# Mode Analytics MCP Server

An MCP server that lets you browse reports, manage queries, and fetch results from [Mode Analytics](https://mode.com) directly through Claude.

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
| `delete_report` | Delete a report you created (requires `confirm=True` to execute) |
| `add_query` | Add a SQL query to an existing report |
| `update_query` | Update a query's SQL, name, or data source |
| `delete_query` | Delete a query from a report (only queries you created) |

## Setup

### 1. Get Mode API credentials

Go to **Mode > Settings > API Tokens** and create a token/secret pair.

### 2. Configure Claude Code

The repo is private, so install directly from GitHub over SSH (uses your existing SSH key — make sure `ssh -T git@github.com` works first).

Add to your Claude Code MCP settings (`~/.claude/settings.json` or project `.mcp.json`):

```json
{
  "mcpServers": {
    "mode-analytics": {
      "command": "uvx",
      "args": ["--from", "git+ssh://git@github.com/nataliemona/mode_mcp", "mode-mcp"],
      "env": {
        "MODE_WORKSPACE": "your-workspace-name",
        "MODE_API_TOKEN": "${MODE_API_TOKEN}",
        "MODE_API_SECRET": "${MODE_API_SECRET}"
      }
    }
  }
}
```

`uvx` will clone and install the package on first run.

The `${MODE_API_TOKEN}` / `${MODE_API_SECRET}` references are expanded from your shell environment, so no secrets land in the committed config. Set them wherever you keep shell secrets, e.g. in `~/.zshrc`:

```bash
export MODE_API_TOKEN="..."
export MODE_API_SECRET="..."
```

### Once published to PyPI

If/when the package is published, the install line simplifies to:

```json
"args": ["mode-mcp"]
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `MODE_WORKSPACE` | Yes | Mode workspace/account name |
| `MODE_API_TOKEN` | Yes | API token |
| `MODE_API_SECRET` | Yes | API secret |

## Limitations / Safety

**No query execution.** The server deliberately does not expose a "run this query" tool. The Mode API has no endpoint for running a single query — the only way to execute SQL is `POST /reports/{token}/runs`, which runs **every** query in the report at once. On reports with many or expensive queries, that's slow, costly, and impossible to predict from Claude's side. Instead, the server focuses on **browsing**, **authoring**, and **fetching results**:
- Write and manage SQL queries through Claude, then run them in Mode's UI where costs and side effects are visible.
- Use `get_run_results` to pull results from completed runs back into Claude.

**Deletes are scoped and confirmed.**
- `delete_query` only succeeds on queries *you* created — Mode's API rejects deletes on anyone else's work. There is no confirmation step because each call removes exactly one query you own.
- `delete_report` is two-step by design. The first call (without `confirm=True`) returns a preview — report name, creator, query count, and query names — so Claude can show you what's about to be destroyed. The delete only proceeds when you explicitly call it again with `confirm=True`. As with queries, Mode's API enforces that you can only delete reports you own.

**Credentials never touch the committed config.** The `${MODE_API_TOKEN}` / `${MODE_API_SECRET}` references in `mcp.json` are expanded from your shell environment at launch, so the file is safe to commit. Keep the raw secrets in env vars and don't commit them.

## Development

```bash
git clone git@github.com:nataliemona/mode_mcp.git
cd mode_mcp
uv sync --dev
uv run pytest
```
