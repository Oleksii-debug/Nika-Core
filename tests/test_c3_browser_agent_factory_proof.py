from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from scripts import c3_browser_agent_factory_proof as c3


def _post(url: str, values: dict[str, str]) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(values).encode("utf-8"),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_factory_graph_is_exactly_decomposed_and_restart_safe(tmp_path: Path) -> None:
    result = c3._create_factory_state(tmp_path, "a" * 40)

    assert result["project_id"] == c3.PROJECT_ID
    assert result["restart_exact"] is True
    assert result["components"] == {
        "commerce-fixture": ["generated/c3_browser_agent/fixture.py"],
        "package": ["generated/c3_browser_agent/package.py"],
        "semantic-browser-agent": ["generated/c3_browser_agent/agent.py"],
    }
    assert c3._graph().dependency_order() == (
        "commerce-fixture",
        "semantic-browser-agent",
        "package",
    )


def test_local_checkout_retry_is_idempotent_after_uncertain_result() -> None:
    with c3.FixtureServer() as fixture:
        values = {"buyer": "Sandbox User", "key": "c3-order-001"}
        first_status, first_body = _post(f"{fixture.base_url}/checkout/submit", values)
        second_status, second_body = _post(f"{fixture.base_url}/checkout/submit", values)

        assert first_status == 503
        assert "Checkout outcome uncertain" in first_body
        assert second_status == 200
        assert "Simulated checkout confirmed" in second_body
        assert fixture.state.submit_attempts["c3-order-001"] == 2
        assert len(fixture.state.orders) == 1
        assert fixture.state.orders["c3-order-001"]["order_id"] == "SIM-0001"


def test_reconciliation_api_is_read_only_and_eventually_observes_same_order() -> None:
    with c3.FixtureServer() as fixture:
        values = {"buyer": "Sandbox User", "key": "c3-order-001"}
        _post(f"{fixture.base_url}/checkout/submit", values)

        first = c3._read_order_api(fixture.base_url, "c3-order-001")
        second = c3._read_order_api(fixture.base_url, "c3-order-001")

        assert first == {"status": "unknown"}
        assert second["status"] == "completed"
        assert second["order"]["order_id"] == "SIM-0001"
        assert fixture.state.submit_attempts["c3-order-001"] == 1
        assert len(fixture.state.orders) == 1


def test_package_reuses_canonical_release_manifest(tmp_path: Path) -> None:
    package = c3._package(
        tmp_path,
        "b" * 40,
        {"project_id": c3.PROJECT_ID, "restart_exact": True},
        {"semantic_only": True, "simulated_order_count": 1},
    )
    bundle = Path(package["bundle"])
    archive = Path(package["zip"])

    manifest = json.loads((bundle / "release-manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_sha"] == "b" * 40
    assert manifest["product"] == "Nika C3 Browser Agent"
    assert package["manifest_files"] == 3
    assert len(package["zip_sha256"]) == 64
    with zipfile.ZipFile(archive) as zipped:
        names = set(zipped.namelist())
    assert "c3-browser-agent-product/release-manifest.json" in names
    assert "c3-browser-agent-product/browser-agent-proof.py" in names


def test_browser_agent_proof_contains_no_brittle_selector_or_coordinate_escape_hatches() -> None:
    source = Path(c3.__file__).read_text(encoding="utf-8")
    forbidden = (
        ".first",
        ".last",
        ".nth(",
        "xpath=",
        "query_selector",
        "mouse.",
        "bounding_box",
        "get_by_test_id",
    )
    assert all(token not in source for token in forbidden)
    assert "ControlLocator" in source
    assert "resolve_strict" in source
    assert "PlaywrightInteractionAdapter" in source
    assert "vision_ocr_used" in source
    assert "coordinates_used" in source
