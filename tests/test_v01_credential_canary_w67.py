from __future__ import annotations

import asyncio

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.intelligence.contracts import DeterministicAction
from nika_core.intelligence.runtime_effect_journal import RuntimeIdempotencyEffectJournal
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.kernel.task_queue import TaskQueue
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_gateway.providers import DeterministicMockProvider
from nika_core.packaging.release import build_release_manifest, verify_release_manifest
from nika_core.research import ResearchRepository, ResearchWorkspace, SourceKind, SourceSpec
from nika_core.research.models import RefreshDisposition
from nika_core.research.monitoring import build_workspace_health_report
from nika_core.research.network_repository import NetworkResearchRepository
from nika_core.research.pagination_jobs import PaginatedResearchRefreshService, _FrontierItem
from nika_core.runtime.idempotency import IdempotencyLedger

TASK_CANARY = "W67_TASK_9dd4013_SYNTHETIC_SECRET_6e8a"
MODEL_CANARY = "W67_MODEL_9dd4013_SYNTHETIC_SECRET_74c1"
BATCH_CANARY = "W67_BATCH_9dd4013_SYNTHETIC_SECRET_8d2f"
EFFECT_CANARY = "W67_EFFECT_9dd4013_SYNTHETIC_SECRET_a913"
MONITOR_CANARY = "W67_MONITOR_9dd4013_SYNTHETIC_SECRET_b47e"
URL_CANARY = "W67_URL_9dd4013_SYNTHETIC_SECRET_c620"
RELEASE_CANARY = "W67_RELEASE_9dd4013_SYNTHETIC_SECRET_d5fa"


def _store(tmp_path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return store


def _raw_db(store: SQLiteStore) -> str:
    with store.connection() as conn:
        rows: list[str] = []
        for table, columns in (
            ("tasks", "payload_json"),
            ("checkpoints", "payload_json"),
            ("audit_events", "payload_json"),
            ("idempotency_records", "input_fingerprint, result_json"),
            ("research_http_sources", "url, final_url, last_error_message"),
            ("research_http_attempts", "requested_url, final_url, error_message"),
        ):
            for row in conn.execute(f"SELECT {columns} FROM {table}").fetchall():
                rows.extend("" if value is None else str(value) for value in row)
    return "\n".join(rows)


def test_task_durable_state_does_not_persist_raw_credential_canary(tmp_path) -> None:
    store = _store(tmp_path)
    TaskQueue(store).create(
        workspace_id="w67",
        agent_id="credential-canary",
        payload={"api_key": TASK_CANARY},
    )

    assert TASK_CANARY not in _raw_db(store)


def test_model_gateway_error_and_audit_do_not_expose_raw_canary(tmp_path, caplog) -> None:
    class CanaryProvider(DeterministicMockProvider):
        async def complete(self, model_request):
            del model_request
            raise ModelGatewayError(
                ModelErrorCode.UNAVAILABLE,
                f"synthetic provider failure {MODEL_CANARY}",
                provider_id=self.capabilities.provider_id,
                retryable=False,
            )

    store = _store(tmp_path)
    gateway = ModelGateway(audit_log=AuditLog(store))
    gateway.register(CanaryProvider(provider_id="w67-provider"), default=True)
    request = ModelRequest(
        request_id="w67-model",
        messages=(ModelMessage(role="user", content="controlled input"),),
        provider_kind=ProviderKind.NO_LLM,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(gateway.complete(request))

    assert MODEL_CANARY not in _raw_db(store), "ModelGateway AuditLog leaked provider error text"
    assert MODEL_CANARY not in caplog.text, "ordinary Python logs leaked provider error text"
    assert MODEL_CANARY not in str(exc_info.value), "public exception string leaked provider error text"
    assert MODEL_CANARY not in repr(exc_info.value), "public exception repr leaked provider error text"


def test_batch_cursor_does_not_persist_raw_url_canary(tmp_path) -> None:
    store = _store(tmp_path)
    tasks = TaskQueue(store)
    task = tasks.create(workspace_id="w67", agent_id="research.http.paginated_refresh")
    service = PaginatedResearchRefreshService(
        tasks=tasks,
        checkpoints=CheckpointService(store),
        network_repository=object(),  # not used by the exact _save_progress producer path
        web=object(),  # not used by the exact _save_progress producer path
    )
    canary_url = f"https://fixture.invalid/page?api_key={BATCH_CANARY}"

    service._save_progress(
        task.task_id,
        frontier=[_FrontierItem("research.page.w67", canary_url)],
        next_index=0,
        changed=0,
        unchanged=0,
        failed=0,
    )

    assert BATCH_CANARY not in _raw_db(store)


def test_effect_ledger_fingerprints_canary_arguments_instead_of_persisting_them(tmp_path) -> None:
    store = _store(tmp_path)
    task = TaskQueue(store).create(workspace_id="w67", agent_id="deterministic")
    journal = RuntimeIdempotencyEffectJournal(IdempotencyLedger(store))
    action = DeterministicAction(
        action_id="w67-effect",
        adds=frozenset({"done"}),
        tool_id="controlled.effect",
        arguments={"credential": EFFECT_CANARY},
    )

    journal.reserve(task_id=task.task_id, action=action)

    assert EFFECT_CANARY not in _raw_db(store)


def _research_stack(tmp_path):
    store = _store(tmp_path)
    research = ResearchRepository(store)
    research.upsert_workspace(ResearchWorkspace("w67", "W67"))
    network = NetworkResearchRepository(store)
    return store, network


def test_research_url_query_canary_is_not_persisted(tmp_path) -> None:
    store, network = _research_stack(tmp_path)
    canary_url = f"https://fixture.invalid/source?token={URL_CANARY}"
    network.register_source(SourceSpec("w67-source", "w67", SourceKind.HTTP, canary_url))

    assert URL_CANARY not in _raw_db(store)


def test_monitor_error_canary_is_not_persisted_or_reported(tmp_path) -> None:
    store, network = _research_stack(tmp_path)
    safe_url = "https://fixture.invalid/source"
    network.register_source(SourceSpec("w67-source", "w67", SourceKind.HTTP, safe_url))
    network.record_attempt(
        source_id="w67-source",
        attempt_number=1,
        disposition=RefreshDisposition.FAILED,
        requested_url=safe_url,
        final_url=safe_url,
        status_code=None,
        error_code="synthetic_failure",
        error_message=f"SyntheticProviderError: {MONITOR_CANARY}",
        retryable=False,
    )
    network.finalize_source(
        "w67-source",
        disposition=RefreshDisposition.FAILED,
        final_url=safe_url,
        status_code=None,
        error_code="synthetic_failure",
        error_message=f"SyntheticProviderError: {MONITOR_CANARY}",
    )
    report = build_workspace_health_report(network, "w67")

    assert MONITOR_CANARY not in _raw_db(store)
    assert all(MONITOR_CANARY not in (item.last_error_message or "") for item in report.sources)


def test_release_package_verification_rejects_raw_credential_canary(tmp_path) -> None:
    bundle = tmp_path / "NikaCore"
    bundle.mkdir()
    (bundle / "NikaCore.exe").write_bytes(b"controlled-binary")
    (bundle / "provider.env.example").write_text(
        f"api_key={RELEASE_CANARY}\n",
        encoding="utf-8",
    )
    manifest = build_release_manifest(
        bundle,
        product="NikaCore",
        version="0.1.0",
        source_sha="9dd4013625979492a125080f32e307fd5d808d48",
    )

    findings = verify_release_manifest(bundle, manifest)
    assert findings, "release verification admitted a package containing a raw credential canary"
