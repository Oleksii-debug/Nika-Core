from __future__ import annotations

import re
from pathlib import Path

_WORKFLOW_ROOT = Path(".github/workflows")
_ACTION_USE = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)", re.MULTILINE)
_FULL_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_REMOTE_SCRIPT_PIPE = re.compile(
    r"(?:curl|wget)\b[^\n|]*https?://[^\n|]+\|\s*(?:sh|bash)\b",
    re.IGNORECASE,
)
_CHECKOUT_USE = re.compile(
    r"^\s*-?\s*uses:\s*actions/checkout@",
    re.IGNORECASE,
)
_STEP_ITEM = re.compile(r"^(?P<indent>\s*)-\s+")
_PERSIST_SETTING = re.compile(
    r"^\s*persist-credentials:\s*([^\s#]+)\s*(?:#.*)?$",
    re.IGNORECASE,
)


def _workflows() -> tuple[Path, ...]:
    return tuple(sorted(_WORKFLOW_ROOT.glob("*.yml"))) + tuple(
        sorted(_WORKFLOW_ROOT.glob("*.yaml"))
    )


def _indentation(line: str) -> int:
    return len(line) - len(line.lstrip())


def _step_bounds(lines: list[str], uses_index: int) -> tuple[int, int]:
    uses_line = lines[uses_index]
    uses_indent = _indentation(uses_line)
    step_start: int | None = None
    step_indent: int | None = None

    for candidate in range(uses_index, -1, -1):
        match = _STEP_ITEM.match(lines[candidate])
        if not match:
            continue
        candidate_indent = len(match.group("indent"))
        if candidate == uses_index or candidate_indent < uses_indent:
            step_start = candidate
            step_indent = candidate_indent
            break

    if step_start is None or step_indent is None:
        raise AssertionError(
            f"checkout at line {uses_index + 1} is not inside a YAML list step"
        )

    step_end = len(lines)
    for candidate in range(step_start + 1, len(lines)):
        match = _STEP_ITEM.match(lines[candidate])
        if match and len(match.group("indent")) == step_indent:
            step_end = candidate
            break

    return step_start, step_end


def _uses_key_indent(line: str) -> int:
    indentation = _indentation(line)
    if line.lstrip().startswith("- uses:"):
        return indentation + 2
    return indentation


def _checkout_disables_persisted_credentials(lines: list[str], uses_index: int) -> bool:
    step_start, step_end = _step_bounds(lines, uses_index)
    sibling_indent = _uses_key_indent(lines[uses_index])

    for candidate in range(step_start, step_end):
        line = lines[candidate]
        if line.strip().casefold() != "with:" or _indentation(line) != sibling_indent:
            continue

        with_indent = _indentation(line)
        values: list[str] = []
        for following in lines[candidate + 1 : step_end]:
            if not following.strip():
                continue
            if _indentation(following) <= with_indent:
                break
            match = _PERSIST_SETTING.fullmatch(following)
            if match:
                values.append(match.group(1).casefold())

        return values == ["false"]

    return False


def _checkout_credential_findings(workflow: Path) -> list[str]:
    findings: list[str] = []
    lines = workflow.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if _CHECKOUT_USE.search(line) is None:
            continue
        if not _checkout_disables_persisted_credentials(lines, index):
            findings.append(f"{workflow}:{index + 1}")
    return findings


def test_every_external_action_is_pinned_to_an_immutable_commit() -> None:
    mutable: list[str] = []
    for workflow in _workflows():
        text = workflow.read_text(encoding="utf-8")
        for action_ref in _ACTION_USE.findall(text):
            if action_ref.startswith("./"):
                continue
            action, separator, ref = action_ref.rpartition("@")
            if not separator or not action or not _FULL_COMMIT_SHA.fullmatch(ref):
                mutable.append(f"{workflow}:{action_ref}")

    assert not mutable, f"mutable third-party action references: {mutable}"


def test_checkout_never_persists_ci_credentials() -> None:
    findings = [
        finding
        for workflow in _workflows()
        for finding in _checkout_credential_findings(workflow)
    ]

    assert not findings, f"checkout persists CI credentials: {findings}"


def test_checkout_policy_handles_named_steps_and_rejects_unsafe_settings(
    tmp_path: Path,
) -> None:
    checkout_sha = "11d5960a326750d5838078e36cf38b85af677262"
    action = f"actions/checkout@{checkout_sha}"
    mixed_case_action = f"Actions/Checkout@{checkout_sha}"
    safe = tmp_path / "safe.yml"
    inline_safe = tmp_path / "inline-safe.yml"
    mixed_case_safe = tmp_path / "mixed-case-safe.yml"
    mixed_case_missing = tmp_path / "mixed-case-missing.yml"
    missing = tmp_path / "missing.yml"
    explicitly_true = tmp_path / "true.yml"
    misplaced_env = tmp_path / "misplaced-env.yml"
    duplicate_conflict = tmp_path / "duplicate-conflict.yml"
    duplicate_false = tmp_path / "duplicate-false.yml"

    safe.write_text(
        "jobs:\n"
        "  verify:\n"
        "    steps:\n"
        "      - name: Checkout\n"
        f"        uses: {action}\n"
        "        with:\n"
        "          ref: main\n"
        "          persist-credentials: false\n"
        "      - name: Later step\n"
        "        run: echo safe\n",
        encoding="utf-8",
    )
    inline_safe.write_text(
        "jobs:\n"
        "  verify:\n"
        "    steps:\n"
        f"      - uses: {action}\n"
        "        with:\n"
        "          persist-credentials: false\n",
        encoding="utf-8",
    )
    mixed_case_safe.write_text(
        "jobs:\n"
        "  verify:\n"
        "    steps:\n"
        "      - name: Mixed-case checkout\n"
        f"        uses: {mixed_case_action}\n"
        "        with:\n"
        "          persist-credentials: false\n",
        encoding="utf-8",
    )
    mixed_case_missing.write_text(
        "jobs:\n"
        "  verify:\n"
        "    steps:\n"
        "      - name: Mixed-case checkout without hardening\n"
        f"        uses: {mixed_case_action}\n",
        encoding="utf-8",
    )
    missing.write_text(
        "jobs:\n"
        "  verify:\n"
        "    steps:\n"
        "      - name: Checkout\n"
        f"        uses: {action}\n"
        "        with:\n"
        "          ref: main\n"
        "      - name: Later step\n"
        "        run: echo unsafe\n",
        encoding="utf-8",
    )
    explicitly_true.write_text(
        "jobs:\n"
        "  verify:\n"
        "    steps:\n"
        "      - name: Unsafe checkout\n"
        f"        uses: {action}\n"
        "        with:\n"
        "          persist-credentials: true\n",
        encoding="utf-8",
    )
    misplaced_env.write_text(
        "jobs:\n"
        "  verify:\n"
        "    steps:\n"
        "      - name: Misplaced credential setting\n"
        f"        uses: {action}\n"
        "        env:\n"
        "          persist-credentials: false\n",
        encoding="utf-8",
    )
    duplicate_conflict.write_text(
        "jobs:\n"
        "  verify:\n"
        "    steps:\n"
        "      - name: Ambiguous checkout\n"
        f"        uses: {action}\n"
        "        with:\n"
        "          persist-credentials: false\n"
        "          persist-credentials: true\n",
        encoding="utf-8",
    )
    duplicate_false.write_text(
        "jobs:\n"
        "  verify:\n"
        "    steps:\n"
        "      - name: Duplicate checkout input\n"
        f"        uses: {action}\n"
        "        with:\n"
        "          persist-credentials: false\n"
        "          persist-credentials: false\n",
        encoding="utf-8",
    )

    assert _checkout_credential_findings(safe) == []
    assert _checkout_credential_findings(inline_safe) == []
    assert _checkout_credential_findings(mixed_case_safe) == []
    assert len(_checkout_credential_findings(mixed_case_missing)) == 1
    assert len(_checkout_credential_findings(missing)) == 1
    assert len(_checkout_credential_findings(explicitly_true)) == 1
    assert len(_checkout_credential_findings(misplaced_env)) == 1
    assert len(_checkout_credential_findings(duplicate_conflict)) == 1
    assert len(_checkout_credential_findings(duplicate_false)) == 1


def test_workflows_do_not_pipe_remote_installers_to_shells() -> None:
    findings = [
        str(workflow)
        for workflow in _workflows()
        if _REMOTE_SCRIPT_PIPE.search(workflow.read_text(encoding="utf-8"))
    ]
    assert not findings, f"remote installer piped directly to a shell: {findings}"


def test_live_ollama_proof_uses_exact_runtime_and_model_bytes() -> None:
    workflow = (_WORKFLOW_ROOT / "ci.yml").read_text(encoding="utf-8")

    assert 'OLLAMA_VERSION: "0.32.14"' in workflow
    assert (
        "OLLAMA_LINUX_AMD64_SHA256: "
        "c620917a71e146ab3a7f893084f066069c4c65d144ef8379a91c3cbe8b27de8f"
        in workflow
    )
    assert (
        "resolve/be21a1bc2b344d5b57381053d1dc0faea5f4e40c/"
        "SmolLM2-135M-Instruct-Q5_K_M.gguf"
        in workflow
    )
    assert (
        "NIKA_OLLAMA_PROOF_MODEL_SHA256: "
        "731d0c9cf598dada9712242ceddcca88aa0502fc8f9b8f773917df9f9113463a"
        in workflow
    )
    assert "sha256sum --check -" in workflow
    assert "ollama create" in workflow
    assert re.search(r"\bollama\s+pull\b", workflow, re.IGNORECASE) is None
