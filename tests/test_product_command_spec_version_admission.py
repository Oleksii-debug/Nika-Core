from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_project import (
    ProductProjectRepository,
    ProductProjectSpec,
    StaleProjectVersionError,
)


def _service(
    tmp_path: Path,
) -> tuple[ProductProjectCommandService, ProductProjectRepository]:
    store = SQLiteStore(tmp_path / "Дані з пробілами" / "nika.db")
    store.initialize()
    repository = ProductProjectRepository(store)
    service = ProductProjectCommandService(repository)
    service.create_project(
        project_id="product-command-admission",
        name="Доступний застосунок",
        spec=ProductProjectSpec(
            goal="Початкова ціль",
            desired_outcome="Працюючий Windows-застосунок",
        ),
        idempotency_key="create-product-command-admission",
    )
    return service, repository


@pytest.mark.parametrize(
    "invalid_version",
    [
        pytest.param(True, id="bool-true"),
        pytest.param(False, id="bool-false"),
        pytest.param(1.0, id="float"),
        pytest.param("1", id="string"),
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
    ],
)
def test_update_project_rejects_non_exact_positive_spec_version_without_mutation(
    tmp_path: Path,
    invalid_version: object,
) -> None:
    service, repository = _service(tmp_path)
    before = repository.get("product-command-admission")

    with pytest.raises(
        ValueError,
        match="expected_spec_version must be a positive integer",
    ):
        service.update_project(
            "product-command-admission",
            expected_spec_version=invalid_version,  # type: ignore[arg-type]
            goal="Ця зміна не повинна записатися",
        )

    after = repository.get("product-command-admission")
    assert after == before
    assert len(repository.spec_history("product-command-admission")) == 1


def test_update_project_preserves_stale_exact_integer_rejection(tmp_path: Path) -> None:
    service, repository = _service(tmp_path)

    with pytest.raises(StaleProjectVersionError, match="stale ProductProject spec"):
        service.update_project(
            "product-command-admission",
            expected_spec_version=2,
            goal="Неактуальна зміна",
        )

    assert repository.get("product-command-admission").spec_version == 1


def test_update_project_accepts_exact_version_and_unicode_goal(tmp_path: Path) -> None:
    service, repository = _service(tmp_path)

    detail = service.update_project(
        "product-command-admission",
        expected_spec_version=1,
        goal="Створи доступний застосунок для малого бізнесу",
    )

    stored = repository.get("product-command-admission")
    assert detail.summary.version == 2
    assert detail.summary.goal == "Створи доступний застосунок для малого бізнесу"
    assert stored.spec_version == 2
    assert stored.row_version == 1
    assert stored.spec.goal == detail.summary.goal
    assert len(repository.spec_history("product-command-admission")) == 2
