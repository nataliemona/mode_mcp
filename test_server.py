"""Tests for the Mode Analytics MCP server."""

import json
from unittest.mock import MagicMock

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from mode_mcp import server

WORKSPACE = "test-workspace"
TOKEN = "test-token"
SECRET = "test-secret"
BASE = f"https://app.mode.com/api/{WORKSPACE}"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("MODE_WORKSPACE", WORKSPACE)
    monkeypatch.setenv("MODE_API_TOKEN", TOKEN)
    monkeypatch.setenv("MODE_API_SECRET", SECRET)


def _test_client() -> httpx.AsyncClient:
    """Create an httpx client matching the server's configuration."""
    return httpx.AsyncClient(
        base_url=BASE,
        auth=(TOKEN, SECRET),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/hal+json",
        },
        timeout=30.0,
    )


def _mock_ctx(client: httpx.AsyncClient):
    """Build a mock MCP Context backed by the given httpx client."""
    ctx = MagicMock()
    ctx.request_context.lifespan_context = server.ModeContext(
        client=client, workspace=WORKSPACE
    )
    return ctx


def _hal(key, items):
    """Build a HAL+JSON envelope."""
    return {"_embedded": {key: items}}


def _v2_chart(token, chart_type, title):
    """A version 2 chart whose stale legacy `view` disagrees with `view_vegas`."""
    return {
        "token": token,
        "view_version": 2,
        "color_palette_token": "pal1",
        "switch_view_token": "sv1",
        "_forms": {
            form: {"method": method, "action": f"/api/{WORKSPACE}/switch_views/sv1/visualizations/{token}"}
            for form, method in (("edit", "patch"), ("destroy", "delete"))
        },
        "view": {"selectedChart": "table", "chartTitle": "stale"},
        "view_vegas": {
            "chartType": chart_type,
            "title": title,
            "format": {"colors": {"a": "#fff"}},
            "encoding": {
                "x": [{"id": "1", "formula": "[week]"}],
                "y": [{"id": "2", "formula": "SUM([sessions])"}],
                "row": [],
                "marks": {
                    "properties": {
                        "color": {"values": [{"id": "3", "formula": "[browser]"}]},
                        "size": {"values": []},
                    }
                },
            },
        },
    }


# ---------------------------------------------------------------------------
# _get_config
# ---------------------------------------------------------------------------


def test_get_config_missing(monkeypatch):
    monkeypatch.delenv("MODE_WORKSPACE", raising=False)
    with pytest.raises(ValueError, match="MODE_WORKSPACE"):
        server._get_config()


def test_get_config_unexpanded_reference(monkeypatch):
    monkeypatch.setenv("MODE_API_TOKEN", "${MODE_API_TOKEN}")
    with pytest.raises(ValueError, match="didn't expand"):
        server._get_config()


def test_get_config_ok():
    ws, tok, sec = server._get_config()
    assert (ws, tok, sec) == (WORKSPACE, TOKEN, SECRET)


# ---------------------------------------------------------------------------
# _check — error body extraction
# ---------------------------------------------------------------------------


def test_check_includes_error_detail():
    resp = httpx.Response(
        500,
        json={"error": "something broke"},
        request=httpx.Request("GET", "https://example.com"),
    )
    with pytest.raises(ToolError, match="something broke"):
        server._check(resp)


# ---------------------------------------------------------------------------
# list_data_sources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_list_data_sources():
    respx.get(f"{BASE}/data_sources").mock(
        return_value=httpx.Response(
            200,
            json=_hal(
                "data_sources",
                [
                    {
                        "id": 1,
                        "token": "ds1",
                        "name": "Prod DB",
                        "adapter": "postgres",
                        "database": "analytics",
                    }
                ],
            ),
        )
    )

    async with _test_client() as client:
        result = await server.list_data_sources(_mock_ctx(client))
    assert len(result) == 1
    assert result[0]["name"] == "Prod DB"
    assert result[0]["adapter"] == "postgres"


# ---------------------------------------------------------------------------
# list_spaces
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_list_spaces():
    respx.get(f"{BASE}/spaces").mock(
        return_value=httpx.Response(
            200,
            json=_hal(
                "spaces",
                [{"token": "sp1", "name": "Engineering", "state": "active"}],
            ),
        )
    )

    async with _test_client() as client:
        result = await server.list_spaces(_mock_ctx(client))
    assert result[0]["token"] == "sp1"
    assert result[0]["name"] == "Engineering"


# ---------------------------------------------------------------------------
# list_reports
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_list_reports():
    respx.get(f"{BASE}/spaces/sp1/reports").mock(
        return_value=httpx.Response(
            200,
            json=_hal(
                "reports",
                [
                    {
                        "token": "rpt1",
                        "name": "Daily Metrics",
                        "created_at": "2025-01-01",
                        "last_run_at": "2025-06-01",
                    }
                ],
            ),
        )
    )

    async with _test_client() as client:
        result = await server.list_reports(_mock_ctx(client), "sp1")
    assert result[0]["token"] == "rpt1"
    assert result[0]["name"] == "Daily Metrics"
    assert result[0]["archived"] is False


def _mock_space_with_archived():
    respx.get(f"{BASE}/spaces/sp1/reports").mock(
        return_value=httpx.Response(
            200,
            json=_hal("reports", [
                {"token": "live", "name": "Live metrics", "archived": False},
                {"token": "old", "name": "Old metrics", "archived": True},
            ]),
        )
    )


@pytest.mark.asyncio
@respx.mock
async def test_list_reports_hides_archived_by_default():
    _mock_space_with_archived()

    async with _test_client() as client:
        result = await server.list_reports(_mock_ctx(client), "sp1")
    assert [r["token"] for r in result] == ["live"]


@pytest.mark.asyncio
@respx.mock
async def test_list_reports_include_archived():
    _mock_space_with_archived()

    async with _test_client() as client:
        result = await server.list_reports(_mock_ctx(client), "sp1", include_archived=True)
    assert {r["token"]: r["archived"] for r in result} == {"live": False, "old": True}


@pytest.mark.asyncio
@respx.mock
async def test_search_reports_hides_archived_by_default():
    respx.get(f"{BASE}/spaces").mock(
        return_value=httpx.Response(200, json=_hal("spaces", [{"token": "sp1", "name": "Eng"}]))
    )
    _mock_space_with_archived()

    async with _test_client() as client:
        default = await server.search_reports(_mock_ctx(client), "metrics")
        everything = await server.search_reports(
            _mock_ctx(client), "metrics", include_archived=True
        )
    assert [r["token"] for r in default] == ["live"]
    assert {r["token"]: r["archived"] for r in everything} == {"live": False, "old": True}


@pytest.mark.parametrize(
    "obj, expected",
    [
        ({"_links": {"creator": {"href": "/api/jane_doe"}}}, "jane_doe"),
        ({"_links": {"creator": {"href": "/api/jane_doe/"}}}, "jane_doe"),
        ({"_links": {}}, None),
        ({}, None),
        ({"_links": {"creator": {"href": "weird"}}}, None),
    ],
)
def test_creator(obj, expected):
    assert server._creator(obj) == expected


# ---------------------------------------------------------------------------
# search_reports
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_search_reports_matches_across_spaces():
    respx.get(f"{BASE}/spaces").mock(
        return_value=httpx.Response(
            200,
            json=_hal(
                "spaces",
                [
                    {"token": "sp1", "name": "Engineering"},
                    {"token": "sp2", "name": "Growth"},
                ],
            ),
        )
    )
    respx.get(f"{BASE}/spaces/sp1/reports").mock(
        return_value=httpx.Response(
            200,
            json=_hal(
                "reports",
                [
                    {"token": "rpt1", "name": "Daily Metrics", "description": ""},
                    {"token": "rpt2", "name": "Unrelated", "description": "tracks other stuff"},
                ],
            ),
        )
    )
    respx.get(f"{BASE}/spaces/sp2/reports").mock(
        return_value=httpx.Response(
            200,
            json=_hal(
                "reports",
                [
                    {"token": "rpt3", "name": "User Funnel", "description": "key METRICS here"},
                ],
            ),
        )
    )

    async with _test_client() as client:
        result = await server.search_reports(_mock_ctx(client), "metric")
    tokens = {r["token"] for r in result}
    # rpt1 matches on name, rpt3 matches on description (case-insensitive)
    assert tokens == {"rpt1", "rpt3"}
    by_token = {r["token"]: r for r in result}
    assert by_token["rpt1"]["space_token"] == "sp1"
    assert by_token["rpt1"]["space_name"] == "Engineering"
    assert by_token["rpt3"]["space_token"] == "sp2"


@pytest.mark.asyncio
@respx.mock
async def test_search_reports_empty():
    respx.get(f"{BASE}/spaces").mock(
        return_value=httpx.Response(
            200, json=_hal("spaces", [{"token": "sp1", "name": "Engineering"}])
        )
    )
    respx.get(f"{BASE}/spaces/sp1/reports").mock(
        return_value=httpx.Response(
            200,
            json=_hal("reports", [{"token": "rpt1", "name": "Unrelated"}]),
        )
    )

    async with _test_client() as client:
        result = await server.search_reports(_mock_ctx(client), "nonexistent")
    assert result == []


@pytest.mark.asyncio
@respx.mock
async def test_search_reports_limit():
    respx.get(f"{BASE}/spaces").mock(
        return_value=httpx.Response(
            200, json=_hal("spaces", [{"token": "sp1", "name": "Eng"}])
        )
    )
    respx.get(f"{BASE}/spaces/sp1/reports").mock(
        return_value=httpx.Response(
            200,
            json=_hal(
                "reports",
                [
                    {"token": f"rpt{i}", "name": f"metric {i}", "description": ""}
                    for i in range(10)
                ],
            ),
        )
    )

    async with _test_client() as client:
        result = await server.search_reports(_mock_ctx(client), "metric", limit=3)
    assert len(result) == 3


# ---------------------------------------------------------------------------
# get_report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_get_report():
    respx.get(f"{BASE}/reports/rpt1").mock(
        return_value=httpx.Response(
            200,
            json={
                "token": "rpt1",
                "name": "Daily Metrics",
                "description": "Key metrics",
                "created_at": "2025-01-01",
                "last_run_at": "2025-06-01",
            },
        )
    )
    respx.get(f"{BASE}/reports/rpt1/queries").mock(
        return_value=httpx.Response(
            200,
            json=_hal(
                "queries",
                [
                    {
                        "token": "q1",
                        "name": "revenue",
                        "raw_query": "SELECT sum(amount) FROM orders",
                        "data_source_id": 1,
                    }
                ],
            ),
        )
    )
    respx.get(f"{BASE}/reports/rpt1/queries/q1/charts").mock(
        return_value=httpx.Response(
            200, json=_hal("charts", [_v2_chart("c1", "line", "Revenue")])
        )
    )

    async with _test_client() as client:
        result = await server.get_report(_mock_ctx(client), "rpt1")
    assert result["token"] == "rpt1"
    assert len(result["queries"]) == 1
    assert "SELECT" in result["queries"][0]["raw_query"]
    assert result["queries"][0]["charts"] == [
        {"chart_token": "c1", "type": "line", "title": "Revenue"}
    ]


@pytest.mark.asyncio
@respx.mock
async def test_get_report_survives_chart_fetch_failure():
    respx.get(f"{BASE}/reports/rpt1").mock(
        return_value=httpx.Response(200, json={"token": "rpt1", "name": "R"})
    )
    respx.get(f"{BASE}/reports/rpt1/queries").mock(
        return_value=httpx.Response(
            200, json=_hal("queries", [{"token": "q1", "name": "revenue"}])
        )
    )
    respx.get(f"{BASE}/reports/rpt1/queries/q1/charts").mock(
        return_value=httpx.Response(500)
    )

    async with _test_client() as client:
        result = await server.get_report(_mock_ctx(client), "rpt1")
    assert result["queries"][0]["charts"] is None


# ---------------------------------------------------------------------------
# list_charts / get_chart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_list_charts_fans_out_over_queries():
    respx.get(f"{BASE}/reports/rpt1/queries").mock(
        return_value=httpx.Response(
            200,
            json=_hal(
                "queries",
                [{"token": "q1", "name": "revenue"}, {"token": "q2", "name": "users"}],
            ),
        )
    )
    respx.get(f"{BASE}/reports/rpt1/queries/q1/charts").mock(
        return_value=httpx.Response(
            200, json=_hal("charts", [_v2_chart("c1", "bar", "Revenue")])
        )
    )
    respx.get(f"{BASE}/reports/rpt1/queries/q2/charts").mock(
        return_value=httpx.Response(
            200,
            json=_hal(
                "charts",
                [
                    {
                        "token": "c2",
                        "view_version": 1,
                        "view": {"selectedChart": "pie", "chartTitle": "Users"},
                        "view_vegas": {},
                    }
                ],
            ),
        )
    )

    async with _test_client() as client:
        result = await server.list_charts(_mock_ctx(client), "rpt1")

    assert result == [
        {
            "chart_token": "c1",
            "type": "bar",
            "title": "Revenue",
            "query_token": "q1",
            "query_name": "revenue",
        },
        {
            "chart_token": "c2",
            "type": "pie",
            "title": "Users",
            "query_token": "q2",
            "query_name": "users",
        },
    ]


@pytest.mark.asyncio
@respx.mock
async def test_list_charts_single_query_skips_query_listing():
    queries = respx.get(f"{BASE}/reports/rpt1/queries").mock(
        return_value=httpx.Response(200, json=_hal("queries", []))
    )
    respx.get(f"{BASE}/reports/rpt1/queries/q1/charts").mock(
        return_value=httpx.Response(200, json=_hal("charts", []))
    )

    async with _test_client() as client:
        result = await server.list_charts(_mock_ctx(client), "rpt1", "q1")

    assert result == []
    assert queries.called is False


@pytest.mark.asyncio
@respx.mock
async def test_get_chart_omits_vegas_unless_requested():
    respx.get(f"{BASE}/reports/rpt1/queries/q1/charts/c1").mock(
        return_value=httpx.Response(200, json=_v2_chart("c1", "line", "Revenue"))
    )

    async with _test_client() as client:
        slim = await server.get_chart(_mock_ctx(client), "rpt1", "q1", "c1")
        full = await server.get_chart(
            _mock_ctx(client), "rpt1", "q1", "c1", include_vegas=True
        )

    assert slim["type"] == "line"
    assert slim["title"] == "Revenue"
    assert slim["switch_view_token"] == "sv1"
    assert slim["encoding"] == {
        "x": ["[week]"],
        "y": ["SUM([sessions])"],
        "marks.color": ["[browser]"],
    }
    assert "view_vegas" not in slim
    assert full["view_vegas"]["format"] == {"colors": {"a": "#fff"}}


# ---------------------------------------------------------------------------
# list_report_runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_list_report_runs():
    respx.get(f"{BASE}/reports/rpt1/runs").mock(
        return_value=httpx.Response(
            200,
            json=_hal(
                "report_runs",
                [
                    {
                        "token": "run2",
                        "state": "succeeded",
                        "created_at": "2025-06-02",
                        "completed_at": "2025-06-02",
                    },
                    {
                        "token": "run1",
                        "state": "failed",
                        "created_at": "2025-06-01",
                        "completed_at": "2025-06-01",
                    },
                ],
            ),
        )
    )

    async with _test_client() as client:
        result = await server.list_report_runs(_mock_ctx(client), "rpt1")
    assert len(result) == 2
    assert result[0]["token"] == "run2"
    assert result[0]["state"] == "succeeded"
    assert result[1]["state"] == "failed"


# ---------------------------------------------------------------------------
# create_report — without queries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_create_report_no_queries():
    respx.post(f"{BASE}/reports").mock(
        return_value=httpx.Response(200, json={"token": "rpt_new", "name": None})
    )
    respx.patch(f"{BASE}/reports/rpt_new").mock(
        return_value=httpx.Response(
            200,
            json={
                "token": "rpt_new",
                "name": "New Report",
                "description": "A test report",
            },
        )
    )

    async with _test_client() as client:
        result = await server.create_report(_mock_ctx(client), "sp1", "New Report", "A test report")
    assert json.loads(respx.calls[0].request.content) == {"report": {"space_token": "sp1"}}
    assert result["token"] == "rpt_new"
    assert result["name"] == "New Report"
    assert result["queries_added"] == 0
    assert "rpt_new" in result["url"]


# ---------------------------------------------------------------------------
# create_report — with queries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_create_report_with_queries():
    respx.post(f"{BASE}/reports").mock(
        return_value=httpx.Response(200, json={"token": "rpt_new", "name": None})
    )
    respx.patch(f"{BASE}/reports/rpt_new").mock(
        return_value=httpx.Response(
            200,
            json={"token": "rpt_new", "name": "With Queries", "description": ""},
        )
    )
    respx.post(f"{BASE}/reports/rpt_new/queries").mock(
        return_value=httpx.Response(
            200,
            json={"token": "q1", "name": "revenue", "raw_query": "SELECT 1"},
        )
    )

    async with _test_client() as client:
        result = await server.create_report(
            _mock_ctx(client),
            "sp1",
            "With Queries",
            queries=[{"raw_query": "SELECT 1", "data_source_id": 1, "name": "revenue"}],
        )
    assert result["token"] == "rpt_new"
    assert result["queries_added"] == 1
    assert result["queries_failed"] == []


# ---------------------------------------------------------------------------
# create_report — partial query failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "patch_mock",
    [
        {"return_value": httpx.Response(403, json={"error": "forbidden"})},
        {"side_effect": httpx.ConnectError("connection reset")},
    ],
    ids=["http-error", "network-error"],
)
async def test_create_report_rename_failure_names_orphan(patch_mock):
    respx.post(f"{BASE}/reports").mock(
        return_value=httpx.Response(200, json={"token": "rpt_new", "name": None})
    )
    respx.patch(f"{BASE}/reports/rpt_new").mock(**patch_mock)

    async with _test_client() as client:
        with pytest.raises(ToolError, match="Created report rpt_new") as exc_info:
            await server.create_report(_mock_ctx(client), "sp1", "New Report", "")
    assert f"https://app.mode.com/{WORKSPACE}/reports/rpt_new" in str(exc_info.value)


@pytest.mark.asyncio
@respx.mock
async def test_create_report_partial_query_failure():
    respx.post(f"{BASE}/reports").mock(
        return_value=httpx.Response(200, json={"token": "rpt_new", "name": None})
    )
    respx.patch(f"{BASE}/reports/rpt_new").mock(
        return_value=httpx.Response(
            200,
            json={"token": "rpt_new", "name": "Partial", "description": ""},
        )
    )
    respx.post(f"{BASE}/reports/rpt_new/queries").mock(
        side_effect=[
            httpx.Response(200, json={"token": "q1", "name": "ok"}),
            httpx.Response(500, json={"error": "boom"}),
        ]
    )

    async with _test_client() as client:
        result = await server.create_report(
            _mock_ctx(client),
            "sp1",
            "Partial",
            queries=[
                {"raw_query": "SELECT 1", "data_source_id": 1, "name": "ok"},
                {"raw_query": "SELECT 2", "data_source_id": 1, "name": "bad"},
            ],
        )
    assert result["token"] == "rpt_new"
    assert result["queries_added"] == 1
    assert len(result["queries_failed"]) == 1
    assert result["queries_failed"][0]["name"] == "bad"


# ---------------------------------------------------------------------------
# update_report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_update_report():
    respx.patch(f"{BASE}/reports/rpt1").mock(
        return_value=httpx.Response(
            200,
            json={
                "token": "rpt1",
                "name": "Renamed Report",
                "description": "Updated desc",
            },
        )
    )

    async with _test_client() as client:
        result = await server.update_report(
            _mock_ctx(client), "rpt1", name="Renamed Report", description="Updated desc"
        )
    assert result["token"] == "rpt1"
    assert result["name"] == "Renamed Report"
    assert result["description"] == "Updated desc"
    assert "rpt1" in result["url"]


@pytest.mark.asyncio
async def test_update_report_nothing():
    async with _test_client() as client:
        with pytest.raises(ToolError, match="Nothing to update"):
            await server.update_report(_mock_ctx(client), "rpt1")


# ---------------------------------------------------------------------------
# delete_report
# ---------------------------------------------------------------------------


def _mock_report_for_delete():
    """Set up respx mocks for delete_report tests.

    Matches Mode's real responses: reports carry no creator link, queries do.
    """
    respx.get(f"{BASE}/reports/rpt1").mock(
        return_value=httpx.Response(
            200,
            json={"token": "rpt1", "name": "My Report", "archived": False},
        )
    )
    respx.get(f"{BASE}/reports/rpt1/queries").mock(
        return_value=httpx.Response(
            200,
            json=_hal("queries", [
                {"token": "q1", "name": "revenue", "_links": {"creator": {"href": "/api/alice"}}},
                {"token": "q2", "name": "users", "_links": {"creator": {"href": "/api/bob"}}},
            ]),
        )
    )


@pytest.mark.asyncio
@respx.mock
async def test_delete_report_preview():
    _mock_report_for_delete()
    delete_route = respx.delete(f"{BASE}/reports/rpt1")

    async with _test_client() as client:
        result = await server.delete_report(_mock_ctx(client), "rpt1")
    assert result["confirm_required"] is True
    assert result["name"] == "My Report"
    assert result["archived"] is False
    assert "creator" not in result
    assert result["query_count"] == 2
    assert result["queries"] == [
        {"name": "revenue", "creator": "alice"},
        {"name": "users", "creator": "bob"},
    ]
    assert len(result["confirm_code"]) == 8
    assert not delete_route.called


@pytest.mark.asyncio
@respx.mock
async def test_delete_report_confirmed():
    _mock_report_for_delete()
    delete_route = respx.delete(f"{BASE}/reports/rpt1").mock(
        return_value=httpx.Response(204)
    )

    async with _test_client() as client:
        preview = await server.delete_report(_mock_ctx(client), "rpt1")
        result = await server.delete_report(
            _mock_ctx(client), "rpt1", confirm_code=preview["confirm_code"]
        )
    assert result["deleted"] is True
    assert result["name"] == "My Report"
    assert delete_route.called


@pytest.mark.asyncio
@respx.mock
async def test_delete_report_wrong_code():
    _mock_report_for_delete()
    delete_route = respx.delete(f"{BASE}/reports/rpt1")

    async with _test_client() as client:
        with pytest.raises(ToolError, match="confirm_code does not match"):
            await server.delete_report(_mock_ctx(client), "rpt1", confirm_code="deadbeef")
    assert not delete_route.called


@pytest.mark.asyncio
@respx.mock
async def test_delete_report_changed_since_preview():
    _mock_report_for_delete()
    delete_route = respx.delete(f"{BASE}/reports/rpt1")

    async with _test_client() as client:
        preview = await server.delete_report(_mock_ctx(client), "rpt1")
        # Someone edits a query between preview and delete.
        respx.get(f"{BASE}/reports/rpt1/queries").mock(
            return_value=httpx.Response(
                200,
                json=_hal("queries", [
                    {"token": "q1", "name": "revenue", "raw_query": "SELECT 2"},
                    {"token": "q2", "name": "users"},
                ]),
            )
        )
        with pytest.raises(ToolError, match="confirm_code does not match"):
            await server.delete_report(
                _mock_ctx(client), "rpt1", confirm_code=preview["confirm_code"]
            )
    assert not delete_route.called


# ---------------------------------------------------------------------------
# delete_query
# ---------------------------------------------------------------------------


def _mock_query_for_delete():
    respx.get(f"{BASE}/reports/rpt1/queries/q1").mock(
        return_value=httpx.Response(
            200,
            json={
                "token": "q1",
                "name": "revenue",
                "raw_query": "SELECT 1",
                "_links": {"creator": {"href": "/api/alice"}},
            },
        )
    )


@pytest.mark.asyncio
@respx.mock
async def test_delete_query_preview():
    _mock_query_for_delete()
    delete_route = respx.delete(f"{BASE}/reports/rpt1/queries/q1")

    async with _test_client() as client:
        result = await server.delete_query(_mock_ctx(client), "rpt1", "q1")
    assert result["confirm_required"] is True
    assert result["name"] == "revenue"
    assert result["raw_query"] == "SELECT 1"
    assert result["creator"] == "alice"
    assert not delete_route.called


@pytest.mark.asyncio
@respx.mock
async def test_delete_query_confirmed():
    _mock_query_for_delete()
    delete_route = respx.delete(f"{BASE}/reports/rpt1/queries/q1").mock(
        return_value=httpx.Response(204)
    )

    async with _test_client() as client:
        preview = await server.delete_query(_mock_ctx(client), "rpt1", "q1")
        result = await server.delete_query(
            _mock_ctx(client), "rpt1", "q1", confirm_code=preview["confirm_code"]
        )
    assert result["deleted"] is True
    assert result["query_token"] == "q1"
    assert delete_route.called


@pytest.mark.asyncio
@respx.mock
async def test_delete_query_wrong_code():
    _mock_query_for_delete()
    delete_route = respx.delete(f"{BASE}/reports/rpt1/queries/q1")

    async with _test_client() as client:
        with pytest.raises(ToolError, match="confirm_code does not match"):
            await server.delete_query(_mock_ctx(client), "rpt1", "q1", confirm_code="nope")
    assert not delete_route.called


# ---------------------------------------------------------------------------
# archive / unarchive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_archive_and_unarchive_report():
    archive = respx.patch(f"{BASE}/reports/rpt1/archive").mock(
        return_value=httpx.Response(200, json={"token": "rpt1", "name": "My Report"})
    )
    unarchive = respx.patch(f"{BASE}/reports/rpt1/unarchive").mock(
        return_value=httpx.Response(200, json={"token": "rpt1", "name": "My Report"})
    )

    async with _test_client() as client:
        result = await server.archive_report(_mock_ctx(client), "rpt1")
        assert result == {"archived": True, "report_token": "rpt1", "name": "My Report"}
        result = await server.unarchive_report(_mock_ctx(client), "rpt1")
        assert result["archived"] is False
    assert archive.called and unarchive.called


# ---------------------------------------------------------------------------
# Tool enable/disable config
# ---------------------------------------------------------------------------

ALL_TOOLS = {"list_spaces", "get_report", *server.WRITE_TOOLS}


def test_disabled_tools_deletes_off_by_default():
    assert server._disabled_tools({}, ALL_TOOLS) == server.DELETE_TOOLS


def test_disabled_tools_enable_deletes():
    assert server._disabled_tools({"MODE_ENABLE_DELETES": "true"}, ALL_TOOLS) == set()



def test_disabled_tools_read_only_wins_over_enable_deletes():
    env = {"MODE_READ_ONLY": "true", "MODE_ENABLE_DELETES": "true"}
    assert server._disabled_tools(env, ALL_TOOLS) == server.WRITE_TOOLS


def test_disabled_tools_list():
    env = {
        "MODE_ENABLE_DELETES": "true",
        "MODE_DISABLED_TOOLS": " create_report, add_query ,",
    }
    assert server._disabled_tools(env, ALL_TOOLS) == {"create_report", "add_query"}


def test_disabled_tools_unknown_name():
    with pytest.raises(ValueError, match="delete_reports"):
        server._disabled_tools({"MODE_DISABLED_TOOLS": "delete_reports"}, ALL_TOOLS)


def test_write_tools_are_registered():
    import asyncio
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert server.WRITE_TOOLS <= names


# ---------------------------------------------------------------------------
# add_query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_add_query():
    respx.post(f"{BASE}/reports/rpt1/queries").mock(
        return_value=httpx.Response(
            200,
            json={
                "token": "q_new",
                "name": "daily_users",
                "raw_query": "SELECT count(*) FROM users",
                "data_source_id": 1,
            },
        )
    )

    async with _test_client() as client:
        result = await server.add_query(
            _mock_ctx(client), "rpt1", "SELECT count(*) FROM users", 1, "daily_users"
        )
    assert result["token"] == "q_new"
    assert result["name"] == "daily_users"
    assert "SELECT" in result["raw_query"]


# ---------------------------------------------------------------------------
# update_query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_update_query():
    respx.patch(f"{BASE}/reports/rpt1/queries/q1").mock(
        return_value=httpx.Response(
            200,
            json={
                "token": "q1",
                "name": "updated_query",
                "raw_query": "SELECT 2",
                "data_source_id": 1,
            },
        )
    )

    async with _test_client() as client:
        result = await server.update_query(
            _mock_ctx(client), "rpt1", "q1", raw_query="SELECT 2", name="updated_query"
        )
    assert result["token"] == "q1"
    assert result["name"] == "updated_query"
    assert result["raw_query"] == "SELECT 2"


@pytest.mark.asyncio
async def test_update_query_nothing_to_update():
    async with _test_client() as client:
        with pytest.raises(ToolError, match="Nothing to update"):
            await server.update_query(_mock_ctx(client), "rpt1", "q1")


# ---------------------------------------------------------------------------
# get_run_results — explicit run token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_get_run_results_explicit_token():
    respx.get(f"{BASE}/reports/rpt1/runs/run1/query_runs").mock(
        return_value=httpx.Response(
            200,
            json=_hal(
                "query_runs",
                [{"token": "qr1", "query_name": "revenue", "state": "succeeded"}],
            ),
        )
    )
    respx.get(
        f"{BASE}/reports/rpt1/runs/run1/query_runs/qr1/results/content.json"
    ).mock(return_value=httpx.Response(200, json=[{"total": 99}]))

    async with _test_client() as client:
        result = await server.get_run_results(_mock_ctx(client), "rpt1", "run1")
    assert result[0]["data"] == [{"total": 99}]
    assert result[0]["row_count"] == 1
    assert result[0]["truncated"] is False


# ---------------------------------------------------------------------------
# get_run_results — default truncation kicks in on large payloads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_get_run_results_truncates_by_default():
    rows = [{"i": i} for i in range(1200)]
    respx.get(f"{BASE}/reports/rpt1/runs/run1/query_runs").mock(
        return_value=httpx.Response(
            200,
            json=_hal(
                "query_runs",
                [{"token": "qr1", "query_name": "big", "state": "succeeded"}],
            ),
        )
    )
    respx.get(
        f"{BASE}/reports/rpt1/runs/run1/query_runs/qr1/results/content.json"
    ).mock(return_value=httpx.Response(200, json=rows))

    async with _test_client() as client:
        result = await server.get_run_results(_mock_ctx(client), "rpt1", "run1")
    assert result[0]["row_count"] == 1200
    assert result[0]["truncated"] is True
    assert len(result[0]["data"]) == 500


# ---------------------------------------------------------------------------
# get_run_results — max_rows_per_query=None fetches everything
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_get_run_results_no_truncation():
    rows = [{"i": i} for i in range(1200)]
    respx.get(f"{BASE}/reports/rpt1/runs/run1/query_runs").mock(
        return_value=httpx.Response(
            200,
            json=_hal(
                "query_runs",
                [{"token": "qr1", "query_name": "big", "state": "succeeded"}],
            ),
        )
    )
    respx.get(
        f"{BASE}/reports/rpt1/runs/run1/query_runs/qr1/results/content.json"
    ).mock(return_value=httpx.Response(200, json=rows))

    async with _test_client() as client:
        result = await server.get_run_results(
            _mock_ctx(client), "rpt1", "run1", max_rows_per_query=None
        )
    assert result[0]["row_count"] == 1200
    assert result[0]["truncated"] is False
    assert len(result[0]["data"]) == 1200


# ---------------------------------------------------------------------------
# get_run_results — latest run fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_get_run_results_latest_fallback():
    respx.get(f"{BASE}/reports/rpt1/runs").mock(
        return_value=httpx.Response(
            200,
            json=_hal(
                "report_runs",
                [
                    {"token": "run2", "state": "succeeded"},
                    {"token": "run1", "state": "failed"},
                ],
            ),
        )
    )
    respx.get(f"{BASE}/reports/rpt1/runs/run2/query_runs").mock(
        return_value=httpx.Response(
            200,
            json=_hal(
                "query_runs",
                [{"token": "qr1", "query_name": "q", "state": "succeeded"}],
            ),
        )
    )
    respx.get(
        f"{BASE}/reports/rpt1/runs/run2/query_runs/qr1/results/content.json"
    ).mock(return_value=httpx.Response(200, json=[{"v": 1}]))

    async with _test_client() as client:
        result = await server.get_run_results(_mock_ctx(client), "rpt1")
    assert result[0]["data"] == [{"v": 1}]


# ---------------------------------------------------------------------------
# get_run_results — no succeeded runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_get_run_results_no_succeeded():
    respx.get(f"{BASE}/reports/rpt1/runs").mock(
        return_value=httpx.Response(
            200, json=_hal("report_runs", [{"token": "r1", "state": "failed"}])
        )
    )

    async with _test_client() as client:
        with pytest.raises(ToolError, match="No succeeded runs"):
            await server.get_run_results(_mock_ctx(client), "rpt1")


# ---------------------------------------------------------------------------
# _fetch_run_results — parallel fetch, mixed success/failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_fetch_run_results_parallel_mixed():
    respx.get(f"{BASE}/reports/rpt1/runs/run1/query_runs").mock(
        return_value=httpx.Response(
            200,
            json=_hal(
                "query_runs",
                [
                    {
                        "token": "qr1",
                        "query_token": "q1",
                        "query_name": "good",
                        "state": "succeeded",
                    },
                    {
                        "token": "qr2",
                        "query_token": "q2",
                        "query_name": "bad",
                        "state": "failed",
                        "error_code": "SqlGenericError",
                        "error_message": "SQL compilation error: syntax error line 14",
                    },
                    {"token": "qr3", "query_token": "q3", "query_name": "gone", "state": "succeeded"},
                ],
            ),
        )
    )
    respx.get(
        f"{BASE}/reports/rpt1/runs/run1/query_runs/qr1/results/content.json"
    ).mock(return_value=httpx.Response(200, json=[{"ok": True}]))
    failed_content = respx.get(
        f"{BASE}/reports/rpt1/runs/run1/query_runs/qr2/results/content.json"
    ).mock(return_value=httpx.Response(404))
    respx.get(
        f"{BASE}/reports/rpt1/runs/run1/query_runs/qr3/results/content.json"
    ).mock(return_value=httpx.Response(404))

    async with _test_client() as client:
        results = await server._fetch_run_results(client, "rpt1", "run1")

    assert len(results) == 3
    assert results[0]["data"] == [{"ok": True}]
    assert results[0]["query_token"] == "q1"
    assert results[1] == {
        "query_name": "bad",
        "query_token": "q2",
        "state": "failed",
        "error": "SQL compilation error: syntax error line 14",
        "error_code": "SqlGenericError",
    }
    assert failed_content.called is False
    assert results[2]["error"] == "HTTP 404"


# ---------------------------------------------------------------------------
# get_run_results — include_failed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_get_run_results_include_failed_picks_latest_any_state():
    respx.get(f"{BASE}/reports/rpt1/runs").mock(
        return_value=httpx.Response(
            200,
            json=_hal(
                "report_runs",
                [
                    {"token": "run2", "state": "failed"},
                    {"token": "run1", "state": "succeeded"},
                ],
            ),
        )
    )
    respx.get(f"{BASE}/reports/rpt1/runs/run2/query_runs").mock(
        return_value=httpx.Response(
            200,
            json=_hal(
                "query_runs",
                [
                    {
                        "token": "qr1",
                        "query_token": "q1",
                        "query_name": "q",
                        "state": "failed",
                        "error_message": "boom",
                    }
                ],
            ),
        )
    )

    async with _test_client() as client:
        result = await server.get_run_results(
            _mock_ctx(client), "rpt1", include_failed=True
        )
    assert result[0]["error"] == "boom"
    assert result[0]["query_token"] == "q1"


@pytest.mark.asyncio
@respx.mock
async def test_get_run_results_include_failed_no_runs():
    respx.get(f"{BASE}/reports/rpt1/runs").mock(
        return_value=httpx.Response(200, json=_hal("report_runs", []))
    )

    async with _test_client() as client:
        with pytest.raises(ToolError, match="No runs found"):
            await server.get_run_results(
                _mock_ctx(client), "rpt1", include_failed=True
            )


# ---------------------------------------------------------------------------
# run_report
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_clock(monkeypatch):
    """Replace asyncio.sleep/time.monotonic in server with a manual clock."""
    clock = {"now": 0.0, "sleeps": []}

    async def _sleep(seconds):
        clock["sleeps"].append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(server.asyncio, "sleep", _sleep)
    monkeypatch.setattr(server.time, "monotonic", lambda: clock["now"])
    return clock


def _mock_query_runs(run_token="run1", state="succeeded", **extra):
    respx.get(f"{BASE}/reports/rpt1/runs/{run_token}/query_runs").mock(
        return_value=httpx.Response(
            200,
            json=_hal(
                "query_runs",
                [
                    {
                        "token": "qr1",
                        "query_token": "q1",
                        "query_name": "big",
                        "state": state,
                        **extra,
                    }
                ],
            ),
        )
    )


@pytest.mark.asyncio
@respx.mock
async def test_run_report_polls_until_succeeded(fake_clock):
    post = respx.post(f"{BASE}/reports/rpt1/runs").mock(
        return_value=httpx.Response(200, json={"token": "run1", "state": "pending"})
    )
    respx.get(f"{BASE}/reports/rpt1/runs/run1").mock(
        side_effect=[
            httpx.Response(200, json={"token": "run1", "state": "running"}),
            httpx.Response(200, json={"token": "run1", "state": "succeeded"}),
        ]
    )
    _mock_query_runs()
    respx.get(
        f"{BASE}/reports/rpt1/runs/run1/query_runs/qr1/results/content.json"
    ).mock(return_value=httpx.Response(200, json=[{"i": i} for i in range(200)]))

    async with _test_client() as client:
        result = await server.run_report(
            _mock_ctx(client), "rpt1", parameters={"weeks": 8}
        )

    assert json.loads(post.calls[0].request.content) == {"parameters": {"weeks": 8}}
    assert result["run_token"] == "run1"
    assert result["state"] == "succeeded"
    query = result["queries"][0]
    assert query["query_token"] == "q1"
    assert query["row_count"] == 200
    assert query["truncated"] is True
    assert len(query["data"]) == 50
    assert fake_clock["sleeps"] == [1.0, 1.5]


@pytest.mark.asyncio
@respx.mock
async def test_run_report_without_parameters_posts_empty_body(fake_clock):
    post = respx.post(f"{BASE}/reports/rpt1/runs").mock(
        return_value=httpx.Response(200, json={"token": "run1", "state": "failed"})
    )
    _mock_query_runs(state="failed", error_message="bad column")

    async with _test_client() as client:
        result = await server.run_report(_mock_ctx(client), "rpt1")

    assert json.loads(post.calls[0].request.content) == {}
    assert result["state"] == "failed"
    assert result["queries"][0]["error"] == "bad column"


@pytest.mark.asyncio
@respx.mock
async def test_run_report_times_out(fake_clock):
    respx.post(f"{BASE}/reports/rpt1/runs").mock(
        return_value=httpx.Response(200, json={"token": "run1", "state": "pending"})
    )
    respx.get(f"{BASE}/reports/rpt1/runs/run1").mock(
        return_value=httpx.Response(200, json={"token": "run1", "state": "running"})
    )

    async with _test_client() as client:
        result = await server.run_report(
            _mock_ctx(client), "rpt1", timeout_seconds=10
        )

    assert result == {"run_token": "run1", "state": "running", "timed_out": True}
    assert fake_clock["now"] == 10
    assert max(fake_clock["sleeps"]) <= 5.0


@pytest.mark.asyncio
@respx.mock
async def test_run_report_no_wait_does_not_poll(fake_clock):
    respx.post(f"{BASE}/reports/rpt1/runs").mock(
        return_value=httpx.Response(200, json={"token": "run1", "state": "pending"})
    )
    poll = respx.get(f"{BASE}/reports/rpt1/runs/run1").mock(
        return_value=httpx.Response(200, json={"token": "run1", "state": "succeeded"})
    )

    async with _test_client() as client:
        result = await server.run_report(_mock_ctx(client), "rpt1", wait=False)

    assert result == {"run_token": "run1", "state": "pending"}
    assert poll.called is False
    assert fake_clock["sleeps"] == []


def test_disabled_tools_read_only_disables_run_report():
    assert "run_report" in server.WRITE_TOOLS
    disabled = server._disabled_tools({"MODE_READ_ONLY": "true"}, ALL_TOOLS)
    assert "run_report" in disabled


# ---------------------------------------------------------------------------
# chart building
# ---------------------------------------------------------------------------


def _marks(vegas):
    return vegas["encoding"]["marks"]


def test_build_vegas_grouped_vertical_bar():
    vegas = server._build_vegas("bar", "PATH", ["SESSIONS", "USERS"], None, "Top", False, False)
    enc = vegas["encoding"]
    assert vegas["chartType"] == "bar"
    assert vegas["title"] == "Top"
    assert [i["formula"] for i in enc["x"]] == ["[PATH]"]
    assert [i["formula"] for i in enc["y"]] == ["$VALUES"]
    assert [i["formula"] for i in enc["value"]] == ["SUM([SESSIONS])", "SUM([USERS])"]
    series = _marks(vegas)["series"][0]
    assert _marks(vegas)["channel"] == "y"
    assert series["field"] == enc["y"][0]["id"]
    assert series["properties"]["options"] == {"Bar": {"type": "Bar", "transform": "group"}}
    assert [i["formula"] for i in series["properties"]["color"]["values"]] == ["$NAMES"]


def test_build_vegas_stacked_horizontal_bar_swaps_axes():
    vegas = server._build_vegas("bar", "PATH", ["SESSIONS"], "BROWSER", None, True, True)
    enc = vegas["encoding"]
    assert vegas["chartType"] == "hStackedBar"
    assert [i["formula"] for i in enc["y"]] == ["[PATH]"]
    assert [i["formula"] for i in enc["x"]] == ["$VALUES"]
    assert _marks(vegas)["channel"] == "x"
    props = _marks(vegas)["series"][0]["properties"]
    assert props["options"]["Bar"]["transform"] == "stack"
    assert [i["formula"] for i in props["color"]["values"]] == ["[BROWSER]"]


def test_build_vegas_ids_are_unique():
    vegas = server._build_vegas("line", "DAY", ["A", "B", "C"], "G", None, False, False)
    ids = [i["id"] for ch in ("x", "y", "value") for i in vegas["encoding"][ch]]
    ids += [i["id"] for i in _marks(vegas)["series"][0]["properties"]["color"]["values"]]
    assert len(ids) == len(set(ids))


def test_build_vegas_pie_big_number_table():
    pie = server._build_vegas("pie", "PATH", ["SESSIONS"], None, None, False, False)
    props = _marks(pie)["properties"]
    assert pie["chartType"] == "pie"
    assert _marks(pie)["channel"] == "*"
    assert props["color"]["values"][0]["formula"] == "[PATH]"
    assert props["angle"]["values"][0]["formula"] == "SUM([SESSIONS])"

    big = server._build_vegas("big_number", None, ["SESSIONS"], None, None, False, False)
    assert big["chartType"] == "bigValue"
    assert _marks(big)["properties"]["text"]["values"][0]["formula"] == "SUM([SESSIONS])"

    table = server._build_vegas("table", "PATH", ["SESSIONS", "USERS"], None, None, False, False)
    texts = [i["formula"] for i in _marks(table)["properties"]["text"]["values"]]
    assert texts == ["[PATH]", "[SESSIONS]", "[USERS]"]


@pytest.mark.parametrize(
    "chart_type, x, y, message",
    [
        ("line", None, ["A"], "line needs x and y"),
        ("bar", "A", [], "bar needs x and y"),
        ("big_number", "A", [], "big_number needs y"),
        ("table", None, [], "table needs x or y"),
    ],
)
def test_build_vegas_requires_columns(chart_type, x, y, message):
    with pytest.raises(ToolError, match=message):
        server._build_vegas(chart_type, x, y, None, None, False, False)


@pytest.mark.parametrize(
    "fields",
    [
        dict(type="line", x="DAY", y=["A", "B"], color=None, stacked=False, horizontal=False),
        dict(type="area", x="DAY", y=["A"], color="G", stacked=True, horizontal=False),
        dict(type="bar", x="P", y=["A"], color="G", stacked=False, horizontal=False),
        dict(type="bar", x="P", y=["A"], color=None, stacked=True, horizontal=True),
        dict(type="pie", x="P", y=["A"], color=None, stacked=False, horizontal=False),
        dict(type="big_number", x=None, y=["A"], color=None, stacked=False, horizontal=False),
        dict(type="table", x=None, y=["A", "B"], color=None, stacked=False, horizontal=False),
    ],
)
def test_describe_vegas_inverts_build_vegas(fields):
    vegas = server._build_vegas(
        fields["type"], fields["x"], fields["y"], fields["color"], None,
        fields["stacked"], fields["horizontal"],
    )
    assert server._describe_vegas(vegas) == fields


def test_describe_vegas_unreadable_shape_is_none():
    vegas = server._build_vegas("line", "DAY", ["A"], None, None, False, False)
    vegas["encoding"]["value"][0]["formula"] = "AVG([A])"
    assert server._describe_vegas(vegas) is None


def test_resolve_columns_ignores_case_and_lists_available():
    assert server._resolve_columns(["path", "Sessions"], ["PATH", "SESSIONS"]) == ["PATH", "SESSIONS"]
    with pytest.raises(ToolError, match=r"Unknown column\(s\): nope.*PATH, SESSIONS"):
        server._resolve_columns(["nope"], ["PATH", "SESSIONS"])


# ---------------------------------------------------------------------------
# create_chart / update_chart
# ---------------------------------------------------------------------------


PALETTE = ["#111111", "#222222", "#333333"]


def _mock_palette():
    respx.get(f"{BASE}/color_palettes/pal1").mock(
        return_value=httpx.Response(
            200, json={"token": "pal1", "value": "#111111, #222222, #333333"}
        )
    )


def _saved_vegas(route, call=0):
    """The `view_vegas` object a mocked PATCH route received."""
    body = json.loads(route.calls[call].request.content)["visualization"]
    return json.loads(body["view_vegas"]), body


def _mock_latest_columns(header="PATH,SESSIONS\r\n"):
    respx.get(f"{BASE}/reports/rpt1/runs").mock(
        return_value=httpx.Response(
            200,
            json=_hal("report_runs", [{"token": "run1", "state": "succeeded"}]),
        )
    )
    respx.get(f"{BASE}/reports/rpt1/runs/run1/query_runs").mock(
        return_value=httpx.Response(
            200,
            json=_hal(
                "query_runs",
                [{"token": "qr1", "query_token": "q1", "state": "succeeded"}],
            ),
        )
    )
    return respx.get(
        f"{BASE}/reports/rpt1/runs/run1/query_runs/qr1/results/content.csv"
    ).mock(return_value=httpx.Response(200, text=header + "/,731967\r\n"))


@pytest.mark.asyncio
@respx.mock
async def test_create_chart_posts_then_patches_json_string():
    _mock_latest_columns()
    _mock_palette()
    post = respx.post(f"{BASE}/reports/rpt1/queries/q1/charts").mock(
        return_value=httpx.Response(
            200,
            json={
                **_v2_chart("c9", "line", "x"),
                "view_vegas": {},
                "_links": {"report_viz_web": {"href": "/ws/reports/rpt1/viz/c9"}},
            },
        )
    )
    patch = respx.patch(f"{BASE}/switch_views/sv1/visualizations/c9").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json={
                "token": "c9",
                "view_version": 2,
                "view_vegas": json.loads(json.loads(req.content)["visualization"]["view_vegas"]),
            },
        )
    )

    async with _test_client() as client:
        result = await server.create_chart(
            _mock_ctx(client), "rpt1", "q1", "bar", x="path", y=["sessions"], title="Top paths"
        )

    assert json.loads(post.calls[0].request.content) == {"chart": {"view_version": 2}}
    body = json.loads(patch.calls[0].request.content)["visualization"]
    assert body["view_version"] == 2
    assert isinstance(body["view_vegas"], str)
    vegas = json.loads(body["view_vegas"])
    assert vegas["encoding"]["x"][0]["formula"] == "[PATH]"
    assert vegas["encoding"]["value"][0]["formula"] == "SUM([SESSIONS])"
    assert result == {
        "chart_token": "c9",
        "type": "bar",
        "title": "Top paths",
        "query_token": "q1",
        "web_path": "/ws/reports/rpt1/viz/c9",
    }


@pytest.mark.asyncio
@respx.mock
async def test_create_chart_unknown_column_creates_nothing():
    _mock_latest_columns()
    post = respx.post(f"{BASE}/reports/rpt1/queries/q1/charts").mock(
        return_value=httpx.Response(200, json=_v2_chart("c9", "line", "x"))
    )

    async with _test_client() as client:
        with pytest.raises(ToolError, match="Unknown column.*bogus.*Available columns: PATH, SESSIONS"):
            await server.create_chart(
                _mock_ctx(client), "rpt1", "q1", "bar", x="bogus", y=["SESSIONS"]
            )
    assert post.called is False


@pytest.mark.asyncio
@respx.mock
async def test_create_chart_needs_a_succeeded_run():
    respx.get(f"{BASE}/reports/rpt1/runs").mock(
        return_value=httpx.Response(200, json=_hal("report_runs", []))
    )

    async with _test_client() as client:
        with pytest.raises(ToolError, match="Call run_report first"):
            await server.create_chart(
                _mock_ctx(client), "rpt1", "q1", "line", x="A", y=["B"]
            )


@pytest.mark.asyncio
@respx.mock
async def test_create_chart_deletes_empty_chart_when_patch_fails():
    _mock_latest_columns()
    _mock_palette()
    respx.post(f"{BASE}/reports/rpt1/queries/q1/charts").mock(
        return_value=httpx.Response(200, json=_v2_chart("c9", "line", "x"))
    )
    respx.patch(f"{BASE}/switch_views/sv1/visualizations/c9").mock(
        return_value=httpx.Response(500)
    )
    delete = respx.delete(f"{BASE}/switch_views/sv1/visualizations/c9").mock(
        return_value=httpx.Response(200, json={})
    )

    async with _test_client() as client:
        with pytest.raises(ToolError):
            await server.create_chart(
                _mock_ctx(client), "rpt1", "q1", "line", x="PATH", y=["SESSIONS"]
            )
    assert delete.called is True


@pytest.mark.asyncio
@respx.mock
async def test_create_chart_view_overrides_simple_fields():
    _mock_latest_columns()
    _mock_palette()
    respx.post(f"{BASE}/reports/rpt1/queries/q1/charts").mock(
        return_value=httpx.Response(200, json=_v2_chart("c9", "line", "x"))
    )
    patch = respx.patch(f"{BASE}/switch_views/sv1/visualizations/c9").mock(
        return_value=httpx.Response(200, json={"token": "c9", "view_version": 2})
    )

    async with _test_client() as client:
        await server.create_chart(
            _mock_ctx(client), "rpt1", "q1", "bar", x="PATH", y=["SESSIONS"],
            title="simple", view={"title": "raw", "format": {"legend": {"enabled": False}}},
        )

    vegas = json.loads(json.loads(patch.calls[0].request.content)["visualization"]["view_vegas"])
    assert vegas["title"] == "raw"
    assert vegas["format"]["legend"]["enabled"] is False
    assert vegas["format"]["legend"]["position"] == "auto"
    assert vegas["chartType"] == "bar"


@pytest.mark.asyncio
@respx.mock
async def test_update_chart_title_keeps_formatting_and_skips_column_lookup():
    _mock_palette()
    respx.get(f"{BASE}/reports/rpt1/queries/q1/charts/c1").mock(
        return_value=httpx.Response(200, json=_v2_chart("c1", "line", "Old"))
    )
    patch = respx.patch(f"{BASE}/switch_views/sv1/visualizations/c1").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json={
                "token": "c1",
                "view_version": 2,
                "view_vegas": json.loads(json.loads(req.content)["visualization"]["view_vegas"]),
            },
        )
    )
    runs = respx.get(f"{BASE}/reports/rpt1/runs").mock(
        return_value=httpx.Response(200, json=_hal("report_runs", []))
    )

    async with _test_client() as client:
        result = await server.update_chart(_mock_ctx(client), "rpt1", "q1", "c1", title="New")

    vegas = json.loads(json.loads(patch.calls[0].request.content)["visualization"]["view_vegas"])
    assert vegas["title"] == "New"
    assert vegas["format"]["colors"]["a"] == "#fff"
    assert vegas["encoding"]["x"][0]["formula"] == "[week]"
    assert result["title"] == "New"
    assert runs.called is False


@pytest.mark.asyncio
@respx.mock
async def test_update_chart_type_change_rebuilds_encoding_but_keeps_format():
    _mock_latest_columns("week,sessions\r\n")
    _mock_palette()
    chart = _v2_chart("c1", "line", "Revenue")
    chart["view_vegas"]["encoding"]["value"] = [{"id": "4", "formula": "SUM([sessions])"}]
    respx.get(f"{BASE}/reports/rpt1/queries/q1/charts/c1").mock(
        return_value=httpx.Response(200, json=chart)
    )
    patch = respx.patch(f"{BASE}/switch_views/sv1/visualizations/c1").mock(
        return_value=httpx.Response(200, json={"token": "c1", "view_version": 2})
    )

    async with _test_client() as client:
        await server.update_chart(
            _mock_ctx(client), "rpt1", "q1", "c1", type="bar", color="week"
        )

    vegas = json.loads(json.loads(patch.calls[0].request.content)["visualization"]["view_vegas"])
    assert vegas["chartType"] == "bar"
    assert vegas["title"] == "Revenue"
    assert vegas["format"]["colors"]["a"] == "#fff"
    assert vegas["encoding"]["x"][0]["formula"] == "[week]"
    assert vegas["encoding"]["value"][0]["formula"] == "SUM([sessions])"
    series = vegas["encoding"]["marks"]["series"][0]["properties"]
    assert series["color"]["values"][0]["formula"] == "[week]"


@pytest.mark.asyncio
@respx.mock
async def test_update_chart_unreadable_chart_needs_full_fields():
    chart = _v2_chart("c1", "line", "Hand made")
    chart["view_vegas"]["encoding"]["y"][0]["formula"] = "$VALUES"
    chart["view_vegas"]["encoding"]["value"] = [{"id": "9", "formula": "AVG([sessions])"}]
    respx.get(f"{BASE}/reports/rpt1/queries/q1/charts/c1").mock(
        return_value=httpx.Response(200, json=chart)
    )
    patch = respx.patch(f"{BASE}/switch_views/sv1/visualizations/c1")

    async with _test_client() as client:
        with pytest.raises(ToolError, match="can't read back"):
            await server.update_chart(_mock_ctx(client), "rpt1", "q1", "c1", color="browser")
    assert patch.called is False


# ---------------------------------------------------------------------------
# run_report — parameters feedback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_run_report_warns_when_no_form_fields_registered(fake_clock):
    respx.post(f"{BASE}/reports/rpt1/runs").mock(
        return_value=httpx.Response(
            200,
            json={"token": "run1", "state": "succeeded", "parameters": {"weeks": "2"}, "form_fields": []},
        )
    )
    _mock_query_runs()
    respx.get(
        f"{BASE}/reports/rpt1/runs/run1/query_runs/qr1/results/content.json"
    ).mock(return_value=httpx.Response(200, json=[{"a": 1}]))

    async with _test_client() as client:
        result = await server.run_report(
            _mock_ctx(client), "rpt1", parameters={"weeks": 2}
        )

    assert result["applied_parameters"] == {"weeks": "2"}
    assert len(result["warnings"]) == 1
    assert "weeks" in result["warnings"][0]
    assert "{% form %}" in result["warnings"][0]


@pytest.mark.asyncio
@respx.mock
async def test_run_report_no_warning_with_registered_form_fields(fake_clock):
    respx.post(f"{BASE}/reports/rpt1/runs").mock(
        return_value=httpx.Response(
            200,
            json={
                "token": "run1",
                "state": "succeeded",
                "parameters": {"weeks": "2"},
                "form_fields": [{"name": "weeks"}],
            },
        )
    )
    _mock_query_runs()
    respx.get(
        f"{BASE}/reports/rpt1/runs/run1/query_runs/qr1/results/content.json"
    ).mock(return_value=httpx.Response(200, json=[{"a": 1}]))

    async with _test_client() as client:
        result = await server.run_report(
            _mock_ctx(client), "rpt1", parameters={"weeks": 2}
        )

    assert result["applied_parameters"] == {"weeks": "2"}
    assert "warnings" not in result


@pytest.mark.asyncio
@respx.mock
async def test_run_report_no_warning_without_parameters_or_form_data(fake_clock):
    respx.post(f"{BASE}/reports/rpt1/runs").mock(
        return_value=httpx.Response(
            200, json={"token": "run1", "state": "succeeded", "form_fields": []}
        )
    )
    _mock_query_runs()
    respx.get(
        f"{BASE}/reports/rpt1/runs/run1/query_runs/qr1/results/content.json"
    ).mock(return_value=httpx.Response(200, json=[]))

    async with _test_client() as client:
        plain = await server.run_report(_mock_ctx(client), "rpt1")
    assert "warnings" not in plain
    assert "applied_parameters" not in plain


# ---------------------------------------------------------------------------
# columns filter
# ---------------------------------------------------------------------------


def test_project_rows_matches_case_insensitively_and_reports_missing():
    rows = [{"EMAIL": "a@x.com", "PATH": "/", "N": 1}]
    projected, missing = server._project_rows(rows, ["path", "n", "nope"])
    assert projected == [{"PATH": "/", "N": 1}]
    assert missing == ["nope"]


@pytest.mark.asyncio
@respx.mock
async def test_get_run_results_columns_drops_other_columns_before_truncation():
    rows = [{"EMAIL": f"u{i}@x.com", "PATH": "/", "N": i} for i in range(80)]
    respx.get(f"{BASE}/reports/rpt1/runs/run1/query_runs").mock(
        return_value=httpx.Response(
            200,
            json=_hal(
                "query_runs",
                [{"token": "qr1", "query_name": "q", "state": "succeeded"}],
            ),
        )
    )
    respx.get(
        f"{BASE}/reports/rpt1/runs/run1/query_runs/qr1/results/content.json"
    ).mock(return_value=httpx.Response(200, json=rows))

    async with _test_client() as client:
        result = await server.get_run_results(
            _mock_ctx(client), "rpt1", "run1",
            max_rows_per_query=10, columns=["path", "n", "ghost"],
        )

    assert result[0]["row_count"] == 80
    assert result[0]["truncated"] is True
    assert result[0]["data"][0] == {"PATH": "/", "N": 0}
    assert all("EMAIL" not in row for row in result[0]["data"])
    assert result[0]["missing_columns"] == ["ghost"]


@pytest.mark.asyncio
@respx.mock
async def test_run_report_columns(fake_clock):
    respx.post(f"{BASE}/reports/rpt1/runs").mock(
        return_value=httpx.Response(200, json={"token": "run1", "state": "succeeded"})
    )
    _mock_query_runs()
    respx.get(
        f"{BASE}/reports/rpt1/runs/run1/query_runs/qr1/results/content.json"
    ).mock(return_value=httpx.Response(200, json=[{"EMAIL": "a@x.com", "N": 1}]))

    async with _test_client() as client:
        result = await server.run_report(_mock_ctx(client), "rpt1", columns=["n"])

    assert result["queries"][0]["data"] == [{"N": 1}]


# ---------------------------------------------------------------------------
# get_report — parameters
# ---------------------------------------------------------------------------


def _mock_report_with_last_run(last_run_response):
    respx.get(f"{BASE}/reports/rpt1").mock(
        return_value=httpx.Response(
            200,
            json={
                "token": "rpt1",
                "name": "R",
                "_links": {"last_run": {"href": f"/api/{WORKSPACE}/reports/rpt1/runs/run1"}},
            },
        )
    )
    respx.get(f"{BASE}/reports/rpt1/queries").mock(
        return_value=httpx.Response(200, json=_hal("queries", []))
    )
    respx.get(f"{BASE}/reports/rpt1/runs/run1").mock(return_value=last_run_response)


@pytest.mark.asyncio
@respx.mock
async def test_get_report_exposes_registered_form_fields():
    fields = [{"name": "weeks", "type": "text", "default": "16"}]
    _mock_report_with_last_run(
        httpx.Response(200, json={"token": "run1", "form_fields": fields})
    )

    async with _test_client() as client:
        result = await server.get_report(_mock_ctx(client), "rpt1")
    assert result["parameters"] == fields


@pytest.mark.asyncio
@respx.mock
async def test_get_report_parameters_empty_vs_unreadable():
    _mock_report_with_last_run(
        httpx.Response(200, json={"token": "run1", "form_fields": []})
    )
    async with _test_client() as client:
        assert (await server.get_report(_mock_ctx(client), "rpt1"))["parameters"] == []

    respx.reset()
    _mock_report_with_last_run(httpx.Response(500))
    async with _test_client() as client:
        assert (await server.get_report(_mock_ctx(client), "rpt1"))["parameters"] is None


# ---------------------------------------------------------------------------
# default format and color settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_create_chart_fills_default_format_colors_and_palette():
    _mock_latest_columns("PATH,SESSIONS,USERS\r\n")
    _mock_palette()
    respx.post(f"{BASE}/reports/rpt1/queries/q1/charts").mock(
        return_value=httpx.Response(200, json=_v2_chart("c9", "line", "x"))
    )
    patch = respx.patch(f"{BASE}/switch_views/sv1/visualizations/c9").mock(
        return_value=httpx.Response(200, json={"token": "c9", "view_version": 2})
    )

    async with _test_client() as client:
        await server.create_chart(
            _mock_ctx(client), "rpt1", "q1", "line", x="PATH", y=["SESSIONS", "USERS"]
        )

    vegas, body = _saved_vegas(patch)
    assert body["color_palette_token"] == "pal1"
    assert vegas["format"]["colors"] == {"theme": "pal1", "custom": PALETTE, "map": {}}
    assert vegas["format"]["useMuzeRenderer"] is True
    assert set(vegas["format"]) >= {
        "xAxis", "yAxis", "y2Axis", "legend", "flatTable", "bigValue", "nullToZero", "showValues",
    }
    assert vegas["format"]["xAxis"]["numberRange"] == {"min": None, "max": None}
    assert vegas["fieldColors"] == {
        "@DIS_$NAMES": {
            "colorPaletteToken": "pal1",
            "palette": PALETTE,
            "map": {"SESSIONS": 0, "USERS": 1},
        }
    }


@pytest.mark.asyncio
@respx.mock
async def test_create_chart_uses_default_palette_when_chart_has_none():
    _mock_latest_columns()
    _mock_palette()
    respx.get(f"{BASE}/color_palettes").mock(
        return_value=httpx.Response(
            200,
            json=_hal(
                "color_palettes",
                [{"token": "other", "is_default": False}, {"token": "pal1", "is_default": True}],
            ),
        )
    )
    created = {**_v2_chart("c9", "line", "x"), "color_palette_token": None}
    respx.post(f"{BASE}/reports/rpt1/queries/q1/charts").mock(
        return_value=httpx.Response(200, json=created)
    )
    patch = respx.patch(f"{BASE}/switch_views/sv1/visualizations/c9").mock(
        return_value=httpx.Response(200, json={"token": "c9", "view_version": 2})
    )

    async with _test_client() as client:
        await server.create_chart(
            _mock_ctx(client), "rpt1", "q1", "table", x="PATH", y=["SESSIONS"]
        )

    vegas, body = _saved_vegas(patch)
    assert body["color_palette_token"] == "pal1"
    assert vegas["format"]["colors"]["theme"] == "pal1"
    assert vegas["format"]["useMuzeRenderer"] is False


@pytest.mark.asyncio
@respx.mock
async def test_update_chart_with_no_changes_repairs_missing_defaults():
    _mock_palette()
    chart = _v2_chart("c1", "line", "Old")
    chart["color_palette_token"] = None
    del chart["view_vegas"]["format"]
    respx.get(f"{BASE}/reports/rpt1/queries/q1/charts/c1").mock(
        return_value=httpx.Response(200, json=chart)
    )
    respx.get(f"{BASE}/color_palettes").mock(
        return_value=httpx.Response(
            200, json=_hal("color_palettes", [{"token": "pal1", "is_default": True}])
        )
    )
    patch = respx.patch(f"{BASE}/switch_views/sv1/visualizations/c1").mock(
        return_value=httpx.Response(200, json={"token": "c1", "view_version": 2})
    )

    async with _test_client() as client:
        await server.update_chart(_mock_ctx(client), "rpt1", "q1", "c1")

    vegas, body = _saved_vegas(patch)
    assert body["color_palette_token"] == "pal1"
    assert vegas["format"]["colors"] == {"theme": "pal1", "custom": PALETTE, "map": {}}
    assert vegas["title"] == "Old"
    assert vegas["encoding"]["x"][0]["formula"] == "[week]"


@pytest.mark.asyncio
@respx.mock
async def test_update_chart_never_overwrites_existing_format_or_field_colors():
    _mock_palette()
    chart = _v2_chart("c1", "line", "Old")
    chart["view_vegas"]["format"] = {
        "colors": {"theme": "mine", "custom": ["#abcdef"], "map": {"a": "#000"}},
        "legend": {"enabled": False},
    }
    chart["view_vegas"]["fieldColors"] = {
        "@DIS_[browser]": {"colorPaletteToken": "mine", "palette": ["#abcdef"], "map": {"x": 0}}
    }
    respx.get(f"{BASE}/reports/rpt1/queries/q1/charts/c1").mock(
        return_value=httpx.Response(200, json=chart)
    )
    patch = respx.patch(f"{BASE}/switch_views/sv1/visualizations/c1").mock(
        return_value=httpx.Response(200, json={"token": "c1", "view_version": 2})
    )

    async with _test_client() as client:
        await server.update_chart(_mock_ctx(client), "rpt1", "q1", "c1", title="New")

    vegas, _ = _saved_vegas(patch)
    assert vegas["format"]["colors"] == {
        "theme": "mine", "custom": ["#abcdef"], "map": {"a": "#000"}
    }
    assert vegas["format"]["legend"] == {
        "enabled": False, "position": "auto", "title": None, "titleEnabled": True, "baseFormat": {},
    }
    assert vegas["fieldColors"]["@DIS_[browser]"]["map"] == {"x": 0}


@pytest.mark.asyncio
@respx.mock
async def test_palette_falls_back_to_modes_predefined_default():
    respx.get(f"{BASE}/color_palettes").mock(
        return_value=httpx.Response(
            200, json=_hal("color_palettes", [{"token": "custom", "is_default": False}])
        )
    )
    respx.get(f"{BASE}/color_palettes/{server.MODE_DEFAULT_PALETTE}").mock(
        return_value=httpx.Response(200, json={"value": "#111111, #222222"})
    )

    async with _test_client() as client:
        token, colors = await server._palette(client, None)

    assert token == server.MODE_DEFAULT_PALETTE
    assert colors == ["#111111", "#222222"]
