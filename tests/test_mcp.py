import json
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from video_agent.store import Store


@pytest.mark.anyio
async def test_real_stdio_mcp_handshake_and_read_tools(tmp_path):
    with Store(tmp_path):
        pass
    params = StdioServerParameters(command=sys.executable, args=["mcp_server.py", str(tmp_path)])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name for tool in (await session.list_tools()).tools}
            assert {"search_library", "library_overview", "list_events", "inspect_item", "get_frame"} <= tools
            result = await session.call_tool("library_overview", {})
            assert not result.isError
            payload = result.structuredContent or json.loads(result.content[0].text)
            assert payload["total"] == 0
            invalid = await session.call_tool("library_overview", {"offset": -1})
            assert invalid.isError


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_active_stdio_server_follows_persisted_folder_without_restart(tmp_path):
    from video_agent.app_state import AppState

    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    with Store(first) as store:
        store.register(first / "one.mp4", "one", {"duration_ms": 1000})
    with Store(second) as store:
        store.register(second / "two.mp4", "two", {"duration_ms": 2000})
    registry = AppState(tmp_path / "state")
    first_library = registry.select(str(first))
    params = StdioServerParameters(
        command=sys.executable, args=["mcp_server.py", "--active", "--state-dir", str(registry.directory)]
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("library_context", {})
            payload = result.structuredContent or json.loads(result.content[0].text)
            assert not result.isError and payload["folder"] == str(first)
            registry.select(str(second))
            result = await session.call_tool("library_context", {})
            payload = result.structuredContent or json.loads(result.content[0].text)
            assert not result.isError and payload["folder"] == str(second)
            overview = await session.call_tool("library_overview", {})
            payload = overview.structuredContent or json.loads(overview.content[0].text)
            assert not overview.isError and payload["videos"][0]["path"].endswith("two.mp4")
            listing = await session.call_tool("list_libraries", {})
            payload = listing.structuredContent or json.loads(listing.content[0].text)
            assert len(payload["libraries"]) == 2
            selected_before = registry.active_id()
            pinned = await session.call_tool("library_overview", {"library_id": first_library["id"]})
            payload = pinned.structuredContent or json.loads(pinned.content[0].text)
            assert not pinned.isError and payload["videos"][0]["path"].endswith("one.mp4")
            assert payload["library_id"] == first_library["id"]
            assert registry.active_id() == selected_before
            for tool in [
                "library_overview",
                "search_library",
                "list_events",
                "inspect_item",
                "list_children",
                "object_inventory",
                "get_frame",
            ]:
                schema = next(t.inputSchema for t in (await session.list_tools()).tools if t.name == tool)
                assert "library_id" in schema["properties"]
            invalid = await session.call_tool("library_context", {"library_id": "not-registered"})
            assert invalid.isError
            registry.remove_library(first_library["id"])
            listing = await session.call_tool("list_libraries", {})
            payload = listing.structuredContent or json.loads(listing.content[0].text)
            assert len(payload["libraries"]) == 1
            assert payload["libraries"][0]["folder"] == str(second)
            detached = await session.call_tool("library_overview", {"library_id": first_library["id"]})
            assert detached.isError
            assert not (first / ".video-library" / "library-v2.sqlite3").exists()
            second_id = payload["libraries"][0]["library_id"]
            registry.remove_library(second_id)
            listing = await session.call_tool("list_libraries", {})
            payload = listing.structuredContent or json.loads(listing.content[0].text)
            assert payload["libraries"] == []
