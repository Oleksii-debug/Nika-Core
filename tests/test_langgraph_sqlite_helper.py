from __future__ import annotations

import os
import sys
from types import ModuleType

import pytest

from nika_core.runtime.langgraph_runtime import open_langgraph_sqlite


def _install_fake_saver(monkeypatch: pytest.MonkeyPatch, saver_type: type[object]) -> None:
    langgraph = ModuleType("langgraph")
    checkpoint = ModuleType("langgraph.checkpoint")
    sqlite_module = ModuleType("langgraph.checkpoint.sqlite")
    sqlite_module.SqliteSaver = saver_type
    monkeypatch.setitem(sys.modules, "langgraph", langgraph)
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint", checkpoint)
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.sqlite", sqlite_module)


def test_sqlite_helper_enables_strict_deserialization_before_saver_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    created: list[object] = []

    class FakeSaver:
        def __init__(self, connection) -> None:
            assert os.environ.get("LANGGRAPH_STRICT_MSGPACK") == "true"
            self.connection = connection
            self.setup_called = False
            created.append(self)

        def setup(self) -> None:
            self.setup_called = True

    _install_fake_saver(monkeypatch, FakeSaver)
    monkeypatch.delenv("LANGGRAPH_STRICT_MSGPACK", raising=False)

    path = tmp_path / "runtime" / "checkpoints.sqlite"
    with open_langgraph_sqlite(path) as handle:
        assert path.parent.is_dir()
        assert created and created[0] is handle.checkpointer
        assert handle.checkpointer.setup_called is True
        handle.connection.execute("CREATE TABLE proof(value TEXT)")
        handle.connection.execute("INSERT INTO proof(value) VALUES ('ok')")
        handle.connection.commit()
        row = handle.connection.execute("SELECT value FROM proof").fetchone()
        assert row[0] == "ok"


def test_sqlite_helper_overrides_insecure_strict_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    class FakeSaver:
        def __init__(self, connection) -> None:
            assert os.environ["LANGGRAPH_STRICT_MSGPACK"] == "true"
            self.connection = connection

        def setup(self) -> None:
            pass

    _install_fake_saver(monkeypatch, FakeSaver)
    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "false")

    with open_langgraph_sqlite(tmp_path / "checkpoints.sqlite"):
        assert os.environ["LANGGRAPH_STRICT_MSGPACK"] == "true"
