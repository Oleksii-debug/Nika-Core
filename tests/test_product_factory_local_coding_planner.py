from __future__ import annotations

import asyncio
import json
import pathlib
import shutil
import subprocess
import sys

import pytest

from nika_core.model_gateway.contracts import (
    ModelResponse,
    ModelUsage,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.product_factory_local_coding_planner import (
    ModelGatewayLocalCodingPlanner,
    ModelGatewayLocalCodingPlannerError,
)
from nika_core.toolsmith.contracts import (
    AllowedPathPolicy,
    CodingJob,
    IsolationClass,
    NetworkPolicy,
    ProcessPolicy,
    RepositorySnapshot,
    ResourceBudget,
    WorkspaceLease,
)
from nika_core.toolsmith.local_worker import LocalCodingPlan

PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


def _run(coroutine):
    return asyncio.run(coroutine)


def _git(root: pathlib.Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def _repository(tmp_path: pathlib.Path, *, content: bytes = b"VALUE = 1\n"):
    if shutil.which("git") is None:
        pytest.skip("Git CLI unavailable")
    root = tmp_path / "trusted repository"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.name", "Nika Test")
    _git(root, "config", "user.email", "nika@example.invalid")
    source = root / "src"
    source.mkdir()
    (source / "value.py").write_bytes(content)
    _git(root, "add", "src/value.py")
    _git(root, "commit", "-m", "base")
    base_sha = _git(root, "rev-parse", "HEAD")
    tree_sha = _git(root, "rev-parse", f"{base_sha}^{{tree}}")
    return root, base_sha, tree_sha


def _job(
    tmp_path: pathlib.Path,
    *,
    base_sha: str,
    tree_sha: str,
    allowed_paths: tuple[str, ...] = ("src",),
) -> CodingJob:
    python = str(pathlib.Path(sys.executable).resolve(strict=True))
    return CodingJob(
        job_id="planner-work-1",
        task_id="product:p1:component:core",
        goal="Change the value to 2 and keep the file valid Python.",
        repository=RepositorySnapshot("repo-1", base_sha, tree_sha),
        lease=WorkspaceLease(
            lease_id="planner-lease-1",
            workspace_root=tmp_path / "unused worker workspace",
            isolation_class=(
                IsolationClass.PROCESS_CONTAINED
                if sys.platform == "win32"
                else IsolationClass.POLICY_ONLY
            ),
            expires_at="2099-01-01T00:00:00+00:00",
        ),
        allowed_paths=AllowedPathPolicy(allowed_paths),
        process_policy=ProcessPolicy((python,)),
        network_policy=NetworkPolicy(),
        resource_budget=ResourceBudget(30, 1024 * 1024, 10),
        acceptance_commands=(),
        permission_ceiling=PERMISSIONS,
    )


class _Provider:
    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.requests = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id="planner-local",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
        )

    async def complete(self, request):
        self.requests.append(request)
        return ModelResponse(
            request_id=request.request_id,
            text=self.response_text,
            provider_id="planner-local",
            provider_kind=ProviderKind.LOCAL,
            model=request.model or "planner-model",
            usage=ModelUsage(input_tokens=100, output_tokens=30, total_tokens=130),
            latency_ms=10,
        )


def _planner(
    repository: pathlib.Path,
    provider: _Provider,
    **kwargs,
) -> ModelGatewayLocalCodingPlanner:
    gateway = ModelGateway()
    gateway.register(provider)
    return ModelGatewayLocalCodingPlanner(
        gateway=gateway,
        repositories={"repo-1": repository},
        provider_id="planner-local",
        model="planner-model",
        **kwargs,
    )


def _valid_response(
    *,
    path: str = "src/value.py",
    content: str = "VALUE = 2\n",
) -> str:
    return json.dumps(
        {
            "schema": "nika.local-coding-plan:v1",
            "edits": [{"path": path, "content": content}],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def test_planner_reads_exact_base_objects_not_dirty_production_worktree(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha, tree_sha = _repository(tmp_path)
    (repository / "src" / "value.py").write_text("VALUE = 999\n", encoding="utf-8")
    provider = _Provider(_valid_response())
    planner = _planner(repository, provider)
    job = _job(tmp_path, base_sha=base_sha, tree_sha=tree_sha)

    plan = _run(planner.plan(job))

    assert isinstance(plan, LocalCodingPlan)
    assert plan.edits[0].path == "src/value.py"
    assert plan.edits[0].content == b"VALUE = 2\n"
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.privacy is PrivacyClass.PRIVATE
    assert request.provider_id == "planner-local"
    assert request.model == "planner-model"
    assert request.temperature == 0.0
    payload = json.loads(request.messages[1].content)
    assert payload["repository"]["base_sha"] == base_sha
    assert payload["repository"]["tree_digest"] == tree_sha
    assert payload["files"] == [
        {"path": "src/value.py", "content": "VALUE = 1\n", "size_bytes": 10}
    ]
    assert "VALUE = 999" not in request.messages[1].content


def test_planner_normalizes_windows_style_allowed_root_for_model_context(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha, tree_sha = _repository(tmp_path)
    provider = _Provider(_valid_response())
    planner = _planner(repository, provider)
    job = _job(
        tmp_path,
        base_sha=base_sha,
        tree_sha=tree_sha,
        allowed_paths=("src\\",),
    )

    _run(planner.plan(job))

    payload = json.loads(provider.requests[0].messages[1].content)
    assert payload["allowed_paths"] == ["src"]
    assert payload["files"] == [
        {"path": "src/value.py", "content": "VALUE = 1\n", "size_bytes": 10}
    ]


def test_planner_request_identity_binds_allowed_scope(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha, tree_sha = _repository(tmp_path)
    provider = _Provider(_valid_response())
    planner = _planner(repository, provider)
    broad = _job(
        tmp_path,
        base_sha=base_sha,
        tree_sha=tree_sha,
        allowed_paths=("src",),
    )
    narrow = _job(
        tmp_path,
        base_sha=base_sha,
        tree_sha=tree_sha,
        allowed_paths=("src/value.py",),
    )

    _run(planner.plan(broad))
    _run(planner.plan(narrow))

    assert len(provider.requests) == 2
    assert provider.requests[0].request_id != provider.requests[1].request_id


def test_planner_rejects_stale_tree_identity_before_model_effect(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha, _tree_sha = _repository(tmp_path)
    provider = _Provider(_valid_response())
    planner = _planner(repository, provider)
    job = _job(tmp_path, base_sha=base_sha, tree_sha="f" * 40)

    with pytest.raises(
        ModelGatewayLocalCodingPlannerError,
        match="tree identity differs",
    ):
        _run(planner.plan(job))

    assert provider.requests == []


def test_planner_rejects_non_utf8_source_before_model_effect(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha, tree_sha = _repository(tmp_path, content=b"\xff\xfe\x80")
    provider = _Provider(_valid_response())
    planner = _planner(repository, provider)
    job = _job(tmp_path, base_sha=base_sha, tree_sha=tree_sha)

    with pytest.raises(
        ModelGatewayLocalCodingPlannerError,
        match="non-UTF-8 source",
    ):
        _run(planner.plan(job))

    assert provider.requests == []


def test_planner_rejects_symlink_tree_entry_before_model_effect(
    tmp_path: pathlib.Path,
) -> None:
    repository, _base_sha, _tree_sha = _repository(tmp_path)
    blob = _git(repository, "hash-object", "-w", "src/value.py")
    _git(
        repository,
        "update-index",
        "--add",
        "--cacheinfo",
        f"120000,{blob},src/link.py",
    )
    _git(repository, "commit", "-m", "add synthetic symlink entry")
    base_sha = _git(repository, "rev-parse", "HEAD")
    tree_sha = _git(repository, "rev-parse", f"{base_sha}^{{tree}}")
    provider = _Provider(_valid_response())
    planner = _planner(repository, provider)
    job = _job(tmp_path, base_sha=base_sha, tree_sha=tree_sha)

    with pytest.raises(
        ModelGatewayLocalCodingPlannerError,
        match="symlink, submodule or unsupported entry",
    ):
        _run(planner.plan(job))

    assert provider.requests == []


def test_planner_bounds_git_stdout_before_model_effect(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha, tree_sha = _repository(tmp_path)
    provider = _Provider(_valid_response())
    planner = _planner(
        repository,
        provider,
        max_git_output_bytes=40,
    )
    job = _job(tmp_path, base_sha=base_sha, tree_sha=tree_sha)

    with pytest.raises(
        ModelGatewayLocalCodingPlannerError,
        match="Git command output exceeds the byte limit",
    ):
        _run(planner.plan(job))

    assert provider.requests == []


def test_planner_rejects_oversized_source_before_model_effect(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha, tree_sha = _repository(tmp_path, content=b"123456789")
    provider = _Provider(_valid_response())
    planner = _planner(repository, provider, max_file_bytes=8, max_source_bytes=16)
    job = _job(tmp_path, base_sha=base_sha, tree_sha=tree_sha)

    with pytest.raises(
        ModelGatewayLocalCodingPlannerError,
        match="source file exceeds",
    ):
        _run(planner.plan(job))

    assert provider.requests == []


@pytest.mark.parametrize(
    ("response_text", "message"),
    [
        (
            '{"schema":"nika.local-coding-plan:v1","schema":"duplicate","edits":[]}',
            "strict JSON",
        ),
        (
            '{"schema":"nika.local-coding-plan:v1","edits":[], "extra": true}',
            "unexpected fields",
        ),
        (
            '{"schema":"wrong","edits":[{"path":"src/value.py","content":"x"}]}',
            "schema is unsupported",
        ),
        (
            '{"schema":"nika.local-coding-plan:v1","edits":'
            '[{"path":"docs/outside.md","content":"x"}]}',
            "escapes allowed scope",
        ),
        (
            '{"schema":"nika.local-coding-plan:v1","edits":'
            '[{"path":".github/workflows/unsafe.yml","content":"x"}]}',
            "escapes allowed scope",
        ),
    ],
)
def test_planner_rejects_untrusted_response_shapes_and_paths(
    tmp_path: pathlib.Path,
    response_text: str,
    message: str,
) -> None:
    repository, base_sha, tree_sha = _repository(tmp_path)
    provider = _Provider(response_text)
    planner = _planner(repository, provider)
    job = _job(tmp_path, base_sha=base_sha, tree_sha=tree_sha)

    with pytest.raises(ModelGatewayLocalCodingPlannerError, match=message):
        _run(planner.plan(job))

    assert len(provider.requests) == 1


def test_planner_rejects_control_plane_edit_even_when_scope_is_broad(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha, tree_sha = _repository(tmp_path)
    provider = _Provider(
        _valid_response(
            path=".github/workflows/unsafe.yml",
            content="name: unsafe\n",
        )
    )
    planner = _planner(repository, provider)
    job = _job(
        tmp_path,
        base_sha=base_sha,
        tree_sha=tree_sha,
        allowed_paths=("src", ".github"),
    )

    with pytest.raises(
        ModelGatewayLocalCodingPlannerError,
        match="edit is not admissible",
    ):
        _run(planner.plan(job))


def test_planner_rejects_case_drift_for_existing_path(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha, tree_sha = _repository(tmp_path)
    provider = _Provider(_valid_response(path="src/VALUE.py"))
    planner = _planner(repository, provider)
    job = _job(tmp_path, base_sha=base_sha, tree_sha=tree_sha)

    with pytest.raises(
        ModelGatewayLocalCodingPlannerError,
        match="changes existing path casing",
    ):
        _run(planner.plan(job))


def test_planner_rejects_oversized_model_response(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha, tree_sha = _repository(tmp_path)
    provider = _Provider(_valid_response())
    planner = _planner(repository, provider, max_response_bytes=32)
    job = _job(tmp_path, base_sha=base_sha, tree_sha=tree_sha)

    with pytest.raises(
        ModelGatewayLocalCodingPlannerError,
        match="response exceeds the byte limit",
    ):
        _run(planner.plan(job))

    assert len(provider.requests) == 1


def test_planner_requires_explicit_modelgateway_route(
    tmp_path: pathlib.Path,
) -> None:
    repository, _base_sha, _tree_sha = _repository(tmp_path)
    gateway = ModelGateway()
    gateway.register(_Provider(_valid_response()))

    with pytest.raises(ValueError, match="explicit ModelGateway provider route"):
        ModelGatewayLocalCodingPlanner(
            gateway=gateway,
            repositories={"repo-1": repository},
        )
