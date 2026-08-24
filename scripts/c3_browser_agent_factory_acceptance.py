from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import runpy
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.packaging.release import (
    build_release_manifest,
    verify_release_manifest,
    write_release_manifest,
)
from nika_core.product_factory_checkpoint_host import ProductFactoryCheckpointHost
from nika_core.product_factory_coordinator import (
    ComponentWorkRequest,
    ReviewDecision,
    WorkerResultEnvelope,
    WorkState,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_program_host import ProductFactoryProgramHost
from nika_core.product_factory_project_binding import ProductProjectCoordinatorBinding
from nika_core.product_project import (
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
)
from nika_core.toolsmith.contracts import (
    ChangedFile,
    CodingResult,
    RecoveryState,
    TestEvidence,
    WorkerFailure,
    WorkerFailureKind,
    normalize_relative_path,
)
from scripts import c3_browser_agent_factory_proof as browser_proof

_PROJECT_ID = browser_proof.PROJECT_ID
_REPOSITORY_ID = "c3-repo"
_REPOSITORY_LOCATOR = browser_proof.REPOSITORY_LOCATOR
_PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


class C3FactoryAcceptanceError(RuntimeError):
    pass


@dataclass(slots=True)
class SandboxC3Worker:
    """Deterministic acceptance worker constrained to coordinator-owned sandbox paths."""

    workspace: Path
    dispatch_calls: list[str] = field(default_factory=list)

    async def dispatch(self, request: ComponentWorkRequest) -> WorkerResultEnvelope:
        self.dispatch_calls.append(request.component_id)
        return self._execute(request)

    async def inspect(self, work_id: str) -> RecoveryState | None:
        return RecoveryState(phase="checkpointed", opaque_token=work_id)

    async def recover(
        self,
        request: ComponentWorkRequest,
        state: RecoveryState,
    ) -> WorkerResultEnvelope:
        if state.opaque_token not in {None, request.work_id}:
            raise C3FactoryAcceptanceError("worker recovery token does not match work id")
        return self._execute(request)

    def _execute(self, request: ComponentWorkRequest) -> WorkerResultEnvelope:
        changed_files: list[ChangedFile] = []
        for relative, text in _component_files(request.component_id).items():
            if not _path_allowed(relative, request.allowed_paths):
                raise C3FactoryAcceptanceError(
                    f"worker attempted path outside component ownership: {relative}"
                )
            target = self.workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="\n")
            payload = text.encode()
            changed_files.append(
                ChangedFile(
                    path=relative,
                    sha256=hashlib.sha256(payload).hexdigest(),
                    size_bytes=len(payload),
                )
            )

        test_evidence: list[TestEvidence] = []
        failure: WorkerFailure | None = None
        for declared in request.acceptance_commands:
            argv = list(declared)
            if argv[0].casefold() in {"python", "python.exe", "python3"}:
                argv[0] = sys.executable
            completed = subprocess.run(
                argv,
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            output = (completed.stdout or "") + (completed.stderr or "")
            test_evidence.append(
                TestEvidence(
                    command=declared,
                    exit_code=completed.returncode,
                    output_digest=hashlib.sha256(output.encode(errors="replace")).hexdigest(),
                )
            )
            if completed.returncode != 0:
                failure = WorkerFailure(
                    kind=WorkerFailureKind.PROCESS_FAILED,
                    message=(
                        f"generated component acceptance failed: {request.component_id}; "
                        f"exit={completed.returncode}"
                    ),
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
        digest = hashlib.sha256(material).hexdigest()
        return WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=request.component_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha=digest[:40],
            diff_digest=digest,
            coding_result=CodingResult(
                job_id=request.work_id,
                changed_files=tuple(changed_files),
                test_evidence=tuple(test_evidence),
                failure=failure,
            ),
        )


def _test_command(filename: str) -> tuple[str, ...]:
    return (
        "python",
        "-m",
        "unittest",
        "discover",
        "-s",
        "generated/tests",
        "-p",
        filename,
    )


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id=_PROJECT_ID,
        repositories=(
            RepositoryRef(
                repository_id=_REPOSITORY_ID,
                provider="local-sandbox",
                locator=_REPOSITORY_LOCATOR,
                default_branch="main",
            ),
        ),
        components=(
            ProductComponent(
                component_id="commerce-fixture",
                repository_id=_REPOSITORY_ID,
                paths=(
                    "generated/c3_browser_agent/fixture.py",
                    "generated/tests/test_fixture.py",
                ),
                test_commands=(_test_command("test_fixture.py"),),
            ),
            ProductComponent(
                component_id="semantic-browser-agent",
                repository_id=_REPOSITORY_ID,
                paths=(
                    "generated/c3_browser_agent/agent.py",
                    "generated/tests/test_agent.py",
                ),
                dependencies=("commerce-fixture",),
                test_commands=(_test_command("test_agent.py"),),
            ),
            ProductComponent(
                component_id="package",
                repository_id=_REPOSITORY_ID,
                paths=(
                    "generated/c3_browser_agent/package.py",
                    "generated/tests/test_package.py",
                ),
                dependencies=("semantic-browser-agent",),
                test_commands=(_test_command("test_package.py"),),
            ),
        ),
    )


def _spec() -> ProductProjectSpec:
    return ProductProjectSpec(
        goal="Generate a sandbox semantic browser-agent commerce product through Product Factory",
        desired_outcome=(
            "Component-owned generated source is tested, independently reviewed, restart-safe, "
            "and packaged with exact semantic browser evidence"
        ),
        repository_refs=(_REPOSITORY_LOCATOR,),
        requirements=(
            ProductRequirement(
                requirement_id="c3-factory-generation",
                text="Product Factory materializes component-owned source with passing tests",
                acceptance=(
                    "all components reach accepted through ProductFactoryProgramHost",
                    "generated files remain inside component ownership",
                ),
            ),
            ProductRequirement(
                requirement_id="c3-safe-browser-flow",
                text="Browser interaction stays semantic and loopback-only",
                acceptance=(
                    "search through confirmation succeeds",
                    "uncertain submit reconciles before bounded idempotent retry",
                ),
            ),
        ),
    )


def _component_goals() -> dict[str, str]:
    return {
        "commerce-fixture": "Generate a loopback-only commerce fixture contract and tests",
        "semantic-browser-agent": "Generate exact semantic browser-agent policy and tests",
        "package": "Generate package and restart contract tests",
    }


def _component_files(component_id: str) -> dict[str, str]:
    builders = {
        "commerce-fixture": _fixture_files,
        "semantic-browser-agent": _agent_files,
        "package": _package_files,
    }
    try:
        return builders[component_id]()
    except KeyError as exc:
        raise C3FactoryAcceptanceError(f"unknown C3 component: {component_id}") from exc


def _fixture_files() -> dict[str, str]:
    source = '''from __future__ import annotations

FIXTURE_HOST = "127.0.0.1"
FIXTURE_SCHEME = "http"
REAL_PURCHASE_ENABLED = False
THIRD_PARTY_ACCOUNT_ENABLED = False
CAPTCHA_BYPASS_ENABLED = False


def fixture_policy() -> dict[str, object]:
    return {
        "host": FIXTURE_HOST,
        "scheme": FIXTURE_SCHEME,
        "real_purchase": REAL_PURCHASE_ENABLED,
        "third_party_account": THIRD_PARTY_ACCOUNT_ENABLED,
        "captcha_bypass": CAPTCHA_BYPASS_ENABLED,
    }
'''
    test = '''from __future__ import annotations

import runpy
import unittest
from pathlib import Path


class FixtureContractTest(unittest.TestCase):
    def test_fixture_is_loopback_and_non_commercial(self) -> None:
        target = Path(__file__).resolve().parents[1] / "c3_browser_agent" / "fixture.py"
        policy = runpy.run_path(str(target))["fixture_policy"]()
        self.assertEqual(policy["host"], "127.0.0.1")
        self.assertEqual(policy["scheme"], "http")
        self.assertFalse(policy["real_purchase"])
        self.assertFalse(policy["third_party_account"])
        self.assertFalse(policy["captcha_bypass"])


if __name__ == "__main__":
    unittest.main()
'''
    return {
        "generated/c3_browser_agent/fixture.py": source,
        "generated/tests/test_fixture.py": test,
    }


def _agent_files() -> dict[str, str]:
    source = '''from __future__ import annotations

from nika_core.interaction import ControlLocator

SAFE_FLOW = (
    "search catalog",
    "inspect semantic DOM",
    "choose item",
    "fill form",
    "cart",
    "simulated checkout",
    "confirmation",
)
INTERACTION_PRIORITY = (
    "api/native",
    "dom/accessibility",
    "named-controls",
    "vision/ocr",
    "coordinates",
)
UNCERTAIN_SUBMIT_POLICY = "reconcile-read-only-before-bounded-idempotent-retry"
USES_CSS_XPATH = False
USES_POSITIONAL_TARGETING = False
USES_COORDINATES = False


def semantic_target(role: str, name: str) -> ControlLocator:
    return ControlLocator(role=role, name=name)
'''
    test = '''from __future__ import annotations

import runpy
import unittest
from pathlib import Path


class AgentContractTest(unittest.TestCase):
    def test_agent_uses_exact_semantic_contract(self) -> None:
        target = Path(__file__).resolve().parents[1] / "c3_browser_agent" / "agent.py"
        namespace = runpy.run_path(str(target))
        locator = namespace["semantic_target"]("button", "Search")
        self.assertEqual((locator.role, locator.name), ("button", "Search"))
        self.assertFalse(namespace["USES_CSS_XPATH"])
        self.assertFalse(namespace["USES_POSITIONAL_TARGETING"])
        self.assertFalse(namespace["USES_COORDINATES"])
        self.assertEqual(
            namespace["UNCERTAIN_SUBMIT_POLICY"],
            "reconcile-read-only-before-bounded-idempotent-retry",
        )


if __name__ == "__main__":
    unittest.main()
'''
    return {
        "generated/c3_browser_agent/agent.py": source,
        "generated/tests/test_agent.py": test,
    }


def _package_files() -> dict[str, str]:
    source = '''from __future__ import annotations

PACKAGE_NAME = "c3-browser-agent-product"
CANONICAL_RELEASE_MANIFEST_REQUIRED = True
RESTART_EVIDENCE_REQUIRED = True
HUMAN_TESTED = False
NVDA_VERIFIED = False


def package_policy() -> dict[str, object]:
    return {
        "package_name": PACKAGE_NAME,
        "canonical_manifest": CANONICAL_RELEASE_MANIFEST_REQUIRED,
        "restart_evidence": RESTART_EVIDENCE_REQUIRED,
        "human_tested": HUMAN_TESTED,
        "nvda_verified": NVDA_VERIFIED,
    }
'''
    test = '''from __future__ import annotations

import runpy
import unittest
from pathlib import Path


class PackageContractTest(unittest.TestCase):
    def test_package_has_manifest_restart_and_truthful_human_state(self) -> None:
        target = Path(__file__).resolve().parents[1] / "c3_browser_agent" / "package.py"
        policy = runpy.run_path(str(target))["package_policy"]()
        self.assertTrue(policy["canonical_manifest"])
        self.assertTrue(policy["restart_evidence"])
        self.assertFalse(policy["human_tested"])
        self.assertFalse(policy["nvda_verified"])


if __name__ == "__main__":
    unittest.main()
'''
    return {
        "generated/c3_browser_agent/package.py": source,
        "generated/tests/test_package.py": test,
    }


def _path_allowed(path: str, allowed_paths: tuple[str, ...]) -> bool:
    candidate = normalize_relative_path(path)
    roots = tuple(normalize_relative_path(root) for root in allowed_paths)
    return any(candidate == root or root in candidate.parents for root in roots)


def _run_factory_program(
    root: Path,
    source_sha: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    workspace = root / "generated-product"
    workspace.mkdir(parents=True, exist_ok=True)
    db_path = root / "factory-program.db"
    store = SQLiteStore(db_path)
    store.initialize()
    project = ProductProjectRepository(store).create(
        project_id=_PROJECT_ID,
        name="C3 Browser Agent Product",
        spec=_spec(),
        idempotency_key="c3:factory-program:create",
    )
    graph = _graph()
    binding = ProductProjectCoordinatorBinding(project=project, graph=graph)
    coordinator = binding.plan(
        base_shas={_REPOSITORY_ID: source_sha},
        component_goals=_component_goals(),
        permission_ceiling=_PERMISSIONS,
    )
    task = TaskQueue(store).create(
        workspace_id="c3-factory-workspace",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": project.project_id},
    )
    ProductFactoryCheckpointHost(store).save(
        host_task_id=task.task_id,
        checkpoint=binding.checkpoint(coordinator),
    )

    worker = SandboxC3Worker(workspace)
    host = ProductFactoryProgramHost(store=store, worker=worker)
    restart_exact = False

    for _cycle in range(12):
        if all(record.state is WorkState.ACCEPTED for record in coordinator.snapshot().records):
            break
        progressed = False
        ready = coordinator.ready_requests()
        if ready:
            outcomes = asyncio.run(
                host.dispatch_ready(
                    host_task_id=task.task_id,
                    binding=binding,
                    coordinator=coordinator,
                    max_parallel=1,
                    max_count=1,
                )
            )
            if any(outcome.state is not WorkState.REVIEW_REQUIRED for outcome in outcomes):
                raise C3FactoryAcceptanceError(
                    "generated component did not reach review_required with passing evidence"
                )
            progressed = True

        review_record = next(
            (
                record
                for record in coordinator.snapshot().records
                if record.state is WorkState.REVIEW_REQUIRED
            ),
            None,
        )
        if review_record is not None:
            host.review_and_checkpoint(
                host_task_id=task.task_id,
                binding=binding,
                coordinator=coordinator,
                component_id=review_record.request.component_id,
                decision=ReviewDecision(
                    reviewer_id="c3-independent-qa",
                    accepted=True,
                    reason="generated component passed declared tests and path-scope review",
                    evidence_refs=(
                        f"c3:qa:{review_record.request.component_id}:"
                        f"attempt-{review_record.request.attempt}",
                    ),
                ),
            )
            progressed = True

        fixture_state = next(
            record.state
            for record in coordinator.snapshot().records
            if record.request.component_id == "commerce-fixture"
        )
        if fixture_state is WorkState.ACCEPTED and not restart_exact:
            before_restart = coordinator.snapshot()
            reopened_store = SQLiteStore(db_path)
            reopened_store.initialize()
            project = ProductProjectRepository(reopened_store).get(_PROJECT_ID)
            binding = ProductProjectCoordinatorBinding(project=project, graph=graph)
            host = ProductFactoryProgramHost(store=reopened_store, worker=worker)
            coordinator = host.restore_latest(
                host_task_id=task.task_id,
                binding=binding,
            )
            if coordinator.snapshot() != before_restart:
                raise C3FactoryAcceptanceError("factory restart changed durable coordinator state")
            restart_exact = True
            progressed = True

        if not progressed:
            raise C3FactoryAcceptanceError("C3 Product Factory made no progress")
    else:
        raise C3FactoryAcceptanceError("C3 Product Factory exceeded bounded convergence cycles")

    records = coordinator.snapshot().records
    if not restart_exact:
        raise C3FactoryAcceptanceError("factory restart was not proven")
    if not all(record.state is WorkState.ACCEPTED for record in records):
        raise C3FactoryAcceptanceError("not all generated components were accepted")

    generated_sources: dict[str, str] = {}
    generated_sha256: dict[str, str] = {}
    for component in graph.components:
        for relative in component.paths:
            target = workspace / relative
            if not target.is_file():
                raise C3FactoryAcceptanceError(f"generated file is missing: {relative}")
            generated_sources[relative] = target.read_text(encoding="utf-8")
            generated_sha256[relative] = hashlib.sha256(target.read_bytes()).hexdigest()

    evidence: dict[str, Any] = {
        "project_id": project.project_id,
        "spec_version": project.spec_version,
        "task_id": task.task_id,
        "factory_program_host_used": True,
        "restart_exact": True,
        "all_components_accepted": True,
        "component_states": {
            record.request.component_id: record.state.value for record in records
        },
        "component_attempts": {
            record.request.component_id: record.request.attempt for record in records
        },
        "worker_dispatches": list(worker.dispatch_calls),
        "components": {
            component.component_id: list(component.paths) for component in graph.components
        },
        "generated_files": sorted(generated_sources),
        "generated_sha256": dict(sorted(generated_sha256.items())),
    }
    return evidence, generated_sources


def _augment_package_with_generated_product(
    package: dict[str, Any],
    *,
    output_root: Path,
    source_sha: str,
    generated_sources: dict[str, str],
) -> dict[str, Any]:
    bundle = Path(package["bundle"])
    source_root = bundle / "generated-product"
    for relative, text in sorted(generated_sources.items()):
        target = source_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")

    manifest = build_release_manifest(
        bundle,
        product="Nika C3 Browser Agent",
        version="1.0.0-c3",
        source_sha=source_sha,
    )
    write_release_manifest(bundle, manifest)
    findings = verify_release_manifest(bundle, manifest)
    if findings:
        raise C3FactoryAcceptanceError(
            "augmented C3 package failed canonical manifest verification: "
            + ", ".join(findings)
        )

    zip_path = Path(package["zip"])
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(output_root).as_posix())

    updated = dict(package)
    updated["manifest_files"] = len(manifest.files)
    updated["zip_sha256"] = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    updated["generated_source_files"] = len(generated_sources)
    return updated


def run(output_root: Path) -> dict[str, Any]:
    source_sha = browser_proof._source_sha()
    output_root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="nika-c3-factory-program-") as temp:
        temp_root = Path(temp)
        factory, generated_sources = _run_factory_program(temp_root, source_sha)
        browser = browser_proof._run_browser_flow(temp_root)

        agent_path = (
            temp_root
            / "generated-product"
            / "generated"
            / "c3_browser_agent"
            / "agent.py"
        )
        agent_contract = runpy.run_path(str(agent_path))
        if tuple(browser["safe_flow"]) != tuple(agent_contract["SAFE_FLOW"]):
            raise C3FactoryAcceptanceError(
                "executed browser flow does not match generated semantic-agent contract"
            )

        fixture_path = (
            temp_root
            / "generated-product"
            / "generated"
            / "c3_browser_agent"
            / "fixture.py"
        )
        fixture_contract = runpy.run_path(str(fixture_path))
        if fixture_contract["FIXTURE_HOST"] != "127.0.0.1":
            raise C3FactoryAcceptanceError("generated fixture is not loopback-only")

    package = browser_proof._package(output_root, source_sha, factory, browser)
    package = _augment_package_with_generated_product(
        package,
        output_root=output_root,
        source_sha=source_sha,
        generated_sources=generated_sources,
    )
    result = {
        "source_sha": source_sha,
        "factory": factory,
        "browser": browser,
        "package": package,
        "human_tested": False,
        "nvda_verified": False,
    }
    (output_root / "c3-result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts") / "c3-browser-agent",
    )
    args = parser.parse_args()
    result = run(args.output.resolve())
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()