from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import NoReturn

_MAX_CONFIG_BYTES = 64 * 1024
_MAX_REQUEST_BYTES = 256 * 1024
_MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
_HEX = frozenset("0123456789abcdef")
_CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "model_dir",
        "model_dir_manifest_sha256",
        "base_gguf_path",
        "base_gguf_sha256",
        "scratch_root",
        "max_new_tokens",
        "torch_num_threads",
    }
)
_REQUEST_KEYS = frozenset({"protocol_version", "request", "binding", "candidate", "attestor"})
_REQUEST_INNER_KEYS = frozenset(
    {
        "request_id",
        "messages",
        "model",
        "provider_id",
        "provider_kind",
        "privacy",
        "timeout_seconds",
        "temperature",
        "metadata",
    }
)
_BINDING_KEYS = frozenset(
    {
        "binding_sha256",
        "challenger_candidate_id",
        "artifact_sha256",
        "artifact_size_bytes",
        "descriptor_digest",
        "descriptor_registry_key",
    }
)
_CANDIDATE_KEYS = frozenset({"path", "sha256", "size_bytes"})
_ATTESTOR_KEYS = frozenset({"attestor_id", "attestor_sha256"})


class EvaluatorError(RuntimeError):
    """The real PEFT evaluator rejected its immutable invocation authority."""


def _fail(message: str) -> NoReturn:
    raise EvaluatorError(message)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON constant: {value}")


def _load_json_object(path: Path, *, max_bytes: int) -> dict[str, object]:
    raw = path.read_bytes()
    if not raw or len(raw) > max_bytes:
        _fail(f"JSON authority has invalid size: {path.name}")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise EvaluatorError(f"invalid JSON authority: {path.name}") from exc
    if type(value) is not dict:
        _fail(f"JSON authority must be an object: {path.name}")
    return value


def _sha256(value: object, *, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        _fail(f"{name} must be a lowercase SHA-256 digest")
    return value


def _canonical_path(value: object, *, name: str, directory: bool = False) -> Path:
    if type(value) is not str:
        _fail(f"{name} must be an absolute path")
    path = Path(value)
    if not path.is_absolute():
        _fail(f"{name} must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
        snapshot = os.lstat(path)
    except OSError as exc:
        raise EvaluatorError(f"{name} is unavailable") from exc
    if resolved != path:
        _fail(f"{name} must be canonical")
    is_reparse = bool(
        int(getattr(snapshot, "st_file_attributes", 0))
        & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    )
    if stat.S_ISLNK(snapshot.st_mode) or is_reparse:
        _fail(f"{name} must not be linked or reparse-backed")
    if directory:
        if not stat.S_ISDIR(snapshot.st_mode):
            _fail(f"{name} must be a directory")
    elif not stat.S_ISREG(snapshot.st_mode):
        _fail(f"{name} must be a regular file")
    return resolved


def _load_runtime_config(path: Path) -> dict[str, object]:
    value = _load_json_object(path, max_bytes=_MAX_CONFIG_BYTES)
    if frozenset(value) != _CONFIG_KEYS or value.get("schema_version") != 1:
        _fail("evaluator runtime config schema is invalid")
    model_dir = _canonical_path(value["model_dir"], name="model_dir", directory=True)
    base_gguf = _canonical_path(value["base_gguf_path"], name="base_gguf_path")
    scratch_root = _canonical_path(
        value["scratch_root"], name="scratch_root", directory=True
    )
    max_new_tokens = value["max_new_tokens"]
    torch_num_threads = value["torch_num_threads"]
    if type(max_new_tokens) is not int or not 1 <= max_new_tokens <= 32:
        _fail("max_new_tokens must be an integer from 1 through 32")
    if type(torch_num_threads) is not int or not 1 <= torch_num_threads <= 8:
        _fail("torch_num_threads must be an integer from 1 through 8")
    return {
        "schema_version": 1,
        "model_dir": model_dir,
        "model_dir_manifest_sha256": _sha256(
            value["model_dir_manifest_sha256"],
            name="model_dir_manifest_sha256",
        ),
        "base_gguf_path": base_gguf,
        "base_gguf_sha256": _sha256(
            value["base_gguf_sha256"],
            name="base_gguf_sha256",
        ),
        "scratch_root": scratch_root,
        "max_new_tokens": max_new_tokens,
        "torch_num_threads": torch_num_threads,
    }


def _validated_request(raw: bytes) -> dict[str, object]:
    if not raw or len(raw) > _MAX_REQUEST_BYTES:
        _fail("evaluation request size is invalid")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise EvaluatorError("evaluation request is invalid JSON") from exc
    if type(value) is not dict or frozenset(value) != _REQUEST_KEYS:
        _fail("evaluation request schema is invalid")
    if value.get("protocol_version") != 1:
        _fail("evaluation protocol version is unsupported")
    request = value.get("request")
    binding = value.get("binding")
    candidate = value.get("candidate")
    attestor = value.get("attestor")
    if type(request) is not dict or frozenset(request) != _REQUEST_INNER_KEYS:
        _fail("evaluation request payload is invalid")
    if type(binding) is not dict or frozenset(binding) != _BINDING_KEYS:
        _fail("evaluation binding payload is invalid")
    if type(candidate) is not dict or frozenset(candidate) != _CANDIDATE_KEYS:
        _fail("evaluation candidate payload is invalid")
    if type(attestor) is not dict or frozenset(attestor) != _ATTESTOR_KEYS:
        _fail("evaluation attestor payload is invalid")
    for field in ("request_id", "model", "provider_id"):
        if type(request[field]) is not str or not request[field]:
            _fail(f"request.{field} must be non-empty text")
    messages = request["messages"]
    if (
        type(messages) is not list
        or not messages
        or len(messages) > 128
        or any(type(item) is not dict for item in messages)
    ):
        _fail("request.messages must be a bounded non-empty list")
    for item in messages:
        if frozenset(item) != {"role", "content"}:
            _fail("request message schema is invalid")
        if (
            type(item["role"]) is not str
            or not item["role"]
            or type(item["content"]) is not str
            or not item["content"]
        ):
            _fail("request messages require non-empty text")
    _sha256(candidate["sha256"], name="candidate.sha256")
    _sha256(binding["artifact_sha256"], name="binding.artifact_sha256")
    _sha256(binding["descriptor_digest"], name="binding.descriptor_digest")
    _sha256(binding["descriptor_registry_key"], name="binding.descriptor_registry_key")
    _sha256(binding["binding_sha256"], name="binding.binding_sha256")
    _sha256(attestor["attestor_sha256"], name="attestor.attestor_sha256")
    if candidate["sha256"] != binding["artifact_sha256"]:
        _fail("candidate digest disagrees with evaluation binding")
    if (
        type(candidate["size_bytes"]) is not int
        or not 1 <= candidate["size_bytes"] <= _MAX_ARTIFACT_BYTES
        or candidate["size_bytes"] != binding["artifact_size_bytes"]
    ):
        _fail("candidate size disagrees with evaluation binding")
    return value


def _snapshot_artifact(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str,
    expected_size: int,
) -> None:
    source = _canonical_path(os.fspath(source), name="model artifact")
    if destination.exists():
        _fail("model snapshot destination already exists")
    digest = hashlib.sha256()
    total = 0
    try:
        with source.open("rb", buffering=0) as incoming, destination.open("xb", buffering=0) as out:
            while True:
                chunk = incoming.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_ARTIFACT_BYTES:
                    _fail("model artifact exceeds evaluator byte bound")
                digest.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    except EvaluatorError:
        destination.unlink(missing_ok=True)
        raise
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise EvaluatorError("model artifact snapshot failed") from exc
    if total != expected_size or digest.hexdigest() != expected_sha256:
        destination.unlink(missing_ok=True)
        _fail("model artifact bytes do not match attested candidate identity")


def _prompt(messages: object) -> str:
    assert type(messages) is list
    parts = []
    for item in messages:
        assert type(item) is dict
        parts.append(f"{item['role']}: {item['content']}")
    parts.append("assistant:")
    return "\n".join(parts)


def _copy_adapter_config(candidate: Path, adapter_dir: Path) -> None:
    from nika_core.training_peft_worker import candidate_adapter_manifest

    manifest = candidate_adapter_manifest(candidate)
    config = manifest.get("adapter_config")
    if type(config) is not dict or not config:
        _fail("candidate adapter manifest does not contain canonical adapter config")
    encoded = json.dumps(
        config,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    path = adapter_dir / "adapter_config.json"
    with path.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def _infer(
    request: dict[str, object],
    runtime: dict[str, object],
) -> tuple[str, int, int]:
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    from nika_core.training_peft_worker import model_directory_manifest_sha256

    candidate = request["candidate"]
    request_payload = request["request"]
    assert type(candidate) is dict
    assert type(request_payload) is dict

    candidate_path = _canonical_path(candidate["path"], name="candidate.path")
    candidate_sha256 = _sha256(candidate["sha256"], name="candidate.sha256")
    candidate_size = int(candidate["size_bytes"])
    base_gguf = runtime["base_gguf_path"]
    model_dir = runtime["model_dir"]
    scratch_root = runtime["scratch_root"]
    assert isinstance(base_gguf, Path)
    assert isinstance(model_dir, Path)
    assert isinstance(scratch_root, Path)

    if model_directory_manifest_sha256(model_dir) != runtime["model_dir_manifest_sha256"]:
        _fail("model directory manifest changed before evaluator load")

    torch.set_num_threads(int(runtime["torch_num_threads"]))
    torch.use_deterministic_algorithms(True)
    torch.manual_seed(1729)

    with tempfile.TemporaryDirectory(
        prefix="nika-real-eval-",
        dir=os.fspath(scratch_root),
    ) as temporary_name:
        temporary = Path(temporary_name).resolve(strict=True)
        base_snapshot = temporary / "base.gguf"
        base_stat = _canonical_path(
            os.fspath(base_gguf),
            name="base_gguf_path",
        ).stat()
        _snapshot_artifact(
            base_gguf,
            base_snapshot,
            expected_sha256=str(runtime["base_gguf_sha256"]),
            expected_size=base_stat.st_size,
        )

        tokenizer = AutoTokenizer.from_pretrained(
            os.fspath(model_dir),
            gguf_file=os.fspath(base_snapshot),
            local_files_only=True,
            trust_remote_code=False,
        )
        if tokenizer.pad_token_id is None:
            if tokenizer.eos_token_id is None:
                _fail("tokenizer does not expose a padding or EOS token")
            tokenizer.pad_token = tokenizer.eos_token

        model = AutoModelForCausalLM.from_pretrained(
            os.fspath(model_dir),
            gguf_file=os.fspath(base_snapshot),
            local_files_only=True,
            trust_remote_code=False,
            dtype="auto",
        )
        if candidate_sha256 == runtime["base_gguf_sha256"]:
            if candidate_path != base_gguf:
                _fail("champion candidate path is not the configured base GGUF")
            if candidate_size != base_stat.st_size:
                _fail("champion candidate size changed")
        else:
            adapter_dir = temporary / "adapter"
            adapter_dir.mkdir()
            adapter_snapshot = adapter_dir / "adapter_model.safetensors"
            _snapshot_artifact(
                candidate_path,
                adapter_snapshot,
                expected_sha256=candidate_sha256,
                expected_size=candidate_size,
            )
            _copy_adapter_config(adapter_snapshot, adapter_dir)
            model = PeftModel.from_pretrained(
                model,
                os.fspath(adapter_dir),
                is_trainable=False,
                local_files_only=True,
            )

        if model_directory_manifest_sha256(model_dir) != runtime["model_dir_manifest_sha256"]:
            _fail("model directory manifest changed during evaluator load")

        model.eval()
        encoded = tokenizer(
            _prompt(request_payload["messages"]),
            return_tensors="pt",
            add_special_tokens=True,
        )
        input_tokens = int(encoded["input_ids"].shape[-1])
        with torch.inference_mode():
            generated = model.generate(
                **encoded,
                max_new_tokens=int(runtime["max_new_tokens"]),
                do_sample=False,
                pad_token_id=tokenizer.pad_token_id,
            )
        output_ids = generated[0, input_tokens:].tolist()
        output_tokens = len(output_ids)
        if output_tokens < 1:
            _fail("model inference produced no output token evidence")
        response_text = "tokens:" + ",".join(str(int(item)) for item in output_ids)
        return response_text, input_tokens, output_tokens


def _run(runtime_path: Path) -> None:
    runtime = _load_runtime_config(runtime_path)
    raw = sys.stdin.buffer.read(_MAX_REQUEST_BYTES + 1)
    request = _validated_request(raw)
    candidate = request["candidate"]
    binding = request["binding"]
    request_payload = request["request"]
    assert type(candidate) is dict
    assert type(binding) is dict
    assert type(request_payload) is dict

    text, input_tokens, output_tokens = _infer(request, runtime)
    response = {
        "protocol_version": 1,
        "request_id": request_payload["request_id"],
        "provider_id": request_payload["provider_id"],
        "model": request_payload["model"],
        "text": text,
        "loaded_artifact_sha256": candidate["sha256"],
        "loaded_artifact_size_bytes": candidate["size_bytes"],
        "descriptor_digest": binding["descriptor_digest"],
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }
    sys.stdout.write(
        json.dumps(
            response,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("physical PEFT evaluator requires exactly one runtime config", file=sys.stderr)
        return 2
    try:
        runtime_path = _canonical_path(arguments[0], name="runtime config")
        _run(runtime_path)
    except (EvaluatorError, OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"physical PEFT evaluator failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
