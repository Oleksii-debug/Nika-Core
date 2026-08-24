from __future__ import annotations

import pathlib
import re


_WORKFLOW_ROOT = pathlib.Path(".github/workflows")
_ACTION_REF = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)", re.MULTILINE)
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_REMOTE_PIPE = re.compile(
    r"(?:curl|wget)\b[^\n|]*https?://[^\n|]+\|\s*(?:sh|bash)\b",
    re.IGNORECASE,
)
_OLLAMA_SHA = "c620917a71e146ab3a7f893084f066069c4c65d144ef8379a91c3cbe8b27de8f"
_MODEL_COMMIT = "be21a1bc2b344d5b57381053d1dc0faea5f4e40c"
_MODEL_SHA = "731d0c9cf598dada9712242ceddcca88aa0502fc8f9b8f773917df9f9113463a"


def _workflow_paths() -> tuple[pathlib.Path, ...]:
    return tuple(sorted(_WORKFLOW_ROOT.glob("*.yml"))) + tuple(
        sorted(_WORKFLOW_ROOT.glob("*.yaml"))
    )


def test_all_external_actions_are_commit_pinned() -> None:
    findings: list[str] = []
    for path in _workflow_paths():
        text = path.read_text(encoding="utf-8")
        for value in _ACTION_REF.findall(text):
            if value.startswith("./"):
                continue
            action, separator, ref = value.rpartition("@")
            if not separator or not action or not _SHA40.fullmatch(ref):
                findings.append(f"{path}:{value}")
    assert not findings, findings


def test_every_checkout_disables_credential_persistence() -> None:
    findings: list[str] = []
    for path in _workflow_paths():
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if "uses: actions/checkout@" not in line:
                continue
            checkout_indent = len(line) - len(line.lstrip())
            block: list[str] = []
            for following in lines[index + 1 :]:
                stripped = following.strip()
                indent = len(following) - len(following.lstrip())
                if stripped and indent <= checkout_indent:
                    break
                block.append(following)
            normalized = "".join(block).casefold().replace(" ", "")
            if "persist-credentials:false" not in normalized:
                findings.append(f"{path}:{index + 1}")
    assert not findings, findings


def test_no_workflow_executes_remote_installer_pipe() -> None:
    findings = [
        str(path)
        for path in _workflow_paths()
        if _REMOTE_PIPE.search(path.read_text(encoding="utf-8"))
    ]
    assert not findings, findings


def test_live_ollama_proof_binds_runtime_and_model_bytes_before_import() -> None:
    workflow = (_WORKFLOW_ROOT / "ci.yml").read_text(encoding="utf-8")

    assert 'OLLAMA_VERSION: "0.32.14"' in workflow
    assert _OLLAMA_SHA in workflow
    assert f"resolve/{_MODEL_COMMIT}/SmolLM2-135M-Instruct-Q5_K_M.gguf" in workflow
    assert _MODEL_SHA in workflow
    assert workflow.count("sha256sum --check -") >= 2
    assert "ollama create" in workflow
    assert re.search(r"\bollama\s+pull\b", workflow, re.IGNORECASE) is None

    runtime_hash = workflow.index(_OLLAMA_SHA)
    runtime_extract = workflow.index("tar --zstd -xf")
    model_hash = workflow.index(_MODEL_SHA)
    model_import = workflow.index("ollama create")
    assert runtime_hash < runtime_extract
    assert model_hash < model_import
