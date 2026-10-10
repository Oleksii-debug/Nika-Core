from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_PATH = Path(__file__).parents[1] / "scripts" / "prove_foundry_acceptance.py"
SPEC = importlib.util.spec_from_file_location("prove_foundry_acceptance", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


def _model(*, loaded: bool) -> dict[str, object]:
    return {
        "model_id": "model-id@1",
        "model_version": "1",
        "alias": "model-alias",
        "cached": True,
        "loaded": loaded,
        "cache_path_available": True,
    }


def _response() -> dict[str, object]:
    text = harness.FIXTURE_RESPONSE
    return {
        "provider_id": "foundry-local",
        "provider_kind": "local",
        "model": "model-alias",
        "text_nonempty": True,
        "text_length": len(text),
        "text_sha256": harness._sha256_text(text),
        "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
        "latency_ms": 12.5,
    }


def _child() -> dict[str, object]:
    return {
        "schema": harness.CHILD_SCHEMA,
        "platform": {"system": "Windows"},
        "sdk": {"package": "foundry-local-sdk-winml", "version": "1.2.4"},
        "model_license_review": "reviewed-license-ref",
        "expected_model_id": "model-id@1",
        "model_before": _model(loaded=False),
        "model_after_first_inference": _model(loaded=True),
        "model_final": _model(loaded=False),
        "resources_before": {"system_memory_percent": 20.0},
        "resources_after_first_inference": {"system_memory_percent": 21.0},
        "resources_after_reload_inference": {"system_memory_percent": 21.0},
        "first_inference": _response(),
        "reload_inference": _response(),
        "model_gateway_path_used": True,
        "explicit_model_download_action_executed": False,
        "physical_inference_executed": True,
        "unload_reload_proof_executed": True,
    }


def test_child_evidence_accepts_exact_real_contract() -> None:
    harness.validate_child_evidence(
        _child(),
        model="model-alias",
        model_id="model-id@1",
        model_license="reviewed-license-ref",
    )


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (
            lambda proof: proof.__setitem__("explicit_model_download_action_executed", True),
            "download",
        ),
        (lambda proof: proof["first_inference"].__setitem__("provider_id", "ollama"), "fallback"),
        (lambda proof: proof["model_before"].__setitem__("cached", False), "cache"),
        (lambda proof: proof["model_final"].__setitem__("loaded", True), "load state"),
        (lambda proof: proof["first_inference"].__setitem__("text_sha256", "0" * 64), "content"),
        (lambda proof: proof["first_inference"].__setitem__("latency_ms", float("nan")), "latency"),
    ],
)
def test_child_evidence_fails_closed(mutator, match: str) -> None:
    proof = _child()
    mutator(proof)
    with pytest.raises(RuntimeError, match=match):
        harness.validate_child_evidence(
            proof,
            model="model-alias",
            model_id="model-id@1",
            model_license="reviewed-license-ref",
        )


def test_child_command_never_grants_download_authority(tmp_path: Path) -> None:
    args = SimpleNamespace(
        model="model-alias",
        model_id="model-id@1",
        model_license="reviewed-license-ref",
        timeout=30.0,
        hash_model_cache=False,
        max_cpu_percent=None,
        max_memory_percent=None,
        min_available_memory_gb=None,
    )
    command = harness._child_command(args, output=tmp_path / "evidence.json", repo_root=tmp_path)
    assert command[1] == "-P"
    assert command[2] == str(tmp_path / "scripts" / "prove_foundry_local.py")
    assert "--allow-download" not in command
    assert command.count("--prompt") == 1
    assert command[command.index("--prompt") + 1] == harness.FIXTURE_PROMPT


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


def test_run_child_binds_repo_source_and_disables_user_site(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "src").mkdir()
    child_path = tmp_path / "scripts" / "prove_foundry_local.py"
    child_path.write_text("# child\n", encoding="utf-8")
    output = tmp_path / "child-evidence.json"
    observed: dict[str, object] = {}
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "foreign-source"))

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["env"] = kwargs["env"]
        output.write_text(json.dumps(_child()), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    proof = harness._run_child(_args(), output=output, repo_root=tmp_path)

    command = observed["command"]
    env = observed["env"]
    assert isinstance(command, list)
    assert command[1] == "-P"
    assert isinstance(env, dict)
    assert env["PYTHONPATH"] == str((tmp_path / "src").resolve())
    assert env["PYTHONNOUSERSITE"] == "1"
    assert proof["schema"] == harness.CHILD_SCHEMA


def test_run_acceptance_uses_two_child_processes_and_binds_sha(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "prove_foundry_local.py").write_text("# child\n", encoding="utf-8")
    calls: list[Path] = []
    exact_sha = "a" * 40

    monkeypatch.setattr(harness.platform, "system", lambda: "Windows")
    monkeypatch.setattr(harness, "_tracked_worktree_is_clean", lambda _: True)
    monkeypatch.setattr(harness, "_git", lambda *_args: exact_sha)

    def fake_run_child(args, *, output: Path, repo_root: Path):
        calls.append(output)
        return _child()

    monkeypatch.setattr(harness, "_run_child", fake_run_child)
    evidence = harness.run_acceptance(_args(), repo_root=tmp_path)

    assert len(calls) == 2
    assert calls[0] != calls[1]
    assert evidence["nika_sha"] == exact_sha
    assert evidence["restart_rerun"]["fresh_child_processes"] == 2
    assert evidence["model"]["acquisition_state"] == "cached_before_harness"
    assert evidence["fixture"]["validated_real_response"] == harness.FIXTURE_RESPONSE
    assert evidence["harness"]["source_binding"] == {
        "safe_path": True,
        "pythonpath": "src",
        "user_site_disabled": True,
    }
    assert evidence["no_silent_download"] is True
    assert evidence["no_silent_fallback"] is True


def test_run_acceptance_rejects_non_windows_before_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(harness.platform, "system", lambda: "Linux")

    def child_must_not_run(*args, **kwargs):
        raise AssertionError("child proof must not run on non-Windows host")

    monkeypatch.setattr(harness, "_run_child", child_must_not_run)
    with pytest.raises(RuntimeError, match="must run on Windows"):
        harness.run_acceptance(_args(), repo_root=tmp_path)


def test_run_acceptance_rejects_sha_drift_after_first_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "prove_foundry_local.py").write_text("# child\n", encoding="utf-8")
    shas = iter(("a" * 40, "b" * 40))

    monkeypatch.setattr(harness.platform, "system", lambda: "Windows")
    monkeypatch.setattr(harness, "_tracked_worktree_is_clean", lambda _: True)
    monkeypatch.setattr(harness, "_git", lambda *_args: next(shas))
    monkeypatch.setattr(harness, "_run_child", lambda *args, **kwargs: _child())

    with pytest.raises(RuntimeError, match="SHA changed after first"):
        harness.run_acceptance(_args(), repo_root=tmp_path)
