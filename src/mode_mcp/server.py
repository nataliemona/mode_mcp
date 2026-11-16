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
import csv
import hashlib
import json
import os
import re
import secrets
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

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


async def _report_parameters(
    mode: ModeContext, report: dict[str, Any]
) -> list[Any] | None:
    """The form fields on the report's latest run, or None if unreadable."""
    href = report.get("_links", {}).get("last_run", {}).get("href")
    if not href:
        return None
    try:
        resp = await mode.client.get(href.removeprefix(f"/api/{mode.workspace}"))
        _check(resp)
    except (httpx.HTTPError, ToolError):
        return None
    return resp.json().get("form_fields") or []


@mcp.tool()
async def get_report(ctx: Context, report_token: str) -> dict[str, Any]:
    """Get a report's details, its queries (SQL) and their charts.

    `parameters` are the form fields Mode has registered for the report (as
    recorded on its latest run), i.e. what run_report can take as `parameters`.
    It is null when that couldn't be read, and empty when the report has none;
    a `{% form %}` in the SQL only counts once it is saved in Mode.

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
    charts_by_query = await asyncio.gather(
        *[_fetch_charts(mode.client, report_token, q["token"]) for q in queries],
        return_exceptions=True,
    )
    parameters = await _report_parameters(mode, report)

    return {
        "token": report["token"],
        "name": report.get("name"),
        "description": report.get("description", ""),
        "archived": bool(report.get("archived")),
        "created_at": report.get("created_at"),
        "last_run_at": report.get("last_run_at"),
        "parameters": parameters,
        "queries": [
            {
                "token": q["token"],
                "name": q.get("name", ""),
                "creator": _creator(q),
                "raw_query": q.get("raw_query", ""),
                "data_source_id": q.get("data_source_id"),
                "charts": (
                    None
                    if isinstance(charts, BaseException)
                    else [_chart_summary(c) for c in charts]
                ),
            }
            for q, charts in zip(queries, charts_by_query)
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
# Charts
# ---------------------------------------------------------------------------

ENCODING_CHANNELS = ("x", "y", "x2", "y2", "value", "row", "column")


async def _fetch_charts(
    client: httpx.AsyncClient, report_token: str, query_token: str
) -> list[dict[str, Any]]:
    """Fetch the raw chart objects attached to one query."""
    resp = await client.get(f"/reports/{report_token}/queries/{query_token}/charts")
    _check(resp)
    return _embedded(resp.json(), "charts")


def _chart_summary(chart: dict[str, Any]) -> dict[str, Any]:
    """Chart token, type and title.

    Version 2 charts render from `view_vegas` and their legacy `view` is stale,
    so it is only read for version 1 charts or charts with no `view_vegas` yet.
    """
    vegas = chart.get("view_vegas") or {}
    if chart.get("view_version") == 2 and vegas:
        chart_type, title = vegas.get("chartType"), vegas.get("title")
    else:
        view = chart.get("view") or {}
        chart_type, title = view.get("selectedChart"), view.get("chartTitle")
    return {"chart_token": chart["token"], "type": chart_type, "title": title}


def _encoding_digest(vegas: dict[str, Any]) -> dict[str, list[str]]:
    """The formulas bound to each channel of a `view_vegas` encoding.

    e.g. {"x": ["[week]"], "y": ["SUM([sessions])"], "marks.color": ["[browser]"]}.
    """
    encoding = vegas.get("encoding") or {}
    digest = {
        channel: [item.get("formula") for item in encoding[channel]]
        for channel in ENCODING_CHANNELS
        if encoding.get(channel)
    }
    properties = (encoding.get("marks") or {}).get("properties") or {}
    for name, prop in properties.items():
        values = prop.get("values") if isinstance(prop, dict) else None
        if values:
            digest[f"marks.{name}"] = [item.get("formula") for item in values]
    return digest


@mcp.tool()
async def list_charts(
    ctx: Context, report_token: str, query_token: str | None = None
) -> list[dict[str, Any]]:
    """List the charts in a report, or on one query.

    Args:
        report_token: The report token.
        query_token: Only list charts on this query. If omitted, covers every
            query in the report.
    """
    mode = _mode(ctx)
    if query_token:
        queries = [{"token": query_token, "name": None}]
    else:
        queries_resp = await mode.client.get(f"/reports/{report_token}/queries")
        _check(queries_resp)
        queries = _embedded(queries_resp.json(), "queries")

    charts_by_query = await asyncio.gather(
        *[_fetch_charts(mode.client, report_token, q["token"]) for q in queries]
    )
    return [
        {
            **_chart_summary(chart),
            "query_token": q["token"],
            "query_name": q.get("name"),
        }
        for q, charts in zip(queries, charts_by_query)
        for chart in charts
    ]


@mcp.tool()
async def get_chart(
    ctx: Context,
    report_token: str,
    query_token: str,
    chart_token: str,
    include_vegas: bool = False,
) -> dict[str, Any]:
    """Get one chart's type, title and what it plots.

    For version 2 charts `encoding` lists the formulas bound to each channel
    (x, y, color, ...). The full `view_vegas` is large (tens of KB), so it is
    only returned with `include_vegas`; pass it when you need to copy or edit
    a chart's raw settings.

    Args:
        report_token: The report token.
        query_token: The query the chart is attached to.
        chart_token: The chart token.
        include_vegas: Also return the raw `view_vegas` object.
    """
    mode = _mode(ctx)
    resp = await mode.client.get(
        f"/reports/{report_token}/queries/{query_token}/charts/{chart_token}"
    )
    _check(resp)
    chart = resp.json()
    vegas = chart.get("view_vegas") or {}
    out: dict[str, Any] = {
        **_chart_summary(chart),
        "query_token": query_token,
        "view_version": chart.get("view_version"),
        "switch_view_token": chart.get("switch_view_token"),
        "view": chart.get("view"),
        "encoding": _encoding_digest(vegas),
    }
    if include_vegas:
        out["view_vegas"] = vegas
    return out


ChartType = Literal["line", "bar", "area", "pie", "table", "big_number"]
Orientation = Literal["vertical", "horizontal"]

MARK_CHANNELS = ("baseline", "color", "size", "text", "label", "detail", "angle", "tooltip")

# Mode's chartType for each (type, stacked, horizontal) combination.
BAR_CHART_TYPES = {
    (False, False): "bar",
    (True, False): "stackedBar",
    (False, True): "hBar",
    (True, True): "hStackedBar",
}
CHART_TYPES = {
    "line": "line", "area": "area", "pie": "pie", "table": "table", "big_number": "bigValue",
}


# Mode's predefined default palette ("Mode Ten"). Workspace palette listings only
# include custom palettes, so this is the fallback when none is marked default.
MODE_DEFAULT_PALETTE = "e7882faa26e7"

# Mode's default `format` for a chart, minus `colors` (which come from the palette).
DEFAULT_FORMAT: dict[str, Any] = {
    "xAxis": {
        "rotate": None,
        "dateFormat": "auto",
        "numberRange": {
            "min": None,
            "max": None
        },
        "numberFormat": {
            "format": None,
            "precision": None
        },
        "title": None,
        "titleEnabled": True,
        "compact": False,
        "showAxisLabels": True,
        "axisLabels": None,
        "baseFormat": {}
    },
    "yAxis": {
        "numberRange": {
            "min": None,
            "max": None
        },
        "numberFormat": {
            "format": None,
            "precision": None
        },
        "title": None,
        "titleEnabled": True,
        "scaleType": "linear",
        "baseFormat": {},
        "valueFieldFormatMap": {}
    },
    "y2Axis": {
        "numberRange": {
            "min": None,
            "max": None
        },
        "numberFormat": {
            "format": None,
            "precision": None
        },
        "title": None,
        "titleEnabled": True,
        "scaleType": "linear",
        "baseFormat": {}
    },
    "flatTable": {
        "general": {
            "sizingStyle": "sizeToFit",
            "colorScheme": "mode-gray",
            "density": "standard",
            "colGridlines": True,
            "rowGridlines": True,
            "rowNumbers": False,
            "dataProfilingEnabled": True,
            "pageSize": 100
        },
        "columns": []
    },
    "legend": {
        "enabled": True,
        "position": "auto",
        "title": None,
        "titleEnabled": True,
        "baseFormat": {}
    },
    "bigValue": {
        "description": None,
        "primaryTrendFormat": {
            "label": None
        }
    },
    "nullToZero": False,
    "showValues": True,
    "pieLabelsNotPercentages": False,
    "drilldownEnabled": True,
    "useMuzeRenderer": True
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `override` into `base`; lists and scalars are replaced."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _resolve_columns(names: list[str], available: list[str]) -> list[str]:
    """Match requested column names to the result's columns, ignoring case.

    Warehouses differ in the case they return (Snowflake upper-cases), so a
    near match is rewritten to the canonical name instead of rejected.
    """
    lookup = {c.lower(): c for c in available}
    missing = [n for n in names if n.lower() not in lookup]
    if missing:
        raise ToolError(
            f"Unknown column(s): {', '.join(missing)}. "
            f"Available columns: {', '.join(available)}."
        )
    return [lookup[n.lower()] for n in names]


async def _latest_columns(
    client: httpx.AsyncClient, report_token: str, query_token: str
) -> list[str]:
    """Column names from a query's latest succeeded run.

    Reads only the CSV header line, since the JSON result can be huge.
    """
    runs_resp = await client.get(
        f"/reports/{report_token}/runs",
        params={"order": "desc", "order_by": "created_at"},
    )
    _check(runs_resp)
    run = next(
        (r for r in _embedded(runs_resp.json(), "report_runs") if r.get("state") == "succeeded"),
        None,
    )
    if run is None:
        raise ToolError(
            "No succeeded run to read columns from. Call run_report first."
        )
    qr_resp = await client.get(f"/reports/{report_token}/runs/{run['token']}/query_runs")
    _check(qr_resp)
    qr = next(
        (
            q
            for q in _embedded(qr_resp.json(), "query_runs")
            if q.get("query_token") == query_token and q.get("state") == "succeeded"
        ),
        None,
    )
    if qr is None:
        raise ToolError(
            "The latest succeeded run has no results for this query. Call run_report first."
        )

    results = f"/reports/{report_token}/runs/{run['token']}/query_runs/{qr['token']}/results"
    async with client.stream("GET", f"{results}/content.csv") as resp:
        if resp.status_code == 200:
            async for line in resp.aiter_lines():
                header = next(csv.reader([line.lstrip("﻿")]))
                if header:
                    return header
    json_resp = await client.get(f"{results}/content.json")
    _check(json_resp)
    rows = json_resp.json()
    if not rows:
        raise ToolError("The latest succeeded run returned no rows, so there are no columns.")
    return list(rows[0])


def _build_vegas(
    type: ChartType,
    x: str | None,
    y: list[str],
    color: str | None,
    title: str | None,
    stacked: bool,
    horizontal: bool,
) -> dict[str, Any]:
    """A version 2 `view_vegas` object for the simple chart fields.

    Follows the shapes Mode's own editor produces: cartesian charts bind the
    category column and `$VALUES` to the axes with one SUM measure per y column;
    pie, table and big-number charts put their columns in the mark properties.
    """
    needs = {
        "line": x and y, "bar": x and y, "area": x and y,
        "pie": x and y, "big_number": y, "table": x or y,
    }
    if not needs[type]:
        raise ToolError(
            {"big_number": "big_number needs y.", "table": "table needs x or y."}.get(
                type, f"{type} needs x and y."
            )
        )

    seen: set[str] = set()

    def item(formula: str, kind: str) -> dict[str, Any]:
        while (item_id := secrets.token_hex(2)) in seen:
            pass
        seen.add(item_id)
        return {"id": item_id, "formula": formula, "variable": {"type": kind}}

    def properties(mark: dict[str, Any], values: dict[str, list], options: dict) -> dict:
        return {
            "type": mark,
            **{name: {"values": values.get(name, [])} for name in MARK_CHANNELS},
            "options": options,
        }

    encoding: dict[str, Any] = {
        channel: [] for channel in ("row", "column", "x", "y", "x2", "y2", "value")
    }
    if type in ("line", "bar", "area"):
        category, measure = ("y", "x") if horizontal else ("x", "y")
        mark = {"line": "Line", "bar": "Bar", "area": "Area"}[type]
        values_field = item("$VALUES", "CONTINUOUS")
        encoding[category] = [item(f"[{x}]", "DISCRETE")]
        encoding[measure] = [values_field]
        encoding["value"] = [item(f"SUM([{c}])", "CONTINUOUS") for c in y]
        color_formula = f"[{color}]" if color else "$NAMES" if len(y) > 1 else None
        options = (
            {mark: {"type": mark, "transform": "stack" if stacked else "group"}}
            if type == "bar"
            else {mark: {"type": mark, "transform": "stack"}} if type == "area" and stacked
            else {}
        )
        encoding["marks"] = {
            "channel": measure,
            "series": [
                {
                    "field": values_field["id"],
                    "properties": properties(
                        {"value": mark, "automatic": False},
                        {"color": [item(color_formula, "DISCRETE")]} if color_formula else {},
                        options,
                    ),
                }
            ],
            "properties": properties({"value": None, "automatic": True}, {}, {}),
        }
        chart_type = (
            BAR_CHART_TYPES[(stacked, horizontal)] if type == "bar" else CHART_TYPES[type]
        )
    else:
        if type == "pie":
            values = {
                "color": [item(f"[{x}]", "DISCRETE")],
                "angle": [item(f"SUM([{y[0]}])", "CONTINUOUS")],
            }
            mark = {"value": "Pie", "automatic": False}
        elif type == "big_number":
            values = {"text": [item(f"SUM([{y[0]}])", "CONTINUOUS")]}
            mark = {"value": "Text", "automatic": True}
        else:
            values = {"text": [item(f"[{c}]", "DISCRETE") for c in ([x] if x else []) + y]}
            mark = {"value": "Text", "automatic": True}
        encoding["marks"] = {
            "channel": "*",
            "series": [],
            "properties": properties(mark, values, {}),
        }
        chart_type = CHART_TYPES[type]

    encoding.update(
        sorts=[], filter=[], totals={"grand": {"row": False, "column": False}}, groups=[]
    )
    return {
        "encoding": encoding,
        "chartType": chart_type,
        "title": title or "",
        "description": "",
        "caption": {"text": "", "isHidden": False, "isTitleHidden": False, "position": "top"},
        "fieldFormats": {},
        "facetSizes": {},
        "isAutoUpdateDisabled": False,
    }


def _describe_vegas(vegas: dict[str, Any]) -> dict[str, Any] | None:
    """Invert `_build_vegas`: the simple fields behind a `view_vegas` object.

    Returns None when the chart has a shape this server doesn't build (e.g. one
    made with several measures on both axes in the Mode editor).
    """
    chart_type = vegas.get("chartType")
    encoding = vegas.get("encoding") or {}
    marks = encoding.get("marks") or {}

    def columns(items: list[dict[str, Any]], aggregated: bool = False) -> list[str]:
        pattern = r"SUM\(\[(.+)\]\)" if aggregated else r"\[(.+)\]"
        found = [re.fullmatch(pattern, i.get("formula", "")) for i in items]
        if not found or not all(found):
            raise ValueError
        return [m.group(1) for m in found if m]

    def values(props: dict[str, Any], name: str) -> list[dict[str, Any]]:
        return (props.get(name) or {}).get("values", [])

    try:
        props = marks.get("properties") or {}
        if chart_type in ("line", "area", "bar", "stackedBar", "hBar", "hStackedBar"):
            horizontal = chart_type in ("hBar", "hStackedBar")
            series = (marks.get("series") or [{}])[0].get("properties") or {}
            color_items = [i for i in values(series, "color") if i["formula"] != "$NAMES"]
            transform = next(iter((series.get("options") or {}).values()), {}).get("transform")
            return {
                "type": "bar" if chart_type.lower().endswith("bar") else chart_type,
                "x": columns(encoding["y" if horizontal else "x"])[0],
                "y": columns(encoding.get("value", []), aggregated=True),
                "color": columns(color_items)[0] if color_items else None,
                "stacked": chart_type in ("stackedBar", "hStackedBar") or transform == "stack",
                "horizontal": horizontal,
            }
        if chart_type == "pie":
            return {
                "type": "pie",
                "x": columns(values(props, "color"))[0],
                "y": columns(values(props, "angle"), aggregated=True),
                "color": None, "stacked": False, "horizontal": False,
            }
        if chart_type == "bigValue":
            return {
                "type": "big_number", "x": None,
                "y": columns(values(props, "text"), aggregated=True),
                "color": None, "stacked": False, "horizontal": False,
            }
        if chart_type == "table":
            return {
                "type": "table", "x": None, "y": columns(values(props, "text")),
                "color": None, "stacked": False, "horizontal": False,
            }
    except (KeyError, IndexError, ValueError):
        return None
    return None


async def _palette(
    client: httpx.AsyncClient, token: str | None
) -> tuple[str, list[str]]:
    """A color palette's token and colors; the workspace default if no token."""
    if token is None:
        resp = await client.get("/color_palettes")
        _check(resp)
        default = next(
            (p for p in _embedded(resp.json(), "color_palettes") if p.get("is_default")),
            None,
        )
        token = default["token"] if default else MODE_DEFAULT_PALETTE
    resp = await client.get(f"/color_palettes/{token}")
    _check(resp)
    return token, [c.strip() for c in resp.json().get("value", "").split(",") if c.strip()]


def _field_colors(
    vegas: dict[str, Any], token: str, colors: list[str]
) -> dict[str, Any]:
    """`fieldColors` entries for every field the chart colors by.

    A `$NAMES` split colors by measure, so its map is the y column names in
    order; a column split would need its distinct values, which Mode fills in
    when it renders.
    """
    encoding = vegas.get("encoding") or {}
    marks = encoding.get("marks") or {}
    names = [
        m.group(1)
        for i in encoding.get("value") or []
        if (m := re.fullmatch(r"SUM\(\[(.+)\]\)", i.get("formula", "")))
    ]
    holders = [s.get("properties") or {} for s in marks.get("series") or []]
    holders.append(marks.get("properties") or {})
    out: dict[str, Any] = {}
    for props in holders:
        for item in (props.get("color") or {}).get("values", []):
            formula = item["formula"]
            out[f"@DIS_{formula}"] = {
                "colorPaletteToken": token,
                "palette": colors,
                "map": {n: i for i, n in enumerate(names)} if formula == "$NAMES" else {},
            }
    return out


def _with_color_defaults(
    vegas: dict[str, Any], token: str, colors: list[str]
) -> dict[str, Any]:
    """Fill in Mode's default format and color settings, keeping what's set.

    Mode's UI breaks on a chart without `format` (including `format.colors`)
    and `fieldColors`, so anything missing is added and nothing set is changed.
    """
    defaults = _deep_merge(
        DEFAULT_FORMAT,
        {
            "useMuzeRenderer": vegas.get("chartType") not in ("table", "bigValue"),
            "colors": {"theme": token, "custom": colors, "map": {}},
        },
    )
    return {
        **vegas,
        "format": _deep_merge(defaults, vegas.get("format") or {}),
        "fieldColors": {
            **_field_colors(vegas, token, colors),
            **(vegas.get("fieldColors") or {}),
        },
    }


def _form_path(chart: dict[str, Any], form: str, workspace: str) -> str:
    """A HAL form's action as a path relative to the client's base URL."""
    action = chart["_forms"][form]["action"]
    return action.removeprefix(f"/api/{workspace}")


async def _save_vegas(
    client: httpx.AsyncClient, path: str, vegas: dict[str, Any], palette_token: str
) -> dict[str, Any]:
    """PATCH a chart's `view_vegas` and palette. Mode only takes `view_vegas` as a JSON string."""
    resp = await client.patch(
        path,
        json={
            "visualization": {
                "view_vegas": json.dumps(vegas),
                "view_version": 2,
                "color_palette_token": palette_token,
            }
        },
    )
    _check(resp)
    return resp.json()


@mcp.tool()
async def create_chart(
    ctx: Context,
    report_token: str,
    query_token: str,
    type: ChartType,
    x: str | None = None,
    y: list[str] | None = None,
    color: str | None = None,
    title: str | None = None,
    stacked: bool = False,
    orientation: Orientation = "vertical",
    view: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add a chart to a query. Uses an undocumented Mode endpoint; may break without notice.

    Column names are checked against the query's latest succeeded run (run
    the report first), case-insensitively. Each y column is plotted as SUM(y).
    Check the result in Mode's UI: this server can't render the chart.

    Args:
        report_token: The report token.
        query_token: The query that feeds the chart.
        type: line, bar, area, pie, table or big_number.
        x: Category/x-axis column. For a pie, the slice column; for a table,
            an optional first column.
        y: Value columns. For a pie or big_number only the first is used; for
            a table they are the remaining columns.
        color: Column that splits the series by color (line, bar, area).
        title: Chart title.
        stacked: Stack bars or areas instead of grouping them.
        orientation: "horizontal" swaps the axes of a bar chart.
        view: Raw `view_vegas` object, usually copied from
            get_chart(include_vegas=True) and edited. Merged over the fields
            above, so it wins where they overlap.
    """
    mode = _mode(ctx)
    y = y or []
    wanted = [c for c in [x, color, *y] if c]
    if wanted:
        available = await _latest_columns(mode.client, report_token, query_token)
        resolved = dict(zip(wanted, _resolve_columns(wanted, available)))
        x, color = resolved.get(x or ""), resolved.get(color or "")
        y = [resolved[c] for c in y]
    vegas = _build_vegas(type, x, y, color, title, stacked, orientation == "horizontal")

    base = f"/reports/{report_token}/queries/{query_token}/charts"
    created = await mode.client.post(base, json={"chart": {"view_version": 2}})
    _check(created)
    chart = created.json()
    try:
        palette_token, palette_colors = await _palette(
            mode.client, chart.get("color_palette_token")
        )
        vegas = _with_color_defaults(vegas, palette_token, palette_colors)
        if view:
            vegas = _deep_merge(vegas, view)
        saved = await _save_vegas(
            mode.client, _form_path(chart, "edit", mode.workspace), vegas, palette_token
        )
    except Exception:
        await mode.client.delete(_form_path(chart, "destroy", mode.workspace))
        raise
    return {
        **_chart_summary(saved),
        "query_token": query_token,
        "web_path": chart.get("_links", {}).get("report_viz_web", {}).get("href"),
    }


@mcp.tool()
async def update_chart(
    ctx: Context,
    report_token: str,
    query_token: str,
    chart_token: str,
    type: ChartType | None = None,
    x: str | None = None,
    y: list[str] | None = None,
    color: str | None = None,
    title: str | None = None,
    stacked: bool | None = None,
    orientation: Orientation | None = None,
    view: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Change a chart. Uses an undocumented Mode endpoint; may break without notice.

    Only the fields you pass change; the chart's colors, number formats and
    other settings are kept. Any default format and color settings the chart is
    missing (charts made by older versions of create_chart lack them, and
    break in Mode's UI) are filled in on every call, so calling this with no
    changes repairs such a chart. If you change type, x, y, color, stacked or
    orientation, the rest of those are read back from the chart. A chart built
    in Mode's editor with a shape this server can't read back needs all of
    them passed, or the change made through `view`. Same arguments as create_chart.

    Args:
        report_token: The report token.
        query_token: The query the chart is attached to.
        chart_token: The chart token.
        type: line, bar, area, pie, table or big_number.
        x: Category/x-axis column.
        y: Value columns.
        color: Column that splits the series by color.
        title: Chart title.
        stacked: Stack bars or areas.
        orientation: "horizontal" or "vertical" for a bar chart.
        view: Raw `view_vegas` object merged over the result; it wins.
    """
    mode = _mode(ctx)
    resp = await mode.client.get(
        f"/reports/{report_token}/queries/{query_token}/charts/{chart_token}"
    )
    _check(resp)
    chart = resp.json()
    vegas = chart.get("view_vegas") or {}

    fields = {"type": type, "x": x, "y": y, "color": color, "stacked": stacked}
    if orientation is not None:
        fields["horizontal"] = orientation == "horizontal"
    if any(v is not None for v in fields.values()):
        current = _describe_vegas(vegas)
        if current is None:
            required = {"type": type, "x": x, "y": y}
            if type in ("big_number", "table"):
                required.pop("x")
            if any(v is None for v in required.values()):
                raise ToolError(
                    "This chart has a shape update_chart can't read back. Pass type, "
                    "x and y together, or make the change through `view`."
                )
            current = {"color": None, "stacked": False, "horizontal": False}
        merged = {**current, **{k: v for k, v in fields.items() if v is not None}}
        named = [c for c in [x, color, *(y or [])] if c]
        if named:
            available = await _latest_columns(mode.client, report_token, query_token)
            resolved = dict(zip(named, _resolve_columns(named, available)))
            merged["x"] = resolved.get(x or "", merged.get("x"))
            merged["color"] = resolved.get(color or "", merged.get("color"))
            merged["y"] = [resolved[c] for c in y] if y else merged.get("y", [])
        rebuilt = _build_vegas(
            merged["type"], merged.get("x"), merged.get("y") or [], merged.get("color"),
            None, merged["stacked"], merged["horizontal"],
        )
        old_encoding = vegas.get("encoding") or {}
        vegas = {
            **vegas,
            "chartType": rebuilt["chartType"],
            "encoding": {
                **rebuilt["encoding"],
                **{k: old_encoding[k] for k in ("filter", "totals", "groups") if k in old_encoding},
            },
        }
    if title is not None:
        vegas = {**vegas, "title": title}
    palette_token, palette_colors = await _palette(
        mode.client, chart.get("color_palette_token")
    )
    vegas = _with_color_defaults(vegas, palette_token, palette_colors)
    if view:
        vegas = _deep_merge(vegas, view)

    saved = await _save_vegas(
        mode.client, _form_path(chart, "edit", mode.workspace), vegas, palette_token
    )
    return {**_chart_summary(saved), "query_token": query_token}


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


TERMINAL_RUN_STATES = frozenset({"succeeded", "failed", "cancelled", "completed"})


def _project_rows(
    rows: list[dict[str, Any]], columns: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep only `columns` (matched ignoring case) in each row.

    Returns the projected rows and the requested names no column matched.
    """
    lookup = {c.lower(): c for c in rows[0]} if rows else {}
    keep = {n: lookup[n.lower()] for n in columns if n.lower() in lookup}
    missing = [n for n in columns if n not in keep]
    return [{name: row[name] for name in keep.values()} for row in rows], missing


async def _fetch_run_results(
    client: httpx.AsyncClient,
    report_token: str,
    run_token: str,
    max_rows_per_query: int | None = 500,
    columns: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Fetch results for all query runs in a report run.

    Query runs that didn't succeed have no result file, so their warehouse
    error is read from the query run itself. With `columns`, rows are cut down
    to those columns before truncation, and names no column matched are listed
    under `missing_columns`.
    """
    qr_resp = await client.get(f"/reports/{report_token}/runs/{run_token}/query_runs")
    _check(qr_resp)
    query_runs = _embedded(qr_resp.json(), "query_runs")

    async def _fetch_one(qr: dict[str, Any]) -> dict[str, Any]:
        qr_token = qr["token"]
        out: dict[str, Any] = {
            "query_name": qr.get("query_name", ""),
            "query_token": qr.get("query_token"),
            "state": qr.get("state"),
        }
        if qr.get("state") != "succeeded":
            out["error"] = qr.get("error_message")
            out["error_code"] = qr.get("error_code")
            return out
        r = await client.get(
            f"/reports/{report_token}/runs/{run_token}"
            f"/query_runs/{qr_token}/results/content.json"
        )
        if r.status_code != 200:
            out["error"] = f"HTTP {r.status_code}"
            return out
        data = r.json()
        if isinstance(data, list):
            if columns:
                data, missing = _project_rows(data, columns)
                if missing:
                    out["missing_columns"] = missing
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
    include_failed: bool = False,
    columns: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Get results from a report run. Defaults to the latest run.

    Each query's rows are truncated to `max_rows_per_query` by default so a
    single call can't blow past the agent's context window. When a query was
    truncated, the result includes `truncated: true` and `row_count: <total>`
    so you know to re-call with a higher limit (or null) if you need all rows.

    Args:
        report_token: The report token.
        run_token: Specific run token. If omitted, uses the latest succeeded run,
            or the latest run of any state when `include_failed` is set.
        max_rows_per_query: Max rows to return per query. Default 500. Pass null
            to fetch every row — can be very large (hundreds of MB) for
            dashboards with long time-series or wide cross-tabs.
        include_failed: When no run_token is given, use the latest run of any
            state instead of the latest succeeded one. Failed queries report
            their warehouse `error` and the `query_token` to fix with
            update_query.
        columns: Only return these columns (matched ignoring case). Saves
            context and keeps other columns, e.g. emails, out of the
            transcript. Names that match nothing are listed in
            `missing_columns`.
    """
    mode = _mode(ctx)
    if not run_token:
        runs_resp = await mode.client.get(
            f"/reports/{report_token}/runs",
            params={"order": "desc", "order_by": "created_at"},
        )
        _check(runs_resp)
        runs = _embedded(runs_resp.json(), "report_runs")
        if include_failed:
            if not runs:
                raise ToolError("No runs found for this report.")
            run_token = runs[0]["token"]
        else:
            succeeded = [r for r in runs if r.get("state") == "succeeded"]
            if not succeeded:
                raise ToolError("No succeeded runs found for this report.")
            run_token = succeeded[0]["token"]

    return await _fetch_run_results(
        mode.client, report_token, run_token, max_rows_per_query, columns
    )


def _parameter_report(
    requested: dict[str, Any] | None, run: dict[str, Any]
) -> dict[str, Any]:
    """`applied_parameters` and any `warnings` about parameters Mode may have ignored.

    Mode records the parameters it was sent even when the report has no form
    field to bind them to, so a run with no registered form fields is the signal.
    """
    out: dict[str, Any] = {}
    if "parameters" in run:
        out["applied_parameters"] = run["parameters"]
    if requested and "form_fields" in run and not run["form_fields"]:
        out["warnings"] = [
            f"Parameters {sorted(requested)} were sent, but this report has no registered "
            "form fields, so Mode probably ran with the form defaults. Declare them in a "
            "{% form %} block in a query's SQL and save the query in Mode."
        ]
    return out


@mcp.tool()
async def run_report(
    ctx: Context,
    report_token: str,
    parameters: dict[str, Any] | None = None,
    wait: bool = True,
    timeout_seconds: int = 120,
    max_rows_per_query: int | None = 50,
    columns: list[str] | None = None,
) -> dict[str, Any]:
    """Run a report and return per-query results, including warehouse errors.

    This is a write operation: it runs EVERY query in the report, which spends
    warehouse compute and adds to the report's run history. It is meant for
    checking that SQL works, so rows are truncated to a small default; keep
    reports small (or split them) while iterating.

    The API can't run a single query. To iterate on one expensive query, put it
    in a scratch report on its own and run that.

    A failed query reports its `error` and `query_token`, which maps straight
    to update_query. If the run is still going after `timeout_seconds`, returns
    `timed_out: true`; follow up with get_run_results.

    `applied_parameters` is what Mode recorded for the run. Parameters only
    take effect when the report has registered form fields (a `{% form %}`
    block in a query's SQL, saved in Mode); otherwise the defaults apply and
    the result carries a `warnings` entry.

    Args:
        report_token: The report token.
        parameters: Mode form values, e.g. {"weeks": 8}. If omitted, the
            report's form defaults apply.
        wait: Poll until the run finishes (default). If false, return the
            run_token and state immediately.
        timeout_seconds: Max seconds to wait for the run to finish.
        max_rows_per_query: Max rows to return per query. Default 50. Pass null
            to fetch every row.
        columns: Only return these columns (matched ignoring case); names that
            match nothing are listed in `missing_columns`.
    """
    mode = _mode(ctx)
    resp = await mode.client.post(
        f"/reports/{report_token}/runs",
        json={"parameters": parameters} if parameters else {},
    )
    _check(resp)
    run = resp.json()
    run_token = run["token"]
    state = run.get("state")
    if not wait:
        return {"run_token": run_token, "state": state, **_parameter_report(parameters, run)}

    deadline = time.monotonic() + timeout_seconds
    delay = 1.0
    while state not in TERMINAL_RUN_STATES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"run_token": run_token, "state": "running", "timed_out": True}
        await asyncio.sleep(min(delay, remaining))
        delay = min(delay * 1.5, 5.0)
        poll = await mode.client.get(f"/reports/{report_token}/runs/{run_token}")
        _check(poll)
        run = poll.json()
        state = run.get("state")

    return {
        "run_token": run_token,
        "state": state,
        **_parameter_report(parameters, run),
        "queries": await _fetch_run_results(
            mode.client, report_token, run_token, max_rows_per_query, columns
        ),
    }



WRITE_TOOLS = frozenset({
    "create_report",
    "update_report",
    "archive_report",
    "unarchive_report",
    "add_query",
    "update_query",
    "delete_query",
    "delete_report",
    "run_report",
    "create_chart",
    "update_chart",
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
