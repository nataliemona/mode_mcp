"""Tests for the Mode Analytics MCP server."""

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

    async with _test_client() as client:
        result = await server.get_report(_mock_ctx(client), "rpt1")
    assert result["token"] == "rpt1"
    assert len(result["queries"]) == 1
    assert "SELECT" in result["queries"][0]["raw_query"]


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
    respx.post(f"{BASE}/spaces/sp1/reports").mock(
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
    respx.post(f"{BASE}/spaces/sp1/reports").mock(
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
async def test_create_report_partial_query_failure():
    respx.post(f"{BASE}/spaces/sp1/reports").mock(
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


def _mock_report_for_delete(owner="testuser"):
    """Set up respx mocks for delete_report tests."""
    respx.get(f"{BASE}/reports/rpt1").mock(
        return_value=httpx.Response(
            200,
            json={
                "token": "rpt1",
                "name": "My Report",
                "_links": {
                    "creator": {"href": f"/api/{owner}/reports/rpt1"}
                },
            },
        )
    )
    respx.get(f"{BASE}/reports/rpt1/queries").mock(
        return_value=httpx.Response(
            200,
            json=_hal("queries", [
                {"token": "q1", "name": "revenue"},
                {"token": "q2", "name": "users"},
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
    assert result["creator"] == "testuser"
    assert result["query_count"] == 2
    assert result["queries"] == ["revenue", "users"]
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
            json={"token": "q1", "name": "revenue", "raw_query": "SELECT 1"},
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
    env = {"MODE_ENABLE_DELETES": "true", "MODE_DISABLED_TOOLS": " create_report, add_query ,"}
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
                    {"token": "qr1", "query_name": "good", "state": "succeeded"},
                    {"token": "qr2", "query_name": "bad", "state": "failed"},
                ],
            ),
        )
    )
    respx.get(
        f"{BASE}/reports/rpt1/runs/run1/query_runs/qr1/results/content.json"
    ).mock(return_value=httpx.Response(200, json=[{"ok": True}]))
    respx.get(
        f"{BASE}/reports/rpt1/runs/run1/query_runs/qr2/results/content.json"
    ).mock(return_value=httpx.Response(404))

    async with _test_client() as client:
        results = await server._fetch_run_results(client, "rpt1", "run1")

    assert len(results) == 2
    assert results[0]["data"] == [{"ok": True}]
    assert results[1]["error"] == "HTTP 404"
