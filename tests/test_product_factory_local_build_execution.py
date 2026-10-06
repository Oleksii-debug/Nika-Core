from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import subprocess
import sys

import pytest

import nika_core.product_factory_local_build_execution as local_build
from nika_core.product_factory_build_execution import (
    BuildExecutionDispatch,
    BuildExecutionPortError,
    ExecutionGrant,
)
from nika_core.product_factory_local_build_execution import (
    ContainedLocalBuildExecutionNode,
    LocalBuildArtifactBinding,
    local_build_platform,
)
from nika_core.product_factory_local_coding import ContainedLocalCodingPolicy
from nika_core.product_factory_local_repository_binding import (
    ProductFactoryLocalRepositoryBinding,
)
from nika_core.toolsmith.contracts import IsolationClass, ResourceBudget
from nika_core.toolsmith.execution import ProcessExecutionResult


class _Bindings:
    def __init__(self, binding: ProductFactoryLocalRepositoryBinding) -> None:
        self.binding = binding

    def require(
        self,
        project_id: str,
        repository_id: str,
    ) -> ProductFactoryLocalRepositoryBinding:
        assert project_id == self.binding.project_id
        assert repository_id == self.binding.repository_id
        return self.binding


def _git_executable() -> str:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git executable is required for contained-local build tests")
    return str(pathlib.Path(executable).resolve())


def _run_git(root: pathlib.Path, *args: str) -> str:
    result = subprocess.run(
        [_git_executable(), *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _repository(
    tmp_path: pathlib.Path,
    *,
    existing_artifact: bool = False,
) -> tuple[pathlib.Path, str]:
    root = tmp_path / "repository"
    root.mkdir()
    _run_git(root, "init")
    _run_git(root, "config", "user.name", "Nika Test")
    _run_git(root, "config", "user.email", "nika@example.invalid")
    component = root / "component"
    component.mkdir()
    (component / "input.txt").write_text("seed", encoding="utf-8")
    if existing_artifact:
        artifact = component / "dist" / "artifact.txt"
        artifact.parent.mkdir()
        artifact.write_text("old", encoding="utf-8")
    _run_git(root, "add", ".")
    _run_git(root, "commit", "-m", "fixture")
    return root, _run_git(root, "rev-parse", "HEAD").casefold()


def _node(
    tmp_path: pathlib.Path,
    root: pathlib.Path,
) -> ContainedLocalBuildExecutionNode:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    python = str(pathlib.Path(sys.executable).resolve())
    binding = ProductFactoryLocalRepositoryBinding(
        project_id="project",
        repository_id="repo",
        provider="github",
        locator="owner/repo",
        root=root.resolve(),
        binding_version=1,
        updated_at="2026-10-06T00:00:00+00:00",
    )
    return ContainedLocalBuildExecutionNode(
        node_id="local-test",
        platform=local_build_platform(),
        workspace_parent=workspace,
        repository_bindings=_Bindings(binding),
        policy=ContainedLocalCodingPolicy(
            allowed_executables=(python,),
            resource_budget=ResourceBudget(
                timeout_seconds=30,
                max_output_bytes=1024 * 1024,
                max_changed_files=32,
            ),
        ),
        artifact_bindings=(
            LocalBuildArtifactBinding(
                command_id="build",
                artifact_relpath="component/dist/artifact.txt",
            ),
        ),
        git_executable=_git_executable(),
        source_environment={},
    )


def _dispatch(
    source_sha: str,
    argv: tuple[str, ...],
    *,
    network_scopes: tuple[str, ...] = (),
    credential_refs: tuple[str, ...] = (),
) -> BuildExecutionDispatch:
    grant = ExecutionGrant(
        project_id="project",
        repository_id="repo",
        work_id="work",
        workspace_relpath="component",
        allowed_node_ids=("local-test",),
        network_scopes=network_scopes,
        credential_refs=credential_refs,
        command_id="build",
        argv=argv,
        authority_evidence_refs=("authority:test",),
    )
    return BuildExecutionDispatch(
        dispatch_id="dispatch:project:work:1",
        project_id="project",
        work_id="work",
        node_id="local-test",
        platform=local_build_platform(),
        source_sha=source_sha,
        grant=grant,
        attempt=1,
    )


def _build_argv() -> tuple[str, ...]:
    python = str(pathlib.Path(sys.executable).resolve())
    script = (
        "from pathlib import Path; "
        "Path('dist').mkdir(exist_ok=True); "
        "Path('dist/artifact.txt').write_text("
        "Path('input.txt').read_text(encoding='utf-8') + '-built', "
        "encoding='utf-8')"
    )
    return (python, "-c", script)


def test_contained_local_build_executes_exact_source_and_recovers_receipt(
    tmp_path: pathlib.Path,
) -> None:
    root, source_sha = _repository(tmp_path)
    node = _node(tmp_path, root)
    dispatch = _dispatch(source_sha, _build_argv())

    result = node.run(dispatch)

    assert result.succeeded is True
    assert result.uncertain is False
    assert result.source_sha == source_sha
    assert result.artifact_digest == hashlib.sha256(b"seed-built").hexdigest()
    assert root.joinpath("component", "dist", "artifact.txt").exists() is False

    changed = node.collect(dispatch, result)
    assert tuple(item.path for item in changed) == ("component/dist/artifact.txt",)
    assert changed[0].sha256 == result.artifact_digest
    assert changed[0].size_bytes == len(b"seed-built")

    restarted = _node(tmp_path, root)
    assert restarted.inspect(dispatch) == result
    assert restarted.collect(dispatch, result) == changed
    assert not tuple((tmp_path / "workspace").glob("pf5-build-*"))


@pytest.mark.parametrize(
    ("network_scopes", "credential_refs"),
    [
        (("pypi.org",), ()),
        ((), ("credref:package-index",)),
    ],
)
def test_contained_local_build_rejects_unenforced_external_scope_before_effect(
    tmp_path: pathlib.Path,
    network_scopes: tuple[str, ...],
    credential_refs: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, source_sha = _repository(tmp_path)
    node = _node(tmp_path, root)
    calls = 0

    def forbidden(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("process must not launch")

    monkeypatch.setattr(local_build, "run_typed_process", forbidden)
    dispatch = _dispatch(
        source_sha,
        _build_argv(),
        network_scopes=network_scopes,
        credential_refs=credential_refs,
    )

    with pytest.raises(BuildExecutionPortError):
        node.run(dispatch)

    assert calls == 0
    assert not tuple((tmp_path / "workspace").glob("pf5-build-*"))
    assert not tuple((tmp_path / "workspace" / "_nika_pf5_build_node").glob("dispatch-*.lock"))


def test_contained_local_build_rejects_nonallowlisted_executable_before_effect(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, source_sha = _repository(tmp_path)
    node = _node(tmp_path, root)
    calls = 0

    def forbidden(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("process must not launch")

    monkeypatch.setattr(local_build, "run_typed_process", forbidden)
    argv = (_git_executable(), "--version")

    with pytest.raises(BuildExecutionPortError):
        node.run(_dispatch(source_sha, argv))

    assert calls == 0
    assert not tuple((tmp_path / "workspace" / "_nika_pf5_build_node").glob("dispatch-*.lock"))


def test_contained_local_build_records_definite_process_failure_for_restart(
    tmp_path: pathlib.Path,
) -> None:
    root, source_sha = _repository(tmp_path)
    node = _node(tmp_path, root)
    python = str(pathlib.Path(sys.executable).resolve())
    dispatch = _dispatch(source_sha, (python, "-c", "raise SystemExit(7)"))

    result = node.run(dispatch)

    assert result.succeeded is False
    assert result.uncertain is False
    assert result.artifact_digest
    assert result.evidence_refs[0].startswith("local-build-process:sha256:")

    restarted = _node(tmp_path, root)
    assert restarted.inspect(dispatch) == result
    assert restarted.collect(dispatch, result) == ()


def test_contained_local_build_requires_fresh_artifact_effect(
    tmp_path: pathlib.Path,
) -> None:
    root, source_sha = _repository(tmp_path, existing_artifact=True)
    node = _node(tmp_path, root)
    python = str(pathlib.Path(sys.executable).resolve())
    dispatch = _dispatch(source_sha, (python, "-c", "pass"))

    result = node.run(dispatch)

    assert result.succeeded is False
    assert result.uncertain is False
    assert result.evidence_refs[0].startswith(
        "local-build-artifact-not-produced:sha256:"
    )
    assert node.collect(dispatch, result) == ()


def test_contained_local_build_receipt_tamper_fails_closed(
    tmp_path: pathlib.Path,
) -> None:
    root, source_sha = _repository(tmp_path)
    node = _node(tmp_path, root)
    dispatch = _dispatch(source_sha, _build_argv())
    result = node.run(dispatch)
    assert result.succeeded

    receipts = tuple(
        (tmp_path / "workspace" / "_nika_pf5_build_node").glob("receipt-*.json")
    )
    assert len(receipts) == 1
    envelope = json.loads(receipts[0].read_text(encoding="utf-8"))
    envelope["checksum"] = "0" * 64
    receipts[0].write_text(
        json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    restarted = _node(tmp_path, root)
    with pytest.raises(BuildExecutionPortError):
        restarted.inspect(dispatch)


def test_contained_local_build_stale_claim_never_replays_effect_after_receipt_loss(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, source_sha = _repository(tmp_path)
    node = _node(tmp_path, root)
    dispatch = _dispatch(source_sha, _build_argv())
    calls = 0

    def fake_process(argv, *, cwd, **kwargs):
        nonlocal calls
        calls += 1
        artifact = cwd / "dist" / "artifact.txt"
        artifact.parent.mkdir(exist_ok=True)
        artifact.write_text("seed-built", encoding="utf-8")
        return ProcessExecutionResult(
            argv=tuple(argv),
            returncode=0,
            stdout="",
            stderr="",
            timed_out=False,
            cancelled=False,
            output_limit_exceeded=False,
            isolation_class=IsolationClass.PROCESS_CONTAINED
            if sys.platform.startswith("win")
            else IsolationClass.POLICY_ONLY,
        )

    def lose_receipt(self, fingerprint, result, changed_files):
        raise BuildExecutionPortError("simulated provider receipt write failure")

    monkeypatch.setattr(local_build, "run_typed_process", fake_process)
    monkeypatch.setattr(
        ContainedLocalBuildExecutionNode,
        "_save_receipt",
        lose_receipt,
    )

    with pytest.raises(BuildExecutionPortError):
        node.run(dispatch)
    assert calls == 1

    with pytest.raises(BuildExecutionPortError):
        node.run(dispatch)
    assert calls == 1
    assert node.inspect(dispatch) is None
