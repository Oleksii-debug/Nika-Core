from __future__ import annotations

import hashlib
import json

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import (
    ProductFactoryCheckpointError,
    ProductFactoryCheckpointHost,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_project_binding import ProductProjectCoordinatorBinding
from nika_core.product_project import (
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
)

PROJECT_ID = "qa-durable-plan-forgery"
LOCATOR = "owner/product"
SHA_A = "a" * 40
SHA_C = "c" * 40
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id=PROJECT_ID,
        repositories=(RepositoryRef("repo-main", "github", LOCATOR, "main"),),
        components=(
            ProductComponent(
                "core",
                "repo-main",
                ("src/core",),
                test_commands=(("python", "-m", "pytest", "tests/core"),),
            ),
            ProductComponent(
                "ui",
                "repo-main",
                ("src/ui",),
                test_commands=(("python", "-m", "pytest", "tests/ui"),),
            ),
        ),
    )


def _setup(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    project = ProductProjectRepository(store).create(
        project_id=PROJECT_ID,
        name="Durable plan forgery oracle",
        spec=ProductProjectSpec(
            goal="Build the declared product",
            desired_outcome="Only the original plan may resume",
            requirements=(
                ProductRequirement(
                    "req-plan",
                    "Restore preserves immutable plan authority",
                    ("Forged plan identity is rejected",),
                ),
            ),
            repository_refs=(LOCATOR,),
        ),
        idempotency_key="create:qa-durable-plan-forgery",
    )
    binding = ProductProjectCoordinatorBinding(project, _graph())
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": PROJECT_ID},
    )
    coordinator = binding.plan(
        base_shas={"repo-main": SHA_A},
        component_goals={"core": "Implement core", "ui": "Implement ui"},
        permission_ceiling=PERMISSIONS,
    )
    return store, binding, task.task_id, coordinator


def _work_id(request: dict[str, object]) -> str:
    parts = (
        request["project_id"],
        request["component_id"],
        request["repository_id"],
        request["goal"],
        request["base_sha"],
        tuple(request["allowed_paths"]),
        tuple(sorted(request["permission_ceiling"])),
        tuple(tuple(command) for command in request["acceptance_commands"]),
        request["attempt"],
    )
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return f"work-{hashlib.sha256(payload.encode()).hexdigest()[:24]}"


def _attack_request(request: dict[str, object], attack: str) -> None:
    if attack == "permission-expansion":
        request["permission_ceiling"] = sorted(
            {*request["permission_ceiling"], "admin_project"}
        )
    elif attack == "initial-base-rebind":
        request["base_sha"] = SHA_C
    elif attack == "goal-rewrite" and request["component_id"] == "core":
        request["goal"] = "Ship an unrelated hidden objective"
    elif attack == "sibling-base-split" and request["component_id"] == "core":
        request["base_sha"] = SHA_C
    request["work_id"] = _work_id(request)


def _forge_persisted_checkpoint(store, *, checkpoint_id: str, host_task_id: str, attack: str) -> None:
    with store.connection() as conn:
        checkpoint_row = conn.execute(
            "SELECT payload_json FROM checkpoints WHERE checkpoint_id = ?",
            (checkpoint_id,),
        ).fetchone()
        host_task_row = conn.execute(
            "SELECT payload_json FROM tasks WHERE task_id = ?",
            (host_task_id,),
        ).fetchone()
        assert checkpoint_row is not None
        assert host_task_row is not None
        host_payload_before = host_task_row["payload_json"]

        payload = json.loads(checkpoint_row["payload_json"])
        for record in payload["coordinator"]["records"]:
            _attack_request(record["request"], attack)

        payload_json = _canonical(payload)
        checksum = _sha256(payload_json)
        identity = _canonical(
            {
                "host_task_id": host_task_id,
                "project_id": payload["project_id"],
                "spec_version": payload["spec_version"],
                "row_version": payload["row_version"],
                "revision": payload["coordinator"]["revision"],
                "checksum": checksum,
            }
        )
        forged_checkpoint_id = f"pf2-{_sha256(identity)[:32]}"
        conn.execute(
            "UPDATE checkpoints SET checkpoint_id = ?, payload_json = ?, checksum_sha256 = ? "
            "WHERE checkpoint_id = ?",
            (forged_checkpoint_id, payload_json, checksum, checkpoint_id),
        )

        host_payload_after = conn.execute(
            "SELECT payload_json FROM tasks WHERE task_id = ?",
            (host_task_id,),
        ).fetchone()["payload_json"]
        assert host_payload_after == host_payload_before


@pytest.mark.parametrize(
    "attack",
    (
        "permission-expansion",
        "initial-base-rebind",
        "goal-rewrite",
        "sibling-base-split",
    ),
)
def test_durable_restart_rejects_forged_plan_after_attacker_rehashes_every_candidate_identity(
    tmp_path,
    attack: str,
) -> None:
    store, binding, task_id, coordinator = _setup(tmp_path)
    saved = ProductFactoryCheckpointHost(store).save(
        host_task_id=task_id,
        checkpoint=binding.checkpoint(coordinator),
    )
    _forge_persisted_checkpoint(
        store,
        checkpoint_id=saved.checkpoint_id,
        host_task_id=task_id,
        attack=attack,
    )

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_project = ProductProjectRepository(restarted_store).get(PROJECT_ID)
    restarted_binding = ProductProjectCoordinatorBinding(restarted_project, _graph())

    with pytest.raises(ProductFactoryCheckpointError):
        ProductFactoryCheckpointHost(restarted_store).restore_latest(
            host_task_id=task_id,
            binding=restarted_binding,
        )
