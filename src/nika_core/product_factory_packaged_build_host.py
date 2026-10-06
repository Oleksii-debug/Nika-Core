from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_build_execution import (
    BuildExecutionCoordinator,
    ExecutionNodeAvailabilityPort,
    TrustedExecutionAuthorityPort,
)
from nika_core.product_factory_build_execution_host import (
    DurableBuildExecutionHost,
    TrustedBuildOutputPolicyPort,
)
from nika_core.product_factory_build_execution_persistence import (
    SQLiteBuildExecutionCheckpointStore,
)
from nika_core.product_factory_deployment import (
    ExecutionNode,
    ExecutionNodeRegistry,
    NodeCapabilities,
    NodeIdentity,
    Platform,
    ResourceEnvelope,
)
from nika_core.product_factory_local_build_execution import (
    build_packaged_local_build_execution_node,
)
from nika_core.product_factory_packaged_local_startup import (
    PackagedLocalProductFactoryStartup,
)
from nika_core.toolsmith.workspace_security import (
    WorkspaceSecurityError,
    ensure_real_directory_root,
)


class PackagedLocalBuildHostError(ValueError):
    """Raised when packaged PF5 durable-host composition is not trustworthy."""


@dataclass(frozen=True, slots=True)
class PackagedLocalBuildNodeAvailability(ExecutionNodeAvailabilityPort):
    """Read-only readiness for the exact configured contained-local PF5 node."""

    node_id: str
    startup: PackagedLocalProductFactoryStartup

    def __post_init__(self) -> None:
        if (
            type(self.node_id) is not str
            or not self.node_id
            or self.node_id != self.node_id.strip()
        ):
            raise PackagedLocalBuildHostError(
                "local PF5 availability node id must be canonical non-empty text"
            )
        if type(self.startup) is not PackagedLocalProductFactoryStartup:
            raise PackagedLocalBuildHostError(
                "local PF5 availability requires exact packaged startup authority"
            )
        self.startup.__post_init__()

    def is_available(self, node_id: str) -> bool:
        if type(node_id) is not str or node_id != self.node_id:
            return False
        try:
            ensure_real_directory_root(
                self.startup.workspace_parent,
                label="PF5 local build workspace parent",
            )
        except (OSError, TypeError, ValueError, WorkspaceSecurityError):
            return False
        return _is_real_regular_file(self.startup.git_executable)


def build_packaged_local_durable_build_host(
    store: SQLiteStore,
    *,
    host_task_id: str,
    project_id: str,
    node: ExecutionNode,
    startup: PackagedLocalProductFactoryStartup,
    trusted_authority: TrustedExecutionAuthorityPort,
    output_policies: TrustedBuildOutputPolicyPort,
) -> DurableBuildExecutionHost:
    """Compose one canonical durable PF5 host around the local node adapter.

    This function owns no execution policy. The caller supplies the exact trusted
    ExecutionNode, PF5 execution authority and output policy. The incumbent PF5
    coordinator/checkpoint host remain the only state/effect authorities. Existing
    checkpoints are restored before the returned host can be used, so packaged restart
    cannot silently begin from an empty build state.
    """

    if type(store) is not SQLiteStore:
        raise PackagedLocalBuildHostError("PF5 composition store must be exact SQLiteStore")
    _exact_text(host_task_id, "host_task_id")
    _exact_text(project_id, "project_id")
    local_node = _snapshot_local_node(node)
    if type(startup) is not PackagedLocalProductFactoryStartup:
        raise PackagedLocalBuildHostError(
            "PF5 composition requires exact packaged local startup authority"
        )
    startup.__post_init__()
    if not callable(getattr(trusted_authority, "resolve", None)):
        raise PackagedLocalBuildHostError(
            "PF5 composition requires TrustedExecutionAuthorityPort"
        )
    if not callable(getattr(output_policies, "resolve", None)):
        raise PackagedLocalBuildHostError(
            "PF5 composition requires TrustedBuildOutputPolicyPort"
        )

    registry = ExecutionNodeRegistry()
    registry.register(local_node)
    availability = PackagedLocalBuildNodeAvailability(
        local_node.identity.node_id,
        startup,
    )
    coordinator = BuildExecutionCoordinator(
        registry,
        availability,
        trusted_authority,
    )
    checkpoints = SQLiteBuildExecutionCheckpointStore(
        store,
        host_task_id,
        project_id,
    )
    node_port = build_packaged_local_build_execution_node(
        store,
        node_id=local_node.identity.node_id,
        startup=startup,
        trusted_authority=trusted_authority,
        output_policies=output_policies,
    )
    host = DurableBuildExecutionHost(
        coordinator,
        node_port,
        node_port,
        output_policies,
        checkpoints,
    )
    if checkpoints.has_checkpoint():
        host.restore_latest()
    return host


def _snapshot_local_node(value: object) -> ExecutionNode:
    if type(value) is not ExecutionNode:
        raise PackagedLocalBuildHostError(
            "PF5 composition node must be exact ExecutionNode"
        )
    identity = value.identity
    capabilities = value.capabilities
    resources = value.resources
    if type(identity) is not NodeIdentity or type(identity.platform) is not Platform:
        raise PackagedLocalBuildHostError("PF5 local node identity carrier is invalid")
    for label, item in (
        ("node_id", identity.node_id),
        ("architecture", identity.architecture),
        ("instance_id", identity.instance_id),
    ):
        _exact_text(item, label)
    if identity.platform is not _local_platform():
        raise PackagedLocalBuildHostError(
            "PF5 local node platform does not match the packaged host platform"
        )
    if type(capabilities) is not NodeCapabilities:
        raise PackagedLocalBuildHostError("PF5 local node capabilities carrier is invalid")
    _exact_text_set(capabilities.features, "node features")
    _exact_text_set(capabilities.toolchains, "node toolchains")
    if "build" not in capabilities.features:
        raise PackagedLocalBuildHostError(
            "PF5 local node must explicitly advertise build capability"
        )
    if type(capabilities.gpu) is not bool:
        raise PackagedLocalBuildHostError("PF5 local node gpu flag must be exact bool")
    if type(resources) is not ResourceEnvelope:
        raise PackagedLocalBuildHostError("PF5 local node resources carrier is invalid")
    for label, amount in (
        ("cpu_cores", resources.cpu_cores),
        ("memory_mb", resources.memory_mb),
        ("disk_mb", resources.disk_mb),
    ):
        if type(amount) is not int or amount <= 0:
            raise PackagedLocalBuildHostError(
                f"PF5 local node {label} must be an exact positive integer"
            )
    if type(value.enabled) is not bool or not value.enabled:
        raise PackagedLocalBuildHostError(
            "packaged PF5 local node must be explicitly enabled"
        )
    return ExecutionNode(
        NodeIdentity(
            identity.node_id,
            identity.platform,
            identity.architecture,
            identity.instance_id,
        ),
        NodeCapabilities(
            frozenset(capabilities.features),
            frozenset(capabilities.toolchains),
            capabilities.gpu,
        ),
        ResourceEnvelope(
            resources.cpu_cores,
            resources.memory_mb,
            resources.disk_mb,
        ),
        enabled=True,
    )


def _local_platform() -> Platform:
    if os.name == "nt":
        return Platform.WINDOWS
    if os.name == "posix":
        return Platform.LINUX
    raise PackagedLocalBuildHostError(
        "packaged PF5 local build host does not support this OS"
    )


def _is_real_regular_file(path: Path) -> bool:
    try:
        details = path.lstat()
    except (OSError, ValueError):
        return False
    if not stat.S_ISREG(details.st_mode):
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(details, "st_file_attributes", 0)
    return not reparse_flag or not bool(file_attributes & reparse_flag)


def _exact_text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise PackagedLocalBuildHostError(
            f"PF5 local {label} must be canonical non-empty text"
        )
    return value


def _exact_text_set(value: object, label: str) -> None:
    if type(value) is not frozenset:
        raise PackagedLocalBuildHostError(f"PF5 local {label} must be exact frozenset")
    if any(
        type(item) is not str or not item or item != item.strip()
        for item in value
    ):
        raise PackagedLocalBuildHostError(
            f"PF5 local {label} must contain canonical non-empty text"
        )
