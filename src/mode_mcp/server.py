"""
Mode Analytics MCP Server

Browse reports and fetch results from Mode Analytics.

Env vars:
    MODE_WORKSPACE       — Mode workspace/account name
    MODE_API_TOKEN       — API token (Workspace Settings > Personal > My API Keys)
    MODE_API_SECRET      — API secret
    MODE_ENABLE_DELETES  — "true" to enable delete_report and delete_query
                           (off by default)
    MODE_READ_ONLY       — "true" to disable every tool that writes to Mode
    MODE_DISABLED_TOOLS  — comma-separated tool names to disable, e.g.
                           "delete_report,delete_query"
"""

import asyncio
import hashlib
import json
import os
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

MODE_API_BASE = "https://app.mode.com/api"


@dataclass
class ModeContext:
    """Shared state created once at server startup."""

    client: httpx.AsyncClient
    workspace: str


def _get_config() -> tuple[str, str, str]:
    workspace = os.environ.get("MODE_WORKSPACE", "")
    token = os.environ.get("MODE_API_TOKEN", "")
    secret = os.environ.get("MODE_API_SECRET", "")
    if not all([workspace, token, secret]):
        raise ValueError(
            "Set MODE_WORKSPACE, MODE_API_TOKEN, and MODE_API_SECRET env vars"
        )
    for name, value in [
        ("MODE_WORKSPACE", workspace),
        ("MODE_API_TOKEN", token),
        ("MODE_API_SECRET", secret),
    ]:
        if value.startswith("${"):
            raise ValueError(
                f"{name} is the literal text {value!r}. Your MCP client didn't expand "
                f"the variable reference; check its docs for env var syntax or set the value directly."
            )
    return workspace, token, secret


@asynccontextmanager
async def lifespan(server: MCPServer):
    """Create a shared httpx client for the lifetime of the server."""
    workspace, token, secret = _get_config()
    async with httpx.AsyncClient(
        base_url=f"{MODE_API_BASE}/{workspace}",
        auth=(token, secret),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/hal+json",
        },
        timeout=30.0,
    ) as client:
        yield ModeContext(client=client, workspace=workspace)


mcp = MCPServer("mode-analytics", lifespan=lifespan)


def _mode(ctx: Context) -> ModeContext:
    """Extract the ModeContext from the request context."""
    return ctx.request_context.lifespan_context


def _check(resp: httpx.Response) -> None:
    """Raise a ToolError with a friendly message if the response is an error."""
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        detail = ""
        try:
            detail = exc.response.json().get("error", "")
        except Exception:
            pass
        if status == 404:
            msg = "Resource not found."
        elif status == 401:
            msg = ("Authentication failed — check MODE_API_TOKEN and MODE_API_SECRET. "
                   "Mode API keys expire after 90 days by default; create a new one if yours has expired.")
        elif status == 403:
            msg = "Permission denied — your API token may lack access to this resource."
        else:
            msg = f"Mode API returned HTTP {status}."
        if detail:
            msg = f"{msg} Detail: {detail}"
        raise ToolError(msg) from exc


def _embedded(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Extract a list from Mode's HAL+JSON _embedded envelope."""
    return data.get("_embedded", {}).get(key, [])


def _creator(obj: dict[str, Any]) -> str | None:
    """Mode username from an object's creator link, e.g. "/api/jane_doe" -> "jane_doe".

    Mode includes this on queries but not on reports.
    """
    href = obj.get("_links", {}).get("creator", {}).get("href", "")
    parts = [p for p in href.split("/") if p]
    return parts[1] if len(parts) >= 2 and parts[0] == "api" else None



# ---------------------------------------------------------------------------
# Browsing tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_data_sources(ctx: Context, limit: int = 50) -> list[dict[str, Any]]:
    """List database connections in the Mode workspace.

    Args:
        limit: Max number of results to return (default 50).
    """
    mode = _mode(ctx)
    resp = await mode.client.get("/data_sources", params={"limit": limit})
    _check(resp)
    sources = _embedded(resp.json(), "data_sources")
    return [
        {
            "id": s["id"],
            "token": s["token"],
            "name": s["name"],
            "adapter": s.get("adapter"),
            "database": s.get("database"),
        }
        for s in sources[:limit]
    ]


@mcp.tool()
async def list_spaces(ctx: Context, limit: int = 50) -> list[dict[str, Any]]:
    """List spaces (collections) in the Mode workspace.

    Args:
        limit: Max number of results to return (default 50).
    """
    mode = _mode(ctx)
    resp = await mode.client.get("/spaces", params={"limit": limit})
    _check(resp)
    spaces = _embedded(resp.json(), "spaces")
    return [
        {
            "token": s["token"],
            "name": s["name"],
            "description": s.get("description", ""),
            "state": s.get("state"),
        }
        for s in spaces[:limit]
    ]


@mcp.tool()
async def list_reports(
    ctx: Context, space_token: str, limit: int = 50, include_archived: bool = False
) -> list[dict[str, Any]]:
    """List reports in a space. Archived reports are hidden unless include_archived.

    Args:
        space_token: Space token (use list_spaces to find it).
        limit: Max number of results to return (default 50).
        include_archived: Also return archived reports (each has an `archived` flag).
    """
    mode = _mode(ctx)
    resp = await mode.client.get(
        f"/spaces/{space_token}/reports", params={"limit": limit}
    )
    _check(resp)
    reports = _embedded(resp.json(), "reports")
    if not include_archived:
        reports = [r for r in reports if not r.get("archived")]
    return [
        {
            "token": r["token"],
            "name": r["name"],
            "description": r.get("description", ""),
            "archived": bool(r.get("archived")),
            "created_at": r.get("created_at"),
            "last_run_at": r.get("last_run_at"),
        }
        for r in reports[:limit]
    ]


@mcp.tool()
async def search_reports(
    ctx: Context, query: str, limit: int = 50, include_archived: bool = False
) -> list[dict[str, Any]]:
    """Search for reports by name or description across all spaces.

    Mode's REST API has no native report-search endpoint, so this fans out
    across every space and filters client-side (case-insensitive substring
    match on name and description). May be slow on large workspaces.
    Archived reports are skipped unless include_archived.

    Args:
        query: Search term to match against report names and descriptions.
        limit: Max number of results to return (default 50).
        include_archived: Also return archived reports (each has an `archived` flag).
    """
    mode = _mode(ctx)
    q = query.lower()

    spaces_resp = await mode.client.get("/spaces", params={"limit": 100})
    _check(spaces_resp)
    spaces = _embedded(spaces_resp.json(), "spaces")

    sem = asyncio.Semaphore(5)

    async def _reports_in(space: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        async with sem:
            r = await mode.client.get(
                f"/spaces/{space['token']}/reports", params={"limit": 500}
            )
            _check(r)
            return space, _embedded(r.json(), "reports")

    groups = await asyncio.gather(*[_reports_in(s) for s in spaces])

    matches: list[dict[str, Any]] = []
    for space, reports in groups:
        for r in reports:
            if r.get("archived") and not include_archived:
                continue
            name = (r.get("name") or "").lower()
            desc = (r.get("description") or "").lower()
            if q in name or q in desc:
                matches.append(
                    {
                        "token": r["token"],
                        "name": r.get("name", ""),
                        "description": r.get("description", ""),
                        "space_token": space["token"],
                        "space_name": space.get("name", ""),
                        "archived": bool(r.get("archived")),
                        "created_at": r.get("created_at"),
                        "last_run_at": r.get("last_run_at"),
                    }
                )

    return matches[:limit]


@mcp.tool()
async def get_report(ctx: Context, report_token: str) -> dict[str, Any]:
    """Get a report's details and its queries (SQL).

    Args:
        report_token: The report token.
    """
    mode = _mode(ctx)
    report_resp, queries_resp = await asyncio.gather(
        mode.client.get(f"/reports/{report_token}"),
        mode.client.get(f"/reports/{report_token}/queries"),
    )
    _check(report_resp)
    _check(queries_resp)

    report = report_resp.json()
    queries = _embedded(queries_resp.json(), "queries")

    return {
        "token": report["token"],
        "name": report.get("name"),
        "description": report.get("description", ""),
        "archived": bool(report.get("archived")),
        "created_at": report.get("created_at"),
        "last_run_at": report.get("last_run_at"),
        "queries": [
            {
                "token": q["token"],
                "name": q.get("name", ""),
                "creator": _creator(q),
                "raw_query": q.get("raw_query", ""),
                "data_source_id": q.get("data_source_id"),
            }
            for q in queries
        ],
    }


@mcp.tool()
async def list_report_runs(
    ctx: Context, report_token: str, limit: int = 10
) -> list[dict[str, Any]]:
    """List recent runs for a report.

    Args:
        report_token: The report token.
        limit: Max number of runs to return (default 10).
    """
    mode = _mode(ctx)
    resp = await mode.client.get(
        f"/reports/{report_token}/runs",
        params={"order": "desc", "order_by": "created_at", "limit": limit},
    )
    _check(resp)
    runs = _embedded(resp.json(), "report_runs")
    return [
        {
            "token": r["token"],
            "state": r.get("state"),
            "created_at": r.get("created_at"),
            "completed_at": r.get("completed_at"),
        }
        for r in runs[:limit]
    ]


# ---------------------------------------------------------------------------
# Creation tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def create_report(
    ctx: Context,
    space_token: str,
    name: str,
    description: str = "",
    queries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a new report in a space, optionally with initial SQL queries.

    This is a write operation — it creates a report visible to the workspace.
    If any queries fail to add, the report is still created; inspect
    `queries_added` and `queries_failed` in the result.

    Args:
        space_token: Space to create the report in (use list_spaces to find it).
        name: Report name.
        description: Optional report description.
        queries: Optional list of queries to add. Each dict should have:
            - raw_query (str): The SQL text.
            - data_source_id (int): Data source ID (use list_data_sources).
            - name (str, optional): Query name.
    """
    mode = _mode(ctx)
    resp = await mode.client.post(
        "/reports",
        json={"report": {"space_token": space_token}},
    )
    _check(resp)
    report_token = resp.json()["token"]

    # Mode's create endpoint ignores name and description; set them with a PATCH.
    # The report already exists at this point, so a failure here must say which
    # report was left untitled — otherwise it's orphaned with no way to find it.
    try:
        resp = await mode.client.patch(
            f"/reports/{report_token}",
            json={"report": {"name": name, "description": description}},
        )
        _check(resp)
    except (ToolError, httpx.HTTPError) as exc:
        raise ToolError(
            f"Created report {report_token} but failed to set its name and "
            f"description ({exc}). It exists untitled at "
            f"https://app.mode.com/{mode.workspace}/reports/{report_token} — retry "
            f"with update_report(report_token={report_token!r}, ...) or archive it."
        ) from exc
    report = resp.json()

    async def _add_one(q: dict[str, Any]) -> dict[str, Any]:
        q_resp = await mode.client.post(
            f"/reports/{report_token}/queries",
            json={
                "query": {
                    "raw_query": q["raw_query"],
                    "data_source_id": q["data_source_id"],
                    "name": q.get("name", ""),
                }
            },
        )
        _check(q_resp)
        return q_resp.json()

    # The report POST already succeeded; don't let one bad query raise and
    # leave the caller without a handle on the partially-built report.
    query_list = queries or []
    results = await asyncio.gather(
        *[_add_one(q) for q in query_list], return_exceptions=True
    )
    failures = [
        {"name": q.get("name", ""), "error": str(r)}
        for q, r in zip(query_list, results)
        if isinstance(r, Exception)
    ]
    added = [r for r in results if not isinstance(r, Exception)]

    return {
        "token": report_token,
        "name": report.get("name"),
        "description": report.get("description", ""),
        "url": f"https://app.mode.com/{mode.workspace}/reports/{report_token}",
        "queries_added": len(added),
        "queries_failed": failures,
    }


@mcp.tool()
async def update_report(
    ctx: Context,
    report_token: str,
    name: str | None = None,
    description: str | None = None,
    space_token: str | None = None,
) -> dict[str, Any]:
    """Update a report's name, description, or move it to a different space.

    This is a write operation — it modifies an existing report.

    Args:
        report_token: The report to update.
        name: New report name. Omit to leave unchanged.
        description: New description. Omit to leave unchanged.
        space_token: Move report to this space. Omit to leave in current space.
    """
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if space_token is not None:
        body["space_token"] = space_token

    if not body:
        raise ToolError("Nothing to update — provide at least one of name, description, or space_token.")

    mode = _mode(ctx)
    resp = await mode.client.patch(
        f"/reports/{report_token}",
        json={"report": body},
    )
    _check(resp)
    report = resp.json()
    return {
        "token": report["token"],
        "name": report.get("name"),
        "description": report.get("description", ""),
        "url": f"https://app.mode.com/{mode.workspace}/reports/{report['token']}",
    }


def _confirm_code(*parts: Any) -> str:
    """Short fingerprint of what a delete preview showed.

    Deletes only proceed when the caller passes back the code from a preview,
    so the preview can't be skipped, and a resource that changed since the
    preview (renamed, SQL edited, queries added) won't be deleted by mistake.
    """
    blob = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:8]


def _verify_confirm_code(given: str, expected: str, tool: str) -> None:
    if given.strip().lower() != expected:
        raise ToolError(
            f"confirm_code does not match. The resource may have changed since the "
            f"preview, or the code is wrong. Call {tool} again without confirm_code "
            f"to get a fresh preview, show it to the user, and only then delete."
        )


@mcp.tool()
async def delete_query(
    ctx: Context,
    report_token: str,
    query_token: str,
    confirm_code: str | None = None,
) -> dict[str, Any]:
    """Permanently delete a query from a report. This cannot be undone.

    Two steps, always:
    1. Call without confirm_code. Returns a preview (query name, author, SQL) and a
       confirm_code. Nothing is deleted.
    2. Show the preview to the user and get their explicit approval. Only then
       call again with the confirm_code from step 1.

    Args:
        report_token: The report containing the query.
        query_token: The query to delete (use get_report to find query tokens).
        confirm_code: The code returned by the preview call. Never guess it.
    """
    mode = _mode(ctx)
    resp = await mode.client.get(f"/reports/{report_token}/queries/{query_token}")
    _check(resp)
    q = resp.json()
    code = _confirm_code(
        query_token, q.get("name"), q.get("raw_query"), q.get("updated_at")
    )

    if confirm_code is None:
        return {
            "confirm_required": True,
            "report_token": report_token,
            "query_token": query_token,
            "name": q.get("name", ""),
            "creator": _creator(q),
            "raw_query": q.get("raw_query", ""),
            "confirm_code": code,
            "message": (
                "Nothing was deleted. Show this to the user; if they approve, "
                "call delete_query again with this confirm_code."
            ),
        }

    _verify_confirm_code(confirm_code, code, "delete_query")
    resp = await mode.client.delete(
        f"/reports/{report_token}/queries/{query_token}"
    )
    _check(resp)
    return {"deleted": True, "query_token": query_token, "name": q.get("name", "")}


@mcp.tool()
async def delete_report(
    ctx: Context, report_token: str, confirm_code: str | None = None
) -> dict[str, Any]:
    """Permanently delete a report and all its queries. This cannot be undone.

    Prefer archive_report unless the user explicitly wants a permanent delete —
    archiving hides the report but can be reversed with unarchive_report.

    Two steps, always:
    1. Call without confirm_code. Returns a preview (name, queries and who wrote
       them, whether it's already archived) and
       a confirm_code. Nothing is deleted.
    2. Show the preview to the user and get their explicit approval. Only then
       call again with the confirm_code from step 1.

    Args:
        report_token: The report to delete (use list_reports to find tokens).
        confirm_code: The code returned by the preview call. Never guess it.
    """
    mode = _mode(ctx)
    report_resp, queries_resp = await asyncio.gather(
        mode.client.get(f"/reports/{report_token}"),
        mode.client.get(f"/reports/{report_token}/queries"),
    )
    _check(report_resp)
    _check(queries_resp)

    report = report_resp.json()
    queries = _embedded(queries_resp.json(), "queries")
    code = _confirm_code(
        report_token,
        report.get("name"),
        report.get("updated_at"),
        sorted((q.get("token"), q.get("raw_query")) for q in queries),
    )

    if confirm_code is None:
        return {
            "confirm_required": True,
            "report_token": report_token,
            "name": report.get("name"),
            "archived": bool(report.get("archived")),
            "query_count": len(queries),
            # Mode doesn't expose a report's creator, only each query's.
            "queries": [
                {"name": q.get("name", ""), "creator": _creator(q)} for q in queries
            ],
            "url": f"https://app.mode.com/{mode.workspace}/reports/{report_token}",
            "confirm_code": code,
            "message": (
                "Nothing was deleted. Show this to the user and suggest "
                "archive_report as a reversible alternative; if they still want "
                "a permanent delete, call delete_report again with this confirm_code."
            ),
        }

    _verify_confirm_code(confirm_code, code, "delete_report")
    resp = await mode.client.delete(f"/reports/{report_token}")
    _check(resp)
    return {"deleted": True, "report_token": report_token, "name": report.get("name")}


@mcp.tool()
async def archive_report(ctx: Context, report_token: str) -> dict[str, Any]:
    """Archive a report, hiding it from view. Reversible with unarchive_report.

    This is the safe alternative to delete_report.

    Args:
        report_token: The report to archive.
    """
    mode = _mode(ctx)
    resp = await mode.client.patch(f"/reports/{report_token}/archive")
    _check(resp)
    report = resp.json()
    return {
        "archived": True,
        "report_token": report_token,
        "name": report.get("name"),
    }


@mcp.tool()
async def unarchive_report(ctx: Context, report_token: str) -> dict[str, Any]:
    """Restore an archived report.

    Args:
        report_token: The report to unarchive.
    """
    mode = _mode(ctx)
    resp = await mode.client.patch(f"/reports/{report_token}/unarchive")
    _check(resp)
    report = resp.json()
    return {
        "archived": False,
        "report_token": report_token,
        "name": report.get("name"),
    }


@mcp.tool()
async def add_query(
    ctx: Context,
    report_token: str,
    raw_query: str,
    data_source_id: int,
    name: str = "",
) -> dict[str, Any]:
    """Add a SQL query to an existing report.

    This is a write operation — it adds a new query to the report.

    Args:
        report_token: The report to add the query to.
        raw_query: SQL query text.
        data_source_id: Data source ID (use list_data_sources to find it).
        name: Optional query name.
    """
    mode = _mode(ctx)
    resp = await mode.client.post(
        f"/reports/{report_token}/queries",
        json={
            "query": {
                "raw_query": raw_query,
                "data_source_id": data_source_id,
                "name": name,
            }
        },
    )
    _check(resp)
    q = resp.json()
    return {
        "token": q["token"],
        "name": q.get("name", ""),
        "raw_query": q.get("raw_query", ""),
        "data_source_id": q.get("data_source_id"),
    }


@mcp.tool()
async def update_query(
    ctx: Context,
    report_token: str,
    query_token: str,
    raw_query: str | None = None,
    name: str | None = None,
    data_source_id: int | None = None,
) -> dict[str, Any]:
    """Update a SQL query in a report.

    This is a write operation — it modifies an existing query.

    Args:
        report_token: The report containing the query.
        query_token: The query to update (use get_report to find query tokens).
        raw_query: New SQL text. Omit to leave unchanged.
        name: New query name. Omit to leave unchanged.
        data_source_id: New data source ID. Omit to leave unchanged.
    """
    body: dict[str, Any] = {}
    if raw_query is not None:
        body["raw_query"] = raw_query
    if name is not None:
        body["name"] = name
    if data_source_id is not None:
        body["data_source_id"] = data_source_id

    if not body:
        raise ToolError("Nothing to update — provide at least one of raw_query, name, or data_source_id.")

    mode = _mode(ctx)
    resp = await mode.client.patch(
        f"/reports/{report_token}/queries/{query_token}",
        json={"query": body},
    )
    _check(resp)
    q = resp.json()
    return {
        "token": q["token"],
        "name": q.get("name", ""),
        "raw_query": q.get("raw_query", ""),
        "data_source_id": q.get("data_source_id"),
    }


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


async def _fetch_run_results(
    client: httpx.AsyncClient,
    report_token: str,
    run_token: str,
    max_rows_per_query: int | None = 500,
) -> list[dict[str, Any]]:
    """Fetch results for all query runs in a report run."""
    qr_resp = await client.get(f"/reports/{report_token}/runs/{run_token}/query_runs")
    _check(qr_resp)
    query_runs = _embedded(qr_resp.json(), "query_runs")

    async def _fetch_one(qr: dict[str, Any]) -> dict[str, Any]:
        qr_token = qr["token"]
        r = await client.get(
            f"/reports/{report_token}/runs/{run_token}"
            f"/query_runs/{qr_token}/results/content.json"
        )
        if r.status_code != 200:
            return {
                "query_name": qr.get("query_name", ""),
                "state": qr.get("state"),
                "error": f"HTTP {r.status_code}",
            }
        data = r.json()
        out: dict[str, Any] = {
            "query_name": qr.get("query_name", ""),
            "state": qr.get("state"),
        }
        if isinstance(data, list):
            total = len(data)
            out["row_count"] = total
            if max_rows_per_query is not None and total > max_rows_per_query:
                out["data"] = data[:max_rows_per_query]
                out["truncated"] = True
            else:
                out["data"] = data
                out["truncated"] = False
        else:
            # Unexpected non-list payload — pass through unchanged.
            out["data"] = data
        return out

    return list(await asyncio.gather(*[_fetch_one(qr) for qr in query_runs]))


@mcp.tool()
async def get_run_results(
    ctx: Context,
    report_token: str,
    run_token: str | None = None,
    max_rows_per_query: int | None = 500,
) -> list[dict[str, Any]]:
    """Get results from a report run. Defaults to the latest run.

    Each query's rows are truncated to `max_rows_per_query` by default so a
    single call can't blow past the agent's context window. When a query was
    truncated, the result includes `truncated: true` and `row_count: <total>`
    so you know to re-call with a higher limit (or null) if you need all rows.

    Args:
        report_token: The report token.
        run_token: Specific run token. If omitted, uses the latest succeeded run.
        max_rows_per_query: Max rows to return per query. Default 500. Pass null
            to fetch every row — can be very large (hundreds of MB) for
            dashboards with long time-series or wide cross-tabs.
    """
    mode = _mode(ctx)
    if not run_token:
        runs_resp = await mode.client.get(
            f"/reports/{report_token}/runs",
            params={"order": "desc", "order_by": "created_at"},
        )
        _check(runs_resp)
        runs = _embedded(runs_resp.json(), "report_runs")
        succeeded = [r for r in runs if r.get("state") == "succeeded"]
        if not succeeded:
            raise ToolError("No succeeded runs found for this report.")
        run_token = succeeded[0]["token"]

    return await _fetch_run_results(
        mode.client, report_token, run_token, max_rows_per_query
    )



WRITE_TOOLS = frozenset({
    "create_report",
    "update_report",
    "archive_report",
    "unarchive_report",
    "add_query",
    "update_query",
    "delete_query",
    "delete_report",
})

DELETE_TOOLS = frozenset({"delete_query", "delete_report"})


def _env_flag(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() in {"1", "true", "yes"}


def _disabled_tools(env: Mapping[str, str], all_tools: set[str]) -> set[str]:
    """Work out which tools to disable from the MODE_* tool settings."""
    disabled: set[str] = set()
    if not _env_flag(env, "MODE_ENABLE_DELETES"):
        disabled |= DELETE_TOOLS
    if _env_flag(env, "MODE_READ_ONLY"):
        disabled |= WRITE_TOOLS
    names = {n.strip() for n in env.get("MODE_DISABLED_TOOLS", "").split(",") if n.strip()}
    unknown = names - all_tools
    if unknown:
        raise ValueError(
            f"MODE_DISABLED_TOOLS has unknown tool(s): {', '.join(sorted(unknown))}. "
            f"Valid tools: {', '.join(sorted(all_tools))}"
        )
    return disabled | names


def main() -> None:
    all_tools = {t.name for t in asyncio.run(mcp.list_tools())}
    for name in _disabled_tools(os.environ, all_tools):
        mcp.remove_tool(name)
    mcp.run()


if __name__ == "__main__":
    main()
