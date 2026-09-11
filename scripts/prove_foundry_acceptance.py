from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SCHEMA = "nika-foundry-local-acceptance-v1"
CHILD_SCHEMA = "nika-foundry-local-physical-proof-v3"
FIXTURE_PROMPT = "Reply with exactly: NIKA_FOUNDRY_LOCAL_OK"
FIXTURE_RESPONSE = "NIKA_FOUNDRY_LOCAL_OK"
PROVIDER_ID = "foundry-local"
PROVIDER_KIND = "local"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _tracked_worktree_is_clean(repo_root: Path) -> bool:
    return not _git(repo_root, "status", "--porcelain", "--untracked-files=all")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _require_normalized_text(value: Any, *, label: str, max_length: int) -> str:
    _require(isinstance(value, str), f"{label} must be text")
    _require(
        bool(value)
        and value == value.strip()
        and len(value) <= max_length
        and not any(ord(char) < 32 for char in value),
        f"{label} must be normalized non-empty text",
    )
    return value


def _external_output_path(output: Path, *, repo_root: Path) -> Path:
    candidate = output.expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    resolved = candidate.resolve(strict=False)
    resolved_repo = repo_root.resolve(strict=True)
    try:
        resolved.relative_to(resolved_repo)
    except ValueError:
        return resolved
    raise RuntimeError("--output must resolve outside the repository worktree")


def _write_acceptance_evidence(
    evidence: dict[str, Any], *, output: Path, repo_root: Path
) -> Path:
    resolved_output = _external_output_path(output, repo_root=repo_root)
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    resolved_output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return resolved_output


def _child_environment(repo_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str((repo_root / "src").resolve())
    env["PYTHONNOUSERSITE"] = "1"
    return env


def _validate_model_evidence(
    evidence: dict[str, Any], *, model: str, model_id: str, require_loaded: bool | None
) -> None:
    _require(evidence.get("alias") == model, "Foundry model alias changed during acceptance")
    _require(evidence.get("model_id") == model_id, "Foundry model ID changed during acceptance")
    _require(evidence.get("cached") is True, "Foundry model is not explicitly available in cache")
    if require_loaded is not None:
        _require(
            evidence.get("loaded") is require_loaded,
            "Foundry model load state does not match acceptance lifecycle",
        )


def _validate_response(response: dict[str, Any], *, model: str) -> None:
    _require(response.get("provider_id") == PROVIDER_ID, "silent provider fallback detected")
    _require(response.get("provider_kind") == PROVIDER_KIND, "unexpected provider kind")
    _require(response.get("model") == model, "response model does not match requested alias")
    _require(response.get("text_nonempty") is True, "Foundry returned an empty response")
    _require(
        response.get("text_length") == len(FIXTURE_RESPONSE),
        "fixture response length mismatch",
    )
    _require(
        response.get("text_sha256") == _sha256_text(FIXTURE_RESPONSE),
        "fixture response content mismatch",
    )
    latency = response.get("latency_ms")
    _require(
        isinstance(latency, (int, float))
        and not isinstance(latency, bool)
        and math.isfinite(float(latency))
        and float(latency) >= 0,
        "Foundry response latency is invalid",
    )
    usage = response.get("usage")
    _require(isinstance(usage, dict), "Foundry usage evidence is missing")


def validate_child_evidence(
    evidence: dict[str, Any], *, model: str, model_id: str, model_license: str
) -> None:
    _require(evidence.get("schema") == CHILD_SCHEMA, "unexpected child Foundry proof schema")
    _require(evidence.get("platform", {}).get("system") == "Windows", "child proof is not Windows")
    _require(
        evidence.get("sdk", {}).get("package") == "foundry-local-sdk-winml",
        "Windows Foundry WinML SDK is not the active package",
    )
    _require(evidence.get("expected_model_id") == model_id, "expected model ID evidence mismatch")
    _require(
        evidence.get("model_license_review") == model_license,
        "model license evidence mismatch",
    )
    _require(
        evidence.get("explicit_model_download_action_executed") is False,
        "acceptance harness must never acquire/download a model",
    )
    _require(evidence.get("model_gateway_path_used") is True, "ModelGateway path was not used")
    _require(evidence.get("physical_inference_executed") is True, "physical inference not proven")
    _require(
        evidence.get("unload_reload_proof_executed") is True,
        "load lifecycle proof incomplete",
    )

    _validate_model_evidence(
        evidence["model_before"], model=model, model_id=model_id, require_loaded=False
    )
    _validate_model_evidence(
        evidence["model_after_first_inference"], model=model, model_id=model_id, require_loaded=True
    )
    _validate_model_evidence(
        evidence["model_final"], model=model, model_id=model_id, require_loaded=False
    )
    _validate_response(evidence["first_inference"], model=model)
    _validate_response(evidence["reload_inference"], model=model)

    resource_keys = (
        "resources_before",
        "resources_after_first_inference",
        "resources_after_reload_inference",
    )
    for key in resource_keys:
        _require(isinstance(evidence.get(key), dict) and evidence[key], f"{key} is missing")


def _child_command(args: argparse.Namespace, *, output: Path, repo_root: Path) -> list[str]:
    command = [
        sys.executable,
        "-P",
        str(repo_root / "scripts" / "prove_foundry_local.py"),
        "--model",
        args.model,
        "--model-id",
        args.model_id,
        "--model-license",
        args.model_license,
        "--prompt",
        FIXTURE_PROMPT,
        "--timeout",
        str(args.timeout),
        "--output",
        str(output),
    ]
    if args.hash_model_cache:
        command.append("--hash-model-cache")
    if args.max_cpu_percent is not None:
        command.extend(("--max-cpu-percent", str(args.max_cpu_percent)))
    if args.max_memory_percent is not None:
        command.extend(("--max-memory-percent", str(args.max_memory_percent)))
    if args.min_available_memory_gb is not None:
        command.extend(("--min-available-memory-gb", str(args.min_available_memory_gb)))
    return command


def _run_child(args: argparse.Namespace, *, output: Path, repo_root: Path) -> dict[str, Any]:
    try:
        result = subprocess.run(
            _child_command(args, output=output, repo_root=repo_root),
            cwd=repo_root,
            env=_child_environment(repo_root),
            check=False,
            capture_output=True,
            text=True,
            timeout=args.timeout + 120.0,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "physical Foundry proof child exceeded the outer process deadline"
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(
            "physical Foundry proof child failed with exit code "
            f"{result.returncode}; inspect local console"
        )
    _require(output.is_file(), "physical Foundry proof child did not write evidence")
    payload = json.loads(output.read_text(encoding="utf-8"))
    _require(isinstance(payload, dict), "physical Foundry child evidence must be a JSON object")
    return payload


def run_acceptance(args: argparse.Namespace, *, repo_root: Path) -> dict[str, Any]:
    _require(platform.system() == "Windows", "real Foundry acceptance must run on Windows")
    _require_normalized_text(args.model, label="model alias", max_length=512)
    _require_normalized_text(args.model_id, label="model ID", max_length=1024)
    _require_normalized_text(
        args.model_license,
        label="reviewed model-license reference",
        max_length=2048,
    )
    _require(
        _tracked_worktree_is_clean(repo_root),
        "worktree must be clean, including untracked files, for SHA-bound proof",
    )
    start_sha = _git(repo_root, "rev-parse", "HEAD")
    _require(len(start_sha) == 40, "could not resolve exact Nika commit SHA")

    with tempfile.TemporaryDirectory(prefix="nika-foundry-acceptance-") as temp_dir:
        temp_root = Path(temp_dir)
        first = _run_child(args, output=temp_root / "run-1.json", repo_root=repo_root)
        validate_child_evidence(
            first, model=args.model, model_id=args.model_id, model_license=args.model_license
        )
        middle_sha = _git(repo_root, "rev-parse", "HEAD")
        _require(middle_sha == start_sha, "Nika SHA changed after first Foundry run")

        second = _run_child(args, output=temp_root / "run-2.json", repo_root=repo_root)
        validate_child_evidence(
            second, model=args.model, model_id=args.model_id, model_license=args.model_license
        )

    final_sha = _git(repo_root, "rev-parse", "HEAD")
    _require(final_sha == start_sha, "Nika SHA changed during Foundry acceptance")
    _require(
        _tracked_worktree_is_clean(repo_root),
        "worktree changed or gained untracked files during acceptance",
    )

    proof_script = repo_root / "scripts" / "prove_foundry_local.py"
    return {
        "schema": SCHEMA,
        "nika_sha": start_sha,
        "harness": {
            "path": "scripts/prove_foundry_acceptance.py",
            "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "child_path": "scripts/prove_foundry_local.py",
            "child_sha256": hashlib.sha256(proof_script.read_bytes()).hexdigest(),
            "source_binding": {
                "safe_path": True,
                "pythonpath": "src",
                "user_site_disabled": True,
            },
        },
        "model": {
            "provider_id": PROVIDER_ID,
            "alias": args.model,
            "model_id": args.model_id,
            "license_review": args.model_license,
            "acquisition_state": "cached_before_harness",
            "acquisition_allowed_by_harness": False,
        },
        "fixture": {
            "prompt": FIXTURE_PROMPT,
            "expected_response": FIXTURE_RESPONSE,
            "validated_real_response": FIXTURE_RESPONSE,
            "expected_response_sha256": _sha256_text(FIXTURE_RESPONSE),
        },
        "restart_rerun": {
            "fresh_child_processes": 2,
            "same_nika_sha": True,
            "same_model_identity": True,
            "same_fixture_response": True,
        },
        "run_1": first,
        "run_2": second,
        "no_silent_download": True,
        "no_silent_fallback": True,
        "physical_windows_foundry_inference_proven": True,
        "retest_runtime_required": False,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the existing real Foundry Local proof twice in fresh child processes and bind "
            "the resulting evidence to one exact clean Nika SHA. This harness never "
            "downloads models."
        )
    )
    parser.add_argument("--model", required=True, help="Exact Foundry Local model alias")
    parser.add_argument("--model-id", required=True, help="Exact public Foundry model variant ID")
    parser.add_argument(
        "--model-license",
        required=True,
        help="Human-reviewed model license identifier or evidence reference; never inferred",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--hash-model-cache", action="store_true")
    parser.add_argument("--max-cpu-percent", type=float)
    parser.add_argument("--max-memory-percent", type=float)
    parser.add_argument("--min-available-memory-gb", type=float)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Aggregate evidence JSON path; must resolve outside the repository worktree",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("--timeout must be finite and greater than zero")
    repo_root = Path(__file__).resolve().parents[1]
    output = _external_output_path(args.output, repo_root=repo_root)
    evidence = run_acceptance(args, repo_root=repo_root)
    output = _write_acceptance_evidence(evidence, output=output, repo_root=repo_root)
    print(f"Foundry Local acceptance evidence written to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
