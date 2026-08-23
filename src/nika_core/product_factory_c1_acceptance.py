from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_checkpoint_host import ProductFactoryCheckpointHost
from nika_core.product_factory_coordinator import (
    ComponentWorkRequest,
    ProductFactoryCoordinator,
    ReviewDecision,
    WorkerResultEnvelope,
    WorkState,
)
from nika_core.product_factory_orchestration import (
    ComponentBrief,
    DynamicTeamComposer,
    OwnershipLease,
    ProductComponent,
    ProductRepositoryGraph,
    ProjectScale,
    RepositoryRef,
    TeamCompositionRequest,
)
from nika_core.product_factory_program_host import ProductFactoryProgramHost
from nika_core.product_factory_project_binding import ProductProjectCoordinatorBinding
from nika_core.product_project import (
    ProductAcceptanceCriterion,
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
    ProductRequirementKind,
)
from nika_core.toolsmith.contracts import (
    ChangedFile,
    CodingResult,
    RecoveryState,
    TestEvidence,
    WorkerFailure,
    WorkerFailureKind,
)

_C1_PROJECT_ID = "product-c1-medium-expense-manager"
_C1_REPOSITORY_ID = "repo-c1-medium-expense-manager"
_C1_REPOSITORY_LOCATOR = "sandbox://c1-medium-expense-manager"
_C1_HOST_TASK_ID = "pf11-c1-medium-app-host"
_BASE_SHA = "0" * 40


class C1MediumAppAcceptanceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class C1MediumAppEvidence:
    source_sha: str
    project_id: str
    spec_version: int
    spec_history_versions: tuple[int, ...]
    team_plan_id: str
    independent_qa_role_ids: tuple[str, ...]
    ownership_lease_ids: tuple[str, ...]
    component_attempts: tuple[tuple[str, int], ...]
    rejected_qa_component: str
    worker_repair_component: str
    restart_recovery_proven: bool
    all_components_accepted: bool
    generated_test_digest: str
    package_path: str | None
    package_sha256: str | None
    installer_sha256: str
    installed_executable_proven: bool
    upgrade_safe_data_proven: bool
    accessible_control_contract_proven: bool
    human_tested: bool = False
    nvda_verified: bool = False
    production_release_ready: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "nika-pf11-c1-medium-app-evidence-v1",
            "source_sha": self.source_sha,
            "project_id": self.project_id,
            "spec_version": self.spec_version,
            "spec_history_versions": list(self.spec_history_versions),
            "team_plan_id": self.team_plan_id,
            "independent_qa_role_ids": list(self.independent_qa_role_ids),
            "ownership_lease_ids": list(self.ownership_lease_ids),
            "component_attempts": [
                {"component_id": component_id, "attempt": attempt}
                for component_id, attempt in self.component_attempts
            ],
            "rejected_qa_component": self.rejected_qa_component,
            "worker_repair_component": self.worker_repair_component,
            "restart_recovery_proven": self.restart_recovery_proven,
            "all_components_accepted": self.all_components_accepted,
            "generated_test_digest": self.generated_test_digest,
            "package_path": self.package_path,
            "package_sha256": self.package_sha256,
            "installer_sha256": self.installer_sha256,
            "installed_executable_proven": self.installed_executable_proven,
            "upgrade_safe_data_proven": self.upgrade_safe_data_proven,
            "accessible_control_contract_proven": self.accessible_control_contract_proven,
            "human_tested": self.human_tested,
            "nvda_verified": self.nvda_verified,
            "production_release_ready": self.production_release_ready,
        }


@dataclass(slots=True)
class SandboxMediumAppWorker:
    """Deterministic local PF worker used only for C1 acceptance.

    It materializes component-scoped files inside one sandbox workspace, executes only the
    coordinator-declared Python acceptance commands, and never touches a production repo.
    """

    workspace: Path
    fail_storage_attempt_one: bool = True

    async def dispatch(self, request: ComponentWorkRequest) -> WorkerResultEnvelope:
        return self._execute(request)

    async def inspect(self, work_id: str) -> RecoveryState | None:
        return RecoveryState(phase="checkpointed", opaque_token=work_id)

    async def recover(
        self,
        request: ComponentWorkRequest,
        state: RecoveryState,
    ) -> WorkerResultEnvelope:
        if state.opaque_token not in {None, request.work_id}:
            raise C1MediumAppAcceptanceError("worker recovery token does not match work id")
        return self._execute(request)

    def _execute(self, request: ComponentWorkRequest) -> WorkerResultEnvelope:
        if (
            self.fail_storage_attempt_one
            and request.component_id == "01-storage"
            and request.attempt == 1
        ):
            failure = WorkerFailure(
                WorkerFailureKind.PROCESS_FAILED,
                "controlled C1 storage worker failure",
                retryable=True,
            )
            digest = hashlib.sha256(f"{request.work_id}:controlled-failure".encode()).hexdigest()
            return WorkerResultEnvelope(
                work_id=request.work_id,
                component_id=request.component_id,
                repository_id=request.repository_id,
                base_sha=request.base_sha,
                result_sha=hashlib.sha1(digest.encode()).hexdigest(),
                diff_digest=digest,
                coding_result=CodingResult(job_id=request.work_id, failure=failure),
            )

        files = _component_files(request.component_id, request.attempt)
        changed_files: list[ChangedFile] = []
        for relative, text in files.items():
            if not _path_allowed(relative, request.allowed_paths):
                raise C1MediumAppAcceptanceError(
                    f"worker attempted path outside component lease: {relative}"
                )
            target = self.workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="\n")
            encoded = text.encode("utf-8")
            changed_files.append(
                ChangedFile(
                    path=relative,
                    sha256=hashlib.sha256(encoded).hexdigest(),
                    size_bytes=len(encoded),
                )
            )

        test_evidence: list[TestEvidence] = []
        failed: WorkerFailure | None = None
        for declared in request.acceptance_commands:
            argv = list(declared)
            if argv and argv[0].casefold() in {"python", "python.exe", "python3"}:
                argv[0] = sys.executable
            env = dict(os.environ)
            completed = subprocess.run(
                argv,
                cwd=self.workspace,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            output = (completed.stdout or "") + (completed.stderr or "")
            output_digest = hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest()
            test_evidence.append(
                TestEvidence(
                    command=declared,
                    exit_code=completed.returncode,
                    output_digest=output_digest,
                )
            )
            if completed.returncode != 0:
                failed = WorkerFailure(
                    WorkerFailureKind.PROCESS_FAILED,
                    f"component acceptance command failed with exit {completed.returncode}",
                    retryable=True,
                )
                break

        material = json.dumps(
            {
                "base_sha": request.base_sha,
                "component_id": request.component_id,
                "attempt": request.attempt,
                "files": [(item.path, item.sha256) for item in changed_files],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        diff_digest = hashlib.sha256(material).hexdigest()
        result_sha = hashlib.sha1(material).hexdigest()
        return WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=request.component_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha=result_sha,
            diff_digest=diff_digest,
            coding_result=CodingResult(
                job_id=request.work_id,
                changed_files=tuple(changed_files),
                test_evidence=tuple(test_evidence),
                failure=failed,
            ),
        )


class C1MediumAppAcceptanceRunner:
    def __init__(self, *, root: Path, source_sha: str) -> None:
        if len(source_sha) != 40 or any(char not in "0123456789abcdef" for char in source_sha):
            raise ValueError("C1 acceptance requires exact lowercase 40-character source SHA")
        self.root = Path(root)
        self.workspace = self.root / "product"
        self.database = self.root / "nika-factory.sqlite"
        self.source_sha = source_sha

    def run(self, *, build_windows_package: bool = False) -> C1MediumAppEvidence:
        self.root.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)
        store = SQLiteStore(self.database)
        store.initialize()
        projects = ProductProjectRepository(store)

        project = projects.create(
            project_id=_C1_PROJECT_ID,
            name="C1 Accessible Expense Manager",
            spec=_initial_spec(),
            idempotency_key="pf11-c1-project-create-v1",
        )
        project = projects.update_spec(
            _C1_PROJECT_ID,
            _revised_spec(),
            expected_row_version=project.row_version,
            change_reason="Require upgrade-safe user data and keyboard-operable Windows UI",
        )
        if project.spec_version != 2:
            raise C1MediumAppAcceptanceError("controlled ProductProject spec revision was not durable")

        graph = _repository_graph()
        team = DynamicTeamComposer().compose(_team_request())
        qa_roles = tuple(role.role_id for role in team.roles if role.independent_review)
        if not qa_roles:
            raise C1MediumAppAcceptanceError("medium team did not include independent QA")
        lease_ids = _prove_ownership(graph, team)

        binding = ProductProjectCoordinatorBinding(project=project, graph=graph)
        coordinator = binding.plan(
            base_shas={_C1_REPOSITORY_ID: _BASE_SHA},
            component_goals=_component_goals(),
            permission_ceiling=frozenset({"read_source", "write_source", "run_tests", "build_release"}),
        )
        _ensure_host_task(store, project_id=project.project_id)
        worker = SandboxMediumAppWorker(self.workspace)
        host = ProductFactoryProgramHost(store=store, worker=worker)

        rejected_qa_component = "04-desktop-ui"
        worker_repair_component = "01-storage"
        restart_recovery_proven = False
        restarted = False

        while any(record.state is not WorkState.ACCEPTED for record in coordinator.snapshot().records):
            ready = coordinator.ready_requests()
            if ready:
                asyncio.run(
                    host.dispatch_ready(
                        host_task_id=_C1_HOST_TASK_ID,
                        binding=binding,
                        coordinator=coordinator,
                        max_parallel=1,
                        max_count=1,
                    )
                )

            progressed = False
            for record in coordinator.snapshot().records:
                if record.state is WorkState.REPAIR_REQUIRED:
                    if record.result is None:
                        raise C1MediumAppAcceptanceError("repair state is missing worker result")
                    host.prepare_repair_and_checkpoint(
                        host_task_id=_C1_HOST_TASK_ID,
                        binding=binding,
                        coordinator=coordinator,
                        component_id=record.request.component_id,
                        base_sha=record.result.result_sha,
                        reason=record.blocker or "repair rejected candidate",
                    )
                    progressed = True
                    break
                if record.state is WorkState.REVIEW_REQUIRED:
                    accepted = True
                    reason = "independent deterministic QA accepted component evidence"
                    if record.request.component_id == rejected_qa_component and record.request.attempt == 1:
                        accepted = False
                        reason = "keyboard accelerator acceptance evidence missing from first UI candidate"
                    host.review_and_checkpoint(
                        host_task_id=_C1_HOST_TASK_ID,
                        binding=binding,
                        coordinator=coordinator,
                        component_id=record.request.component_id,
                        decision=ReviewDecision(
                            reviewer_id=qa_roles[0],
                            accepted=accepted,
                            reason=reason,
                            evidence_refs=(
                                f"c1-qa:{record.request.component_id}:attempt-{record.request.attempt}",
                            ),
                        ),
                    )
                    progressed = True
                    break

            storage = next(
                record
                for record in coordinator.snapshot().records
                if record.request.component_id == worker_repair_component
            )
            if storage.state is WorkState.ACCEPTED and not restarted:
                before_restart = coordinator.snapshot()
                reopened_store = SQLiteStore(self.database)
                reopened_store.initialize()
                reopened_project = ProductProjectRepository(reopened_store).get(_C1_PROJECT_ID)
                reopened_binding = ProductProjectCoordinatorBinding(
                    project=reopened_project,
                    graph=graph,
                )
                reopened_host = ProductFactoryProgramHost(
                    store=reopened_store,
                    worker=SandboxMediumAppWorker(
                        self.workspace,
                        fail_storage_attempt_one=False,
                    ),
                )
                restored = reopened_host.restore_latest(
                    host_task_id=_C1_HOST_TASK_ID,
                    binding=reopened_binding,
                )
                if restored.snapshot() != before_restart:
                    raise C1MediumAppAcceptanceError(
                        "restart recovery changed durable coordinator state"
                    )
                store = reopened_store
                projects = ProductProjectRepository(store)
                project = reopened_project
                binding = reopened_binding
                host = reopened_host
                coordinator = restored
                restart_recovery_proven = True
                restarted = True
                progressed = True

            if not ready and not progressed and any(
                record.state is not WorkState.ACCEPTED for record in coordinator.snapshot().records
            ):
                raise C1MediumAppAcceptanceError("C1 factory made no progress")

        attempts = tuple(
            (record.request.component_id, record.request.attempt)
            for record in coordinator.snapshot().records
        )
        if dict(attempts).get(worker_repair_component) != 2:
            raise C1MediumAppAcceptanceError("controlled worker repair was not proven")
        if dict(attempts).get(rejected_qa_component) != 2:
            raise C1MediumAppAcceptanceError("rejected QA candidate was not repaired")

        generated_test_digest = _run_generated_suite(self.workspace)
        upgrade_safe = _run_named_generated_test(self.workspace, "test_storage_upgrade.py")
        accessible_contract = _run_named_generated_test(self.workspace, "test_accessibility_contract.py")
        installer = self.workspace / "installer" / "install.ps1"
        installer_sha = _sha256_file(installer)

        package_path: str | None = None
        package_sha: str | None = None
        installed_executable_proven = False
        if build_windows_package:
            if os.name != "nt":
                raise C1MediumAppAcceptanceError("Windows C1 package proof requires Windows")
            package, installed_executable_proven = _build_and_install_package(
                self.workspace,
                source_sha=self.source_sha,
                project=project,
                team_plan_id=team.plan_id,
            )
            package_path = str(package)
            package_sha = _sha256_file(package)

        return C1MediumAppEvidence(
            source_sha=self.source_sha,
            project_id=project.project_id,
            spec_version=project.spec_version,
            spec_history_versions=tuple(item.spec_version for item in projects.spec_history(project.project_id)),
            team_plan_id=team.plan_id,
            independent_qa_role_ids=qa_roles,
            ownership_lease_ids=lease_ids,
            component_attempts=attempts,
            rejected_qa_component=rejected_qa_component,
            worker_repair_component=worker_repair_component,
            restart_recovery_proven=restart_recovery_proven,
            all_components_accepted=all(
                record.state is WorkState.ACCEPTED for record in coordinator.snapshot().records
            ),
            generated_test_digest=generated_test_digest,
            package_path=package_path,
            package_sha256=package_sha,
            installer_sha256=installer_sha,
            installed_executable_proven=installed_executable_proven,
            upgrade_safe_data_proven=upgrade_safe,
            accessible_control_contract_proven=accessible_contract,
        )


def _initial_spec() -> ProductProjectSpec:
    return ProductProjectSpec(
        goal="Create a medium Windows expense manager for a small business",
        desired_outcome="A durable desktop expense tracker with local SQLite data",
        repository_refs=(_C1_REPOSITORY_LOCATOR,),
        requirements=(
            ProductRequirement(
                requirement_id="expense-domain",
                text="Record and list business expenses",
                acceptance=("Amounts are stored exactly as integer cents",),
            ),
            ProductRequirement(
                requirement_id="local-storage",
                text="Persist expense data in SQLite",
                acceptance=("Data survives application restart",),
                kind=ProductRequirementKind.NON_FUNCTIONAL,
            ),
        ),
    )


def _revised_spec() -> ProductProjectSpec:
    criteria = (
        ProductAcceptanceCriterion(
            criterion_id="upgrade-preserves-data",
            text="A schema-v1 database upgrades to v2 without losing expenses",
        ),
        ProductAcceptanceCriterion(
            criterion_id="keyboard-controls",
            text="Primary desktop controls are named and keyboard operable",
        ),
        ProductAcceptanceCriterion(
            criterion_id="non-admin-install",
            text="Package installs to a caller-selected user path without elevation",
        ),
    )
    return ProductProjectSpec(
        goal="Create a medium Windows expense manager for a small business",
        desired_outcome=(
            "A packaged accessible desktop expense tracker with upgrade-safe local SQLite data"
        ),
        repository_refs=(_C1_REPOSITORY_LOCATOR,),
        requirements=(
            ProductRequirement(
                requirement_id="expense-domain",
                text="Record and list business expenses",
                acceptance=("Amounts are stored exactly as integer cents",),
            ),
            ProductRequirement(
                requirement_id="local-storage",
                text="Persist expense data in SQLite with deterministic migrations",
                acceptance=("Data survives restart and schema upgrade",),
                kind=ProductRequirementKind.NON_FUNCTIONAL,
                acceptance_criteria=(criteria[0],),
            ),
            ProductRequirement(
                requirement_id="desktop-accessibility",
                text="Provide a keyboard-operable Windows desktop UI with named standard controls",
                acceptance=("No coordinate-only interaction is required",),
                kind=ProductRequirementKind.ACCESSIBILITY,
                acceptance_criteria=(criteria[1],),
            ),
            ProductRequirement(
                requirement_id="settings",
                text="Persist user settings outside source code",
                acceptance=("Settings round-trip through JSON configuration",),
                kind=ProductRequirementKind.NON_FUNCTIONAL,
            ),
            ProductRequirement(
                requirement_id="package",
                text="Produce a Windows package and non-admin installer",
                acceptance=("Installed executable exists in the selected destination",),
                kind=ProductRequirementKind.RELEASE,
                acceptance_criteria=(criteria[2],),
            ),
        ),
    )


def _repository_graph() -> ProductRepositoryGraph:
    repository = RepositoryRef(
        repository_id=_C1_REPOSITORY_ID,
        provider="local-sandbox",
        locator=_C1_REPOSITORY_LOCATOR,
        default_branch="main",
        case_sensitive_paths=False,
    )
    components = (
        ProductComponent(
            component_id="01-storage",
            repository_id=_C1_REPOSITORY_ID,
            paths=("src/c1_expense_manager/data", "tests/test_storage_upgrade.py"),
            test_commands=(("python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_storage_upgrade.py"),),
        ),
        ProductComponent(
            component_id="02-settings",
            repository_id=_C1_REPOSITORY_ID,
            paths=("src/c1_expense_manager/config", "tests/test_settings.py"),
            test_commands=(("python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_settings.py"),),
        ),
        ProductComponent(
            component_id="03-domain",
            repository_id=_C1_REPOSITORY_ID,
            paths=("src/c1_expense_manager/domain", "tests/test_domain.py"),
            dependencies=("01-storage",),
            test_commands=(("python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_domain.py"),),
        ),
        ProductComponent(
            component_id="04-desktop-ui",
            repository_id=_C1_REPOSITORY_ID,
            paths=("src/c1_expense_manager/desktop", "tests/test_accessibility_contract.py"),
            dependencies=("02-settings", "03-domain"),
            test_commands=(("python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_accessibility_contract.py"),),
        ),
        ProductComponent(
            component_id="05-package",
            repository_id=_C1_REPOSITORY_ID,
            paths=("src/c1_expense_manager/main.py", "installer", "tests/test_package_contract.py"),
            dependencies=("04-desktop-ui",),
            test_commands=(("python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_package_contract.py"),),
        ),
    )
    return ProductRepositoryGraph(
        project_id=_C1_PROJECT_ID,
        repositories=(repository,),
        components=components,
    )


def _team_request() -> TeamCompositionRequest:
    return TeamCompositionRequest(
        project_id=_C1_PROJECT_ID,
        components=(
            ComponentBrief("01-storage", "data"),
            ComponentBrief("02-settings", "data"),
            ComponentBrief("03-domain", "backend"),
            ComponentBrief("04-desktop-ui", "desktop", frozenset({"accessibility"})),
            ComponentBrief("05-package", "infra", frozenset({"deployment"})),
        ),
        acceptance_criteria=(
            "upgrade-safe SQLite data",
            "accessible keyboard-operable Windows desktop UI",
            "package release and non-admin install",
        ),
        permission_ceiling=frozenset(
            {"read_source", "write_source", "run_tests", "read_project", "update_project", "build_release"}
        ),
        scale=ProjectScale.MEDIUM,
        evidence_refs=("pf11:c1:acceptance-spec-v2",),
    )


def _component_goals() -> dict[str, str]:
    return {
        "01-storage": "Implement SQLite schema v2 plus v1-to-v2 data-preserving migration",
        "02-settings": "Implement JSON settings with deterministic defaults and atomic save",
        "03-domain": "Implement expense domain service over the storage repository",
        "04-desktop-ui": "Implement named keyboard-operable standard Tk desktop controls",
        "05-package": "Implement desktop entrypoint and non-admin PowerShell installer contract",
    }


def _prove_ownership(graph: ProductRepositoryGraph, team) -> tuple[str, ...]:
    active: list[OwnershipLease] = []
    lease_ids: list[str] = []
    for component in graph.components:
        owner = next(
            (
                role.role_id
                for role in team.roles
                if component.component_id in role.component_ids and not role.independent_review
            ),
            None,
        )
        if owner is None:
            raise C1MediumAppAcceptanceError(f"component has no implementation owner: {component.component_id}")
        lease = OwnershipLease(
            lease_id=f"lease:{component.component_id}",
            worker_id=owner,
            component_ids=(component.component_id,),
            allowed_paths=component.paths,
        )
        assessment = graph.assess_lease(lease, active)
        if not assessment.grantable:
            raise C1MediumAppAcceptanceError(
                f"component ownership conflicts: {component.component_id}"
            )
        active.append(lease)
        lease_ids.append(lease.lease_id)
    return tuple(lease_ids)


def _ensure_host_task(store: SQLiteStore, *, project_id: str) -> None:
    now = datetime.now(UTC).isoformat()
    payload = json.dumps(
        {"kind": "product_factory", "product_project_id": project_id},
        sort_keys=True,
        separators=(",", ":"),
    )
    with store.connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO tasks(task_id,workspace_id,agent_id,state,payload_json,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (_C1_HOST_TASK_ID, "pf11-c1", "product-factory", "running", payload, now, now),
        )


def _path_allowed(path: str, allowed_paths: tuple[str, ...]) -> bool:
    normalized = path.replace("\\", "/").strip("/")
    return any(
        normalized == root.replace("\\", "/").strip("/")
        or normalized.startswith(root.replace("\\", "/").strip("/") + "/")
        for root in allowed_paths
    )


def _component_files(component_id: str, attempt: int) -> dict[str, str]:
    if component_id == "01-storage":
        return _storage_files()
    if component_id == "02-settings":
        return _settings_files()
    if component_id == "03-domain":
        return _domain_files()
    if component_id == "04-desktop-ui":
        return _ui_files(repaired=attempt > 1)
    if component_id == "05-package":
        return _package_files()
    raise C1MediumAppAcceptanceError(f"unknown C1 component: {component_id}")


def _storage_files() -> dict[str, str]:
    storage = '''from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 2

@dataclass(frozen=True)
class Expense:
    expense_id: int
    amount_cents: int
    note: str
    category: str

class ExpenseRepository:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version == 0:
                conn.execute("CREATE TABLE expenses (expense_id INTEGER PRIMARY KEY AUTOINCREMENT, amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0), note TEXT NOT NULL)")
                conn.execute("PRAGMA user_version = 1")
                version = 1
            if version == 1:
                conn.execute("ALTER TABLE expenses ADD COLUMN category TEXT NOT NULL DEFAULT 'General'")
                conn.execute("PRAGMA user_version = 2")
                version = 2
            if version != SCHEMA_VERSION:
                raise RuntimeError(f"unsupported expense database schema: {version}")

    def add(self, amount_cents: int, note: str, category: str) -> int:
        if amount_cents < 0:
            raise ValueError("amount_cents must be non-negative")
        self.migrate()
        with sqlite3.connect(self.path) as conn:
            cursor = conn.execute("INSERT INTO expenses(amount_cents,note,category) VALUES (?,?,?)", (amount_cents, note.strip(), category.strip() or "General"))
            return int(cursor.lastrowid)

    def list_all(self) -> tuple[Expense, ...]:
        self.migrate()
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute("SELECT expense_id,amount_cents,note,category FROM expenses ORDER BY expense_id").fetchall()
        return tuple(Expense(int(row[0]), int(row[1]), str(row[2]), str(row[3])) for row in rows)
'''
    test = '''from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from c1_expense_manager.data.storage import ExpenseRepository

class StorageUpgradeTest(unittest.TestCase):
    def test_v1_data_survives_v2_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "expenses.db"
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE expenses (expense_id INTEGER PRIMARY KEY AUTOINCREMENT, amount_cents INTEGER NOT NULL, note TEXT NOT NULL)")
                conn.execute("INSERT INTO expenses(amount_cents,note) VALUES (?,?)", (1250, "Legacy row"))
                conn.execute("PRAGMA user_version = 1")
            repo = ExpenseRepository(path)
            repo.migrate()
            rows = repo.list_all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].amount_cents, 1250)
            self.assertEqual(rows[0].note, "Legacy row")
            self.assertEqual(rows[0].category, "General")
            with sqlite3.connect(path) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)

if __name__ == "__main__":
    unittest.main()
'''
    return {
        "src/c1_expense_manager/data/storage.py": storage,
        "tests/test_storage_upgrade.py": test,
    }


def _settings_files() -> dict[str, str]:
    settings = '''from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

@dataclass(frozen=True)
class AppSettings:
    database_path: str
    currency: str = "UAH"

class SettingsRepository:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> AppSettings:
        if not self.path.exists():
            base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "NikaC1ExpenseManager"
            return AppSettings(database_path=str(base / "expenses.db"))
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return AppSettings(database_path=str(data["database_path"]), currency=str(data.get("currency", "UAH")))

    def save(self, settings: AppSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(asdict(settings), handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\\n")
            os.replace(temp_name, self.path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
'''
    test = '''from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from c1_expense_manager.config.settings import AppSettings, SettingsRepository

class SettingsTest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = SettingsRepository(Path(tmp) / "settings.json")
            expected = AppSettings(database_path=str(Path(tmp) / "data.db"), currency="EUR")
            repo.save(expected)
            self.assertEqual(repo.load(), expected)

if __name__ == "__main__":
    unittest.main()
'''
    return {
        "src/c1_expense_manager/config/settings.py": settings,
        "tests/test_settings.py": test,
    }


def _domain_files() -> dict[str, str]:
    domain = '''from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from c1_expense_manager.data.storage import Expense, ExpenseRepository

class ExpenseService:
    def __init__(self, repository: ExpenseRepository) -> None:
        self.repository = repository

    def add_expense(self, amount_text: str, note: str, category: str) -> int:
        try:
            amount = Decimal(amount_text.strip()).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except (InvalidOperation, AttributeError) as exc:
            raise ValueError("amount must be a decimal number") from exc
        if amount < 0:
            raise ValueError("amount must be non-negative")
        cents = int(amount * 100)
        return self.repository.add(cents, note, category)

    def list_expenses(self) -> tuple[Expense, ...]:
        return self.repository.list_all()
'''
    test = '''from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from c1_expense_manager.data.storage import ExpenseRepository
from c1_expense_manager.domain.service import ExpenseService

class DomainTest(unittest.TestCase):
    def test_amount_is_exact_cents_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = ExpenseService(ExpenseRepository(Path(tmp) / "expenses.db"))
            service.add_expense("12.34", "Taxi", "Travel")
            row = service.list_expenses()[0]
            self.assertEqual(row.amount_cents, 1234)
            self.assertEqual(row.category, "Travel")

if __name__ == "__main__":
    unittest.main()
'''
    return {
        "src/c1_expense_manager/domain/service.py": domain,
        "tests/test_domain.py": test,
    }


def _ui_files(*, repaired: bool) -> dict[str, str]:
    accelerator_lines = "" if not repaired else '''\n        self.root.bind("<Alt-a>", lambda _event: self._add())\n        self.root.bind("<Alt-r>", lambda _event: self.refresh())'''
    ui = f'''from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from c1_expense_manager.domain.service import ExpenseService

class ExpenseWindow:
    def __init__(self, root: tk.Tk, service: ExpenseService) -> None:
        self.root = root
        self.service = service
        root.title("C1 Accessible Expense Manager")
        frame = ttk.Frame(root, padding=12)
        frame.grid(sticky="nsew")
        ttk.Label(frame, text="&Amount", underline=0).grid(row=0, column=0, sticky="w")
        self.amount = ttk.Entry(frame, width=20, takefocus=True)
        self.amount.grid(row=0, column=1, sticky="ew")
        ttk.Label(frame, text="&Note", underline=0).grid(row=1, column=0, sticky="w")
        self.note = ttk.Entry(frame, width=40, takefocus=True)
        self.note.grid(row=1, column=1, sticky="ew")
        ttk.Label(frame, text="&Category", underline=0).grid(row=2, column=0, sticky="w")
        self.category = ttk.Entry(frame, width=20, takefocus=True)
        self.category.grid(row=2, column=1, sticky="ew")
        self.add_button = ttk.Button(frame, text="Add expense", command=self._add, takefocus=True)
        self.add_button.grid(row=3, column=0, pady=8, sticky="w")
        self.refresh_button = ttk.Button(frame, text="Refresh expenses", command=self.refresh, takefocus=True)
        self.refresh_button.grid(row=3, column=1, pady=8, sticky="w")
        ttk.Label(frame, text="Expenses").grid(row=4, column=0, sticky="w")
        self.expenses = tk.Listbox(frame, height=10, takefocus=True, exportselection=False)
        self.expenses.grid(row=5, column=0, columnspan=2, sticky="nsew")
        self.status = tk.StringVar(value="Ready")
        ttk.Label(frame, textvariable=self.status, takefocus=False).grid(row=6, column=0, columnspan=2, sticky="w"){accelerator_lines}
        self.refresh()

    def _add(self) -> None:
        try:
            self.service.add_expense(self.amount.get(), self.note.get(), self.category.get())
        except ValueError as exc:
            self.status.set(f"Error: {{exc}}")
            return
        self.status.set("Expense added")
        self.refresh()

    def refresh(self) -> None:
        self.expenses.delete(0, tk.END)
        for item in self.service.list_expenses():
            self.expenses.insert(tk.END, f"{{item.amount_cents / 100:.2f}} {{item.category}} {{item.note}}")
        self.status.set(f"Loaded {{self.expenses.size()}} expenses")
'''
    test = '''from __future__ import annotations

import unittest
from pathlib import Path

class AccessibilityContractTest(unittest.TestCase):
    def test_standard_named_keyboard_controls_without_coordinate_automation(self):
        source = (Path(__file__).resolve().parents[1] / "src" / "c1_expense_manager" / "desktop" / "ui.py").read_text(encoding="utf-8")
        for expected in ("Add expense", "Refresh expenses", "Expenses", "takefocus=True", "StringVar"):
            self.assertIn(expected, source)
        for forbidden in ("pyautogui", "click(", "moveTo(", "SetCursorPos"):
            self.assertNotIn(forbidden, source)

if __name__ == "__main__":
    unittest.main()
'''
    return {
        "src/c1_expense_manager/desktop/ui.py": ui,
        "tests/test_accessibility_contract.py": test,
    }


def _package_files() -> dict[str, str]:
    main = '''from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path
from c1_expense_manager.config.settings import SettingsRepository
from c1_expense_manager.data.storage import ExpenseRepository
from c1_expense_manager.domain.service import ExpenseService
from c1_expense_manager.desktop.ui import ExpenseWindow

def main() -> int:
    settings_path = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "NikaC1ExpenseManager" / "settings.json"
    settings = SettingsRepository(settings_path).load()
    service = ExpenseService(ExpenseRepository(settings.database_path))
    root = tk.Tk()
    ExpenseWindow(root, service)
    root.mainloop()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
'''
    installer = '''param(
    [Parameter(Mandatory=$true)][string]$BundlePath,
    [Parameter(Mandatory=$true)][string]$Destination
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $BundlePath -PathType Container)) { throw 'BundlePath does not exist' }
if ([System.IO.Path]::GetFullPath($Destination).StartsWith([System.IO.Path]::GetFullPath($env:WINDIR), [System.StringComparison]::OrdinalIgnoreCase)) { throw 'System directory install is forbidden' }
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $BundlePath '*') -Destination $Destination -Recurse -Force
$exe = Join-Path $Destination 'NikaC1ExpenseManager.exe'
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw 'Installed executable is missing' }
Write-Output $exe
'''
    test = '''from __future__ import annotations

import unittest
from pathlib import Path

class PackageContractTest(unittest.TestCase):
    def test_entrypoint_and_non_admin_installer_are_present(self):
        root = Path(__file__).resolve().parents[1]
        self.assertTrue((root / "src" / "c1_expense_manager" / "main.py").is_file())
        installer = (root / "installer" / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("Destination", installer)
        self.assertIn("Copy-Item", installer)
        self.assertNotIn("Start-Process -Verb RunAs", installer)

if __name__ == "__main__":
    unittest.main()
'''
    return {
        "src/c1_expense_manager/main.py": main,
        "installer/install.ps1": installer,
        "tests/test_package_contract.py": test,
    }


def _run_generated_suite(workspace: Path) -> str:
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode != 0:
        raise C1MediumAppAcceptanceError(f"generated product tests failed:\n{output}")
    return hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest()


def _run_named_generated_test(workspace: Path, filename: str) -> bool:
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", filename],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        output = (completed.stdout or "") + (completed.stderr or "")
        raise C1MediumAppAcceptanceError(f"generated proof {filename} failed:\n{output}")
    return True


def _build_and_install_package(
    workspace: Path,
    *,
    source_sha: str,
    project,
    team_plan_id: str,
) -> tuple[Path, bool]:
    import PyInstaller.__main__

    dist_root = workspace / "dist"
    build_root = workspace / "build"
    spec_root = workspace / "spec"
    PyInstaller.__main__.run(
        [
            str(workspace / "src" / "c1_expense_manager" / "main.py"),
            "--name",
            "NikaC1ExpenseManager",
            "--onedir",
            "--windowed",
            "--noconfirm",
            "--clean",
            "--paths",
            str(workspace / "src"),
            "--distpath",
            str(dist_root),
            "--workpath",
            str(build_root),
            "--specpath",
            str(spec_root),
        ]
    )
    bundle = dist_root / "NikaC1ExpenseManager"
    executable = bundle / "NikaC1ExpenseManager.exe"
    if not executable.is_file():
        raise C1MediumAppAcceptanceError("PyInstaller did not produce C1 Windows executable")

    inner = {
        "schema": "nika-c1-product-package-v1",
        "nika_source_sha": source_sha,
        "product_project_id": project.project_id,
        "product_project_spec_version": project.spec_version,
        "team_plan_id": team_plan_id,
        "human_tested": False,
        "nvda_verified": False,
    }
    (bundle / "C1_PRODUCT_EVIDENCE.json").write_text(
        json.dumps(inner, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with tempfile.TemporaryDirectory(prefix="nika-c1-install-") as tmp:
        destination = Path(tmp) / "Installed C1 Expense Manager"
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell is None:
            raise C1MediumAppAcceptanceError("PowerShell is required for C1 installer proof")
        completed = subprocess.run(
            [
                shell,
                "-NoProfile",
                "-File",
                str(workspace / "installer" / "install.ps1"),
                "-BundlePath",
                str(bundle),
                "-Destination",
                str(destination),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            raise C1MediumAppAcceptanceError(
                "C1 installer proof failed: " + (completed.stderr or completed.stdout)
            )
        installed = destination / "NikaC1ExpenseManager.exe"
        installed_ok = installed.is_file() and _sha256_file(installed) == _sha256_file(executable)
        if not installed_ok:
            raise C1MediumAppAcceptanceError("installed C1 executable differs from packaged executable")

    package = dist_root / "NikaC1ExpenseManager-windows-x64.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(path, Path("NikaC1ExpenseManager") / path.relative_to(bundle))
        archive.write(workspace / "installer" / "install.ps1", "install.ps1")
    return package, installed_ok


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_c1_evidence(path: Path, evidence: C1MediumAppEvidence) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(evidence.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path
