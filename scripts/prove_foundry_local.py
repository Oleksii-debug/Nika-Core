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
    PrivacyClass,
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider


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


async def _run(args: argparse.Namespace) -> dict[str, object]:
    provider = FoundryLocalProvider(default_model=args.model)
    package_name, package_version = _installed_foundry_package()
    before = provider.inspect_model()
    evidence: dict[str, object] = {
        "schema": "nika-foundry-local-physical-proof-v2",
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
        "model_before": asdict(before),
        "explicit_model_download_action_executed": False,
        "physical_inference_executed": False,
    }
    try:
        if args.allow_download and not before.cached:
            download_evidence = await provider.download_model(
                ModelDownloadAuthorization(
                    provider_id="foundry-local",
                    model=args.model,
                    license_reference=args.model_license,
                )
            )
            evidence["explicit_model_download_action_executed"] = True
            evidence["model_after_download"] = asdict(download_evidence)

        response = await provider.complete(
            ModelRequest(
                request_id="foundry-physical-proof",
                messages=(ModelMessage(role="user", content=args.prompt),),
                provider_id="foundry-local",
                privacy=PrivacyClass.SENSITIVE,
                timeout_seconds=args.timeout,
                temperature=0.0,
            )
        )
        after = provider.inspect_model()
        evidence["model_after"] = asdict(after)
        evidence["inference"] = {
            "provider_id": response.provider_id,
            "provider_kind": response.provider_kind.value,
            "model": response.model,
            "text": response.text,
            "usage": asdict(response.usage),
            "latency_ms": response.latency_ms,
        }
        evidence["physical_inference_executed"] = True
        if args.hash_model_cache:
            if after.path is None:
                raise ValueError("cached model path is unavailable; cannot hash model cache")
            evidence["model_cache_digest"] = _tree_sha256(Path(after.path))
        return evidence
    finally:
        provider.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a real Foundry Local inference and emit machine-readable physical-Windows "
            "evidence. Inference never downloads a model. --allow-download executes a separate "
            "explicit model-management action bound to --model and --model-license first."
        )
    )
    parser.add_argument("--model", required=True, help="Exact Foundry Local model alias")
    parser.add_argument(
        "--model-license",
        required=True,
        help="Human-reviewed model license identifier or evidence reference",
    )
    parser.add_argument(
        "--prompt",
        default="Reply with exactly: NIKA_FOUNDRY_LOCAL_OK",
        help="Deterministic proof prompt",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--allow-download",
        action="store_true",
        help=(
            "Run the separate explicit model download action for this exact model/license if it "
            "is not already cached"
        ),
    )
    parser.add_argument(
        "--hash-model-cache",
        action="store_true",
        help="Hash every cached model file after successful inference; may take substantial time",
    )
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
