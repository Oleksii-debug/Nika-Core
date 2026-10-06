from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_command.command_center import (
    ProductCommandCenter,
    ProductCommandCenterScopeError,
)
from nika_core.product_command.deployment_adapter import DeploymentPresentationIntegrityError
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_factory_deployment import (
    DeploymentFabricSnapshot,
    DeploymentIntent,
    DeploymentRecord,
    DeploymentState,
    EnvironmentIdentity,
    EnvironmentTier,
    HealthEvidence,
    ReleaseRef,
    RollbackEvidence,
)
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec

NOW = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
DIGEST_A = "1" * 64


def _center(tmp_path) -> ProductCommandCenter:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    service = ProductProjectCommandService(ProductProjectRepository(store))
    service.create_project(
        project_id="p1",
        name="Deployment integrity project",
        spec=ProductProjectSpec(
            goal="Build accessible product",
            desired_outcome="Integrity-checked deployment presentation",
        ),
        idempotency_key="create:p1",
    )
    return ProductCommandCenter(service)


def _intent() -> DeploymentIntent:
    return DeploymentIntent(
        "deploy-p1",
        "p1",
        EnvironmentIdentity(
            "staging",
            "p1",
            EnvironmentTier.STAGING,
            "provider://p1/staging",
        ),
        ReleaseRef("p1", "1.0.0", SHA_A, DIGEST_A),
    )


def test_health_evidence_must_match_environment_and_exact_release_sha(tmp_path) -> None:
    center = _center(tmp_path)
    record = DeploymentRecord(
        _intent(),
        DeploymentState.REJECTED,
        ("deploy://p1/rejected",),
        health=HealthEvidence("other", SHA_A, False, ("health://bad",), NOW),
    )

    with pytest.raises(ProductCommandCenterScopeError, match="health evidence does not match"):
        center.inspect_project(
            "p1",
            deployment=DeploymentFabricSnapshot((record,), (), ()),
        )


def test_healthy_state_requires_positive_health_evidence(tmp_path) -> None:
    center = _center(tmp_path)
    record = DeploymentRecord(
        _intent(),
        DeploymentState.HEALTHY,
        ("deploy://p1/healthy",),
    )

    with pytest.raises(ProductCommandCenterScopeError, match="lacks matching healthy evidence"):
        center.inspect_project(
            "p1",
            deployment=DeploymentFabricSnapshot((record,), (), ()),
        )


def test_successful_rollback_must_restore_recorded_previous_release(tmp_path) -> None:
    center = _center(tmp_path)
    intent = _intent()
    record = DeploymentRecord(
        intent,
        DeploymentState.ROLLED_BACK,
        ("deploy://p1/rollback",),
        health=HealthEvidence("staging", SHA_A, False, ("health://failed",), NOW),
        rollback=RollbackEvidence(
            "staging",
            SHA_A,
            SHA_C,
            True,
            ("rollback://p1/forged-restore",),
        ),
        previous_release_sha=SHA_B,
    )

    with pytest.raises(
        DeploymentPresentationIntegrityError,
        match="restored a release other than recorded previous release",
    ):
        center.inspect_project(
            "p1",
            deployment=DeploymentFabricSnapshot((record,), (), ()),
        )
