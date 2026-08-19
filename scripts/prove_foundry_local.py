from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import platform
import sys
from dataclasses import asdict
from pathlib import Path

from nika_core.model_gateway.contracts import (
    ModelDownloadAuthorization,
    ModelMessage,
    ModelRequest,
    ModelResourcePolicy,
    PrivacyClass,
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider
from nika_core.model_gateway.gateway import ModelGateway


def _installed_foundry_package() -> tuple[str, str]:
    for package_name in ("foundry-local-sdk-winml", "foundry-local-sdk"):
        try:
            return package_name, importlib.metadata.version(package_name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return "not-installed", "not-installed"


def _tree_sha256(root: Path) -> dict[str, object]:
    if not root.exists() or not root.is_dir():
        raise ValueError(f"model cache path is not a directory: {root}")
    digest = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=str):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                total_bytes += len(chunk)
        digest.update(b"\0")
        file_count += 1
    return {
        "algorithm": "sha256-tree-v1",
        "sha256": digest.hexdigest(),
        "file_count": file_count,
        "total_bytes": total_bytes,
    }


def _resource_snapshot() -> dict[str, object]:
    try:
        import psutil
    except ImportError as exc:
        raise RuntimeError(
            "physical Foundry proof requires psutil resource evidence; install Nika's 'agent' "
            "optional component together with 'embedded-ai'"
        ) from exc

    process = psutil.Process()
    memory = psutil.virtual_memory()
    cpu_times = process.cpu_times()
    return {
        "system_cpu_percent": float(psutil.cpu_percent(interval=None)),
        "system_memory_percent": float(memory.percent),
        "system_available_memory_bytes": int(memory.available),
        "system_total_memory_bytes": int(memory.total),
        "process_rss_bytes": int(process.memory_info().rss),
        "process_cpu_seconds": float(cpu_times.user + cpu_times.system),
    }


def _resource_delta(
    before: dict[str, object], after: dict[str, object]
) -> dict[str, float | int]:
    return {
        "process_rss_bytes_delta": int(after["process_rss_bytes"])
        - int(before["process_rss_bytes"]),
        "process_cpu_seconds_delta": float(after["process_cpu_seconds"])
        - float(before["process_cpu_seconds"]),
    }


def _response_evidence(response: object) -> dict[str, object]:
    text = str(getattr(response, "text"))
    usage = getattr(response, "usage")
    provider_kind = getattr(response, "provider_kind")
    return {
        "provider_id": getattr(response, "provider_id"),
        "provider_kind": provider_kind.value,
        "model": getattr(response, "model"),
        "text_nonempty": bool(text),
        "text_length": len(text),
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "usage": asdict(usage),
        "latency_ms": getattr(response, "latency_ms"),
    }


def _model_resource_policy(args: argparse.Namespace) -> ModelResourcePolicy | None:
    min_available_memory_bytes: int | None = None
    if args.min_available_memory_gb is not None:
        min_available_memory_bytes = int(args.min_available_memory_gb * 1024**3)
    if (
        args.max_cpu_percent is None
        and args.max_memory_percent is None
        and min_available_memory_bytes is None
    ):
        return None
    return ModelResourcePolicy(
        max_cpu_percent=args.max_cpu_percent,
        max_memory_percent=args.max_memory_percent,
        min_available_memory_bytes=min_available_memory_bytes,
    )


async def _run(args: argparse.Namespace) -> dict[str, object]:
    if platform.system() != "Windows":
        raise RuntimeError("physical Foundry acceptance proof must run on Windows")

    package_name, package_version = _installed_foundry_package()
    if package_name != "foundry-local-sdk-winml":
        raise RuntimeError(
            "physical Windows proof requires the adopted foundry-local-sdk-winml package"
        )

    resource_policy = _model_resource_policy(args)
    resource_observer = None
    if resource_policy is not None:
        try:
            from nika_core.resources.psutil_adapter import PsutilResourceObserver
        except ImportError as exc:
            raise RuntimeError(
                "configured model resource policy requires Nika's 'agent' optional component"
            ) from exc
        resource_observer = PsutilResourceObserver()

    provider = FoundryLocalProvider(
        default_model=args.model,
        expected_model_id=args.model_id,
        resource_policy=resource_policy,
        resource_observer=resource_observer,
    )
    gateway = ModelGateway()
    gateway.register(provider)

    before = provider.inspect_model()
    if before.loaded:
        raise RuntimeError(
            "selected model is already loaded by another Foundry consumer; unload it first so "
            "Nika can prove lifecycle ownership without disrupting another process consumer"
        )

    resources_before = _resource_snapshot()
    evidence: dict[str, object] = {
        "schema": "nika-foundry-local-physical-proof-v3",
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python": sys.version,
        },
        "sdk": {"package": package_name, "version": package_version},
        "model_license_review": args.model_license,
        "expected_model_id": args.model_id,
        "model_before": asdict(before),
        "resource_policy": asdict(resource_policy) if resource_policy is not None else None,
        "resources_before": resources_before,
        "model_gateway_path_used": False,
        "explicit_model_download_action_executed": False,
        "physical_inference_executed": False,
        "unload_reload_proof_executed": False,
    }

    first_close_completed = False
    final_close_completed = False
    try:
        if args.allow_download and not before.cached:
            download_evidence = await provider.download_model(
                ModelDownloadAuthorization(
                    provider_id="foundry-local",
                    model=args.model,
                    license_reference=args.model_license,
                    expected_model_id=args.model_id,
                ),
                timeout_seconds=args.download_timeout,
            )
            evidence["explicit_model_download_action_executed"] = True
            evidence["model_after_download"] = asdict(download_evidence)

        first_response = await gateway.complete(
            ModelRequest(
                request_id="foundry-physical-proof-first",
                messages=(ModelMessage(role="user", content=args.prompt),),
                provider_id="foundry-local",
                privacy=PrivacyClass.SENSITIVE,
                timeout_seconds=args.timeout,
                temperature=0.0,
            )
        )
        resources_after_first = _resource_snapshot()
        after_first = provider.inspect_model()
        evidence["model_after_first_inference"] = asdict(after_first)
        evidence["first_inference"] = _response_evidence(first_response)
        evidence["resources_after_first_inference"] = resources_after_first
        evidence["first_inference_resource_delta"] = _resource_delta(
            resources_before, resources_after_first
        )
        evidence["model_gateway_path_used"] = True
        evidence["physical_inference_executed"] = True

        provider.close()
        first_close_completed = True
        after_unload = provider.inspect_model()
        evidence["model_after_unload"] = asdict(after_unload)
        if after_unload.loaded:
            raise RuntimeError("Foundry model remained loaded after provider-owned unload")

        reload_response = await gateway.complete(
            ModelRequest(
                request_id="foundry-physical-proof-reload",
                messages=(ModelMessage(role="user", content=args.prompt),),
                provider_id="foundry-local",
                privacy=PrivacyClass.SENSITIVE,
                timeout_seconds=args.timeout,
                temperature=0.0,
            )
        )
        resources_after_reload = _resource_snapshot()
        evidence["reload_inference"] = _response_evidence(reload_response)
        evidence["resources_after_reload_inference"] = resources_after_reload
        evidence["reload_resource_delta_from_start"] = _resource_delta(
            resources_before, resources_after_reload
        )

        provider.close()
        final_close_completed = True
        final_model = provider.inspect_model()
        evidence["model_final"] = asdict(final_model)
        if final_model.loaded:
            raise RuntimeError("Foundry model remained loaded after final provider close")
        evidence["unload_reload_proof_executed"] = True

        if args.hash_model_cache:
            if final_model.path is None:
                raise ValueError("cached model path is unavailable; cannot hash model cache")
            evidence["model_cache_digest"] = _tree_sha256(Path(final_model.path))
        return evidence
    finally:
        original_failure_active = sys.exc_info()[0] is not None
        if not final_close_completed:
            try:
                provider.close()
            except RuntimeError:
                # A timed-out/cancelled native worker still owns the slot. Do not race an unload.
                # Preserve the original proof failure; otherwise cleanup failure is itself fatal.
                if not original_failure_active:
                    raise
            except Exception:
                if not original_failure_active:
                    raise
        if final_close_completed and not first_close_completed:
            raise RuntimeError("Foundry lifecycle proof did not complete its first unload")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a real Foundry Local inference through Nika ModelGateway and emit "
            "machine-readable physical-Windows evidence. Inference never downloads a model. "
            "--allow-download executes a separate explicit model-management action first."
        )
    )
    parser.add_argument("--model", required=True, help="Exact Foundry Local model alias")
    parser.add_argument(
        "--model-id",
        required=True,
        help="Exact public Foundry selected variant ID; fails closed if the alias resolves elsewhere",
    )
    parser.add_argument(
        "--model-license",
        required=True,
        help="Human-reviewed model license identifier or evidence reference",
    )
    parser.add_argument(
        "--prompt",
        default="Reply with exactly: NIKA_FOUNDRY_LOCAL_OK",
        help="Deterministic proof prompt; raw prompt/response text is not written to evidence",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--download-timeout", type=float, default=1800.0)
    parser.add_argument(
        "--allow-download",
        action="store_true",
        help=(
            "Run the separate explicit model download action for this exact alias/ID/license if "
            "the model is not already cached"
        ),
    )
    parser.add_argument(
        "--hash-model-cache",
        action="store_true",
        help="Hash every cached model file after successful lifecycle proof; may take substantial time",
    )
    parser.add_argument("--max-cpu-percent", type=float)
    parser.add_argument("--max-memory-percent", type=float)
    parser.add_argument("--min-available-memory-gb", type=float)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("foundry-local-physical-evidence.json"),
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.timeout <= 0:
        raise ValueError("--timeout must be greater than zero")
    if args.download_timeout <= 0:
        raise ValueError("--download-timeout must be greater than zero")
    if args.min_available_memory_gb is not None and args.min_available_memory_gb <= 0:
        raise ValueError("--min-available-memory-gb must be greater than zero")
    evidence = asyncio.run(_run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Foundry Local evidence written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
