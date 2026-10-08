from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_command.command_center import ProductCommandCenter
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_factory_packaged_journey import (
    PackagedProductCommandRouter,
    PackagedProductSelectionStore,
    PackagedProductStateProvider,
)
from nika_core.product_project import (
    ProductBlocker,
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
)
from nika_core.ui.bridge_models import UIResult

_PROJECT_ID = "product-status-presentation"


def _ordinary_handler(_payload: Mapping[str, Any]) -> UIResult:
    raise AssertionError("ProductProject status presentation must not create an ordinary task")


def _provider(database: Path) -> PackagedProductStateProvider:
    store = SQLiteStore(database)
    store.initialize()
    repository = ProductProjectRepository(store)
    service = ProductProjectCommandService(repository)
    requirements = tuple(
        ProductRequirement(
            requirement_id=f"req-{index:02d}",
            text=f"Requirement {index:02d}",
            acceptance=("Verified by deterministic evidence",),
        )
        for index in range(30)
    )
    service.create_project(
        project_id=_PROJECT_ID,
        name="Bounded status presentation",
        spec=ProductProjectSpec(
            goal="Expose truthful bounded ProductProject status",
            desired_outcome="Keyboard users can read blockers and status details",
            requirements=requirements,
            blockers=(
                ProductBlocker(
                    blocker_id="blocker-nvda",
                    summary="Human NVDA evidence is still pending",
                    evidence_refs=("evidence://nvda/private-proof",),
                ),
            ),
        ),
        idempotency_key="create:product-status-presentation",
    )
    selection = PackagedProductSelectionStore(store)
    selection.select(_PROJECT_ID)
    router = PackagedProductCommandRouter(
        products=service,
        ordinary_handler=_ordinary_handler,
        selection_store=selection,
    )
    return PackagedProductStateProvider(
        base_state=lambda: {},
        router=router,
        command_center=ProductCommandCenter(service),
    )


def test_status_preview_is_bounded_prioritizes_blocker_and_omits_evidence(
    tmp_path: Path,
) -> None:
    provider = _provider(tmp_path / "bounded status.db")

    project = provider()["product_project"]

    assert project is not None
    assert project["project_id"] == _PROJECT_ID
    assert project["status_count"] == 31
    assert project["blocker_count"] == 1
    assert project["status_items_truncated"] is True
    assert len(project["status_items"]) == 24
    assert project["status_items"][0] == {
        "kind": "blocker",
        "item_id": "blocker-nvda",
        "label": "Human NVDA evidence is still pending",
        "state": "active",
        "detail": "Blocks: project-wide.",
    }
    assert [item["item_id"] for item in project["status_items"][1:]] == [
        f"req-{index:02d}" for index in range(23)
    ]

    serialized = json.dumps(project["status_items"], ensure_ascii=False, sort_keys=True)
    assert "evidence://nvda/private-proof" not in serialized
    assert "evidence" not in serialized
    assert "credential" not in serialized
