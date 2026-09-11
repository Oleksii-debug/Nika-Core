from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_PATH = Path(__file__).parents[1] / "scripts" / "prove_foundry_acceptance.py"
SPEC = importlib.util.spec_from_file_location("prove_foundry_acceptance_trust", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


def _args() -> SimpleNamespace:
    return SimpleNamespace(
        model="model-alias",
        model_id="model-id@1",
        model_license="reviewed-license-ref",
        timeout=30.0,
        hash_model_cache=False,
        max_cpu_percent=None,
        max_memory_percent=None,
        min_available_memory_gb=None,
    )


def test_untracked_import_shadowing_blocks_before_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(harness.platform, "system", lambda: "Windows")

    git_calls: list[tuple[str, ...]] = []

    def fake_git(_repo_root: Path, *args: str) -> str:
        git_calls.append(args)
        if args and args[0] == "status":
            assert "--untracked-files=all" in args
            return "?? scripts/nika_core.py"
        raise AssertionError("SHA lookup must not run after dirty-worktree rejection")

    def child_must_not_run(*args, **kwargs):
        raise AssertionError("child proof must not run from an untrusted checkout")

    monkeypatch.setattr(harness, "_git", fake_git)
    monkeypatch.setattr(harness, "_run_child", child_must_not_run)

    with pytest.raises(RuntimeError, match="untracked files"):
        harness.run_acceptance(_args(), repo_root=tmp_path)

    assert git_calls == [("status", "--porcelain", "--untracked-files=all")]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model", ""),
        ("model", " model-alias"),
        ("model", "m" * 513),
        ("model_id", ""),
        ("model_id", "model-id@1 "),
        ("model_id", "m" * 1025),
        ("model_license", ""),
        ("model_license", "   "),
        ("model_license", "license\nref"),
        ("model_license", "l" * 2049),
    ],
)
def test_identity_authority_is_normalized_and_bounded_before_git_or_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    args = _args()
    setattr(args, field, value)
    monkeypatch.setattr(harness.platform, "system", lambda: "Windows")

    def git_must_not_run(*args, **kwargs):
        raise AssertionError("git must not run before identity authority is validated")

    def child_must_not_run(*args, **kwargs):
        raise AssertionError("child proof must not run with invalid identity authority")

    monkeypatch.setattr(harness, "_git", git_must_not_run)
    monkeypatch.setattr(harness, "_run_child", child_must_not_run)

    with pytest.raises(RuntimeError, match="normalized non-empty text"):
        harness.run_acceptance(args, repo_root=tmp_path)
