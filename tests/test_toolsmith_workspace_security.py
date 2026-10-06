from __future__ import annotations

import os
from pathlib import Path

import pytest

from nika_core.toolsmith import IsolationClass
from nika_core.toolsmith.workspace_security import (
    ProductionIntegritySnapshot,
    SterileGitPlan,
    WorkspacePathPolicy,
    WorkspaceSecurityError,
    assert_production_integrity,
    collect_tree_evidence,
    ensure_path_policy,
    make_sterile_git_plan,
    normalize_job_relative_path,
    sterile_git_environment,
    validate_typed_argv,
)


@pytest.mark.parametrize(
    "value",
    [
        "../escape.py",
        "C:/repo/file.py",
        "C:\\repo\\file.py",
        "//server/share/file.py",
        "\\\\server\\share/file.py",
        ".git/config",
        "src/.GIT/config",
        "src/file.py:secret",
        "CON",
        "con.txt",
        "aux.py",
        "COM1.log",
        "LPT9",
        "src/trailing. ",
        " src/file.py",
        "src/file.py ",
        "",
    ],
)
def test_windows_unsafe_paths_fail_closed(value: str) -> None:
    with pytest.raises(WorkspaceSecurityError):
        normalize_job_relative_path(value)


def test_relative_unicode_and_spaces_are_supported() -> None:
    path = normalize_job_relative_path("src/модулі/my file.py")
    assert path.as_posix() == "src/модулі/my file.py"


def test_path_policy_is_component_scoped() -> None:
    policy = WorkspacePathPolicy(("src/nika_core/toolsmith", "tests"))
    assert policy.allows("src/nika_core/toolsmith/new_adapter.py")
    assert policy.allows("tests/test_toolsmith_workspace_security.py")
    assert not policy.allows("src/nika_core/runtime/core.py")


def test_path_policy_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    link = root / "src"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable on this host")

    policy = WorkspacePathPolicy(("src",))
    with pytest.raises(WorkspaceSecurityError, match="symbolic links"):
        ensure_path_policy(root, "src/file.py", policy)


def test_sterile_git_environment_drops_credentials_and_git_overrides() -> None:
    source = {
        "PATH": "x",
        "SYSTEMROOT": "C:\\Windows",
        "GITHUB_TOKEN": "secret",
        "GH_TOKEN": "secret2",
        "GIT_ASKPASS": "askpass.exe",
        "GIT_CONFIG_GLOBAL": "attacker.cfg",
        "SSH_AUTH_SOCK": "agent",
        "PYTHONPATH": "poison",
    }
    environment = sterile_git_environment(source)
    assert environment["PATH"] == "x"
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_GLOBAL"] == ("NUL" if os.name == "nt" else "/dev/null")
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GCM_INTERACTIVE"] == "never"
    assert "GITHUB_TOKEN" not in environment
    assert "GH_TOKEN" not in environment
    assert "GIT_ASKPASS" not in environment
    assert environment["GIT_CONFIG_GLOBAL"] != "attacker.cfg"
    assert "SSH_AUTH_SOCK" not in environment
    assert "PYTHONPATH" not in environment


def test_private_git_plan_separates_production_metadata(tmp_path: Path) -> None:
    production = tmp_path / "production"
    job_root = tmp_path / "jobs" / "job-1"
    production.mkdir()
    job_root.mkdir(parents=True)

    plan = make_sterile_git_plan(
        repository_root=production,
        job_root=job_root,
        branch_name="toolsmith/job-1",
        base_sha="a" * 40,
        source_environment={"PATH": "x", "GITHUB_TOKEN": "secret"},
    )

    assert plan.private_git_dir == job_root.resolve() / "_nika_private_git"
    assert plan.worktree_root == job_root.resolve() / "worktree"
    assert plan.private_git_dir != production.resolve() / ".git"
    assert plan.private_git_dir != plan.worktree_root / ".git"
    assert plan.isolation_class is IsolationClass.POLICY_ONLY
    assert "credential.helper=" in plan.config_args
    assert "protocol.file.allow=never" in plan.config_args
    assert "protocol.ext.allow=never" in plan.config_args
    assert "GITHUB_TOKEN" not in plan.environment


def test_sterile_git_plan_rejects_noncanonical_execution_context(tmp_path: Path) -> None:
    production = tmp_path / "production-context"
    job_root = tmp_path / "jobs" / "job-context"
    production.mkdir()
    job_root.mkdir(parents=True)

    plan = make_sterile_git_plan(
        repository_root=production,
        job_root=job_root,
        branch_name="toolsmith/job-context",
        base_sha="a" * 40,
        source_environment={"PATH": "x"},
    )
    hostile_environment = dict(plan.environment)
    hostile_environment["GIT_CONFIG_COUNT"] = "1"
    hostile_environment["GIT_CONFIG_KEY_0"] = "core.hooksPath"
    hostile_environment["GIT_CONFIG_VALUE_0"] = str(tmp_path / "hooks")

    with pytest.raises(WorkspaceSecurityError, match="environment is not canonical"):
        SterileGitPlan(
            repository_root=plan.repository_root,
            private_git_dir=plan.private_git_dir,
            worktree_root=plan.worktree_root,
            branch_name=plan.branch_name,
            base_sha=plan.base_sha,
            environment=hostile_environment,
            config_args=plan.config_args,
        )

    with pytest.raises(WorkspaceSecurityError, match="config arguments are not canonical"):
        SterileGitPlan(
            repository_root=plan.repository_root,
            private_git_dir=plan.private_git_dir,
            worktree_root=plan.worktree_root,
            branch_name=plan.branch_name,
            base_sha=plan.base_sha,
            environment=plan.environment,
            config_args=("-c", f"core.hooksPath={tmp_path / 'hooks'}"),
        )


@pytest.mark.parametrize(
    "base_sha",
    (
        "a" * 39,
        "a" * 41,
        "g" * 40,
        ("a" * 39) + " ",
    ),
)
def test_sterile_git_plan_rejects_invalid_base_sha(
    tmp_path: Path,
    base_sha: str,
) -> None:
    production = tmp_path / "production"
    job_root = tmp_path / "jobs" / "job-invalid-base"
    production.mkdir()
    job_root.mkdir(parents=True)

    with pytest.raises(WorkspaceSecurityError, match="40-character hexadecimal SHA"):
        make_sterile_git_plan(
            repository_root=production,
            job_root=job_root,
            branch_name="toolsmith/job",
            base_sha=base_sha,
        )


def test_sterile_git_plan_rejects_behavioral_base_sha(tmp_path: Path) -> None:
    class BaseSha(str):
        pass

    production = tmp_path / "production"
    job_root = tmp_path / "jobs" / "job-behavioral-base"
    production.mkdir()
    job_root.mkdir(parents=True)

    with pytest.raises(WorkspaceSecurityError, match="40-character hexadecimal SHA"):
        make_sterile_git_plan(
            repository_root=production,
            job_root=job_root,
            branch_name="toolsmith/job",
            base_sha=BaseSha("a" * 40),
        )


def test_sterile_git_plan_preserves_uppercase_hex_base_sha(tmp_path: Path) -> None:
    production = tmp_path / "production"
    job_root = tmp_path / "jobs" / "job-uppercase-base"
    production.mkdir()
    job_root.mkdir(parents=True)

    plan = make_sterile_git_plan(
        repository_root=production,
        job_root=job_root,
        branch_name="toolsmith/job",
        base_sha="A" * 40,
    )

    assert plan.base_sha == "A" * 40


@pytest.mark.parametrize(
    "branch_name",
    (
        " toolsmith/job",
        "toolsmith/job ",
        "-toolsmith/job",
        "toolsmith\njob",
        "toolsmith\x7fjob",
    ),
)
def test_sterile_git_plan_rejects_ambiguous_branch_identity(
    tmp_path: Path,
    branch_name: str,
) -> None:
    production = tmp_path / "production"
    job_root = tmp_path / "jobs" / "job-ambiguous-branch"
    production.mkdir()
    job_root.mkdir(parents=True)

    with pytest.raises(WorkspaceSecurityError, match="control data"):
        make_sterile_git_plan(
            repository_root=production,
            job_root=job_root,
            branch_name=branch_name,
            base_sha="a" * 40,
        )


def test_sterile_git_plan_rejects_behavioral_string_branch_name(tmp_path: Path) -> None:
    class BranchName(str):
        pass

    production = tmp_path / "production"
    job_root = tmp_path / "jobs" / "job-behavioral-branch"
    production.mkdir()
    job_root.mkdir(parents=True)

    with pytest.raises(WorkspaceSecurityError, match="control data"):
        make_sterile_git_plan(
            repository_root=production,
            job_root=job_root,
            branch_name=BranchName("toolsmith/job"),
            base_sha="a" * 40,
        )


@pytest.mark.parametrize("separator", ("\u0085", "\u2028", "\u2029"))
def test_sterile_git_plan_rejects_unicode_branch_line_boundaries(
    tmp_path: Path,
    separator: str,
) -> None:
    production = tmp_path / "production"
    job_root = tmp_path / "jobs" / "job-line-boundary"
    production.mkdir()
    job_root.mkdir(parents=True)

    with pytest.raises(WorkspaceSecurityError, match="control data"):
        make_sterile_git_plan(
            repository_root=production,
            job_root=job_root,
            branch_name=f"toolsmith{separator}branch",
            base_sha="a" * 40,
        )


def test_sterile_git_plan_preserves_safe_unicode_branch_name(tmp_path: Path) -> None:
    production = tmp_path / "production"
    job_root = tmp_path / "jobs" / "job-unicode"
    production.mkdir()
    job_root.mkdir(parents=True)

    plan = make_sterile_git_plan(
        repository_root=production,
        job_root=job_root,
        branch_name="toolsmith/гілка",
        base_sha="a" * 40,
    )

    assert plan.branch_name == "toolsmith/гілка"


def test_job_workspace_cannot_live_inside_production_repository(tmp_path: Path) -> None:
    production = tmp_path / "production"
    production.mkdir()
    nested = production / "jobs" / "job-1"
    nested.mkdir(parents=True)
    with pytest.raises(WorkspaceSecurityError, match="fully disjoint"):
        make_sterile_git_plan(
            repository_root=production,
            job_root=nested,
            branch_name="toolsmith/job-1",
            base_sha="b" * 40,
        )


def test_typed_argv_rejects_shells_and_non_allowlisted_executables() -> None:
    with pytest.raises(WorkspaceSecurityError, match="shell"):
        validate_typed_argv(("powershell.exe", "-Command", "pytest"), {"powershell.exe"})
    with pytest.raises(WorkspaceSecurityError, match="allowlisted"):
        validate_typed_argv(("python.exe", "-m", "pytest"), {"git.exe"})


def test_typed_argv_preserves_literal_arguments() -> None:
    argv = ("python.exe", "-m", "pytest", "tests/test file.py", "value;not-a-shell-command")
    assert validate_typed_argv(argv, {"python.exe"}) == argv


def test_tree_evidence_is_deterministic_and_content_sensitive(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    nested = root / "src"
    nested.mkdir(parents=True)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    (nested / "модуль.py").write_text("print('ok')\n", encoding="utf-8")

    first = collect_tree_evidence(root)
    second = collect_tree_evidence(root)
    assert first == second
    assert tuple(item.path for item in first.files) == ("README.md", "src/модуль.py")

    (nested / "модуль.py").write_text("print('changed')\n", encoding="utf-8")
    changed = collect_tree_evidence(root)
    assert changed.digest != first.digest


def test_tree_evidence_refuses_dot_git(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    git_dir = root / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "config").write_text("unsafe", encoding="utf-8")
    with pytest.raises(WorkspaceSecurityError, match=".git"):
        collect_tree_evidence(root)


def test_tree_evidence_limits_file_size(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "large.bin").write_bytes(b"12345")
    with pytest.raises(WorkspaceSecurityError, match="size limit"):
        collect_tree_evidence(root, max_file_bytes=4)


def test_tree_evidence_refuses_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("outside", encoding="utf-8")
    link = root / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable on this host")
    with pytest.raises(WorkspaceSecurityError, match="symlinks"):
        collect_tree_evidence(root)


def test_production_integrity_snapshot_rejects_behavioral_identity_carriers() -> None:
    class BaseSha(str):
        pass

    class TreeDigest(str):
        pass

    with pytest.raises(WorkspaceSecurityError, match="40-character hexadecimal SHA"):
        ProductionIntegritySnapshot(BaseSha("c" * 40), "d" * 64)

    with pytest.raises(WorkspaceSecurityError, match="tree_digest"):
        ProductionIntegritySnapshot("c" * 40, TreeDigest("d" * 64))


def test_production_integrity_must_match_exactly() -> None:
    before = ProductionIntegritySnapshot("c" * 40, "d" * 64)
    assert_production_integrity(before, before)

    with pytest.raises(WorkspaceSecurityError, match="identity changed"):
        assert_production_integrity(
            before,
            ProductionIntegritySnapshot("c" * 40, "e" * 64),
        )
