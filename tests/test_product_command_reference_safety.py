from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_command.contracts import EvidenceReference
from nika_core.product_command.deployment_adapter import deployment_status_entries
from nika_core.product_command.factory_status_adapter import deployment_execution_status_entries
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_factory_deployment import (
    DeploymentFabricSnapshot,
    DeploymentIntent,
    DeploymentRecord,
    DeploymentState,
    EnvironmentIdentity,
    EnvironmentTier,
    ExecutionRequest,
    HealthEvidence,
    Platform,
    ReleaseRef,
    ResourceEnvelope,
)
from nika_core.product_factory_deployment_execution import (
    DeploymentExecutionRecord,
    DeploymentExecutionSnapshot,
    DeploymentExecutionSpec,
    OperationState,
)
from nika_core.product_project import ProductBlocker, ProductProjectRepository, ProductProjectSpec

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
SHA = "a" * 40
DIGEST = "1" * 64


def test_public_evidence_contract_hashes_sensitive_and_oversized_references() -> None:
    sensitive = (
        "credential://provider/project-1/writer",
        "credential-use:event-sensitive-name",
        "approval://project-1/change-42",
        "secret://provider/raw",
        "authorization:Bearer raw-value",
        "provider-session:raw-session",
        "https://example.invalid/callback?access_token=raw-token",
    )

    for reference in sensitive:
        presented = EvidenceReference(kind="test", reference=reference, label="Evidence")
        assert presented.reference.startswith("evidence-sha256:")
        assert reference not in presented.reference

    oversized = "evidence://" + "x" * 600
    presented = EvidenceReference(kind="test", reference=oversized, label="Evidence")
    assert presented.reference.startswith("evidence-sha256:")
    assert len(presented.reference) < 512

    safe = EvidenceReference(
        kind="test",
        reference="health://project-1/service-api/healthy",
        label="Evidence",
    )
    assert safe.reference == "health://project-1/service-api/healthy"


@pytest.mark.parametrize(
    "reference",
    (
        "https://service.invalid/callback?api_key=raw-api-key",
        "https://service.invalid/callback?api%5Fkey=raw-encoded-key",
        "https://service.invalid/callback?api%255Fkey=raw-twice-encoded-key",
        "https://service.invalid/callback?api%2525255Fkey=raw-deeply-encoded-key",
        "https://service.invalid/callback?client_secret=raw-client-secret",
        "https://service.invalid/callback?password=raw-password",
        "https://service.invalid/callback?session_token=raw-session-token",
        "https://service.invalid/callback?secret=raw-secret",
        "https://operator:raw-userinfo-password@service.invalid/evidence",
        "https://operator%40team:raw-password@service.invalid/evidence",
        "https%3A%2F%2Foperator:raw-encoded-password%40service.invalid/evidence",
        "authorization=Basic raw-authorization",
        "X-API-Key: raw-header-key",
        "https://[invalid-host/evidence",
    ),
)
def test_public_evidence_hides_common_url_and_header_credentials(reference: str) -> None:
    presented = EvidenceReference(kind="test", reference=reference, label="Evidence")
    assert presented.reference.startswith("evidence-sha256:")
    assert reference not in presented.reference


@pytest.mark.parametrize(
    "reference",
    (
        "https://service.invalid/report?status=healthy",
        "https://service.invalid/report?name=important%20report",
        "health://project-1/service-api/healthy",
        "evidence://project-1/build/123",
    ),
)
def test_public_evidence_retains_nonsensitive_references(reference: str) -> None:
    assert (
        EvidenceReference(kind="test", reference=reference, label="Evidence").reference
        == reference
    )


@pytest.mark.parametrize(
    "key",
    (
        "api_key",
        "api-key",
        "apikey",
        "client_secret",
        "client-secret",
        "password",
        "passwd",
        "secret",
        "secret_key",
        "private_key",
        "access_key",
        "id_token",
        "session_token",
        "auth_token",
        "x-api-key",
        "authorization",
    ),
)
def test_public_evidence_redacts_all_common_credential_query_keys(key: str) -> None:
    reference = f"https://service.invalid/evidence?{key}=raw-credential"
    presented = EvidenceReference(kind="test", reference=reference, label="Evidence")
    assert presented.reference.startswith("evidence-sha256:")
    assert "raw-credential" not in presented.reference


def test_execution_projection_never_surfaces_raw_credential_use_event_id() -> None:
    intent = DeploymentIntent(
        "intent-1",
        "project-1",
        EnvironmentIdentity(
            "stage",
            "project-1",
            EnvironmentTier.STAGING,
            "provider://project-1/stage",
        ),
        ReleaseRef("project-1", "1.0.0", SHA, DIGEST),
    )
    spec = DeploymentExecutionSpec(
        "operation-1",
        ExecutionRequest(
            "project-1",
            "work-1",
            Platform.WINDOWS,
            frozenset({"deploy"}),
            frozenset({"python"}),
            ResourceEnvelope(1, 512, 512),
        ),
        intent,
        "credential://provider/project-1/writer",
        "staging-provider",
        "deploy",
    )
    snapshot = DeploymentExecutionSnapshot(
        (
            DeploymentExecutionRecord(
                spec,
                OperationState.BLOCKED_CREDENTIAL,
                evidence_refs=(
                    "credential-use:event-sensitive-name",
                    "execution://project-1/waiting",
                ),
                attempt=1,
                updated_at=NOW,
            ),
        )
    )

    serialized = "".join(
        item.model_dump_json()
        for item in deployment_execution_status_entries("project-1", snapshot)
    )

    assert "event-sensitive-name" not in serialized
    assert "credential-use:" not in serialized
    assert "evidence-sha256:" in serialized
    assert "execution://project-1/waiting" in serialized


def test_low_level_deployment_adapter_hashes_sensitive_provider_evidence() -> None:
    intent = DeploymentIntent(
        "deploy-1",
        "project-1",
        EnvironmentIdentity(
            "stage",
            "project-1",
            EnvironmentTier.STAGING,
            "provider://project-1/stage",
        ),
        ReleaseRef("project-1", "1.0.0", SHA, DIGEST),
    )
    record = DeploymentRecord(
        intent,
        DeploymentState.HEALTHY,
        (
            "credential://provider/project-1/raw-provider-evidence",
            "https://operator:raw-deployment-password@provider.invalid/evidence",
        ),
        health=HealthEvidence(
            "stage",
            SHA,
            True,
            ("health://project-1/stage",),
            NOW,
        ),
    )
    snapshot = DeploymentFabricSnapshot(
        (record,),
        (("project-1", SHA),),
        (("project-1", "stage", SHA),),
    )

    serialized = "".join(item.model_dump_json() for item in deployment_status_entries(snapshot))

    assert "credential://" not in serialized
    assert "raw-provider-evidence" not in serialized
    assert "raw-deployment-password" not in serialized
    assert "evidence-sha256:" in serialized
    assert "health://project-1/stage" in serialized


def test_structured_product_blocker_evidence_cannot_bypass_credential_redaction(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    service = ProductProjectCommandService(ProductProjectRepository(store))
    detail = service.create_project(
        project_id="project-1",
        name="Evidence boundary",
        spec=ProductProjectSpec(
            goal="Build accessible product",
            desired_outcome="Safe presentation",
            blockers=(
                ProductBlocker(
                    "blocker-1",
                    "Credential evidence must remain opaque",
                    evidence_refs=("credential://provider/project-1/raw-blocker-evidence",),
                ),
            ),
        ),
        idempotency_key="create:project-1",
    )
    serialized = detail.model_dump_json()

    assert "credential://" not in serialized
    assert "raw-blocker-evidence" not in serialized
    assert "evidence-sha256:" in serialized
