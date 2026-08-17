from __future__ import annotations

import asyncio
import os

from nika_core.runtime.langgraph_runtime import open_langgraph_sqlite


def test_async_sqlite_helper_creates_database_and_closes_connection(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "runtime" / "checkpoints.sqlite"
        async with open_langgraph_sqlite(path) as handle:
            assert path.parent.is_dir()
            assert os.environ["LANGGRAPH_STRICT_MSGPACK"] == "true"
            await handle.connection.execute("CREATE TABLE proof(value TEXT)")
            await handle.connection.execute("INSERT INTO proof(value) VALUES ('ok')")
            await handle.connection.commit()
            cursor = await handle.connection.execute("SELECT value FROM proof")
            row = await cursor.fetchone()
            await cursor.close()
            assert row[0] == "ok"
            assert handle._closed is False
        assert handle._closed is True

    asyncio.run(scenario())


def test_async_sqlite_helper_overrides_insecure_strict_setting(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "false")
        async with open_langgraph_sqlite(tmp_path / "checkpoints.sqlite"):
            assert os.environ["LANGGRAPH_STRICT_MSGPACK"] == "true"

    asyncio.run(scenario())
