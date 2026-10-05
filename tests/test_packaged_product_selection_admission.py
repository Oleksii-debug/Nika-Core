from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_packaged_journey import (
    PackagedProductJourneyError,
    PackagedProductSelectionStore,
    packaged_current_product_command,
    packaged_product_reopen_target,
    product_project_identity,
)


def _selection(tmp_path: Path) -> tuple[SQLiteStore, PackagedProductSelectionStore]:
    store = SQLiteStore(tmp_path / "збережений вибір з пробілами.db")
    store.initialize()
    return store, PackagedProductSelectionStore(store)


@pytest.mark.parametrize(
    "stored",
    [
        " product-existing ",
        "product-existing\x00",
        "product-\u202eexisting",
        sqlite3.Binary(b"product-existing"),
    ],
)
def test_corrupt_persisted_selection_is_not_coerced_or_modified(
    tmp_path: Path, stored: object
) -> None:
    store, selection = _selection(tmp_path)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO packaged_product_selection(slot, project_id) VALUES (1, ?)",
            (stored,),
        )
    assert selection.load() is None
    with store.connection() as conn:
        row = conn.execute(
            "SELECT project_id FROM packaged_product_selection WHERE slot = 1"
        ).fetchone()
    assert row is not None and row["project_id"] == stored


@pytest.mark.parametrize(
    "invalid",
    [None, True, b"product-existing", " ", "p\x00", "p\u202e", "p\ud800"],
)
def test_invalid_selection_never_overwrites_previous_selection(
    tmp_path: Path, invalid: object
) -> None:
    _store, selection = _selection(tmp_path)
    selection.select("product-existing")
    with pytest.raises(PackagedProductJourneyError, match="selected ProductProject id"):
        selection.select(invalid)  # type: ignore[arg-type]
    assert selection.load() == "product-existing"


def test_normal_selection_keeps_existing_whitespace_normalization(tmp_path: Path) -> None:
    _store, selection = _selection(tmp_path)
    selection.select("  product-existing  ")
    assert selection.load() == "product-existing"


@pytest.mark.parametrize(
    "command",
    ["Open ProductProjects", "Reopen ProductProjectManager", "Відкрий ProductProjectXYZ"],
)
def test_reopen_prefix_must_end_on_a_command_boundary(command: str) -> None:
    assert packaged_product_reopen_target(command) is None


def test_valid_colon_reopen_and_explicit_missing_id_are_preserved() -> None:
    project_id = "product-" + "a" * 64
    assert packaged_product_reopen_target("Open ProductProject:" + project_id) == project_id
    with pytest.raises(PackagedProductJourneyError, match="64 hex"):
        packaged_product_reopen_target("Open ProductProject")

def test_invalid_utf8_stored_as_sqlite_text_is_not_loaded_or_rewritten(
    tmp_path: Path,
) -> None:
    store, selection = _selection(tmp_path)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO packaged_product_selection(slot, project_id) "
            "VALUES (1, CAST(X'80' AS TEXT))"
        )
    assert selection.load() is None
    with store.connection() as conn:
        row = conn.execute(
            "SELECT hex(CAST(project_id AS BLOB)) AS raw_id "
            "FROM packaged_product_selection WHERE slot = 1"
        ).fetchone()
    assert row is not None and row["raw_id"] == "80"

@pytest.mark.parametrize(
    "invalid",
    [None, True, 42, b"create a product", ["create a product"], "task\x00name", "task\ud800"],
)
def test_packaged_router_rejects_nontext_or_malformed_commands_before_routing(
    invalid: object,
) -> None:
    from nika_core.product_factory_packaged_journey import PackagedProductCommandRouter

    def forbid_ordinary(_payload: object) -> None:
        raise AssertionError("invalid command escaped packaged admission")

    router = PackagedProductCommandRouter(
        products=object(),  # type: ignore[arg-type]
        ordinary_handler=forbid_ordinary,  # type: ignore[arg-type]
    )
    with pytest.raises(PackagedProductJourneyError, match="Команда"):
        router.create({"command": invalid})
    assert router.active_project_id is None

class _HostileDirectHelperText(str):
    def split(self, *args: object, **kwargs: object) -> list[str]:
        del args, kwargs
        raise AssertionError("direct helper must not call untrusted string methods")


@pytest.mark.parametrize(
    "helper",
    (product_project_identity, packaged_product_reopen_target, packaged_current_product_command),
)
@pytest.mark.parametrize(
    "command",
    (
        None,
        17,
        False,
        ["Створи застосунок"],
        _HostileDirectHelperText("Створи застосунок"),
        type("PlainHelperSubclass", (str,), {})("Створи застосунок"),
    ),
)
def test_direct_product_helpers_reject_noncanonical_text_before_methods(
    helper: object,
    command: object,
) -> None:
    with pytest.raises(PackagedProductJourneyError, match="звичайним текстом"):
        helper(command)  # type: ignore[operator]


class _HostileBridgeCommand(str):
    def __str__(self) -> str:
        raise AssertionError("bridge must not stringify hostile command input")

    def split(self, *args: object, **kwargs: object) -> list[str]:
        del args, kwargs
        raise AssertionError("bridge must not split hostile command input")


def test_real_windows_bridge_composes_ui_payload_and_product_admission(
    tmp_path: Path,
) -> None:
    from nika_core.config import AppConfig
    from scripts.nika_windows import build_windows_bridge

    bridge, _products = build_windows_bridge(
        AppConfig(database_path=tmp_path / "combined packaged ingress.db")
    )

    malformed_payload = bridge.dispatch(
        {
            "request_id": "malformed-payload",
            "action_id": "task.create",
            "payload": {"command": object()},
        }
    )
    assert malformed_payload["status"] == "rejected"
    assert malformed_payload["message"].startswith("Invalid UI command:")

    hostile_payload = bridge.dispatch(
        {
            "request_id": "hostile-text",
            "action_id": "task.create",
            "payload": {"command": _HostileBridgeCommand("Create product application")},
        }
    )
    assert hostile_payload["status"] == "rejected"
    assert hostile_payload["message"].startswith("Invalid UI command:")

    nontext_command = bridge.dispatch(
        {
            "request_id": "nontext-command",
            "action_id": "task.create",
            "payload": {"command": 42},
        }
    )
    assert nontext_command["status"] == "rejected"
    assert nontext_command["message"] == "Команда повинна бути текстом."

    before = bridge.get_state()
    assert before["ok"] is True
    assert before["state"]["product_project"] is None

    command = "Create product application for accessible invoice review"
    project_id = product_project_identity(command)
    accepted = bridge.dispatch(
        {
            "request_id": "valid-product",
            "action_id": "task.create",
            "payload": {"command": command},
        }
    )
    assert accepted["status"] == "completed"
    assert accepted["request_id"] == "valid-product"

    after = bridge.get_state()
    assert after["ok"] is True
    assert after["state"]["product_project"]["project_id"] == project_id

