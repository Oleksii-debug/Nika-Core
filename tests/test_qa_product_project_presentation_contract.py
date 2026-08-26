from __future__ import annotations

import re
from pathlib import Path

from nika_core.config import AppConfig
from scripts.nika_windows import build_windows_bridge

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "src" / "nika_core" / "ui" / "web" / "app.js"

_FRONTEND_FIELDS = {
    "title",
    "project_id",
    "goal",
    "state",
    "spec_version",
    "blocker_count",
    "status_count",
    "decision_count",
}
_PROVIDER_ONLY_BOUNDED_FIELDS = {
    "status_counts",
    "decision_state_counts",
}
_FORBIDDEN_AUTHORITY_FIELDS = {
    "evidence",
    "evidence_refs",
    "credential_refs",
    "authorization_ref",
    "provider_session",
    "protected_store_handle",
    "workspace_roots",
    "credential_handle",
    "token",
    "secret",
}


def _frontend_product_fields() -> set[str]:
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("const productProjectFields = Object.freeze({")
    end = source.index("  });", start)
    block = source[start:end]
    return set(
        re.findall(
            r"^\s{4}([A-Za-z_][A-Za-z0-9_]*): document\.getElementById",
            block,
            flags=re.MULTILINE,
        )
    )


def _product_state(bridge) -> dict[str, object]:
    response = bridge.get_state()
    assert response.get("ok") is True
    state = response.get("state")
    assert isinstance(state, dict)
    product = state.get("product_project")
    assert isinstance(product, dict)
    return product


def test_exact_parent_packaged_bridge_matches_product_project_frontend_contract(
    tmp_path: Path,
) -> None:
    """QA_ONLY: prove #492 consumes the real packaged bounded bridge projection."""
    frontend_fields = _frontend_product_fields()
    assert frontend_fields == _FRONTEND_FIELDS

    database = tmp_path / "Дані Nika QA" / "presentation contract.db"
    database.parent.mkdir(parents=True)
    config = AppConfig(database_path=database)
    bridge, _products = build_windows_bridge(config)

    initial = bridge.get_state()
    assert initial.get("ok") is True
    assert initial["state"]["product_project"] is None

    command = "Create product application for exact parent presentation contract QA"
    created = bridge.dispatch(
        {
            "request_id": "qa-product-project-presentation-contract",
            "action_id": "task.create",
            "payload": {"command": command},
        }
    )
    assert created["status"] == "completed"

    product = _product_state(bridge)
    assert set(product) == _FRONTEND_FIELDS | _PROVIDER_ONLY_BOUNDED_FIELDS
    assert frontend_fields <= set(product)
    assert set(product).isdisjoint(_FORBIDDEN_AUTHORITY_FIELDS)

    for field in ("title", "project_id", "goal", "state"):
        value = product[field]
        assert isinstance(value, str)
        assert value.strip()
    assert isinstance(product["spec_version"], int)
    assert not isinstance(product["spec_version"], bool)
    assert product["spec_version"] >= 1
    for field in ("blocker_count", "status_count", "decision_count"):
        value = product[field]
        assert isinstance(value, int)
        assert not isinstance(value, bool)
        assert value >= 0

    restarted_bridge, _restarted_products = build_windows_bridge(config)
    recovered = _product_state(restarted_bridge)
    assert {field: recovered[field] for field in frontend_fields} == {
        field: product[field] for field in frontend_fields
    }
    assert set(recovered).isdisjoint(_FORBIDDEN_AUTHORITY_FIELDS)
